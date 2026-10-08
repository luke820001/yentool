"""
AI report client: Gemini first, Groq second, the local template last.
ASCII only.

Retry policy (2026-10-08, plan P1-7). 59 cloud scans (09-22..10-07) made 176
Gemini calls and 65 failed: 45 HTTP 503 "high demand", 18 timeouts at 60 s,
2 HTTP 429. Only 429 was retried; a 503 or a timeout went straight to the
template, so the OTC report -- the only market the rule buys -- was a template
on 8 of 11 days at 15:00. Now:

  * 5xx (TRANSIENT_STATUS), a timeout or a connection error gets exactly ONE
    retry (TRANSIENT_RETRIES) after TRANSIENT_WAIT seconds; a numeric
    Retry-After is honoured up to TRANSIENT_WAIT_CAP.
  * 429 keeps its old shape: up to RETRY_ATTEMPTS attempts, waits 10 s then
    20 s; Groq's quota-exhausted 429 is never retried.
  * 400 / 401 / 403 / 404, a non-JSON body or an empty answer is not retried.
  * Every retry of one run draws from ONE RetryBudget (RETRY_BUDGET_S, shared
    by the three market reports): a retry starts only when the budget still
    covers its wait plus RETRY_MIN_CALL_S of call time, and the retried call's
    timeout is capped to what is left. Retries therefore add at most about
    RETRY_BUDGET_S to a run -- the Pages publish waits for the whole AI phase,
    so this bound is what keeps the 15:0x list on time (DECISIONS: ~90 s).
  * A provider without a key is skipped without a network call.

generate_report_meta() returns the text AND where it came from, so the
payload can say which reports are templates (meta.report_sources).
"""
import time

import requests

from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_API_URL,
    GROQ_API_KEY,
    GROQ_MODEL,
    GROQ_API_URL,
    GEMINI_REPORT_FILE,
)
from gemini_hook.prompt_builder import (
    build_prompt, build_local_report, system_instruction,
    SYSTEM_INSTRUCTION,  # noqa: F401  (re-exported for older importers)
)

REQUEST_TIMEOUT = 60        # seconds, one attempt
RETRY_ATTEMPTS = 3          # 429 only: attempts in total (unchanged)
RETRY_BASE_WAIT = 10        # 429 wait in seconds; doubles each retry
TRANSIENT_STATUS = (500, 502, 503, 504)
TRANSIENT_RETRIES = 1       # 5xx / timeout / connection error: one retry
TRANSIENT_WAIT = 15         # seconds before that retry
TRANSIENT_WAIT_CAP = 30     # a numeric Retry-After is honoured up to this
RETRY_BUDGET_S = 90         # every retry of one run together (waits + calls)
RETRY_MIN_CALL_S = 30       # a retry needs at least this much call time left
REPORT_SOURCES = ("gemini", "groq", "template")
ERROR_MAX = 200


class AIReportError(Exception):
    """A provider could not produce a report. `transient` marks failures a
    retry may cure (5xx, timeout, connection, rate limit); `status` is the
    HTTP status when there was one; `attempts` the requests it made."""

    def __init__(self, msg, transient=False, status=None, attempts=0):
        Exception.__init__(self, msg)
        self.transient = bool(transient)
        self.status = status
        self.attempts = attempts


# keep old name as alias so any existing import of GeminiError still works
GeminiError = AIReportError


class RetryBudget(object):
    """Seconds that every retry of one run draws from, shared across the
    reports of that run. `clock` and `sleep` are injectable for tests."""

    def __init__(self, seconds=RETRY_BUDGET_S, clock=time.monotonic,
                 sleep=time.sleep):
        self.left = float(seconds)
        self.clock = clock
        self.sleep = sleep
        self.retries = 0
        self.spent = 0.0

    def timeout_for(self, wait):
        """The call timeout a retry after `wait` seconds may use, or None
        when the budget cannot cover the wait plus RETRY_MIN_CALL_S."""
        room = self.left - float(wait)
        if room < RETRY_MIN_CALL_S:
            return None
        return min(float(REQUEST_TIMEOUT), room)

    def charge(self, seconds):
        s = max(0.0, float(seconds))
        self.left -= s
        self.spent += s


def _post(label, url, headers, body, timeout=REQUEST_TIMEOUT):
    """requests.post, with every network failure expressed as AIReportError.

    F24 (2026-09-09 audit): the bare requests.post let requests.Timeout and
    requests.ConnectionError propagate as themselves. generate_report only
    catches AIReportError, so the two failures the fallback chain exists FOR --
    the API being slow or unreachable -- were the two that skipped both the Groq
    fallback AND the local report, and the scan lost its summary entirely. A
    provider is either usable or it is not; how it failed is a message, not a
    different control path. A timeout or a dropped connection is transient
    (one retry may cure it); a TLS failure is not.
    """
    try:
        return requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout:
        raise AIReportError("{}: no response within {:.0f}s".format(
            label, timeout), transient=True)
    except requests.exceptions.SSLError as e:
        raise AIReportError("{}: TLS error: {}".format(label, str(e)[:200]))
    except requests.ConnectionError as e:
        raise AIReportError("{}: connection error: {}".format(
            label, str(e)[:200]), transient=True)
    except requests.RequestException as e:
        raise AIReportError("{}: network error: {}".format(
            label, str(e)[:200]))


def _payload(label, resp):
    """resp.json(), with a non-JSON body expressed as AIReportError too.

    A gateway or captive portal answers HTTP 200 with HTML; resp.json() then
    raises ValueError, which is outside the fallback chain for the same reason
    the network exceptions were.
    """
    try:
        return resp.json()
    except ValueError:
        raise AIReportError("{}: response was not JSON: {}".format(
            label, _squash(resp.text)))


def _squash(text, limit=ERROR_MAX):
    """One line, ASCII, at most `limit` characters -- safe for logs and the
    payload. A provider key that slipped into a message is masked."""
    s = " ".join(str(text or "").split())
    for key in (GEMINI_API_KEY, GROQ_API_KEY):
        if key and len(key) >= 8:
            s = s.replace(key, "***")
    return s.encode("ascii", "replace").decode("ascii")[:limit]


def _retry_after(resp):
    """A numeric Retry-After header in seconds, or None."""
    try:
        v = float((getattr(resp, "headers", None) or {}).get("Retry-After", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v > 0 else None


def _post_with_retry(label, url, headers, body, budget,
                     quota_exhausted=None):
    """POST under the retry policy in the module docstring.

    Returns (resp, attempts) for an HTTP 200; raises AIReportError (with
    .attempts) otherwise. The time a retry costs -- its wait and its call --
    is charged to `budget` when the call returns or fails."""
    attempts = 0
    transient_used = 0
    rate_used = 0
    rate_wait = RETRY_BASE_WAIT
    timeout = REQUEST_TIMEOUT
    started = None          # budget.clock() when the running retry began

    def _schedule(wait, why):
        call = budget.timeout_for(wait)
        if call is None:
            return None
        print("  [{}] {} -- retrying in {:.0f}s".format(label, why, wait))
        begin = budget.clock()
        budget.sleep(wait)
        budget.retries += 1
        return call, begin

    while True:
        attempts += 1
        err, resp = None, None
        try:
            resp = _post(label, url, headers, body, timeout=timeout)
        except AIReportError as e:
            err = e
        if started is not None:
            budget.charge(budget.clock() - started)
            started = None

        if err is not None:
            err.attempts = attempts
            if err.transient and transient_used < TRANSIENT_RETRIES:
                nxt = _schedule(TRANSIENT_WAIT, _squash(err, 80))
                if nxt is not None:
                    transient_used += 1
                    timeout, started = nxt
                    continue
            raise err

        code = resp.status_code
        if code == 429:
            if quota_exhausted is not None and quota_exhausted(resp):
                raise AIReportError("{}: HTTP 429 quota exhausted".format(label),
                                    status=429, attempts=attempts)
            if rate_used < RETRY_ATTEMPTS - 1:
                nxt = _schedule(rate_wait, "HTTP 429 rate limit ({}/{})".format(
                    attempts, RETRY_ATTEMPTS))
                if nxt is not None:
                    rate_used += 1
                    rate_wait *= 2
                    timeout, started = nxt
                    continue
            raise AIReportError(
                "{}: HTTP 429 rate limit after {} attempt(s)".format(
                    label, attempts),
                transient=True, status=429, attempts=attempts)
        if code in TRANSIENT_STATUS:
            if transient_used < TRANSIENT_RETRIES:
                wait = min(_retry_after(resp) or TRANSIENT_WAIT,
                           TRANSIENT_WAIT_CAP)
                nxt = _schedule(wait, "HTTP {}".format(code))
                if nxt is not None:
                    transient_used += 1
                    timeout, started = nxt
                    continue
            raise AIReportError(
                "{}: HTTP {} after {} attempt(s): {}".format(
                    label, code, attempts, _squash(resp.text)),
                transient=True, status=code, attempts=attempts)
        if code != 200:
            raise AIReportError("{}: HTTP {}: {}".format(
                label, code, _squash(resp.text)), status=code, attempts=attempts)
        return resp, attempts


def _call_gemini(prompt, budget):
    """(text, attempts) from Gemini, or AIReportError."""
    if not GEMINI_API_KEY:
        raise AIReportError("Gemini: not configured (GEMINI_API_KEY is empty)")

    url = "{}/{}:generateContent".format(GEMINI_API_URL, GEMINI_MODEL)
    headers = {
        "Content-Type":   "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    body = {
        "system_instruction": {"parts": [{"text": system_instruction()}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }
    resp, attempts = _post_with_retry("Gemini", url, headers, body, budget)
    payload = _payload("Gemini", resp)
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise AIReportError("Gemini: unexpected response: {}".format(
            _squash(payload)), attempts=attempts)
    if not isinstance(text, str) or not text.strip():
        raise AIReportError("Gemini returned empty text.", attempts=attempts)
    return text, attempts


def _groq_quota_exhausted(resp):
    # A 429 without "rate_limit_exceeded" is the daily quota: retrying is
    # pointless.
    return "rate_limit_exceeded" not in str(resp.text or "").lower()


def _call_groq(prompt, budget):
    """(text, attempts) from Groq, or AIReportError. Skipped without a
    network call when GROQ_API_KEY is empty (the cloud never sets it)."""
    if not GROQ_API_KEY:
        raise AIReportError("Groq: not configured (GROQ_API_KEY is empty)")

    headers = {
        "Authorization": "Bearer {}".format(GROQ_API_KEY),
        "Content-Type":  "application/json",
    }
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction()},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.3,
    }
    resp, attempts = _post_with_retry("Groq", GROQ_API_URL, headers, body,
                                      budget,
                                      quota_exhausted=_groq_quota_exhausted)
    payload = _payload("Groq", resp)
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIReportError("Groq: unexpected response: {}".format(
            _squash(payload)), attempts=attempts)
    if not isinstance(text, str) or not text.strip():
        raise AIReportError("Groq returned empty text.", attempts=attempts)
    return text, attempts


def _save_report(text: str) -> None:
    GEMINI_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(GEMINI_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def generate_report_meta(df, budget=None, save=True):
    """One report and where it came from.

    Returns {'text': str, 'source': 'gemini'|'groq'|'template', 'model': str
    ('' for the template), 'attempts': HTTP requests made, 'seconds': float,
    'error': ASCII summary of the provider failures ('' when Gemini
    answered)}. `budget` is the run's RetryBudget (a fresh one when None).
    Raises AIReportError only when there is nothing to summarise."""
    if df is None or getattr(df, "empty", True):
        raise AIReportError("No data to summarize. Run a scan first.")
    errors = []
    try:
        prompt = build_prompt(df)
    except Exception as e:
        # A prompt that cannot be built is a provider-less run, not a lost
        # report: the template below still renders from the same frame.
        prompt = ""
        errors.append(_squash("prompt: {}".format(e), 120))
        print("  [ai] prompt not built: {} -- using the template".format(
            _squash(e)))
    if budget is None:
        budget = RetryBudget()
    t0 = budget.clock()

    # Any exception from a provider -- not only AIReportError and
    # requests.RequestException, which _post converts already (F24) -- falls
    # through to the next provider and finally to the template: the template
    # is the last line of defence, and an unexpected error in a provider path
    # used to lose the market's report altogether.
    text, source, model, attempts = None, "template", "", 0
    providers = ((("gemini", _call_gemini, GEMINI_MODEL),
                  ("groq", _call_groq, GROQ_MODEL)) if prompt else ())
    for name, call, mdl in providers:
        label = name.capitalize()
        try:
            text, n = call(prompt, budget)
            attempts += n
            source, model = name, mdl
            print("  [{}] report generated OK".format(label))
            break
        except Exception as e:
            attempts += int(getattr(e, "attempts", 0) or 0)
            errors.append(_squash(e, 120))
            print("  [{}] API failed: {} -- falling back".format(
                label, _squash(e)))
    if text is None:
        text = build_local_report(df)

    if save:
        try:
            _save_report(text)
        except OSError as e:
            print("  [ai] report not saved: {}".format(_squash(e, 80)))
    return {
        "text": text,
        "source": source,
        "model": model,
        "attempts": attempts,
        "seconds": round(max(0.0, budget.clock() - t0), 1),
        "error": _squash("; ".join(errors)),
    }


def generate_report(df) -> str:
    """The report text alone (gui/app.py). Same chain as
    generate_report_meta."""
    return generate_report_meta(df)["text"]

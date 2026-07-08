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
    build_prompt, build_local_report, SYSTEM_INSTRUCTION
)

REQUEST_TIMEOUT = 60
RETRY_ATTEMPTS  = 3
RETRY_BASE_WAIT = 10  # seconds; doubles each attempt on rate-limit


class AIReportError(Exception):
    pass

# keep old name as alias so any existing import of GeminiError still works
GeminiError = AIReportError


def _call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise AIReportError("GEMINI_API_KEY is empty. Set it in the .env file.")

    url = "{}/{}:generateContent".format(GEMINI_API_URL, GEMINI_MODEL)
    headers = {
        "Content-Type":   "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3},
    }

    wait = RETRY_BASE_WAIT
    resp = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        resp = requests.post(url, headers=headers,
                             json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            if attempt < RETRY_ATTEMPTS:
                print("  [Gemini] 429 rate limit — waiting {}s ({}/{})".format(
                    wait, attempt, RETRY_ATTEMPTS))
                time.sleep(wait)
                wait *= 2
                continue
            raise AIReportError("HTTP 429: rate limit after {} retries".format(RETRY_ATTEMPTS))
        if resp.status_code != 200:
            raise AIReportError("HTTP {}: {}".format(resp.status_code, resp.text[:300]))
        break

    payload = resp.json()
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AIReportError("Unexpected response: {}".format(str(payload)[:300]))

    if not text.strip():
        raise AIReportError("Gemini returned empty text.")
    return text


def _call_groq(prompt: str) -> str:
    if not GROQ_API_KEY:
        raise AIReportError("GROQ_API_KEY is empty. Set it in the .env file.")

    headers = {
        "Authorization": "Bearer {}".format(GROQ_API_KEY),
        "Content-Type":  "application/json",
    }
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.3,
    }

    wait = RETRY_BASE_WAIT
    resp = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        resp = requests.post(GROQ_API_URL, headers=headers,
                             json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            txt = resp.text
            # daily quota exhausted — no point retrying
            if "rate_limit_exceeded" not in txt.lower():
                raise AIReportError("HTTP 429: quota exhausted")
            if attempt < RETRY_ATTEMPTS:
                print("  [Groq] 429 rate limit — waiting {}s ({}/{})".format(
                    wait, attempt, RETRY_ATTEMPTS))
                time.sleep(wait)
                wait *= 2
                continue
            raise AIReportError("HTTP 429: rate limit after {} retries".format(RETRY_ATTEMPTS))
        if resp.status_code != 200:
            raise AIReportError("HTTP {}: {}".format(resp.status_code, resp.text[:300]))
        break

    payload = resp.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise AIReportError("Unexpected response: {}".format(str(payload)[:300]))

    if not text.strip():
        raise AIReportError("Groq returned empty text.")
    return text


def _save_report(text: str) -> None:
    GEMINI_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(GEMINI_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def generate_report(df) -> str:
    prompt = build_prompt(df)
    if not prompt:
        raise AIReportError("No data to summarize. Run a scan first.")

    report = None
    try:
        report = _call_gemini(prompt)
        print("  [Gemini] report generated OK")
    except AIReportError as e:
        print("  [Gemini] API failed: {} — falling back to Groq".format(e))
        try:
            report = _call_groq(prompt)
            print("  [Groq] report generated OK")
        except AIReportError as e2:
            print("  [Groq] API failed: {} — falling back to local report".format(e2))
            report = build_local_report(df)

    _save_report(report)
    return report

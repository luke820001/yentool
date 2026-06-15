import time
import requests
from config.settings import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_API_URL,
    GEMINI_REPORT_FILE,
)
from gemini_hook.prompt_builder import build_prompt

REQUEST_TIMEOUT = 60
RETRY_ATTEMPTS = 3
RETRY_BASE_WAIT = 15  # seconds; doubles each attempt: 15 → 30 → 60


class GeminiError(Exception):
    pass


def _build_endpoint() -> str:
    return "{base}/{model}:generateContent?key={key}".format(
        base=GEMINI_API_URL, model=GEMINI_MODEL, key=GEMINI_API_KEY)


def _call_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY is empty. Set it in the .env file.")

    url = _build_endpoint()
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    wait = RETRY_BASE_WAIT
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        resp = requests.post(url, json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            if attempt < RETRY_ATTEMPTS:
                print("  [Gemini] 429 rate limit — waiting {}s (attempt {}/{})".format(
                    wait, attempt, RETRY_ATTEMPTS))
                time.sleep(wait)
                wait *= 2
                continue
            raise GeminiError("HTTP 429: quota exceeded after {} retries".format(RETRY_ATTEMPTS))
        if resp.status_code != 200:
            raise GeminiError("HTTP {}: {}".format(resp.status_code, resp.text[:300]))
        break

    payload = resp.json()
    try:
        candidates = payload["candidates"]
        parts = candidates[0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError):
        raise GeminiError("Unexpected response shape: {}".format(str(payload)[:300]))

    if not text.strip():
        raise GeminiError("Gemini returned empty text.")
    return text


def _save_report(text: str) -> None:
    GEMINI_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(GEMINI_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def generate_report(df) -> str:
    prompt = build_prompt(df)
    if not prompt:
        raise GeminiError("No data to summarize. Run a scan first.")
    report = _call_gemini(prompt)
    _save_report(report)
    return report

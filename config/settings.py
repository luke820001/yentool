import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Project Root ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Data Storage ---
DATA_DIR = PROJECT_ROOT / "data"
PRICE_VOLUME_FILE = DATA_DIR / "price_volume.db"
LARGE_HOLDER_FILE = DATA_DIR / "large_holder.db"
BROKER_BRANCH_FILE = DATA_DIR / "broker_branch.db"
SIGNAL_LOG_FILE = DATA_DIR / "signal_log.db"
TAIEX_FILE      = DATA_DIR / "taiex.db"

# --- Scan result export (latest version only, for reviewing calculations) ---
SCAN_RESULTS_DIR = DATA_DIR / "scan_results"
SCAN_RESULT_FILE = SCAN_RESULTS_DIR / "scan_result_latest.csv"

# --- Rolling Window ---
# ~400 calendar days => ~270 trading bars. Required so the 52-week-high
# (252-bar) and RS (63-bar) calculations have enough history; 90 days only
# yields ~62 bars, which silently zeroes out both metrics.
ROLLING_DAYS = 400

# --- FinMind API ---
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# --- Groq API (replaces Gemini) ---
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = "llama-3.3-70b-versatile"
GROQ_API_URL   = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_REPORT_FILE = PROJECT_ROOT / "data" / "ai_report.txt"

# --- Watchlist ---
WATCHLIST = [
    "2330",
    "2317",
    "2454",
]

# --- Market Pre-filter ---
PRICE_FILTER_MAX = 100   # legacy: used only by old apply_prefilter path
VOLUME_TOP_N = 50        # legacy

# Full-market scan: no price cap at pre-filter stage, broader candidate pool
# Each scan mode applies its own price filter after verification.
PREFILTER_TOP_N = 200

# --- Signal Thresholds: Condition A ---
CONSOLIDATION_DAYS = 20
CONSOLIDATION_RANGE_PCT = 0.05
EXHAUSTION_VOLUME_LOOKBACK = 20

# --- Signal Thresholds: Condition B ---
LARGE_HOLDER_WEEKS = 3

# Cond_B uses FinMind shareholding data: weekly, optional, and throttled to
# ~1 request / 1.5s. Fetching it for every full-market candidate (100-250
# stocks) serially adds minutes to a scan, so it is OFF by default — the scan
# uses whatever chip data is already cached and never blocks on the network.
# Populate the cache out of band (e.g. main.py on the watchlist) or export
# CHIP_FETCH_IN_SCAN=1 to fetch inline.
CHIP_FETCH_IN_SCAN = os.environ.get("CHIP_FETCH_IN_SCAN", "0") == "1"

# --- Signal Thresholds: Condition C ---
BROKER_NET_SELLER_DAYS = 5
BROKER_CONCENTRATION_PCT = 0.15

# --- Signal Thresholds: Condition D ---
BREAKOUT_VOLUME_MULTIPLIER = 2.5
BREAKOUT_VOLUME_MA_DAYS = 5

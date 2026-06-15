import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Project Root ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Data Storage ---
DATA_DIR = PROJECT_ROOT / "data"
PRICE_VOLUME_FILE = DATA_DIR / "price_volume.xlsx"
LARGE_HOLDER_FILE = DATA_DIR / "large_holder.xlsx"
BROKER_BRANCH_FILE = DATA_DIR / "broker_branch.xlsx"
SIGNAL_LOG_FILE = DATA_DIR / "signal_log.xlsx"
TAIEX_FILE      = DATA_DIR / "taiex.xlsx"

# --- Rolling Window ---
ROLLING_DAYS = 90

# --- FinMind API ---
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# --- Gemini API ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
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

# --- Signal Thresholds: Condition C ---
BROKER_NET_SELLER_DAYS = 5
BROKER_CONCENTRATION_PCT = 0.15

# --- Signal Thresholds: Condition D ---
BREAKOUT_VOLUME_MULTIPLIER = 2.5
BREAKOUT_VOLUME_MA_DAYS = 5

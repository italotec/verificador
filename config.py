import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
REPO_DIR = BASE_DIR.parent   # c:\…\Disparos — shared root for all agents

# AdsPower local API
ADSPOWER_BASE = os.getenv("ADSPOWER_BASE", "http://local.adspower.net:50325")

# Name of the group in AdsPower that holds profiles waiting to be verified
VERIFICAR_GROUP_NAME = os.getenv("VERIFICAR_GROUP_NAME", "Verificar")

# Name of the group profiles are moved to after BM is successfully sent to review
VERIFICADAS_GROUP_NAME = os.getenv("VERIFICADAS_GROUP_NAME", "Verificadas")

# Tag appended to the remark when a BM is confirmed as sent to review
VERIFICADA_REMARK_MARKER = "---VERIFICADA---"

# Gerador CNPJ Flask app (must be running locally)
GERADOR_BASE_URL = os.getenv("GERADOR_BASE_URL", "http://127.0.0.1:5000/")
GERADOR_API_KEY = os.getenv("GERADOR_API_KEY", "")

# SMS24H credentials
SMS24H_API_KEY = os.getenv("SMS24H_API_KEY", "")
SMS24H_COUNTRY = os.getenv("SMS24H_COUNTRY", "73")   # 73 = Brazil
SMS24H_SERVICE = os.getenv("SMS24H_SERVICE", "fb")   # fb = Facebook verification

# How long to wait for an OTP before cancelling (seconds)
SMS_WAIT_TIMEOUT = int(os.getenv("SMS_WAIT_TIMEOUT", "180"))

# Max phone-number retries per profile before giving up
SMS_MAX_ATTEMPTS = int(os.getenv("SMS_MAX_ATTEMPTS", "5"))

# OpenAI API key — used as LLM fallback for wizard step detection
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Temp e-mail site
TUAMAE_URL = "https://tuamaeaquelaursa.com/"

# Marker used inside AdsPower profile remark to store Gerador JSON data
GERADOR_REMARK_MARKER = "---GERADOR---"

# JSON data files (relative to this config file's directory)
PROXIES_FILE = os.getenv("PROXIES_FILE", str(BASE_DIR / "proxies.json"))
ACCOUNTS_FILE = os.getenv("ACCOUNTS_FILE", str(BASE_DIR / "accounts.json"))

# ── debug / observability ─────────────────────────────────────────────────────

# Master switch — set DEBUG=1 in the environment or .env to enable everything below
DEBUG = os.getenv("DEBUG", "1").strip() in ("1", "true", "yes")

# Full-page PNG at every major step + on failure  (auto-on when DEBUG=1)
DEBUG_SCREENSHOTS = os.getenv("DEBUG_SCREENSHOTS", "1" if DEBUG else "0").strip() in ("1", "true", "yes")

# Playwright trace ZIP (screenshots + network + DOM snapshots) — very detailed
DEBUG_TRACE = os.getenv("DEBUG_TRACE", "1").strip() in ("1", "true", "yes")

# Save raw page HTML to disk whenever a stage fails
DEBUG_SAVE_HTML = os.getenv("DEBUG_SAVE_HTML", "0").strip() in ("1", "true", "yes")

# Directory where all debug artifacts are written
DEBUG_DIR = BASE_DIR / os.getenv("DEBUG_DIR", "debug")

# ── automation engine ──────────────────────────────────────────────────────────

# Directory where recorded JSON steps and generated .py scripts are stored
# Lives at the repo root (Disparos/memory/tasks) so all agents share it
TASKS_DIR = REPO_DIR / os.getenv("TASKS_DIR", "memory/tasks")

# How many times the replay engine retries the deterministic script before
# falling back to the LLM-driven automation
REPLAY_MAX_RETRIES = int(os.getenv("REPLAY_MAX_RETRIES", "2"))

# When True, the replay engine auto-confirms saving after a successful LLM run
# (no interactive stdin prompt).  Set REPLAY_AUTO_CONFIRM=1 for unattended runs.
REPLAY_AUTO_CONFIRM = os.getenv("REPLAY_AUTO_CONFIRM", "0").strip() in ("1", "true", "yes")

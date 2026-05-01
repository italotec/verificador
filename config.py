import os
import sys
from pathlib import Path
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    # PyInstaller: .env is extracted to sys._MEIPASS, not os.getcwd()
    load_dotenv(Path(sys._MEIPASS) / ".env")
else:
    load_dotenv(Path(__file__).parent / ".env")

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

# Name of the group profiles are moved to when the BM is restricted
RESTRITA_GROUP_NAME = os.getenv("RESTRITA_GROUP_NAME", "Restrita")

# Tag appended to the remark when a BM is detected as restricted
RESTRITA_REMARK_MARKER = "---RESTRITA---"

# ── Gerador CNPJ (native — no external API dependency) ──────────────────────
# Casa dos Dados API key for CNPJ search & lookup
CASADOSDADOS_API_KEY = os.getenv("CASADOSDADOS_API_KEY", "")

# Population CSV for city filtering during CNPJ search
POP_CSV_PATH = os.getenv("POP_CSV_PATH", str(BASE_DIR / "assets" / "populacao_cidades.csv"))

# CNPJ business card HTML template
CNPJ_CARTAO_TEMPLATE = os.getenv("CNPJ_CARTAO_TEMPLATE", str(BASE_DIR / "assets" / "cnpj_cartao" / "cartaocnpj.html"))

# Storage directory for generated CNPJ artifacts (HTML, PDF, links)
GERADOR_STORAGE_DIR = Path(os.getenv("GERADOR_STORAGE_DIR", str(BASE_DIR / "storage" / "cnpj_runs")))

# CNPJ Bank (pre-generation)
CNPJ_BANK_ENABLED = os.getenv("CNPJ_BANK_ENABLED", "true").lower() == "true"
CNPJ_BANK_TARGET = int(os.getenv("CNPJ_BANK_TARGET", "3"))

# CloudPanel / VPS deployment
CLOUDPANEL_VPS_IP = os.getenv("CLOUDPANEL_VPS_IP", "")
CLOUDPANEL_VPS_USER = os.getenv("CLOUDPANEL_VPS_USER", "")
CLOUDPANEL_VPS_PASS = os.getenv("CLOUDPANEL_VPS_PASS", "")
CLOUDPANEL_SITE_PASS = os.getenv("CLOUDPANEL_SITE_PASS", "")
CLOUDPANEL_PHP_VERSION = os.getenv("CLOUDPANEL_PHP_VERSION", "8.3")
CLOUDPANEL_DOMAINS = os.getenv("CLOUDPANEL_DOMAINS", "lusquetarock.com").split(",")

# Spaceship DNS API
SPACESHIP_API_KEY = os.getenv("SPACESHIP_API_KEY", "")
SPACESHIP_API_SECRET = os.getenv("SPACESHIP_API_SECRET", "")

# SMS24H credentials
SMS24H_API_KEY = os.getenv("SMS24H_API_KEY", "")
SMS24H_COUNTRY = os.getenv("SMS24H_COUNTRY", "73")   # 73 = Brazil
SMS24H_SERVICE = os.getenv("SMS24H_SERVICE", "fb")   # fb = Facebook verification

# How long to wait for an OTP before cancelling (seconds)
SMS_WAIT_TIMEOUT = int(os.getenv("SMS_WAIT_TIMEOUT", "180"))

# Max phone-number retries per profile before giving up
SMS_MAX_ATTEMPTS = int(os.getenv("SMS_MAX_ATTEMPTS", "5"))

# HeroSMS credentials (SMS-Activate compatible alternative to SMS24H)
HEROSMS_API_KEY = os.getenv("HEROSMS_API_KEY", "")
HEROSMS_COUNTRY = os.getenv("HEROSMS_COUNTRY", "73")   # 73 = Brazil
HEROSMS_SERVICE = os.getenv("HEROSMS_SERVICE", "fb")   # fb = Facebook verification
HEROSMS_MAX_PRICE = os.getenv("HEROSMS_MAX_PRICE", "") # max price per number (USD)

# Default SMS provider when no DB setting exists: "sms24h" or "herosms"
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "sms24h")

# OpenAI API key — legacy LLM fallback (being replaced by Claude)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Anthropic API key — used for screenshot analysis, error analysis, browser discovery
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Claude models for different tasks
CLAUDE_FAST_MODEL  = os.getenv("CLAUDE_FAST_MODEL",  "claude-haiku-4-5-20251001")  # fast screenshot analysis
CLAUDE_SMART_MODEL = os.getenv("CLAUDE_SMART_MODEL", "claude-sonnet-4-6")          # complex decisions, error analysis

# Browser Use MCP endpoint (optional — leave empty to disable MCP fallback recovery)
# When set, the bot will invoke this MCP server as a last-resort fallback when both
# keyword and LLM step detection fail (e.g., unknown wizard steps, can't find buttons).
BROWSER_USE_MCP_URL = os.getenv("BROWSER_USE_MCP_URL", "")

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

"""Configuration. Loads .env and exposes typed constants.

Nothing else in the project should read os.environ directly. If you need a new
setting, add it here and to .env.example so the shape of the config stays visible.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root, one level up from src/
ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

# --- Location and time ---
HOME_LAT = float(os.getenv("HOME_LAT", "40.7295"))
HOME_LON = float(os.getenv("HOME_LON", "-73.9965"))
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

# --- Database ---
DB_PATH = ROOT / "assistant.db"

# Backup target on the large archive drive. Set in .env, for example
# BACKUP_DIR=E:\backups\athlete15
# Left empty means backups are skipped, which is fine until phase 3.
BACKUP_DIR = Path(os.getenv("BACKUP_DIR")) if os.getenv("BACKUP_DIR") else None

# --- Telegram (phase 2) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BRIEF_HOUR = int(os.getenv("BRIEF_HOUR", "7"))
BRIEF_MINUTE = int(os.getenv("BRIEF_MINUTE", "0"))

# --- WHOOP (phase 5) ---
WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID", "")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
WHOOP_REDIRECT_URI = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8080/callback")
WHOOP_API_BASE = "https://api.prod.whoop.com"

# OAuth tokens, rewritten on every refresh. Not in .env because this file is
# machine-written rather than hand-edited, and the refresh token rotates. Already
# gitignored.
WHOOP_TOKEN_PATH = ROOT / "whoop_token.json"

# --- Notion (phase 6) ---
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_TASKS_DB_ID = os.getenv("NOTION_TASKS_DB_ID", "")

# Page the coursework database gets created under, used only by
# scripts/create_notion_db.py. A database has to live inside a page; there is no
# workspace root to create one in via the API.
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID", "")

# --- Ollama (phase 7) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# The briefing model. Qwen3 is a hybrid thinking model; llm.py disables thinking
# in the request, so any model swapped in here should tolerate "think": false.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
# Smaller, faster model for interactive replies later. Declared now so the config
# shape is visible; nothing reads it yet. `or` rather than a getenv default
# because .env.example ships the key empty, and an empty string is "set" as far
# as getenv defaults are concerned.
OLLAMA_MODEL_FAST = os.getenv("OLLAMA_MODEL_FAST") or OLLAMA_MODEL

# --- iCloud calendar (CalDAV) ---
# ICLOUD_APP_PASSWORD must be an app-specific password from appleid.apple.com,
# not the Apple ID password. Apple revokes all app-specific passwords whenever
# the Apple ID password changes, so a 401 weeks from now likely means that.
ICLOUD_USERNAME = os.getenv("ICLOUD_USERNAME", "")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD", "")
# The one calendar create_event() may write to. Reads span all calendars.
ICLOUD_CALENDAR_NAME = os.getenv("ICLOUD_CALENDAR_NAME", "athlete15")

# --- Athletics (phase 8) ---
ATHLETICS_ICS_URL = os.getenv("ATHLETICS_ICS_URL", "")

# --- News (phase 1) ---
NEWS_FEEDS = [
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://www.theverge.com/rss/index.xml",
    "https://www.latent.space/feed",
]
NEWS_MAX_ITEMS = 5

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().lstrip("-").isdigit()
}

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DB_PATH = DATA_DIR / "bot.db"
DOWNLOAD_DIR = DATA_DIR / "downloads"

# Адрес локального Bot API сервера (telegram-bot-api).
# Если не задан — используется публичный api.telegram.org (лимит файла 50 МБ).
# С локальным сервером лимит поднимается до 2000 МБ.
TELEGRAM_API_URL = os.getenv("TELEGRAM_API_URL", "").strip()

# API_ID/API_HASH нужны только для контейнера telegram-bot-api.
API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()


def _max_bytes() -> int:
    default_mb = "2000" if TELEGRAM_API_URL else "49"
    raw = os.getenv("TELEGRAM_MAX_MB", default_mb).strip()
    try:
        megabytes = int(raw)
    except ValueError:
        megabytes = int(default_mb)
    return max(megabytes, 1) * 1024 * 1024


TELEGRAM_MAX_BYTES = _max_bytes()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

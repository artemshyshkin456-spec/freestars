import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)


def _stars(name: str, default: str) -> int:
    """Звёзды -> сотые доли (целые числа, без ошибок округления)."""
    return int(round(float(os.getenv(name, default)) * 100))


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip("\"'").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")

TASK_REWARD = _stars("TASK_REWARD", "0.25")   # за канал / бота
REF_REWARD = _stars("REF_REWARD", "0.5")      # за приглашённого друга
MIN_WITHDRAW = _stars("MIN_WITHDRAW", "25")   # минимальный вывод
SEED_TARGET = int(os.getenv("SEED_TARGET", "100"))  # лимит для стартовых ссылок

if not BOT_TOKEN or not ADMIN_ID:
    raise SystemExit("Заполни BOT_TOKEN и ADMIN_ID в файле .env")

SUB_HOLD_DAYS = int(os.getenv("SUB_HOLD_DAYS", "5"))  # срок, в течение которого отписка блокирует вывод
PAY_ACCOUNT = os.getenv("PAY_ACCOUNT", "@silent_warrior_own").strip()  # куда платят за подписчиков
BUY_OPTIONS = (15, 25, 50, 100)  # доступные пакеты (1 подписчик = 1 звезда)

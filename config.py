"""Настройки из .env (образец — .env.example). Все сроки — в секундах, если не сказано иначе."""
import os
import pathlib

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
# Ключи можно держать отдельным файлом вне папки бота: EXTRA_ENV_FILE=/путь/к/файлу (значения .env важнее).
if os.environ.get("EXTRA_ENV_FILE"):
    load_dotenv(os.path.expanduser(os.environ["EXTRA_ENV_FILE"]), override=False)

DATA = ROOT / "data"
REC_DIR = DATA / "rec"
YANDEX_PROFILE = DATA / "yandex-profile"
STATE_FILE = DATA / "state.json"
LOG_DIR = ROOT / "logs"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Кому можно пользоваться ботом (Telegram id через запятую). Пусто — бот закрепится за первым, кто напишет /start.
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}
# 1 — пускать всех (для открытой проверки знакомыми); встречи создаются всё равно с одного аккаунта Яндекса.
ALLOW_ALL = os.environ.get("ALLOW_ALL", "0") == "1"
# Что бот отвечает чужому человеку без доступа: «частный бот» + ссылка, как поднять своего.
# Пусто — только просьба передать id владельцу.
PUBLIC_REPO_URL = os.environ.get("PUBLIC_REPO_URL", "")

BOT_NAME = os.environ.get("BOT_NAME", "Запись встречи")
# 1 — камера бота показывает картинку assets/camera.png; 0 — камера выключена. Микрофон бота выключен всегда.
BOT_CAMERA = os.environ.get("BOT_CAMERA", "1") == "1"

# Все вышли — ждём столько, вдруг кто-то выпал случайно; вернулся — запись продолжается в тот же файл.
# Истекло — запись закрыта, владельцу уходит «встреча закончилась» и сразу аудио.
# Владелец 18.09: 30 с (было 3 мин, потом 2). Цена: переподключение дольше 30 с — это уже две записи.
GRACE_SEC = _int("GRACE_SEC", 30)
# Сколько бот сторожит ссылку ОТ ЕЁ СОЗДАНИЯ: вернулись по той же ссылке внутри срока — запишет снова,
# и эта запись придёт отдельным файлом. Позже бот выходит, для новой записи нужна новая встреча.
# Владелец 21.09: час от создания ссылки (было 5 часов от последнего участника — бот зря сидел полдня).
IDLE_LEAVE_SEC = _int("IDLE_LEAVE_SEC", 3600)
# Одна запись не длиннее — дальше режется на следующую (страховка от бесконечной встречи).
MAX_SEGMENT_SEC = _int("MAX_SEGMENT_SEC", 4 * 3600)
# Сколько последних ссылок бот держит под наблюдением (владелец 18.09: три). Новая ссылка вытесняет
# самую старую, на которой сейчас НЕ идёт запись; идущую запись не трогаем никогда.
MAX_MEETINGS = _int("MAX_MEETINGS", 3)
# 1 — держать заготовку встречи (ссылка сразу, бот в звонке за ~6 с) ценой одного лишнего браузера.
WARM_ENABLED = os.environ.get("WARM_ENABLED", "1") == "1"
# Сколько дней хранить на сервере запись ПОСЛЕ отправки в Telegram (копии остаются у владельца в чате).
# Разрешение владельца 18.09.26: 30 дней. 0 — не удалять никогда.
REC_KEEP_DAYS = _int("REC_KEEP_DAYS", 30)
# Файл больше лимита Telegram (50 МБ у ботов) режется на части.
TG_PART_MB = _int("TG_PART_MB", 45)

# Расшифровка — Groq (бесплатный ключ: console.groq.com). Конспект — DeepSeek, если есть ключ, иначе тоже Groq.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_ASR_MODEL = os.environ.get("GROQ_ASR_MODEL", "whisper-large-v3-turbo")
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_ASR_MODEL = os.environ.get("DEEPGRAM_ASR_MODEL", "nova-3")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

for d in (DATA, REC_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)


def window_words(sec: int | None = None) -> str:
    """Срок сторожения словами для сообщений: «1 ч», «40 мин»."""
    sec = IDLE_LEAVE_SEC if sec is None else int(sec)
    return f"{sec // 3600} ч" if sec >= 3600 else f"{max(1, sec // 60)} мин"

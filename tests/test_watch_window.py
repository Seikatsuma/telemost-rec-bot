"""Час сторожения ссылки считается от её создания (владелец 21.09).

Почему это отдельная проверка: до 21.09 срок считался от ПОСЛЕДНЕГО участника, и встреча,
на которую люди заходили под конец срока, держала бота ещё столько же — а владелец просил
ровно час от создания ссылки, и в этот час повторный вход даёт новую запись.

Запуск: venv/bin/python tests/test_watch_window.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import config  # noqa: E402
config.STATE_FILE = pathlib.Path(tempfile.mkdtemp()) / "state.json"
import bot as B  # noqa: E402
from meeting import Meeting  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    fails += (not ok)


async def _noop(*a, **k):
    return None


def make(created_ago: float) -> Meeting:
    return Meeting("https://telemost.yandex.ru/j/1234567890", 1, _noop, _noop,
                   number=1, created_at=time.time() - created_ago)


def main() -> int:
    check("1. по умолчанию сторожим час", config.IDLE_LEAVE_SEC == 3600, f"{config.IDLE_LEAVE_SEC} с")

    m = make(0)
    check("2. свежая ссылка — впереди почти весь час", 3500 < m.watch_left_sec() <= 3600,
          f"{m.watch_left_sec():.0f} с")

    m_old = make(3601)
    check("3. час прошёл — сторожить нечего", m_old.watch_left_sec() <= 0, f"{m_old.watch_left_sec():.0f} с")

    # Ключевое отличие от старого правила: недавний участник срок НЕ продлевает.
    m_old.last_activity = time.monotonic()
    check("4. недавний участник час не продлевает", m_old.watch_left_sec() <= 0, f"{m_old.watch_left_sec():.0f} с")

    text = make(120)._ended_text(65)
    check("5. внутри часа обещаем записать снова и называем остаток",
          "запишу снова" in text and "мин" in text, text.replace("\n", " / "))
    check("5. и сразу аудио, расшифровка следом", "Аудио пришлю сейчас" in text and "расшифровк" in text, text)

    text_old = make(3600)._ended_text(65)
    check("6. час вышел — честно говорим, что ссылку больше не сторожим",
          "больше не сторожу" in text_old and "запишу снова" not in text_old,
          text_old.replace("\n", " / "))

    # Перезапуск службы не продлевает срок: created_at берётся из state.json.
    started = time.time() - 1800
    B.store.update(lambda d: d["meetings"].__setitem__(
        "1234567890", {"url": "https://telemost.yandex.ru/j/1234567890", "chat_id": 1,
                       "number": 1, "added_at": started}))
    info = B.store.load()["meetings"]["1234567890"]
    m_restored = make(0)
    m_restored.created_at = info["added_at"]
    check("7. после перезапуска остаётся полчаса, а не новый час",
          1750 < m_restored.watch_left_sec() < 1850, f"{m_restored.watch_left_sec():.0f} с")

    print("\nИТОГ:", "всё прошло" if not fails else f"провалено {fails}")
    return 1 if fails else 0


sys.exit(main())

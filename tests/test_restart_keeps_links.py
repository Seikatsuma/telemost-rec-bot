"""Перезапуск службы не должен забывать ссылки; забываются только законченные по делу.

Настоящий браузер не нужен: Meeting подменён заглушкой, state.json — во временной папке.
Запуск: venv/bin/python tests/test_restart_keeps_links.py
"""
import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import config  # noqa: E402
config.STATE_FILE = pathlib.Path(tempfile.mkdtemp()) / "state.json"
import bot as B  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    fails += (not ok)


class FakeMeeting:
    """Ведёт себя как Meeting: finished_for_good ставится, только если run вышел сам."""
    def __init__(self, url, chat_id, *a, number=None, prepared=None, **k):
        self.url, self.chat_id, self.number = url, chat_id, number
        self.finished_for_good = False
        self.stop_event = asyncio.Event()
        self.task = None

    def request_stop(self):
        self.stop_event.set()

    async def run(self):
        await self.stop_event.wait()        # сторожит, пока не остановят
        self.finished_for_good = True


B.Meeting = FakeMeeting


def in_state(url):
    import telemost
    return telemost.meeting_id(url) in B.store.load()["meetings"]


async def main():
    keep = "https://telemost.yandex.ru/j/11111111111111"
    stop = "https://telemost.yandex.ru/j/22222222222222"
    m1, _ = B.start_meeting(keep, 1, number=1)
    m2, _ = B.start_meeting(stop, 1, number=2)
    await asyncio.sleep(0.1)
    check("обе ссылки записаны в состояние", in_state(keep) and in_state(stop))

    m2.request_stop()                       # владелец нажал «Остановить»
    await asyncio.sleep(0.1)
    check("остановленная владельцем — вычеркнута", not in_state(stop))

    m1.task.cancel()                        # так выглядит остановка службы для задачи встречи
    try:
        await m1.task
    except asyncio.CancelledError:
        pass
    check("при остановке службы ссылка осталась — после запуска бот к ней вернётся", in_state(keep))

    print("ИТОГ:", "всё прошло" if not fails else f"провалено {fails}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

"""Правило «три последние ссылки»: вытесняется самая старая БЕЗ записи, запись не трогаем никогда."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as B

class Fake:
    def __init__(self, number, recording):
        self.number, self.segment, self.chat_id = number, (object() if recording else None), 1
        self.label, self.stopped = f"Встреча №{number}", False
        self.task = asyncio.get_event_loop().create_future()
    def request_stop(self):
        self.stopped = True

def setup(spec):
    B.meetings.clear()
    ms = [Fake(n, rec) for n, rec in spec]
    for m in ms:
        B.meetings[str(m.number)] = m
    return ms

fails = 0
def check(name, ok, detail=""):
    global fails
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    fails += (not ok)

async def main():
    ms = setup([(1, False), (2, False)])
    ok, ev = B.make_room()
    check("две ссылки из трёх → место есть, никого не вытесняем", ok and not ev)

    ms = setup([(1, False), (2, False), (3, False)])
    ok, ev = B.make_room()
    check("три пустые → вытеснена самая старая (№1)", ok and [m.number for m in ev] == [1] and ms[0].stopped)

    ms = setup([(1, True), (2, False), (3, False)])
    ok, ev = B.make_room()
    check("на самой старой идёт запись → её не трогаем, вытесняем №2",
          ok and [m.number for m in ev] == [2] and not ms[0].stopped)

    ms = setup([(1, True), (2, True), (3, True)])
    ok, ev = B.make_room()
    check("запись идёт на всех трёх → места нет, никто не остановлен",
          not ok and not ev and not any(m.stopped for m in ms))

    line = B.evicted_line([ms[0]])
    check("строка о вытесненной понятна", "больше не слежу" in line and "Встреча №1" in line, line.strip())
    check("без вытеснения строки нет", B.evicted_line([]) == "")
    print("ИТОГ:", "всё прошло" if not fails else f"провалено {fails}")

asyncio.run(main())

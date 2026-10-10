"""Скорость «🎙 Новая встреча» на живом Телемосте: с заготовкой (warm.py) и без неё.

Меряет: через сколько после «нажатия» есть ссылка и через сколько бот в звонке; затем заходит участник —
проверка, что бот с выключенными камерой и микрофоном и запись пошла. Сообщения в Telegram не шлёт.
Запуск: nice -n 10 venv/bin/python -u tests/live_speed.py
"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from playwright.async_api import async_playwright  # noqa: E402

import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-speed-rec")  # тестовые записи не в data/rec: иначе бот досылает их в «чат 1»
config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"  # тест не пишет встречи в состояние боевого бота
import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from meeting import Meeting  # noqa: E402
from warm import Warm  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        fails.append(name)


async def wait_for(cond, sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.25)
    return cond()


async def run_case(title: str, account, warm: Warm | None, closed: list):
    notes = []

    async def notify(chat, text):
        notes.append(text)

    async def fin(seg):
        closed.append(seg)

    t0 = time.monotonic()
    prepared = await warm.take(wait=0) if warm else None
    url = prepared.url if prepared else await account.create_meeting()
    t_link = time.monotonic() - t0
    m = Meeting(url, 1, notify, fin, grace=10, number=0, prepared=prepared.as_tuple() if prepared else None)
    task = asyncio.create_task(m.run())
    ok = await wait_for(lambda: m.phase.startswith("во встрече"), 120)
    t_in = time.monotonic() - t0
    print(f"\n== {title}: ссылка через {t_link:.1f} с, бот в звонке через {t_in:.1f} с", flush=True)
    check(f"{title}: бот вошёл", ok, m.phase)
    titles = await m.page.evaluate("() => [...document.querySelectorAll('button')].map(b => b.title || b.getAttribute('aria-label') || '').filter(t => /микрофон|камеру/.test(t))")
    check(f"{title}: микрофон бота выключен, камера с картинкой", bool(titles) and "Включить микрофон" in titles and "Выключить микрофон" not in titles and "Выключить камеру" in titles, str(titles))
    return m, task, url, t_link, t_in


async def main():
    account = telemost.YandexAccount()
    warm = Warm(account)
    closed = []
    t0 = time.monotonic()
    warm.ensure()
    await wait_for(lambda: warm.ready is not None, 150)
    print(f"заготовка приготовлена в фоне за {time.monotonic() - t0:.0f} с (пользователь этого не ждёт)", flush=True)
    check("заготовка готова", warm.ready is not None)

    m1, task1, url1, link1, in1 = await run_case("С заготовкой", account, warm, closed)
    check("с заготовкой: ссылка мгновенно (< 1 с)", link1 < 1, f"{link1:.1f} с")
    check("с заготовкой: бот в звонке быстро (< 10 с)", in1 < 10, f"{in1:.1f} с")

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=chromium_args(config.ROOT), env=browser_env())
        c = await b.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 800, "height": 500})
        await c.add_init_script("window.__recKeepMic = true;")
        await c.add_init_script(path=str(config.ROOT / "inject.js"))
        pg = await c.new_page()
        check("участник зашёл во встречу с заготовкой", await telemost.join(pg, url1, "Тест Анна", mute=False) == "joined")
        check("бот увидел участника и пишет", await wait_for(lambda: m1.phase == "идёт запись", 30), m1.phase)
        await pg.screenshot(path="/tmp/tm/speed-anna-view.png")
        await b.close()
    m1.request_stop()
    await asyncio.wait_for(task1, 60)

    warm.ensure()
    await wait_for(lambda: warm.ready is not None, 150)
    own = await account.create_meeting()  # «ссылку прислали сами»
    notes3 = []

    async def n3(c, t):
        notes3.append(t)
    t0 = time.monotonic()
    pr = await warm.take(wait=0)
    m3 = Meeting(own, 1, n3, lambda s: asyncio.sleep(0), grace=10, number=0, prepared=pr.as_tuple() if pr else None)
    task3 = asyncio.create_task(m3.run())
    ok3 = await wait_for(lambda: m3.phase.startswith("во встрече"), 120)
    print(f"\n== Своя ссылка через браузер заготовки: бот в звонке через {time.monotonic()-t0:.1f} с", flush=True)
    check("своя ссылка: заготовка была и бот вошёл именно в эту встречу", pr is not None and ok3 and telemost.meeting_id(m3.page.url) == telemost.meeting_id(own), m3.phase)
    m3.request_stop()
    await asyncio.wait_for(task3, 60)

    m2, task2, _, link2, in2 = await run_case("Без заготовки", account, None, closed)
    m2.request_stop()
    await asyncio.wait_for(task2, 60)
    if warm.ready:
        await warm._dispose(warm.ready)
    print("\nИТОГ:", "всё прошло" if not fails else f"провалено {len(fails)}: {fails}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

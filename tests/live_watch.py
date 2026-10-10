"""Живая проверка «бот сторожит ссылку»: пусто — не пишет и почти не тратит CPU; люди — пишет; все вышли — запись
закрыта, бот остаётся; снова зашли — новая запись. Плюс CPU заготовки на пустой странице. Сообщения в Telegram не шлёт.
Запуск: venv/bin/python -u tests/live_watch.py
"""
import asyncio, os, pathlib, sys, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-watch-rec"); config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"
import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from meeting import Meeting  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from warm import Warm  # noqa: E402
sys.argv = [sys.argv[0]]
import diag_holes_live as H  # noqa: E402


def my_cpu_ticks():
    kids = {}
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                st = open(f'/proc/{d}/stat').read().rsplit(')', 1)[1].split(); kids.setdefault(int(st[1]), []).append(int(d))
            except Exception:
                pass
    tot, stack = 0, list(kids.get(os.getpid(), []))
    while stack:
        p = stack.pop()
        try:
            st = open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split(); tot += int(st[11]) + int(st[12])
        except Exception:
            pass
        stack += kids.get(p, [])
    return tot


async def cpu(sec=20):
    t = my_cpu_ticks(); await asyncio.sleep(sec)
    return (my_cpu_ticks() - t) / os.sysconf('SC_CLK_TCK') / sec * 100


async def wait(cond, sec):
    for _ in range(int(sec)):
        if cond():
            return True
        await asyncio.sleep(1)
    return cond()


async def main():
    warm = Warm(telemost.YandexAccount()); warm.ensure()
    await wait(lambda: warm.ready, 180)
    print(f"заготовка на пустой странице: CPU браузеров {await cpu():.0f}% ядра", flush=True)
    notes = []
    async def notify(c, t): notes.append(t); print("   [TG]", t.split("\n")[0], flush=True)
    t0 = time.monotonic(); pr = await warm.take(wait=0)
    m = Meeting(pr.url, 1, notify, lambda s: asyncio.sleep(0), grace=20, number=0, prepared=pr.as_tuple())
    task = asyncio.create_task(m.run())
    ok = await wait(lambda: m.phase.startswith("во встрече"), 120)
    print(f"бот в звонке: {ok} через {time.monotonic()-t0:.0f} с", flush=True)
    await asyncio.sleep(8)
    print(f"пустая встреча: камера бота вкл={m.camera_on}, CPU {await cpu():.0f}% ядра", flush=True)
    async with async_playwright() as p:
        async def speaker():
            b = await p.chromium.launch(headless=True, args=[a for a in chromium_args(config.ROOT) if "fake-video" not in a], env=browser_env())
            c = await b.new_context(locale="ru-RU", user_agent=UA); await c.add_init_script("window.__recKeepMic = true;")
            await c.add_init_script(H.SENDER_JS); await c.add_init_script(path=str(config.ROOT / "inject.js"))
            pg = await c.new_page(); r = await telemost.join(pg, pr.url, "Тест Анна", mute=False)
            await telemost.click_any(pg, [r"Включить микрофон"], timeout=1500); return b, pg, r
        b, pg, r = await speaker(); print("говорящий:", r, flush=True)
        print("запись началась:", await wait(lambda: m.phase == "идёт запись", 60), "камера вкл:", m.camera_on, flush=True)
        seg1 = m.segment.dir if m.segment else None
        await asyncio.sleep(30)
        await telemost.leave(pg); await b.close()
        closed = await wait(lambda: m.segments_done == 1, 90)
        await asyncio.sleep(4)
        print(f"все вышли → запись закрыта: {closed}; бот остался: {not task.done()} ({m.phase}); камера вкл={m.camera_on}", flush=True)
        b, pg, r = await speaker(); print("говорящий снова:", r, flush=True)
        print("новая запись:", await wait(lambda: m.phase == "идёт запись" and m.segment and m.segment.dir != seg1, 60), flush=True)
        await asyncio.sleep(10)
        await b.close()
    m.request_stop(); await asyncio.wait_for(task, 60)
    if seg1:
        for f in sorted(seg1.glob("part*.webm")):
            rr, c, longest, dur = H.holes(f)
            print(f"запись 1: {dur:.0f} с, дыр в ноль {c} шт ({rr*100:.1f}%), самая длинная {longest} мс", flush=True)
    print("нагрузка сервера:", open('/proc/loadavg').read().split()[:3])

asyncio.run(main())

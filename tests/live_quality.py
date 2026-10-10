"""Качество записи и расход процессора на живом Телемосте — после оптимизаций (заморозка заготовки, редкая перерисовка).

Говорящий подаёт речь прямо в звонок (WebAudio, без фейкового микрофона и шумодава) — его звук без дыр.
Бот идёт штатным путём: заготовка (warm.py) → Meeting(prepared) → запись inject.js. Меряем:
CPU заготовки замороженной, время входа, CPU бота в звонке, дыры «в ноль» в записи бота.
Запуск: venv/bin/python -u tests/live_quality.py
"""
import asyncio
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-quality-rec")
config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"  # тест не пишет встречи в состояние боевого бота
import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from meeting import Meeting  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from warm import Warm  # noqa: E402

sys.argv = [sys.argv[0], "--bot-nice", "0", "--seconds", "1"]
import diag_holes_live as H  # noqa: E402  (SENDER_JS, holes)


def ticks(pid):
    kids = {}
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                st = open(f'/proc/{d}/stat').read().rsplit(')', 1)[1].split()
                kids.setdefault(int(st[1]), []).append(int(d))
            except Exception:
                pass
    tot, stack = 0, [pid]
    while stack:
        p = stack.pop()
        try:
            st = open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split()
            tot += int(st[11]) + int(st[12])
        except Exception:
            pass
        stack += kids.get(p, [])
    return tot


def browser_pid(browser):
    # процесс браузера — ребёнок драйвера playwright текущего процесса; берём самый свежий chrome-headless-shell без --type
    out = os.popen("pgrep -n -f 'chrome-headless-shell.*--remote-debugging-pipe'").read().split()
    return int(out[0]) if out else None


async def cpu(pid, sec=20):
    t1 = ticks(pid)
    await asyncio.sleep(sec)
    return (ticks(pid) - t1) / os.sysconf('SC_CLK_TCK') / sec * 100


async def main():
    warm = Warm(telemost.YandexAccount())
    warm.ensure()
    for _ in range(150):
        if warm.ready:
            break
        await asyncio.sleep(1)
    pid = browser_pid(warm.ready.browser)
    print(f"заготовка замороженная: CPU {await cpu(pid):.0f}% ядра, нагрузка сервера {open('/proc/loadavg').read().split()[0]}", flush=True)

    notes = []

    async def notify(c, t):
        notes.append(t)
    t0 = time.monotonic()
    prepared = await warm.take(wait=0)
    m = Meeting(prepared.url, 1, notify, lambda s: asyncio.sleep(0), grace=20, number=0, prepared=prepared.as_tuple())
    task = asyncio.create_task(m.run())
    for _ in range(90):
        if m.phase.startswith("во встрече"):
            break
        await asyncio.sleep(0.5)
    print(f"бот в звонке через {time.monotonic() - t0:.1f} с ({m.phase})", flush=True)

    async with async_playwright() as p:
        sb = await p.chromium.launch(headless=True, args=[a for a in chromium_args(config.ROOT) if "fake-video" not in a], env=browser_env())
        sc = await sb.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 640, "height": 400})
        await sc.add_init_script("window.__recKeepMic = true;")
        await sc.add_init_script(H.SENDER_JS)
        await sc.add_init_script(path=str(config.ROOT / "inject.js"))
        sp = await sc.new_page()
        print("говорящий:", await telemost.join(sp, prepared.url, "Тест Анна", mute=False), flush=True)
        await telemost.click_any(sp, [r"Включить микрофон"], timeout=1500)
        for _ in range(60):
            if m.phase == "идёт запись":
                break
            await asyncio.sleep(1)
        titles = await m.page.evaluate("() => [...document.querySelectorAll('button')].map(b => b.title || b.getAttribute('aria-label') || '').filter(t => /микрофон|камеру/.test(t))")
        print(f"бот пишет: {m.phase}; видит {m.humans}; кнопки {titles}", flush=True)
        print(f"бот в звонке: CPU {await cpu(pid, 30):.0f}% ядра, нагрузка сервера {open('/proc/loadavg').read().split()[0]}", flush=True)
        await asyncio.sleep(20)
        seg_dir = m.segment.dir if m.segment else None
        m.request_stop()
        await asyncio.wait_for(task, 60)
        await sb.close()
    parts = sorted(seg_dir.glob("part*.webm")) if seg_dir else []
    for f in parts:
        r, c, longest, dur = H.holes(f)
        print(f"запись бота {f.name}: {dur:.0f} с, дыр в ноль {c} шт, {r*100:.1f}% времени, самая длинная {longest} мс", flush=True)


asyncio.run(main())

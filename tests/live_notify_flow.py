"""Живая проверка того, что увидит владелец в Telegram (правила 18.09.26), на настоящем Телемосте.

Настоящие сроки из config (пауза GRACE_SEC — 2 мин), настоящий класс Meeting, но сообщения
не уходят в Telegram, а собираются в список — в чат владельца тест не пишет.

Проверяем:
  1. о входе бота ничего не пришло;
  2. человек поговорил — запись идёт, процессор бота в разговоре;
  3. человек вышел — не позже чем через паузу + 30 с ровно одно сообщение «закончилась»,
     в нём что будет дальше и что можно вернуться;
  4. бот остался в звонке и сторожит ссылку;
  5. запись собрана в файл и в ней есть звук.

Запуск: venv/bin/python -u tests/live_notify_flow.py   (~5 мин)
"""
import asyncio
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-flow-rec")
config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"

import postprocess  # noqa: E402
import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from meeting import Meeting  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

sys.argv = [sys.argv[0], "--bot-nice", "0", "--seconds", "1"]
import diag_holes_live as H  # noqa: E402

fails: list[str] = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        fails.append(name)


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
        cur = stack.pop()
        try:
            st = open(f'/proc/{cur}/stat').read().rsplit(')', 1)[1].split()
            tot += int(st[11]) + int(st[12])
        except Exception:
            pass
        stack += kids.get(cur, [])
    return tot


async def wait_for(cond, sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(1)
    return bool(cond())


async def main() -> int:
    url = await telemost.YandexAccount().create_meeting()
    print("встреча:", url, f"(пауза {config.GRACE_SEC} с)", flush=True)
    notes: list[tuple[float, str]] = []
    closed: list[pathlib.Path] = []
    audio_at: list[float] = []

    async def notify(chat, text):
        notes.append((time.monotonic(), text))
        print("   [в Telegram]", text.replace("\n", " / "), flush=True)

    async def finalize(seg_dir):
        closed.append(await asyncio.to_thread(postprocess.build_audio, seg_dir))
        audio_at.append(time.monotonic())   # в боте в этот миг аудио уходит в Telegram

    m = Meeting(url, 1, notify, finalize, number=0)
    task = asyncio.create_task(m.run())
    check("1. бот вошёл во встречу", await wait_for(lambda: m.phase.startswith("во встрече"), 120), m.phase)
    await asyncio.sleep(3)
    check("1. о входе бота ничего не пришло", not notes, str(notes))

    async with async_playwright() as p:
        sb = await p.chromium.launch(
            headless=True, args=[a for a in chromium_args(config.ROOT) if "fake-video" not in a],
            env=browser_env())
        sctx = await sb.new_context(locale="ru-RU", user_agent=UA,
                                    viewport={"width": 640, "height": 400})
        await sctx.add_init_script("window.__recKeepMic = true;")
        await sctx.add_init_script(H.SENDER_JS)
        await sctx.add_init_script(path=str(ROOT / "inject.js"))
        sp = await sctx.new_page()
        print("говорящий:", await telemost.join(sp, url, "Тест Анна", mute=False), flush=True)
        await telemost.click_any(sp, [r"Включить микрофон"], timeout=1500)
        check("2. запись пошла", await wait_for(lambda: m.phase == "идёт запись", 60), m.phase)

        root = int(subprocess.run(["pgrep", "-n", "-f", "chrome-headless-shell.*telemost-rec-bot"],
                                  capture_output=True, text=True).stdout.split()[0])
        t1, w1 = ticks(root), time.monotonic()
        await asyncio.sleep(20)
        cpu = (ticks(root) - t1) / os.sysconf('SC_CLK_TCK') / (time.monotonic() - w1) * 100
        print(f"   процессор браузера бота в разговоре: {cpu:.0f}% ядра "
              f"(нагрузка сервера {open('/proc/loadavg').read().split()[0]})", flush=True)

        await telemost.leave(sp)
        await sb.close()
        left_at = time.monotonic()

    got = await wait_for(lambda: any("закончилась" in n for _, n in notes), config.GRACE_SEC + 30)
    ended = [(t, n) for t, n in notes if "закончилась" in n]
    check("3. пришло «встреча закончилась»", got, str(notes))
    if ended:
        after = ended[0][0] - left_at
        check(f"3. не позже чем через паузу + 30 с после ухода людей ({config.GRACE_SEC + 30} с)",
              after <= config.GRACE_SEC + 30, f"{after:.0f} с")
        text = ended[0][1]
        check("3. в нём: запись с расшифровкой и что можно вернуться",
              "пришлю" in text and "расшифровк" in text and "запишу снова" in text, text)
    check("3. такое сообщение ровно одно", len(ended) == 1, f"{len(ended)}")
    check("4. бот остался в звонке и сторожит ссылку", not task.done() and m.phase.startswith("во встрече"), m.phase)
    await wait_for(lambda: closed, 60)
    if audio_at:
        after = audio_at[0] - left_at
        check(f"5. аудио готово к отправке не позже паузы + 30 с ({config.GRACE_SEC + 30} с)",
              after <= config.GRACE_SEC + 30, f"{after:.0f} с после ухода человека")
    if closed and closed[0]:
        dur = postprocess.duration(closed[0])
        check("5. запись собрана, звук есть (≥ 15 с)", dur >= 15, f"{dur:.0f} с")
    else:
        check("5. запись собрана", False)

    m.request_stop()
    try:
        await asyncio.wait_for(task, 60)
    except Exception:
        pass
    print("\nИТОГ:", "всё прошло" if not fails else f"провалено {len(fails)}: {fails}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

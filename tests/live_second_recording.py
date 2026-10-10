"""Живая проверка правила 21.09: внутри часа повторный вход даёт ВТОРУЮ отдельную запись.

Настоящий Телемост, настоящий класс Meeting, настоящие сроки из config (пауза GRACE_SEC).
В Telegram ничего не уходит — сообщения собираются в список.

Проверяем:
  1. человек поговорил и вышел → одно «закончилась», в нём назван остаток часа, аудио №1 со звуком;
  2. тот же человек вернулся по той же ссылке → пошла НОВАЯ запись (не продолжение первой);
  3. он снова вышел → второе «закончилась» и аудио №2 — отдельный файл, тоже со звуком;
  4. бот остался сторожить ссылку, и остаток часа честно уменьшается.

Запуск: nice -n 19 venv/bin/python -u tests/live_second_recording.py   (~5 мин)
"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-second-rec")
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


async def wait_for(cond, sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(1)
    return bool(cond())


async def speaker(p, url, name):
    """Участник, который говорит: своя вкладка, речь из файла."""
    b = await p.chromium.launch(headless=True,
                                args=[a for a in chromium_args(config.ROOT) if "fake-video" not in a],
                                env=browser_env())
    ctx = await b.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 640, "height": 400})
    await ctx.add_init_script("window.__recKeepMic = true;")
    await ctx.add_init_script(H.SENDER_JS)
    await ctx.add_init_script(path=str(ROOT / "inject.js"))
    page = await ctx.new_page()
    res = await telemost.join(page, url, name, mute=False)
    for surface in await telemost._surfaces(page):
        if await telemost.click_js(surface, [r"^Включить микрофон$"]):
            break
    return b, page, res


async def main() -> int:
    url = await telemost.YandexAccount().create_meeting()
    print("встреча:", url, f"(пауза {config.GRACE_SEC} с, окно {config.IDLE_LEAVE_SEC // 60} мин)", flush=True)
    notes: list[str] = []
    closed: list[pathlib.Path] = []

    async def notify(chat, text):
        notes.append(text)
        print("   [в Telegram]", text.replace("\n", " / "), flush=True)

    async def finalize(seg_dir):
        closed.append(await asyncio.to_thread(postprocess.build_audio, seg_dir))

    m = Meeting(url, 1, notify, finalize, number=0)
    task = asyncio.create_task(m.run())
    check("0. бот вошёл во встречу", await wait_for(lambda: m.phase.startswith("во встрече"), 150), m.phase)
    left_at_start = m.watch_left_sec()

    async with async_playwright() as p:
        # --- первая встреча ---
        b, page, res = await speaker(p, url, "Тест Анна")
        print("участник:", res, flush=True)
        check("1. запись пошла", await wait_for(lambda: m.phase == "идёт запись", 60), m.phase)
        await asyncio.sleep(14)
        await telemost.leave(page)
        await b.close()
        check("1. пришло «закончилась»", await wait_for(lambda: any("закончилась" in n for n in notes),
                                                        config.GRACE_SEC + 40), str(notes))
        ended = [n for n in notes if "закончилась" in n]
        if ended:
            check("1. в нём назван остаток часа и обещана новая запись",
                  "запишу снова" in ended[0] and "мин" in ended[0], ended[0].replace("\n", " / "))
        check("1. аудио №1 собрано", await wait_for(lambda: len(closed) == 1, 90), f"{len(closed)}")

        # --- возврат по той же ссылке внутри часа ---
        first_dir = closed[0].parent if closed and closed[0] else None
        b2, page2, res2 = await speaker(p, url, "Тест Анна")
        print("участник вернулся:", res2, flush=True)
        check("2. по возврату пошла новая запись",
              await wait_for(lambda: m.phase == "идёт запись" and m.segment is not None
                             and (first_dir is None or m.segment.dir != first_dir), 60), m.phase)
        await asyncio.sleep(14)
        await telemost.leave(page2)
        await b2.close()
        check("3. пришло второе «закончилась»",
              await wait_for(lambda: len([n for n in notes if "закончилась" in n]) == 2,
                             config.GRACE_SEC + 40),
              str([n[:40] for n in notes]))
        check("3. аудио №2 собрано отдельным файлом", await wait_for(lambda: len(closed) == 2, 90),
              f"{len(closed)}")

    if len(closed) == 2 and closed[0] and closed[1]:
        d1, d2 = postprocess.duration(closed[0]), postprocess.duration(closed[1])
        check("3. обе записи со звуком (≥ 8 с)", d1 >= 8 and d2 >= 8, f"{d1:.0f} с и {d2:.0f} с")
        check("3. это разные файлы", closed[0] != closed[1], f"{closed[0].parent.name} / {closed[1].parent.name}")

    check("4. бот остался сторожить ссылку", not task.done() and m.phase.startswith("во встрече"), m.phase)
    left_now = m.watch_left_sec()
    check("4. остаток часа уменьшается, час не продлился",
          left_now < left_at_start <= config.IDLE_LEAVE_SEC,
          f"было {left_at_start / 60:.0f} мин, стало {left_now / 60:.0f} мин")

    m.request_stop()
    try:
        await asyncio.wait_for(task, 60)
    except Exception:
        pass
    print("\nИТОГ:", "всё прошло" if not fails else f"провалено {len(fails)}: {fails}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

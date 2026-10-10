"""Самопроверка на настоящем Телемосте — без людей. Нужен вход в Яндекс (/login).

1. Бот создаёт встречу своим аккаунтом.
2. Бот-записыватель заходит гостем «Запись встречи» (обычный путь Meeting).
3. Второй браузер заходит гостем «Тестовый участник» и 40 с «говорит» записью речи через фейковый микрофон.
4. Участник выходит → через паузу (20 с) запись закрывается и приходит в чат как обычная встреча.
Если что-то не так — в чат уходят снимок экрана и то, что бот видел (число участников, дорожки, уровень звука).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import async_playwright

import config
import telemost
from browser_env import UA, browser_env, chromium_args
from meeting import Meeting

log = logging.getLogger("selftest")

SPEECH = config.ROOT / "tests" / "fixtures" / "speech-ru.ogg"
MIC_ON = [r"Включить микрофон", r"Включить микро"]


def _speech_wav() -> str:
    import subprocess
    wav = config.DATA / "selftest-speech.wav"
    if not wav.exists():
        subprocess.run(["nice", "-n", "19", "ffmpeg", "-loglevel", "error", "-y", "-i", str(SPEECH), "-t", "60",
                        "-ac", "1", "-ar", "48000", str(wav)], check=True)
    return str(wav)


async def run(chat_id: int, account: telemost.YandexAccount, notify: Callable[[int, str], Awaitable[None]],
              send_photo: Callable[[int, bytes, str], Awaitable[None]],
              finalize: Callable[[Path], Awaitable[None]]) -> None:
    await notify(chat_id, "🧪 Проверка: создаю встречу…")
    url = await account.create_meeting()
    await notify(chat_id, "🧪 Проверка: бот и тестовый участник подключаются, ~2 мин")

    closed: list[Path] = []

    async def fin(seg_dir: Path):
        closed.append(seg_dir)
        await finalize(seg_dir)

    bot = Meeting(url, chat_id, notify, fin, grace=20, title="Проверка")
    bot_task = asyncio.create_task(bot.run())

    report: list[str] = []
    try:
        for _ in range(180):
            if bot.phase.startswith("во встрече"):
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"бот не зашёл за 3 мин: {bot.phase}")
        report.append("бот зашёл ✔")

        async with async_playwright() as p:
            args = [a for a in chromium_args(config.ROOT) if not a.startswith("--use-file-for-fake-audio-capture")]
            args.append(f"--use-file-for-fake-audio-capture={_speech_wav()}")
            browser = await p.chromium.launch(headless=True, args=args, env=browser_env())
            ctx = await browser.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 800, "height": 500})
            await ctx.add_init_script("window.__recKeepMic = true;")
            await ctx.add_init_script(path=str(config.ROOT / "inject.js"))
            page = await ctx.new_page()
            res = await telemost.join(page, url, "Тестовый участник", mute=False)
            if res != "joined":
                await send_photo(chat_id, await page.screenshot(), f"Тестовый участник не зашёл: {res}")
                raise RuntimeError("тестовый участник не зашёл")
            # Микрофон тестового участника — в кадре звонка (Телемост 21.09.26), и только DOM-кликом.
            for surface in await telemost._surfaces(page):
                if await telemost.click_js(surface, MIC_ON):
                    break
            report.append("участник зашёл ✔")

            seen = False
            snap = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < 40:
                snap = await telemost.snapshot(bot.page, config.BOT_NAME) if getattr(bot, "page", None) else None
                if bot.segment and bot.phase == "идёт запись":
                    seen = True
                await asyncio.sleep(2)
            report.append(("бот увидел участника и пишет ✔" if seen else "бот НЕ начал запись ✘")
                          + f" (снимок: {json.dumps(snap, ensure_ascii=False)[:400]})")
            if not seen:
                await send_photo(chat_id, await bot.page.screenshot(), "Экран бота: запись не началась")
            await browser.close()
        report.append("участник вышел, жду закрытия записи")

        for _ in range(90):
            if closed:
                break
            await asyncio.sleep(1)
        report.append("запись закрыта и отправлена ✔ — сверьте звук выше" if closed else "запись не закрылась за 90 с ✘")
    except Exception as e:
        log.exception("самопроверка")
        report.append(f"сбой: {e}")
        page_ = getattr(bot, "page", None)
        if page_:
            try:
                await send_photo(chat_id, await page_.screenshot(), "Экран бота в момент сбоя")
            except Exception:
                pass
    finally:
        bot.request_stop()
        try:
            await asyncio.wait_for(bot_task, 60)
        except Exception:
            pass
    await notify(chat_id, "🧪 Проверка:\n" + "\n".join("• " + r.split(" (снимок")[0] for r in report))

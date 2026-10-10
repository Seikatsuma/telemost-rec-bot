"""Полная живая проверка «как пользователь»: настоящий Телемост, настоящие обработчики бота, ответы — в чат владельца.

Команды и кнопки подаются в bot.dp от имени владельца; всё, что бот отправляет, реально уходит в Telegram
и заодно записывается для проверок. Участников играют отдельные браузеры: «Тест Анна» говорит
(tests/fixtures/speech-ru.ogg), «Тест Борис» молчит. Паузы сокращены (GRACE 150 с; IDLE 60 с только на шаге 6), записи — во /tmp.

 0) /start, /login, «🎙 Новая встреча» → ссылка сразу, один раз, в рамке; бот в звонке
 1) Анна говорит → запись; у бота микрофон выкл, камера с картинкой; «📋 Статус» показывает запись
 2) Анна вышла и вернулась в паузу → та же запись
 3) Борис зашёл, Анна вышла → запись без паузы
 4) Борис вышел → пауза → бот отключился; аудио и расшифровка с точными подписями; бота во встрече нет
 5) своя ссылка → бот в звонке; Анна говорит; «⏹ Остановить» посреди записи → запись и расшифровка пришли
 6) «🎙 Новая встреча», никто не пришёл → бот отключился сам с сообщением
 7) «📋 Статус» → встреч нет; чужой человек → «частный бот» + его id; /login от чужого → отказ
Запуск: nice -n 10 venv/bin/python -u tests/live_full_scenario.py  (~15 мин). Пишет в чат владельца — только по его просьбе.
"""
import asyncio
import datetime
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-live-rec")  # не в data/rec: служба бота не должна подхватывать тестовые записи
config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"  # тест не пишет встречи в состояние боевого бота
config.GRACE_SEC = 150  # тестовый участник на загруженном сервере перезаходит до ~75 с
config.IDLE_LEAVE_SEC = 900  # короткий срок ставится только на шаге 6: тестовые участники заходят до ~75 с
import bot as B  # noqa: E402
import selftest  # noqa: E402
import telemost  # noqa: E402
from aiogram.types import Chat, Message, Update, User  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OWNER = next(iter(config.OWNER_IDS))
STRANGER = 777000111
SHOTS = pathlib.Path("/tmp/telemost-live")
SHOTS.mkdir(exist_ok=True)
T0 = time.monotonic()
fails: list[str] = []
sent: list[dict] = []
_uid = [int(time.time())]


def say(msg: str) -> None:
    print(f"[{time.monotonic() - T0:5.0f}с] {msg}", flush=True)


def check(name: str, ok, detail: str = "") -> None:
    say(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


async def wait_for(cond, sec: float) -> bool:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(1)
    return bool(cond())


async def press(text: str, user_id: int = OWNER) -> None:
    _uid[0] += 1
    user = User(id=user_id, is_bot=False, first_name="Тест")
    msg = Message(message_id=_uid[0], date=datetime.datetime.now(), chat=Chat(id=user_id, type="private"),
                  from_user=user, text=text)
    await B.dp.feed_update(B.bot, Update(update_id=_uid[0], message=msg))


def log_requests():
    orig = B.bot.session.make_request

    async def wrapped(bot, method, timeout=None):
        chat = getattr(method, "chat_id", None)
        kind = type(method).__name__
        text = (getattr(method, "text", None) or getattr(method, "caption", None) or "")
        sent.append({"kind": kind, "text": text, "chat": chat})
        say(f"   → {kind}: {text.splitlines()[0] if text else ''}")
        if chat == STRANGER:  # чужому человеку в Telegram не пишем — только проверяем ответ
            return True
        return await orig(bot, method, timeout)
    B.bot.session.make_request = wrapped


def since(i: int) -> list[dict]:
    return sent[i:]


class Person:
    def __init__(self, p, name: str, speaks: bool):
        self.p, self.name, self.speaks = p, name, speaks

    async def join(self, url: str) -> str:
        args = [a for a in chromium_args(config.ROOT)
                if not a.startswith(("--use-file-for-fake-audio-capture", "--use-file-for-fake-video-capture"))]
        args.append(f"--use-file-for-fake-audio-capture={selftest._speech_wav() if self.speaks else config.DATA / 'silence.wav'}")
        self.browser = await self.p.chromium.launch(headless=True, args=args, env=browser_env())
        ctx = await self.browser.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 1000, "height": 650})
        await ctx.add_init_script("window.__recKeepMic = true;")
        await ctx.add_init_script(path=str(config.ROOT / "inject.js"))
        self.page = await ctx.new_page()
        res = await telemost.join(self.page, url, self.name, mute=not self.speaks)
        if self.speaks:
            await telemost.click_any(self.page, selftest.MIC_ON, timeout=1500)
        return res

    async def leave(self) -> None:
        await telemost.leave(self.page)
        await self.browser.close()


def current(chat=OWNER):
    live = B.live_meetings(chat)
    return live[-1] if live else None


CAPTION_RE = re.compile(r"^📝 Расшифровка · Встреча №\d+ · \d\d\.\d\d \d\d:\d\d$")
AUDIO_RE = re.compile(r"^🎧 Встреча №\d+ · \d\d\.\d\d \d\d:\d\d · \d+ мин")


async def wait_delivery(start_idx: int, sec: int = 240) -> tuple[list, list]:
    await wait_for(lambda: any(s["kind"] == "SendDocument" for s in since(start_idx)), sec)
    audios = [s["text"] for s in since(start_idx) if s["kind"] == "SendAudio"]
    docs = [s["text"] for s in since(start_idx) if s["kind"] == "SendDocument"]
    return audios, docs


async def main() -> int:
    log_requests()
    await B.bot.send_message(OWNER, "🧪 Проверка бота «как пользователь» (~15 мин). Ниже — тестовые сообщения.")
    B.warm.ensure()
    say("жду заготовку встречи…")
    await wait_for(lambda: B.warm.ready is not None, 150)

    async with async_playwright() as p:
        # 0
        await press("/start")
        check("0. /start: справка с кнопками", any("Записываю встречи" in s["text"] for s in sent))
        await press("/login")
        check("0. /login: вход в Яндекс есть", any("Вход в Яндекс есть" in s["text"] for s in sent))
        i = len(sent)
        t = time.monotonic()
        await press(B.BTN_NEW)
        card = [s["text"] for s in since(i) if "<pre>https://telemost" in s["text"]]
        check("0. «Новая встреча»: ссылка сразу, в рамке", len(card) == 1 and time.monotonic() - t < 3, f"{time.monotonic()-t:.1f} с")
        check("0. «Создаю встречу…» не понадобилось (заготовка)", not any("Создаю" in s["text"] for s in since(i)))
        m = current()
        url = m.url
        ok = await wait_for(lambda: m.phase.startswith("во встрече"), 60)
        check("0. бот в звонке", ok, f"{time.monotonic()-t:.1f} с")

        # 1
        anna = Person(p, "Тест Анна", True)
        check("1. Анна зашла", await anna.join(url) == "joined")
        check("1. запись началась", await wait_for(lambda: m.phase == "идёт запись", 60), m.phase)
        await asyncio.sleep(15)
        await anna.page.screenshot(path=str(SHOTS / "1-anna-view.png"))
        titles = await m.page.evaluate("() => [...document.querySelectorAll('button')].map(b => b.title || b.getAttribute('aria-label') || '').filter(t => /микрофон|камеру/.test(t))")
        check("1. микрофон бота выключен, камера с картинкой",
              "Включить микрофон" in titles and "Выключить микрофон" not in titles and "Выключить камеру" in titles, str(titles))
        i = len(sent)
        await press(B.BTN_STATUS)
        st = [s["text"] for s in since(i)]
        check("1. «Статус» показывает идущую запись", any("🔴 запись" in x for x in st), str(st))

        # 2
        await anna.leave()
        check("2. Анна вышла → пауза", await wait_for(lambda: "пауз" in m.phase, 60), m.phase)
        await asyncio.sleep(15)
        anna = Person(p, "Тест Анна", True)
        check("2. Анна вернулась", await anna.join(url) == "joined")
        check("2. та же запись", await wait_for(lambda: m.phase == "идёт запись", 60) and m.segments_done == 0, m.phase)
        await asyncio.sleep(20)

        # 3
        boris = Person(p, "Тест Борис", False)
        check("3. Борис зашёл", await boris.join(url) == "joined")
        check("3. бот видит двоих", await wait_for(lambda: len(m.humans) == 2, 40), str(m.humans))
        await anna.leave()
        check("3. Анна вышла, остался Борис", await wait_for(lambda: m.humans == ["Тест Борис"], 60), str(m.humans))
        await asyncio.sleep(10)
        check("3. запись без паузы", m.phase == "идёт запись", m.phase)

        # 4
        i = len(sent)
        await boris.leave()
        check("4. все вышли → бот отключился после паузы", await wait_for(lambda: m.task.done(), config.GRACE_SEC + 90), m.phase)
        check("4. сообщение «все вышли, бот отключился»", any("все вышли, бот отключился" in s["text"] for s in since(i)))
        audios, docs = await wait_delivery(i)
        check("4. аудио с подписью «🎧 Встреча №N · дата · мин»", len(audios) == 1 and AUDIO_RE.match(audios[0]), str(audios))
        check("4. расшифровка — файл с подписью ровно «📝 Расшифровка · Встреча №N · дата»",
              len(docs) == 1 and CAPTION_RE.match(docs[0]), str(docs))
        watcher = Person(p, "Тест Наблюдатель", False)
        await watcher.join(url)
        await asyncio.sleep(6)
        wsnap = await telemost.snapshot(watcher.page, "Тест Наблюдатель")
        check("4. бота во встрече больше нет", wsnap is not None and config.BOT_NAME not in wsnap["humans"],
              str(wsnap and wsnap["humans"]))
        await watcher.leave()

        # 5
        own = await B.account.create_meeting()
        await wait_for(lambda: B.warm.ready is not None, 120)
        t = time.monotonic()
        await press(f"вот ссылка {own}")
        m2 = current()
        check("5. своя ссылка: бот подключается", m2 is not None and m2.id == telemost.meeting_id(own))
        ok = await wait_for(lambda: m2.phase.startswith("во встрече"), 90)
        check("5. бот в звонке", ok, f"{time.monotonic()-t:.1f} с")
        anna = Person(p, "Тест Анна", True)
        await anna.join(own)
        check("5. запись идёт", await wait_for(lambda: m2.phase == "идёт запись", 60), m2.phase)
        await asyncio.sleep(30)
        i = len(sent)
        await press(B.BTN_STOP)
        check("5. «Остановить»: запись остановлена, аудио следом", any("запись остановлена" in s["text"] for s in since(i)))
        check("5. бот отключился", await wait_for(lambda: m2.task.done(), 60), m2.phase)
        audios, docs = await wait_delivery(i)
        check("5. после остановки пришли аудио и расшифровка", len(audios) == 1 and len(docs) == 1, f"{audios} {docs}")
        await anna.leave()

        # 6
        config.IDLE_LEAVE_SEC = 60
        await wait_for(lambda: B.warm.ready is not None, 120)
        i = len(sent)
        await press(B.BTN_NEW)
        m3 = current()
        check("6. новая встреча, бот в звонке", m3 is not None and await wait_for(lambda: m3.phase.startswith("во встрече"), 90))
        check("6. никто не пришёл → бот отключился сам", await wait_for(lambda: m3.task.done(), config.IDLE_LEAVE_SEC + 60), m3.phase)
        check("6. сообщение «никто не пришёл, бот отключился»", any("никто не пришёл, бот отключился" in s["text"] for s in since(i)))

    # 7
    i = len(sent)
    await press(B.BTN_STATUS)
    check("7. «Статус»: встреч нет", any("встреч нет" in s["text"] for s in since(i)))
    i = len(sent)
    await press("/start", STRANGER)
    check("7. чужому: «частный бот» и его id", any("частный бот" in s["text"] and str(STRANGER) in s["text"] for s in since(i)))
    i = len(sent)
    await press("/login", STRANGER)
    check("7. /login от чужого не проходит", not any("QR" in s["text"] or "Вход в Яндекс есть" in s["text"] for s in since(i)))
    links = [s["text"] for s in sent if "telemost.yandex.ru/j/" in s["text"] and "<pre>" not in s["text"]]
    check("7. ссылка не повторяется нигде, кроме карточки", not links, str(links[:2]))
    check("7. нет уведомлений о каждом участнике", not any("Участников:" in s["text"] for s in sent))

    verdict = "всё прошло" if not fails else f"провалено {len(fails)}: " + "; ".join(fails)
    say("ИТОГ: " + verdict)
    await B.bot.send_message(OWNER, "🧪 Проверка закончена: " + verdict)
    if B.warm.ready:
        await B.warm._dispose(B.warm.ready)
    await B.bot.session.close()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

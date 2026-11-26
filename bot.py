"""Telegram-бот записи встреч Яндекс Телемоста.

Управление кнопками под полем ввода (кнопки вместо команд, тексты короче, ссылку не повторять):
🎙 Новая встреча — создать встречу, показать ссылку один раз в рамке, подключиться и записывать.
⏹ Остановить — остановить запись (одна встреча — сразу, несколько — выбор).
⏸ Пауза — бот сохранит запись и выйдет из звонка до «▶️ Продолжить» (в статусе и подтверждении).
📋 Статус — что сейчас пишется.
Ссылка telemost.yandex.ru/j/… в сообщении — подключиться к ней и подтвердить «я в ссылке».
У встречи номер «Встреча №N» — во всех сообщениях он вместо ссылки (названия не даём, Егор 17.09.26).
Редкое — командами: /login (вход в Яндекс по QR), /text (расшифровка вкл/выкл), /selftest.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from logging.handlers import RotatingFileHandler

import requests
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

import config
import postprocess
import selftest
import store
import telemost
from meeting import Meeting
from warm import Warm

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(config.LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")])
log = logging.getLogger("bot")

bot = Bot(config.BOT_TOKEN)
dp = Dispatcher()
account = telemost.YandexAccount()
warm = Warm(account)  # заготовка встречи: ссылка сразу, бот входит за секунды
meetings: dict[str, Meeting] = {}

BTN_NEW, BTN_STOP, BTN_STATUS, BTN_PAUSE = "🎙 Новая встреча", "⏹ Остановить", "📋 Статус", "⏸ Пауза"
KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_STOP)],
                                   [KeyboardButton(text=BTN_PAUSE), KeyboardButton(text=BTN_STATUS)]],
                         resize_keyboard=True, is_persistent=True)

HELP = (
    "Записываю встречи в Яндекс Телемосте.\n\n"
    f"{BTN_NEW} — создам ссылку, запишу разговор\n"
    "🔗 Или пришлите свою ссылку на встречу\n"
    f"{BTN_STOP} — закончить запись сейчас\n"
    f"{BTN_PAUSE} — бот сохранит запись и выйдет; «Продолжить» — вернёт его в звонок\n\n"
    f"Все вышли → через {config.GRACE_SEC} с скажу, что встреча закончилась, сразу пришлю аудио, расшифровку — следом.\n"
    f"Вернулись по той же ссылке в течение {config.window_words()} после её создания → запишу снова.\n"
    f"Слежу за {config.MAX_MEETINGS} последними ссылками: новая вытесняет самую старую."
)
SHORT_DESCRIPTION = "Записываю встречи в Яндекс Телемосте: аудио и расшифровка — сюда в чат."
DESCRIPTION = (
    "Бот записи встреч в Яндекс Телемосте.\n\n"
    "• «Новая встреча» — ссылка сразу, бот в звонке через несколько секунд\n"
    "• или пришлите свою ссылку на встречу\n"
    "• бот виден участникам как «Запись встречи», его микрофон выключен\n"
    "• когда все выйдут — аудио и файл расшифровки придут в этот чат"
)


# ---------------- доступ ----------------

def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id in (set(config.OWNER_IDS) | set(store.load()["owners"]))


def allowed(user_id: int | None) -> bool:
    return user_id is not None and (config.ALLOW_ALL or is_owner(user_id))


def allowed_chat(chat_id: int | None) -> bool:
    return chat_id is not None and chat_id in config.ALLOWED_CHAT_IDS


async def guard(msg: Message) -> bool:
    if allowed(msg.from_user and msg.from_user.id) or allowed_chat(msg.chat.id):
        return True
    # Подсказка, как получить доступ: владелец добавляет id в OWNER_IDS.
    text = f"Это частный бот. Чтобы получить доступ, передайте владельцу ваш id: {msg.from_user.id}"
    if config.PUBLIC_REPO_URL:
        text += (f"\n\nХотите такого же своего? Код открыт: {config.PUBLIC_REPO_URL} — "
                 "в репозитории лежит инструкция для нейросети: отдайте её своему ИИ-агенту, "
                 "он поднимет бота на вашем компьютере и бесплатных ключах.")
    await msg.answer(text, disable_web_page_preview=True)
    return False


async def owner_guard(msg: Message) -> bool:
    if is_owner(msg.from_user and msg.from_user.id):
        return True
    await msg.answer("Эта команда — только для владельца бота.")
    return False


# ---------------- отправка ----------------

async def send_text(chat_id: int, text: str) -> None:
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[i:i + 4000], disable_web_page_preview=True)


async def send_audio(chat_id: int, path: Path, title: str, caption: str) -> None:
    await bot.send_audio(chat_id, FSInputFile(path, filename=f"{title}.m4a"), title=title,
                         performer="Телемост", caption=caption[:1000], request_timeout=600)


async def send_doc(chat_id: int, path: Path, caption: str) -> None:
    await bot.send_document(chat_id, FSInputFile(path), caption=caption[:1024], request_timeout=300)


def want_text(chat_id: int) -> bool:
    return store.load()["chats"].get(str(chat_id), {}).get("text", True)


async def finalize(seg_dir: Path, recovered: bool = False) -> None:
    try:
        await postprocess.process_segment(seg_dir, send_audio, send_text, send_doc, want_text, recovered)
    except Exception:
        log.exception("не отправил запись %s", seg_dir)
        try:
            meta = json.loads((seg_dir / "meta.json").read_text())
            await send_text(int(meta["chat_id"]), f"⚠️ {meta.get('label', 'Встреча')}: не отправилось, повторю при перезапуске.")
        except Exception:
            pass


# ---------------- встречи ----------------

def next_number() -> int:
    box = {}

    def inc(d):
        d["counter"] = d.get("counter", 0) + 1
        box["n"] = d["counter"]
    store.update(inc)
    return box["n"]


def live_meetings(chat_id: int | None = None) -> list[Meeting]:
    """Идущие встречи; с chat_id — только этого чата (чужие встречи не видны и не останавливаются)."""
    return [m for m in meetings.values() if m.task and not m.task.done() and (chat_id is None or m.chat_id == chat_id)]


def start_meeting(url: str, chat_id: int, number: int | None = None, prepared=None,
                  created_at: float | None = None, confirm_join: bool = False) -> tuple[Meeting, bool]:
    mid = telemost.meeting_id(url)
    m = meetings.get(mid)
    if m and m.task and not m.task.done():
        return m, False
    number = number or next_number()
    m = Meeting(url, chat_id, send_text, finalize, number=number, prepared=prepared,
                created_at=created_at, confirm_join=confirm_join)
    meetings[mid] = m
    store.update(lambda d: d["meetings"].__setitem__(mid, {"url": url, "chat_id": chat_id, "number": number,
                                                           "added_at": m.created_at}))

    async def runner():
        try:
            await m.run()
        finally:
            # Вычёркиваем ссылку, только если бот закончил с ней сам. Остановка службы (отмена задачи)
            # и сбой — не повод забывать: после запуска бот вернётся сторожить.
            if m.finished_for_good:
                store.update(lambda d: d["meetings"].pop(mid, None))
            if meetings.get(mid) is m:
                meetings.pop(mid, None)

    m.task = asyncio.create_task(runner())
    return m, True


def stop_text(m: Meeting) -> str:
    if m.segment:
        return f"⏹ {m.label}: запись остановлена, бот отключается. Аудио и расшифровка — следом."
    if m.ever_recorded:
        return f"⏹ {m.label}: бот отключается. Запись уже в чате выше."
    return f"⏹ {m.label}: бот отключается. Записи не было — присылать нечего."


def pause_text(m: Meeting) -> str:
    """Ответ на «⏸ Пауза»: бот сохранит запись и выйдет из звонка, «Продолжить» вернёт (владелец 27.11)."""
    left = config.window_words(m.watch_left_sec())
    if m.segment:
        return (f"⏸ {m.label}: выхожу на паузу — запись сохраню и пришлю. "
                f"«Продолжить» — вернусь в звонок (сторожу ещё {left}).")
    return f"⏸ {m.label}: выхожу на паузу. «Продолжить» — вернусь в звонок (сторожу ещё {left})."


def resume_text(m: Meeting) -> str:
    return f"▶️ {m.label}: продолжаю — захожу в звонок."


def make_room() -> tuple[bool, list[Meeting]]:
    """Освободить место под новую ссылку: бот держит не больше MAX_MEETINGS последних (владелец 18.09).

    Вытесняется самая старая (по номеру) встреча, на которой сейчас НЕ идёт запись — идущую запись
    не обрываем никогда. Если записи идут на всех — места нет, новую ссылку не берём.
    Возвращает (есть ли место, кого вытеснили).
    """
    live = live_meetings()
    evicted: list[Meeting] = []
    while len(live) >= config.MAX_MEETINGS:
        idle = [m for m in live if not m.segment]
        if not idle:
            return False, evicted
        oldest = min(idle, key=lambda m: m.number or 0)
        oldest.request_stop()
        evicted.append(oldest)
        live.remove(oldest)
    return True, evicted


def evicted_line(evicted: list[Meeting]) -> str:
    if not evicted:
        return ""
    names = ", ".join(m.label for m in evicted)
    return f"\n\n{names} — больше не слежу (держу {config.MAX_MEETINGS} последние ссылки)."


BUSY_TEXT = (f"Сейчас идёт запись на всех {config.MAX_MEETINGS} ссылках — новую взять некуда. "
             f"Остановите одну кнопкой «{BTN_STOP}».")


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not config.OWNER_IDS and not store.load()["owners"]:
        store.update(lambda d: d["owners"].append(msg.from_user.id))
    if not await guard(msg):
        return
    text = HELP
    if is_owner(msg.from_user.id) and warm.need_login:
        text += "\n\n👉 Сначала войдите в Яндекс, чтобы бот мог создавать встречи: /login"
    await msg.answer(text, reply_markup=KB)


@dp.message(Command("new"))
@dp.message(F.text == BTN_NEW)
async def cmd_new(msg: Message):
    if not await guard(msg):
        return
    ok, evicted = make_room()
    if not ok:
        await msg.answer(BUSY_TEXT, reply_markup=KB)
        return
    prepared = await warm.take(wait=0)  # готовая заготовка — ссылка сразу, без «Создаю…»
    wait = None
    if prepared is None:
        wait = await msg.answer("⏳ Создаю встречу…")
        prepared = await warm.take(wait=25)  # заготовка как раз готовится — дождаться её быстрее, чем создавать вторую
    try:
        url = prepared.url if prepared else await account.create_meeting()
    except telemost.NotLoggedIn:
        await wait.edit_text("Нужен вход в Яндекс: /login" if is_owner(msg.from_user.id)
                             else "❌ Бот сейчас не может создавать встречи. Пришлите свою ссылку — подключусь.")
        return
    except Exception:
        log.exception("создание встречи")
        await wait.edit_text("❌ Не удалось создать. Пришлите свою ссылку — подключусь.")
        return
    m, _ = start_meeting(url, msg.chat.id, prepared=prepared.as_tuple() if prepared else None)
    warm.ensure(delay=60)  # следующая заготовка — когда этот бот уже войдёт
    # Ссылка — один раз и в рамке (блок кода Telegram: рамка и кнопка «копировать»), дальше только номер.
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть встречу", url=url)]])
    # О входе самого бота не пишем (владелец 18.09): он входит за секунды, люди собираются дольше.
    card = (f"✅ <b>{html.escape(m.label)}</b> создана — можно заходить:\n<pre>{html.escape(url)}</pre>\n"
            "Запись начнётся сама, как только кто-нибудь зайдёт."
            + html.escape(evicted_line(evicted)))
    if wait:
        await wait.edit_text(card, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(card, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("login"))
async def cmd_login(msg: Message):
    if not await owner_guard(msg):
        return

    async def send_qr(png: bytes):
        await msg.answer_photo(BufferedInputFile(png, "qr.png"),
                               caption="Отсканируйте в приложении Яндекс («Умная камера»). Жду 4 мин.")
    try:
        ok = await account.login_qr(send_qr)
    except Exception as e:
        log.exception("вход по QR")
        await msg.answer(f"❌ Вход не удался: {e}")
        return
    if ok:
        warm.need_login = False
        warm.ensure()
    await msg.answer(f"✅ Вход в Яндекс есть — «{BTN_NEW}» создаёт встречи" if ok
                     else "QR не отсканирован за 4 мин. Нажмите /login ещё раз", reply_markup=KB)


@dp.message(Command("selftest"))
async def cmd_selftest(msg: Message):
    if not await owner_guard(msg):
        return

    async def photo(chat_id: int, png: bytes, caption: str):
        await bot.send_photo(chat_id, BufferedInputFile(png, "screen.png"), caption=caption[:1000])

    async def job():
        try:
            await selftest.run(msg.chat.id, account, send_text, photo, finalize)
        except telemost.NotLoggedIn:
            await send_text(msg.chat.id, "Нужен вход в Яндекс: /login")
        except Exception as e:
            log.exception("selftest")
            await send_text(msg.chat.id, f"❌ Проверка упала: {e}")

    asyncio.create_task(job())


@dp.message(Command("text"))
async def cmd_text(msg: Message):
    if not await guard(msg):
        return
    new = not want_text(msg.chat.id)
    store.update(lambda d: d["chats"].setdefault(str(msg.chat.id), {}).__setitem__("text", new))
    await msg.answer("Расшифровка: " + ("вкл" if new else "выкл, только аудио"), reply_markup=KB)


@dp.message(Command("list", "status"))
@dp.message(F.text == BTN_STATUS)
async def cmd_status(msg: Message):
    if not await guard(msg):
        return
    live = live_meetings(msg.chat.id)
    if not live:
        await msg.answer("Сейчас встреч нет.", reply_markup=KB)
        return
    for m in live:
        if m.paused:
            mid_btn = InlineKeyboardButton(text=f"▶️ Продолжить {m.label}", callback_data=f"resume:{m.id}")
        else:
            mid_btn = InlineKeyboardButton(text=f"⏸ Пауза {m.label}", callback_data=f"pause:{m.id}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [mid_btn],
            [InlineKeyboardButton(text=f"⏹ Остановить {m.label}", callback_data=f"stop:{m.id}")]])
        await msg.answer(m.status(), reply_markup=kb)


@dp.message(Command("stop"))
@dp.message(F.text == BTN_STOP)
async def cmd_stop(msg: Message):
    if not await guard(msg):
        return
    live = live_meetings(msg.chat.id)
    if not live:
        await msg.answer("Сейчас встреч нет.", reply_markup=KB)
    elif len(live) == 1:
        # Одна встреча — останавливаем сразу и говорим, что будет дальше.
        live[0].request_stop()
        await msg.answer(stop_text(live[0]), reply_markup=KB)
    else:
        rows = [[InlineKeyboardButton(text=f"⏹ {m.label} — {m.short_state()}", callback_data=f"stop:{m.id}")] for m in live]
        rows.append([InlineKeyboardButton(text="⏹ Все встречи", callback_data="stop:*")])
        await msg.answer(f"Идут {len(live)} встречи. Какую остановить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("pause"))
@dp.message(F.text == BTN_PAUSE)
async def cmd_pause(msg: Message):
    if not await guard(msg):
        return
    live = live_meetings(msg.chat.id)
    pausable = [m for m in live if not m.paused]
    if not live:
        await msg.answer("Сейчас встреч нет.", reply_markup=KB)
    elif not pausable:
        # Всё живое уже на паузе — предложить «продолжить».
        rows = [[InlineKeyboardButton(text=f"▶️ Продолжить {m.label}", callback_data=f"resume:{m.id}")] for m in live]
        await msg.answer("Все встречи на паузе. Какую продолжить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif len(pausable) == 1:
        m = pausable[0]
        m.request_pause()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"▶️ Продолжить {m.label}", callback_data=f"resume:{m.id}")],
            [InlineKeyboardButton(text=f"⏹ Остановить {m.label}", callback_data=f"stop:{m.id}")]])
        await msg.answer(pause_text(m), reply_markup=kb)
    else:
        rows = [[InlineKeyboardButton(text=f"⏸ {m.label} — {m.short_state()}", callback_data=f"pause:{m.id}")] for m in pausable]
        rows.append([InlineKeyboardButton(text="⏸ Все встречи", callback_data="pause:*")])
        await msg.answer(f"Идут {len(pausable)} встречи. Какую поставить на паузу?",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(cq: CallbackQuery):
    if not (allowed(cq.from_user.id) or (cq.message and allowed_chat(cq.message.chat.id))):
        await cq.answer("Нет доступа")
        return
    key = cq.data.split(":", 1)[1]
    mine = live_meetings(cq.message.chat.id)
    targets = mine if key == "*" else [m for m in mine if m.id == key]
    if not targets:
        await cq.answer("Уже остановлена")
        await cq.message.edit_text("Эта встреча уже закончилась.")
        return
    for m in targets:
        m.request_stop()
    await cq.answer("Останавливаю")
    await cq.message.edit_text("\n".join(stop_text(m) for m in targets))


@dp.callback_query(F.data.startswith(("pause:", "resume:")))
async def cb_pause_resume(cq: CallbackQuery):
    if not (allowed(cq.from_user.id) or (cq.message and allowed_chat(cq.message.chat.id))):
        await cq.answer("Нет доступа")
        return
    action, _, key = cq.data.partition(":")
    mine = live_meetings(cq.message.chat.id)
    targets = mine if key == "*" else [m for m in mine if m.id == key]
    if action == "pause":
        targets = [m for m in targets if not m.paused]
        if not targets:
            await cq.answer("Уже на паузе")
            return
        for m in targets:
            m.request_pause()
        await cq.answer("Ставлю на паузу")
        await cq.message.edit_text("\n".join(pause_text(m) for m in targets))
    else:
        changed = [m for m in targets if m.request_resume()]
        if not changed:
            await cq.answer("Уже идёт" if targets else "Встреча закончилась")
            if not targets:
                await cq.message.edit_text("Эта встреча уже закончилась.")
            return
        await cq.answer("Продолжаю")
        await cq.message.edit_text("\n".join(resume_text(m) for m in changed))


@dp.message(F.text | F.caption)
async def on_text(msg: Message):
    text = msg.text or msg.caption or ""
    url = telemost.parse_link(text)
    if not url:
        if msg.chat.type == "private" and await guard(msg):
            await msg.answer(f"Нажмите «{BTN_NEW}» или пришлите ссылку.", reply_markup=KB)
        return
    if not await guard(msg):
        return
    evicted: list[Meeting] = []
    if telemost.meeting_id(url) not in meetings:
        ok, evicted = make_room()
        if not ok:
            await msg.answer(BUSY_TEXT, reply_markup=KB)
            return
    # Браузер заготовки уже скачал код Телемоста — по своей ссылке бот входит на ~5 с быстрее (замер 17.09).
    prepared = await warm.take(wait=0) if telemost.meeting_id(url) not in meetings else None
    # Владелец 23.11: по присланной ссылке подтвердить вход («да, в ссылку я вошёл») — на чужой
    # встрече он бота не видит. У созданных кнопкой карточка и без того есть.
    m, new = start_meeting(url, msg.chat.id, prepared=prepared.as_tuple() if prepared else None,
                           confirm_join=True)
    if prepared:
        warm.ensure(delay=60)
    if new:
        # Управление — сразу под карточкой (владелец 27.11): пауза «вышел, но вернётся» и стоп «конец».
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⏸ Пауза {m.label}", callback_data=f"pause:{m.id}")],
            [InlineKeyboardButton(text=f"⏹ Остановить {m.label}", callback_data=f"stop:{m.id}")]])
        await msg.answer(f"🔗 {m.label}: ссылку принял. Запись начнётся сама, как только кто-нибудь зайдёт."
                         + evicted_line(evicted), reply_markup=kb)
    else:
        await msg.answer(m.status(), reply_markup=KB)


async def setup_branding() -> None:
    """Иконка, описание и короткое описание бота в Telegram — один раз на версию (флаг в state.json)."""
    version = "branding-1"
    if store.load().get("branding") == version:
        return
    try:
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
        avatar = config.ROOT / "assets" / "avatar.png"
        if avatar.exists():
            # setMyProfilePhoto ещё нет в aiogram 3.13 — прямой запрос к Bot API.
            def upload():
                with open(avatar, "rb") as f:
                    r = requests.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/setMyProfilePhoto",
                                      data={"photo": json.dumps({"type": "static", "photo": "attach://avatar"})},
                                      files={"avatar": ("avatar.png", f, "image/png")}, timeout=60)
                return r.json()
            res = await asyncio.to_thread(upload)
            if not res.get("ok"):
                log.warning("иконка бота не поставилась: %s", res)
                return
        store.update(lambda d: d.__setitem__("branding", version))
        log.info("иконка и описание бота обновлены")
    except Exception:
        log.exception("иконка/описание бота")


async def cleanup_loop() -> None:
    """Раз в сутки убирать с сервера записи, доставленные больше REC_KEEP_DAYS дней назад."""
    while True:
        try:
            await asyncio.to_thread(postprocess.cleanup_old_recordings)
        except Exception:
            log.exception("очистка старых записей")
        await asyncio.sleep(86400)


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в .env")
    await setup_branding()
    await bot.set_my_commands([
        BotCommand(command="start", description="Кнопки и справка"),
        BotCommand(command="text", description="Расшифровка вкл/выкл"),
        BotCommand(command="login", description="Вход в Яндекс по QR"),
        BotCommand(command="selftest", description="Проверка на тестовой встрече"),
    ])
    # Записи, не отправленные до перезапуска, — отправить; встречи, которые сторожили, — продолжить.
    for seg in postprocess.unfinished_segments():
        asyncio.create_task(finalize(seg, recovered=True))
    for mid, info in store.load()["meetings"].items():
        # created_at из state: перезапуск службы не продлевает час сторожения (владелец 21.09).
        start_meeting(info["url"], info["chat_id"], info.get("number"), created_at=info.get("added_at"))
    warm.ensure(delay=20)
    asyncio.create_task(cleanup_loop())
    log.info("бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

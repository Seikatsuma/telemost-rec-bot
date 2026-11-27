"""Один бот во одной встрече: заходит, следит за людьми, режет запись на встречи, переживает обрывы.

Правила записи (что считается «встречей»):
- есть хоть один человек (или слышен звук) → идёт запись;
- все вышли → запись на паузе GRACE_SEC; кто-то вернулся за это время → пишем дальше в ту же запись
  (случайный вылет или «переподключусь» не рвёт запись на куски);
- пауза истекла (все вышли, бот один) → запись закрыта, уходит в Telegram, бот выходит из встречи
  записать снова — прислать ссылку ещё раз;
- бота выкинуло / упал браузер → заходит заново, открытая запись продолжается (новый кусок того же файла);
- все вышли и пауза истекла → запись закрыта и отправлена, бот ОСТАЁТСЯ во встрече и ждёт: пришли снова — новая запись;
- «⏸ Пауза» владельца → открытая запись закрыта и отправлена, бот ВЫХОДИТ из звонка и ждёт «продолжить»
  до конца окна сторожения; «продолжить» → заходит заново и пишет при людях (новая запись);
- пока во встрече пусто, камера бота выключена и запись не идёт (почти без CPU);
- прошёл IDLE_LEAVE_SEC от создания ссылки (по умолчанию час) и запись не идёт → бот выходит сам.
Камера и микрофон бота выключены кнопками Телемоста (telemost.media_off) — у участников перечёркнуты.
Каждые 5 с звук уже на диске, поэтому сбой процесса теряет не больше 5 с (см. recover_unfinished в postprocess).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import async_playwright

import config
import telemost
import timeline
from browser_env import BOT_PAGE_INIT, BOT_VIEWPORT, TRACK_STREAMS_INIT, UA, browser_env, chromium_args

log = logging.getLogger("meeting")

Notify = Callable[[int, str], Awaitable[None]]
Finalize = Callable[[Path], Awaitable[None]]

SOUND_LEVEL = 0.02  # слышен звук — значит, люди есть, даже если счётчик участников ошибся
DROP_SEC = 45  # сигнальное соединение молчит столько — считаем, что бота выкинуло


def write_chunk(source, part: str, b64: str) -> None:
    """Кусок звука из страницы. part = "<папка записи>|partNNN": последний кусок приходит уже после закрытия
    записи — пишем по имени, не в «текущую запись». Общая для Meeting и заготовки warm.py."""
    seg_name, _, name = part.partition("|")
    if not seg_name or "/" in seg_name or ".." in seg_name or not name.startswith("part"):
        return
    with open(config.REC_DIR / seg_name / f"{name}.webm", "ab") as f:
        f.write(base64.b64decode(b64))


@dataclass
class Segment:
    dir: Path
    started: float
    parts: int = 0
    active_sec: float = 0.0
    names: set = field(default_factory=set)
    empty_since: float | None = None
    last_tick: float = field(default_factory=time.monotonic)

    def save_meta(self, meeting: "Meeting", done: bool = False) -> None:
        meta = {
            "url": meeting.url, "chat_id": meeting.chat_id, "started": self.started, "label": meeting.label,
            "names": sorted(n for n in self.names if n), "active_sec": round(self.active_sec),
            "parts": self.parts, "done": done,
        }
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "meta.json")


class Meeting:
    def __init__(self, url: str, chat_id: int, notify: Notify, finalize: Finalize, grace: int | None = None,
                 number: int | None = None, title: str | None = None, prepared=None,
                 created_at: float | None = None, confirm_join: bool = False):
        self.url = url
        # prepared — заготовка из warm.py: (playwright, browser, page) уже на экране подключения этой встречи
        self.prepared = prepared
        self.number = number
        self.title = title
        self.grace = grace if grace is not None else config.GRACE_SEC
        self.id = telemost.meeting_id(url)
        self.chat_id = chat_id
        self.notify = notify
        self.finalize = finalize
        self.stop_event = asyncio.Event()
        self.segment: Segment | None = None
        self.humans: list[str] = []
        self.phase = "захожу во встречу"
        self.task: asyncio.Task | None = None
        self.last_activity = time.monotonic()
        self.created_at = created_at or time.time()  # когда появилась ссылка: от неё считается срок сторожения
        self.segments_done = 0
        self.all_left = False
        self.ever_recorded = False     # была ли по этой ссылке хоть одна запись
        self.finished_for_good = False  # бот закончил с этой ссылкой сам — можно забыть её
        self.last_recorded_sec = 0.0
        self.camera_on = True  # после входа media_off включает камеру; в пустой встрече _watch её выключит
        # Встреча по присланной ссылке (владелец 23.11): подтвердить вход — на чужой ссылке владельца
        # может не быть в звонке, иначе он не узнает, что бот добрался. Один раз, перезаход — молча.
        self.confirm_join = confirm_join
        self.join_notified = False
        # «в зале ожидания» — один раз на серию попыток войти (владелец 27.11: бот повторял вход
        # каждые ~30 мин и каждый раз слал «впустите» — 4 одинаковых сообщения за ночь).
        # Сбрасывается после удачного входа: выпал из звонка и снова в зале — сказать ещё раз.
        self.waiting_notified = False
        # Пауза (владелец 27.11): бот сохраняет запись и ВЫХОДИТ из звонка (экономит процессор),
        # встреча ждёт «продолжить» до конца окна сторожения ссылки. «Остановить» — конец в любой момент.
        self.pause_event = asyncio.Event()
        self.resume_event = asyncio.Event()
        self.paused = False

    @property
    def label(self) -> str:
        """Как встречу называть в сообщениях вместо ссылки: название или «Встреча №N»."""
        if self.title:
            return self.title
        return f"Встреча №{self.number}" if self.number else "Встреча"

    def _idle_words(self) -> str:
        return config.window_words()

    def watch_left_sec(self) -> float:
        """Сколько ещё бот сторожит эту ссылку. Отсчёт — от создания ссылки (владелец 21.09),
        поэтому перезапуск службы срок не продлевает: created_at берётся из state.json."""
        return self.created_at + config.IDLE_LEAVE_SEC - time.time()

    def _ended_text(self, rec_sec: float) -> str:
        """Одно сообщение об окончании встречи: что случилось, что будет дальше и что можно сделать."""
        mins = max(1, round(rec_sec / 60))
        left = self.watch_left_sec()
        tail = (f"Если вернётесь по этой ссылке в ближайшие {config.window_words(left)} — запишу снова."
                if left > 120 else
                "Эту ссылку я больше не сторожу — для новой записи создайте встречу заново.")
        return (f"⚪️ {self.label} закончилась — все вышли (записано {mins} мин).\n"
                f"Аудио пришлю сейчас, расшифровку — как будет готова. " + tail)

    def short_state(self) -> str:
        if self.paused:
            return "на паузе"
        if self.segment and self.segment.empty_since:
            return "все вышли"
        if self.segment:
            return f"запись {int(self.segment.active_sec // 60)} мин"
        return "ждёт участников" if self.phase.startswith("во встрече") else "подключается"

    # ---------- статус для кнопки «Статус» ----------
    def status(self) -> str:
        seg = self.segment
        if self.paused:
            what = f"⏸ пауза — «Продолжить» вернёт меня (ещё {config.window_words(self.watch_left_sec())})"
        elif seg and seg.empty_since:
            left = max(0, int(self.grace - (time.monotonic() - seg.empty_since)))
            what = f"все вышли, жду {left // 60}:{left % 60:02d}"
        elif seg:
            what = f"🔴 запись · {int(seg.active_sec // 60)} мин · {len(self.humans) or 1} чел."
        elif self.phase.startswith("во встрече"):
            what = "🟢 бот в звонке, ждёт участников"
        elif self.phase.startswith("захожу") or "заново" in self.phase or "повтор" in self.phase:
            what = "бот подключается"
        else:
            what = self.phase
        return f"{self.label} — {what}"

    def request_stop(self) -> None:
        self.stop_event.set()

    def request_pause(self) -> bool:
        """Поставить на паузу: сохранить запись и выйти из звонка до «продолжить» или конца окна сторожения."""
        if self.paused or self.stop_event.is_set():
            return False
        self.pause_event.set()
        return True

    def request_resume(self) -> bool:
        """«Продолжить»: ещё в звонке — отменить выход; уже вышел — вернуться."""
        if self.stop_event.is_set() or self.finished_for_good:
            return False
        if self.paused:
            self.resume_event.set()
            return True
        if self.pause_event.is_set():
            self.pause_event.clear()
            return True
        return False

    # ---------- жизненный цикл ----------
    async def run(self) -> None:
        fails = 0
        first = self.prepared
        self.prepared = None
        pw = first[0] if first else await async_playwright().start()
        try:
            while not self.stop_event.is_set():
                reason = None
                if first:
                    _, browser, page = first
                    # Заготовка своей встречи — уже на экране подключения; чужой ссылки — браузер с кэшем Телемоста, открыть ссылку.
                    from_warm, first = telemost.meeting_id(page.url) == self.id, None
                    used_warm = True
                else:
                    browser = await pw.chromium.launch(headless=True, args=chromium_args(config.ROOT), env=browser_env())
                    page, from_warm, used_warm = None, False, False
                try:
                    if page is None:
                        page = await self._open_page(browser)
                    self.phase = "захожу во встречу"
                    result = await telemost.join(page, self.url, config.BOT_NAME, on_waiting=self._on_waiting,
                                                 stop_event=self.stop_event, prepared=from_warm)
                    if result == "stopped":
                        break
                    if result == "not_found":
                        await self.notify(self.chat_id, f"❌ {self.label}: встреча по ссылке не найдена или завершена")
                        break
                    if result != "joined":
                        fails += 1
                        await page.screenshot(path=str(config.LOG_DIR / f"join-failed-{self.id}.png"))
                        if fails >= 5 and not self.segment:
                            await self.notify(self.chat_id, f"❌ {self.label}: бот не смог подключиться. Пришлите ссылку ещё раз")
                            break
                        self.phase = f"не зашёл, повтор ({fails})"
                        await self._sleep(min(60, 10 * fails))
                        continue
                    fails = 0
                    self.waiting_notified = False  # вошёл — следующий зал ожидания скажет ещё раз
                    # В журнал — каждый вход, включая перезаход после обрыва: 18.09 бот перезашёл за 14 с,
                    # но в журнале было пусто, и со стороны это выглядело как зависание на 24 минуты.
                    log.info("%s: бот в звонке (%s)", self.id, "браузер заготовки" if used_warm else "новый браузер")
                    # О самом входе не сообщаем (владелец 18.09): бот входит за секунды, а люди собираются
                    # минутами — уведомление «бот зашёл» было шумом. Пишем только о том, что требует внимания.
                    # Исключение — ссылка, присланная владельцем (23.11): там он просил «да, в ссылку я вошёл».
                    if self.confirm_join and not self.join_notified:
                        self.join_notified = True
                        await self.notify(self.chat_id, f"✅ {self.label}: я в ссылке.")
                    reason = await self._watch(page)
                    log.info("%s: выход из наблюдения: %s", self.id, reason)
                    if reason == "idle":
                        if not self.ever_recorded:
                            await self.notify(self.chat_id, f"⚪️ {self.label}: за {self._idle_words()} никто не зашёл — "
                                                            "бот отключился. Чтобы записать встречу, создайте новую.")
                        break
                    if reason == "stop":
                        break
                    if reason != "pause":  # паузу обработаем после закрытия браузера — ниже
                        self.phase = "связь прервалась, захожу заново"
                except Exception:
                    log.exception("%s: сбой браузера", self.id)
                    self.phase = "сбой браузера, захожу заново"
                finally:
                    await self._stop_recorder_quiet()
                    try:
                        await asyncio.wait_for(browser.close(), timeout=30)
                    except Exception:
                        pass
                if reason == "pause":
                    # Пауза (владелец 27.11): бот вышел, запись сохранена и ушла в чат. Ждём «продолжить»
                    # до конца окна сторожения; «Остановить» или истёкшее окно — конец работы со ссылкой.
                    self.paused = True
                    self.phase = "на паузе — жду «продолжить»"
                    resumed = await self._wait_resume()
                    self.paused = False
                    if not resumed:
                        if not self.stop_event.is_set():
                            await self.notify(self.chat_id,
                                              f"⏹ {self.label}: час на паузе истёк — закончил с этой ссылкой. "
                                              "Для новой записи пришлите ссылку ещё раз.")
                        break
                    self.phase = "снова захожу во встречу"
                    continue
                await self._sleep(10)
                # Бот вне встречи дольше паузы — открытая запись закрывается, чтобы не держать её вечно.
                if self.segment and self.segment.empty_since and time.monotonic() - self.segment.empty_since > self.grace:
                    sec = self.segment.active_sec
                    await self._close_segment()
                    await self.notify(self.chat_id, self._ended_text(sec))
                    break
            # Из цикла вышли САМИ (стоп владельцем, никто не пришёл за срок, ссылка мёртвая, вытеснена).
            # При остановке службы сюда не доходим — задачу отменяют, и ссылка остаётся в state.json,
            # чтобы после запуска бот вернулся к ней (18.09.26 перезапуск наугад «забыл» встречу №11).
            self.finished_for_good = True
        finally:
            if self.segment:
                await self._close_segment()
            self.phase = "вышел"
            if first:  # остановили до входа — браузер заготовки не успел попасть в цикл
                try:
                    await asyncio.wait_for(first[1].close(), timeout=20)
                except Exception:
                    pass
            try:
                await pw.stop()
            except Exception:
                pass

    async def _open_page(self, browser):
        ctx = await browser.new_context(locale="ru-RU", user_agent=UA, viewport=BOT_VIEWPORT)
        await ctx.add_init_script(BOT_PAGE_INIT)
        await ctx.add_init_script(TRACK_STREAMS_INIT)
        await ctx.add_init_script(path=str(config.ROOT / "inject.js"))
        await ctx.expose_binding("__recChunk", write_chunk)
        await ctx.expose_binding("__recEvent", timeline.write_event)
        return await ctx.new_page()

    async def _on_waiting(self) -> None:
        self.phase = "в зале ожидания"
        if not self.waiting_notified:
            self.waiting_notified = True
            await self.notify(self.chat_id, f"⏳ {self.label}: бот в зале ожидания — впустите «{config.BOT_NAME}»")

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=sec)
        except asyncio.TimeoutError:
            pass

    async def _wait_resume(self) -> bool:
        """Пауза: ждать «продолжить» до конца окна сторожения ссылки (само окно идёт от её создания).
        True — возвращаемся в звонок; False — стоп владельцем или окно истекло."""
        while not self.stop_event.is_set():
            if self.resume_event.is_set():
                self.resume_event.clear()
                return True
            if self.watch_left_sec() <= 0:
                return False
            await self._sleep(2)
        return False

    # ---------- наблюдение ----------
    @staticmethod
    def _service_memory_mb() -> int | None:
        """Память всей службы бота вместе с браузерами (cgroup): для раннего сигнала об утечке."""
        try:
            group = open("/proc/self/cgroup").read().strip().split("::", 1)[1]
            return int(open(f"/sys/fs/cgroup{group}/memory.current").read()) // (1024 * 1024)
        except Exception:
            return None

    async def _watch(self, page) -> str:
        self.page = page
        signal_lost_at = None
        mem_logged_at = time.monotonic()
        while True:
            if self.stop_event.is_set():
                return "stop"
            if self.pause_event.is_set():
                # Пауза: открытую запись закрыть и отправить, выйти кнопкой — участники видят уход сразу.
                self.pause_event.clear()
                if self.segment:
                    self.last_recorded_sec = self.segment.active_sec
                    await self._close_segment(page)
                try:
                    await telemost.leave(page)
                except Exception:
                    pass
                return "pause"
            snap = await telemost.snapshot(page, config.BOT_NAME)
            if snap is None:
                return "dropped"
            now = time.monotonic()

            if not snap["signalingOpen"]:
                signal_lost_at = signal_lost_at or now
                if now - signal_lost_at > DROP_SEC:
                    return "dropped"
            else:
                signal_lost_at = None

            humans = snap["humans"]
            n = len(humans)
            if n == 0:
                dom = await telemost.dom_people_count(page)
                if dom and dom > 1:
                    n = dom - 1
            if n == 0 and snap["level"] > SOUND_LEVEL and self.segment:
                # Звук ушедших пропадает вместе с их дорожкой; слышна речь при нуле — счётчик ошибся, люди есть.
                n = 1
            self.humans = humans

            self.all_left = False
            try:
                await self._tick(page, n, humans, snap)
            except Exception as e:  # страница закрылась посреди вызова — следующий снимок вернёт None и бот перезайдёт
                log.warning("%s: тик не прошёл: %s", self.id, e)
            if self.all_left:
                await self.notify(self.chat_id, self._ended_text(self.last_recorded_sec))

            if config.BOT_CAMERA and bool(n) != self.camera_on:
                await telemost.set_camera(page, bool(n))
                self.camera_on = bool(n)

            # Раз в 10 минут записи — память службы в журнал. Долгую запись живьём не гоняем (дорого),
            # поэтому утечку, если она есть, увидим на настоящих встречах по росту этого числа (18.09.26).
            if self.segment and now - mem_logged_at > 600:
                mem_logged_at = now
                log.info("%s: пишу %d мин, память службы %s МБ", self.id,
                         int(self.segment.active_sec // 60), self._service_memory_mb())

            if not self.segment and n == 0:
                self.phase = "во встрече, жду участников"
                # Срок считается от создания ссылки, а не от последнего участника: владелец 21.09 —
                # «в течение часа после создания ссылки». Идущую запись это не трогает: сюда попадаем,
                # только когда записи нет и людей нет.
                if self.watch_left_sec() <= 0:
                    await telemost.leave(page)
                    return "idle"
            await self._sleep(2)

    async def _tick(self, page, n: int, humans: list[str], snap: dict) -> None:
        now = time.monotonic()
        seg = self.segment
        if seg:
            if snap["state"] == "recording":
                seg.active_sec += now - seg.last_tick
            seg.last_tick = now
            seg.names.update(humans)

        if n > 0:
            self.last_activity = now
            if not seg:
                seg = self._new_segment()
                self.ever_recorded = True
            if snap["state"] == "idle":
                seg.parts += 1
                await telemost.rec_eval(page, "(p) => __rec.start(p)", f"{seg.dir.name}|part{seg.parts:03d}")
            elif snap["state"] == "paused":
                await telemost.rec_eval(page, "__rec.resume()")
            seg.empty_since = None
            self.phase = "идёт запись"
            if seg.active_sec > config.MAX_SEGMENT_SEC:
                await self._close_segment(page)
            else:
                seg.save_meta(self)
        elif seg:
            if seg.empty_since is None:
                seg.empty_since = now
                if snap["state"] == "recording":
                    await telemost.rec_eval(page, "__rec.pause()")
                self.phase = "все вышли, запись на паузе"
                seg.save_meta(self)
            elif now - seg.empty_since > self.grace:
                self.last_recorded_sec = seg.active_sec
                await self._close_segment(page)
                self.all_left = True

    def _new_segment(self) -> Segment:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        d = config.REC_DIR / f"{self.id}-{stamp}"
        d.mkdir(parents=True, exist_ok=True)
        self.segment = Segment(dir=d, started=time.time())
        self.segment.save_meta(self)
        return self.segment

    async def _stop_recorder_quiet(self, page=None) -> None:
        page = page or getattr(self, "page", None)
        if page is None:
            return
        try:
            await asyncio.wait_for(telemost.rec_eval(page, "__rec.stop()"), timeout=20)
        except Exception:
            pass

    async def _close_segment(self, page=None) -> None:
        seg, self.segment = self.segment, None
        if not seg:
            return
        await self._stop_recorder_quiet(page)
        seg.save_meta(self, done=True)
        # Дать очереди биндингов __recEvent доехать: evaluate доезжает после них по
        # тому же соединению, а write_event без await исполняется за один тик цикла.
        try:
            if page is not None:
                await telemost.rec_eval(page, "1")
        except Exception:
            pass
        await asyncio.sleep(0)
        try:
            timeline.build_speakers(seg.dir)
        except Exception:
            log.exception("%s: не свёл журнал говорящих %s", self.id, seg.dir.name)
        self.segments_done += 1
        self.last_activity = time.monotonic()
        self.phase = "запись закрыта, отправляю; жду новых участников"
        asyncio.create_task(self.finalize(seg.dir))

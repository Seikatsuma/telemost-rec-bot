"""Заготовка встречи: чтобы «🎙 Новая встреча» отдавала ссылку сразу и бот входил за секунды.

Замер до заготовки: ссылка через 12 с (запуск браузера с входом в Яндекс, открытие Телемоста, кнопка «Создать»),
бот во встрече через 35 с (свой браузер, загрузка страницы встречи ~8 с, вход, выключение камеры/микрофона).
Заготовка делает всё это заранее, в фоне: создаёт встречу и держит браузер бота на её экране подключения
(имя введено, камера и микрофон выключены). По кнопке ссылка отдаётся сразу, а боту остаётся нажать «Подключиться».
Сразу после выдачи готовится следующая заготовка. Страница обновляется раз в REFRESH_SEC, чтобы не протухла.
Цена: один лишний браузер без звонка (~250 МБ, ~17 % ядра припаркованным) и неиспользованные встречи
в аккаунте Телемоста — новая создаётся только когда заготовку забрали или она сломалась.

Парковка 27.11: страница остаётся на экране подключения (вход по кнопке ~6 с против ~43 с с перезагрузкой),
но чтобы фейковые камера и микрофон не ели ~30–40 % ядра, треки останавливаются (stop_tracks) и страница
замораживается (set_frozen). take() размораживает. Тот же браузер достаётся и присланной ссылке —
под чужой URL она перезагружается, но код Телемоста уже в кэше.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from playwright.async_api import async_playwright

import config
import telemost
import timeline
from browser_env import BOT_PAGE_INIT, BOT_VIEWPORT, TRACK_STREAMS_INIT, UA, browser_env, chromium_args
from meeting import write_chunk

log = logging.getLogger("warm")

REFRESH_SEC = 20 * 60
HEALTH_SEC = 30
RETRY_SEC = 120


async def _park(page) -> None:
    """Припарковать страницу на экране подключения: остановить фейковые камеру/микрофон
    (иначе ~30–40 % ядра на пустой странице) и заморозить. Замер 27.11 — ~17 % ядра."""
    await telemost.stop_tracks(page)
    await telemost.set_frozen(page, True)


@dataclass
class Prepared:
    playwright: object
    browser: object
    page: object
    url: str
    ready_at: float

    def as_tuple(self):
        return self.playwright, self.browser, self.page


class Warm:
    def __init__(self, account: telemost.YandexAccount):
        self.account = account
        self.ready: Prepared | None = None
        self.task: asyncio.Task | None = None
        self.need_login = False  # заготовка упёрлась в отсутствие входа в Яндекс — /start подскажет владельцу /login
        # Сброшено, пока страница заготовки обновляется (~6 с раз в 20 мин): take() в это окно ждёт конца обновления,
        # иначе встреча получила бы недогруженную страницу (находка ревью 17.09).
        self.not_refreshing = asyncio.Event()
        self.not_refreshing.set()

    def ensure(self, delay: float = 0) -> None:
        """Начать готовить заготовку, если её нет и она не готовится."""
        if not config.WARM_ENABLED:
            return
        if self.ready is None and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._prepare(delay))

    async def take(self, wait: float = 25) -> Prepared | None:
        """Забрать заготовку. Если она прямо сейчас готовится — подождать до wait секунд."""
        if self.ready is None and self.task and not self.task.done():
            await asyncio.wait({self.task}, timeout=wait)
        if self.ready is not None and not self.not_refreshing.is_set():
            try:
                await asyncio.wait_for(self.not_refreshing.wait(), timeout=30)
            except asyncio.TimeoutError:
                return None
        prepared, self.ready = self.ready, None
        if prepared is None:
            return None
        if self.task and not self.task.done():
            self.task.cancel()  # останавливаем обновление страницы — теперь она принадлежит встрече
        self.task = None
        # Страница могла закрыться (сбой браузера) — тогда заготовки нет.
        if prepared.page.is_closed():
            await self._dispose(prepared)
            return None
        # Страница припаркована замороженной — разбудить: на замороженной не выполняется JS
        # (все evaluate в join/prepare молча упали бы).
        await telemost.set_frozen(prepared.page, False)
        log.info("заготовка выдана: %s (готова %d с назад)", prepared.url, time.monotonic() - prepared.ready_at)
        return prepared

    async def _dispose(self, prepared: Prepared) -> None:
        for close in (prepared.browser.close, prepared.playwright.stop):
            try:
                await asyncio.wait_for(close(), timeout=20)
            except Exception:
                pass

    async def _open(self, pw):
        browser = await pw.chromium.launch(headless=True, args=chromium_args(config.ROOT), env=browser_env())
        browser.on("disconnected", lambda _: log.warning("браузер заготовки отключился"))
        ctx = await browser.new_context(locale="ru-RU", user_agent=UA, viewport=BOT_VIEWPORT)
        await ctx.add_init_script(BOT_PAGE_INIT)
        await ctx.add_init_script(TRACK_STREAMS_INIT)
        await ctx.add_init_script(path=str(config.ROOT / "inject.js"))
        await ctx.expose_binding("__recChunk", write_chunk)
        await ctx.expose_binding("__recEvent", timeline.write_event)
        page = await ctx.new_page()
        page.on("crash", lambda _: log.warning("страница заготовки упала"))
        page.on("close", lambda _: log.warning("страница заготовки закрылась"))
        return browser, page

    async def _prepare(self, delay: float) -> None:
        await asyncio.sleep(delay)
        pw = None
        prepared = None
        try:
            url = await self.account.create_meeting()
            pw = await async_playwright().start()
            browser, page = await self._open(pw)
            res = await telemost.prepare(page, url, config.BOT_NAME)
            prepared = Prepared(pw, browser, page, url, time.monotonic())
            if res != "ready":
                raise RuntimeError(f"экран подключения не готов: {res}")
            await _park(page)
            self.ready = prepared
            log.info("заготовка готова: %s", url)
            while True:
                await asyncio.sleep(HEALTH_SEC)
                if self.ready is not prepared:
                    return
                dead = prepared.page.is_closed() or not prepared.browser.is_connected()
                stale = time.monotonic() - prepared.ready_at > REFRESH_SEC
                if not dead and not stale:
                    continue
                self.not_refreshing.clear()
                try:
                    if dead:
                        log.warning("браузер заготовки умер — поднимаю заново")
                        try:
                            await asyncio.wait_for(prepared.browser.close(), timeout=10)
                        except Exception:
                            pass
                        prepared.browser, prepared.page = await self._open(pw)
                    else:
                        log.info("обновляю страницу заготовки")
                        await telemost.set_frozen(prepared.page, False)
                    if await telemost.prepare(prepared.page, url, config.BOT_NAME) != "ready":
                        raise RuntimeError("заготовка не поднялась")
                    await _park(prepared.page)
                    prepared.ready_at = time.monotonic()
                finally:
                    self.not_refreshing.set()
        except asyncio.CancelledError:
            raise
        except telemost.NotLoggedIn:
            self.need_login = True
            log.warning("заготовка: нет входа в Яндекс — жду /login")
            self._drop(prepared, pw)
        except Exception:
            log.exception("заготовка не получилась, повтор через %d с", RETRY_SEC)
            self._drop(prepared, pw)
            asyncio.get_running_loop().call_later(RETRY_SEC, self.ensure)

    def _drop(self, prepared: Prepared | None, pw) -> None:
        if self.ready is prepared:
            self.ready = None
        if prepared:
            asyncio.create_task(self._dispose(prepared))
        elif pw:
            asyncio.create_task(pw.stop())

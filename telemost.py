"""Яндекс Телемост: вход в аккаунт по QR, создание встречи, заход бота гостем.

Почему через сайт, а не API: API Телемоста есть только у платного Яндекс 360 для бизнеса
(yandex.ru/dev/telemost/doc/ru/access), у бесплатного аккаунта его нет. Поэтому встреча
создаётся кнопкой «Создать видеовстречу» в браузере с сохранённым входом, а сам бот заходит
по ссылке гостем, отдельно от аккаунта, — так у него своё имя «Запись встречи».
Кнопки и порядок захода взяты у telemost-music-bot (github.com/eddyy9/telemost-music-bot, browser.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

from playwright.async_api import Page, async_playwright

import config
from browser_env import UA, browser_env, chromium_args

log = logging.getLogger("telemost")

LINK_RE = re.compile(r"https?://(?:telemost\.yandex\.(?:ru|com)|telemost\.360\.yandex\.ru)/j/(\d{6,})", re.I)

JOIN_BUTTONS = [
    r"^Продолжить в браузере$",
    r"^Открыть в браузере$",
    r"Продолжить как гость",
    r"Войти как гость",
    r"^Подключиться$",
    r"^Присоединиться$",
    r"Войти во встречу",
    r"^Продолжить$",
    r"^Join$",
    r"^Continue in browser$",
]
COOKIE_BUTTONS = [r"Allow essential cookies", r"Разрешить необходимые", r"^Allow all$", r"^Принять$"]

# 21.09.26 Телемост переехал в оболочку мессенджера: встреча создаётся кнопкой «Новый звонок»
# (старой «Создать видеовстречу» на странице больше нет — бот молча перестал создавать встречи).
CREATE_BUTTONS = [r"^Новый звонок$", r"Новый звонок", r"Создать видеовстречу", r"Создать встречу"]
# Окна, которые перекрывают главную страницу аккаунта: оценка качества прошлого звонка,
# рассказ про обновление. Пока они висят, до кнопки создания не добраться.
ACCOUNT_MODALS = [r"^Закрыть$", r"^Не сейчас$", r"^Пропустить$", r"^Звучит отлично$", r"^Понятно$"]


def parse_link(text: str) -> str | None:
    m = LINK_RE.search(text or "")
    return f"https://telemost.yandex.ru/j/{m.group(1)}" if m else None


def meeting_id(url: str) -> str:
    m = LINK_RE.search(url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-20:]


async def click_any(page: Page, patterns: list[str], timeout: int = 600) -> str | None:
    """Нажать первую найденную кнопку в главном окне. Ждёт появления до timeout мс на весь список
    (is_visible у Playwright не ждёт вовсе — из-за этого /new не находил кнопку, пока страница дорисовывалась)."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        for pat in patterns:
            rx = re.compile(pat, re.I)
            for loc in (page.get_by_role("button", name=rx), page.get_by_text(rx)):
                try:
                    el = loc.first
                    if await el.is_visible():
                        await el.click(timeout=2500)
                        return pat
                except Exception:
                    continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.3)


# ---------------- Кадр звонка ----------------
# 21.09.26 Телемост переехал в оболочку мессенджера: сам звонок теперь живёт во вложенном кадре
# telemost.yandex.ru/private-join/<id>, а главное окно — это мессенджер. В главном окне нет ни
# кнопки «Подключиться», ни поля имени, ни панели микрофона, ни дорожек, поэтому старый код
# «нажимаю в page» молча не находил ничего: бот отвечал «скоро зайду» и не заходил.
# Второе: поверх экрана подключения висит накладка модального окна (Orb-Overlay2), она съедает
# обычный клик Playwright — кнопки жмём самим DOM-событием.
CALL_FRAME_RX = re.compile(r"/private-join/|/j/\d", re.I)

# «Отлично» — окно «Можно включить ИИ-конспект звонка» в рабочем Телемосте (Яндекс 360), 24.09.26.
ONBOARDING_BUTTONS = [r"^Звучит отлично$", r"^Понятно$", r"^Закрыть ознакомление$", r"^Отлично$"]

_CLICK_JS = """(pats) => {
  const rx = pats.map(p => new RegExp(p, 'i'));
  const els = [...document.querySelectorAll('button,[role=button],[role=menuitem],a')];
  for (const r of rx) {
    for (const el of els) {
      if (!el.getClientRects().length || el.disabled) continue;
      const parts = [(el.innerText || '').trim(), el.getAttribute('aria-label') || '', el.title || ''];
      if (parts.some(t => t && r.test(t))) { el.click(); return r.source; }
    }
  }
  return null;
}"""

_HAS_CONNECT_JS = """() => [...document.querySelectorAll('button')].some(b =>
    b.getClientRects().length && /^(Подключиться|Присоединиться)$/i.test((b.innerText || '').trim()))"""

_TEXT_JS = "() => document.body ? document.body.innerText : ''"


async def click_js(surface, patterns: list[str]) -> str | None:
    """Нажать кнопку самим DOM-событием — мимо накладки модального окна Телемоста."""
    try:
        return await surface.evaluate(_CLICK_JS, patterns)
    except Exception:
        return None


def _frames(page) -> list:
    try:
        return list(page.frames)
    except Exception:
        return []


async def _frame_snapshot(frame, bot_name: str = "") -> dict | None:
    try:
        return await frame.evaluate("(n) => window.__rec && window.__rec.snapshot(n)", bot_name)
    except Exception:
        return None


async def call_frame(page: Page):
    """Кадр, в котором идёт звонок: где дорожки — там и запись. Пока звонка нет — кадр экрана
    подключения (по кнопке «Подключиться» или по адресу), иначе главное окно."""
    fallback = None
    for fr in _frames(page):
        snap = await _frame_snapshot(fr)
        if snap and (snap.get("tracks") or snap.get("signalingEverOpen")):
            return fr
        if fallback is None and fr is not page.main_frame and CALL_FRAME_RX.search(fr.url or ""):
            fallback = fr
        if fallback is None:
            try:
                if await fr.evaluate(_HAS_CONNECT_JS):
                    fallback = fr
            except Exception:
                pass
    return fallback or page.main_frame


async def _surfaces(page: Page) -> list:
    """Кадр звонка первым, главное окно вторым: кнопки ищем сначала там, где звонок."""
    fr = await call_frame(page)
    return [fr] if fr is page.main_frame else [fr, page.main_frame]


async def rec_eval(page: Page, expr: str, arg=None):
    """Вызов записи (__rec) — в том кадре, где живёт звонок."""
    fr = await call_frame(page)
    return await (fr.evaluate(expr, arg) if arg is not None else fr.evaluate(expr))


async def body_text(page) -> str:
    """Текст страницы вместе с кадром звонка: надписи «встречи не существует», «ожидание» живут в кадре."""
    parts = []
    for fr in _frames(page)[:5]:
        try:
            parts.append(await fr.evaluate(_TEXT_JS))
        except Exception:
            continue
    return "\n".join(p for p in parts if p)


# Только поля имени. Общий «input[type=text]» убран 24.09.26: пока кадр звонка не дорисован, он ловил
# строку поиска мессенджера — имя уходило туда, а бот входил «Гостем».
_NOT_SEARCH = ':not([placeholder*="оиск" i]):not([placeholder*="earch" i])'
NAME_SELECTORS = tuple(s + _NOT_SEARCH for s in (
    'input.Orb-Textinput-input', 'input[placeholder*="мя" i]', 'input[placeholder*="name" i]',
    'input[name*="name" i]'))
_NAME_VALUE_JS = """(sels) => { const e = [...document.querySelectorAll(sels)].find(x => x.getClientRects().length);
  return e ? e.value : null; }"""
# Сколько ждать, пока в поле встанет наше имя, прежде чем войти как есть (лучше «Гостем», чем никак).
NAME_WAIT_SEC = 20
CONNECT_BUTTONS = [r"^Подключиться$", r"^Присоединиться$", r"^Join$"]


async def name_ready(page, name: str) -> bool:
    """В поле имени на экране подключения уже стоит наше имя. Нет поля — ещё не готово."""
    for surface in await _surfaces(page):
        try:
            value = await surface.evaluate(_NAME_VALUE_JS, ", ".join(NAME_SELECTORS))
        except Exception:
            continue
        if value is not None:
            return value.strip() == name
    return False


async def fill_name(page, name: str) -> bool:
    """Имя бота на экране подключения. Поле — обычный input без подписи (Orb-Textinput), лежит в кадре
    звонка; fill проходит, хотя клик по кнопке рядом перехватывает накладка (проверено 21.09.26)."""
    for surface in await _surfaces(page):
        for sel in NAME_SELECTORS:
            try:
                el = surface.locator(sel).first
                if not await el.is_visible(timeout=400):
                    continue
                if (await el.input_value()).strip() != name:
                    await el.fill(name, timeout=3000)
                return True
            except Exception:
                continue
    return False


WAITING_ROOM_MAX_SEC = 30 * 60


MIC_OFF_TITLES = ("Выключить микрофон", "Turn off microphone")
CAMERA_ON_TITLES = ("Включить камеру", "Turn on camera")
CAMERA_OFF_TITLES = ("Выключить камеру", "Turn off camera")


async def media_off(page: Page) -> int:
    """Микрофон бота — выключить; камеру — включить с картинкой «Запись встречи» (BOT_CAMERA=1) или выключить.
    Подписи кнопок Телемоста: aria-label «Выключить …» ↔ «Включить …» (живая встреча 21.09.26)."""
    clicked = 0
    titles = MIC_OFF_TITLES + (CAMERA_ON_TITLES if config.BOT_CAMERA else CAMERA_OFF_TITLES)
    for surface in await _surfaces(page):
        hit = 0
        for title in titles:
            if await click_js(surface, [rf"^{re.escape(title)}$"]):
                hit += 1
                await page.wait_for_timeout(250)
        clicked += hit
        if hit:  # нашлись в кадре звонка — в оболочке мессенджера искать нечего (иначе переключим обратно)
            break
    return clicked


async def set_camera(page: Page, on: bool) -> None:
    """Камера бота (картинка «Запись встречи»): включать, когда во встрече люди, и выключать в пустой — меньше CPU."""
    title = "Включить камеру" if on else "Выключить камеру"
    for surface in await _surfaces(page):
        if await click_js(surface, [rf"^{re.escape(title)}$"]):
            return


async def set_frozen(page: Page, frozen: bool) -> None:
    """Заморозить/разморозить страницу: замороженная не выполняет скрипты и не рисует — ~0 CPU."""
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.enable")
        await cdp.send("Page.setWebLifecycleState", {"state": "frozen" if frozen else "active"})
        await cdp.detach()
    except Exception as e:
        log.warning("заморозка страницы (%s): %s", frozen, e)


async def leave(page: Page) -> None:
    """Выйти из встречи кнопкой — участники сразу видят, что бот ушёл (а не через тайм-аут соединения)."""
    for surface in await _surfaces(page):
        if await click_js(surface, [r"^Выйти из встречи$", r"^Выйти из звонка$"]):
            await page.wait_for_timeout(1500)
            return
    # Бот всё равно выйдет — браузер закроется и Телемост уберёт его по разрыву; в логе видно, что кнопка не нашлась.
    log.warning("кнопка «Выйти из встречи» не нашлась")


async def _goto(page: Page, url: str) -> bool:
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return True
        except Exception as e:  # CDN Телемоста иногда не отвечает — повтор
            log.warning("goto %s попытка %d: %s", url, attempt + 1, e)
            await asyncio.sleep(5)
    return False


PRE_JOIN_BUTTONS = [r"^Продолжить в браузере$", r"^Открыть в браузере$", r"Продолжить как гость"]
CONNECT_RX = re.compile(r"^(Подключиться|Присоединиться)$", re.I)


async def connect_ready(page: Page) -> bool:
    """Видна ли кнопка «Подключиться» — экран подготовки дорисован."""
    for surface in await _surfaces(page):
        try:
            if await surface.evaluate(_HAS_CONNECT_JS):
                return True
        except Exception:
            continue
    return False


async def _clear_overlays(page: Page) -> None:
    """Окно «Большое обновление в Телемосте» и баннер кук перекрывают экран подключения."""
    await click_any(page, COOKIE_BUTTONS, timeout=0)
    for surface in await _surfaces(page):
        await click_js(surface, ONBOARDING_BUTTONS)


async def prepare(page: Page, url: str, bot_name: str, timeout: float = 90) -> str:
    """Открыть встречу и остановиться на экране подключения: имя введено, камера и микрофон выключены,
    кнопка «Подключиться» видна. Потом join(..., prepared=True) входит за секунды. ready | not_found | failed."""
    if not await _goto(page, url):
        return "failed"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = await body_text(page)
        if "встречи не существует" in text or "Встреча завершена" in text:
            return "not_found"
        await _clear_overlays(page)
        await click_js(await call_frame(page), PRE_JOIN_BUTTONS)
        filled = await fill_name(page, bot_name)
        await media_off(page)
        if filled and await connect_ready(page):
            return "ready"
        await page.wait_for_timeout(700)
    return "failed"


async def join(page: Page, url: str, bot_name: str,
               on_waiting: Callable[[], Awaitable[None]] | None = None,
               stop_event: asyncio.Event | None = None, mute: bool = True, prepared: bool = False) -> str:
    """Зайти во встречу гостем. Возвращает joined | not_found | failed | stopped.
    prepared=True — страница уже на экране подключения (warm.py), без повторной загрузки."""
    if not prepared and not await _goto(page, url):
        return "failed"

    name = bot_name
    waiting_told = False
    started = time.monotonic()
    deadline = started + 90
    name_warned = False
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return "stopped"
        snap = await snapshot(page, bot_name)
        if snap and snap.get("signalingEverOpen"):
            if mute:
                await page.wait_for_timeout(300)
                await media_off(page)
            return "joined"
        text = await body_text(page)
        if "встречи не существует" in text or "Встреча завершена" in text:
            return "not_found"
        if re.search(r"ожидани|впуст|организатор скоро", text, re.I):
            # Зал ожидания: ждём дольше и один раз говорим, что бота надо впустить.
            if not waiting_told and on_waiting:
                waiting_told = True
                await on_waiting()
            if time.monotonic() - started < WAITING_ROOM_MAX_SEC:
                deadline = max(deadline, time.monotonic() + 30)
        if "Некорректное имя" in text and name != "Zapis vstrechi":
            name = "Zapis vstrechi" if name == "Запись встречи" else "Запись встречи"
        await _clear_overlays(page)
        await fill_name(page, name)
        if mute:
            await media_off(page)  # на экране подготовки — чтобы бот вошёл уже без камеры и микрофона
        # «Подключиться» — только когда в поле уже наше имя: 24.09.26 на боевой встрече бот нажал
        # раньше, чем поле встало, и вошёл «Гостем» — люди не поняли, что это бот записи.
        ready = await name_ready(page, name)
        buttons = JOIN_BUTTONS
        if not ready:
            if time.monotonic() - started < NAME_WAIT_SEC:
                buttons = [b for b in JOIN_BUTTONS if b not in CONNECT_BUTTONS]
            elif not name_warned:
                name_warned = True
                log.warning("имя «%s» не встало за %d с — вхожу как есть", name, NAME_WAIT_SEC)
        hit = None
        for surface in await _surfaces(page):
            hit = hit or await click_js(surface, buttons)
        await page.wait_for_timeout(1000 if hit else 700)
    await page.screenshot(path=str(config.LOG_DIR / f"join-failed-{meeting_id(url)}.png"))
    log.warning("не вошёл за 90 с, кадры: %s", [f.url[:60] for f in _frames(page)])
    return "failed"


async def snapshot(page: Page, bot_name: str) -> dict | None:
    """Снимок звонка из того кадра, где он идёт. None — страницы больше нет (бот выпал)."""
    best = None
    for fr in _frames(page):
        snap = await _frame_snapshot(fr, bot_name)
        if snap is None:
            continue
        if snap.get("tracks") or snap.get("signalingEverOpen"):
            return snap
        best = best or snap
    return best


async def dom_people_count(page: Page) -> int | None:
    """Запасной признак: подписи «На встрече N человек» (в звонке) и «N участник» (в оболочке мессенджера)."""
    text = await body_text(page)
    m = re.search(r"На встрече (\d+) человек", text) or re.search(r"(\d+) участник", text)
    return int(m.group(1)) if m else None


async def click_create(page: Page, timeout: int = 25000) -> bool:
    """Нажать «Новый звонок». 21.09.26 у Телемоста два вида главной страницы: кнопка видна сразу
    либо спрятана в меню «Создать» (зелёный плюс), и поверх может висеть окно «Оцените качество
    связи» после прошлого звонка — из-за него бот однажды не смог создать встречу. Жмём DOM-событием:
    обычный клик уходит в накладку."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        await click_js(page, ACCOUNT_MODALS)
        if await click_js(page, CREATE_BUTTONS):
            return True
        if await click_js(page, [r"^Создать$"]):
            await page.wait_for_timeout(1200)
            if await click_js(page, CREATE_BUTTONS):
                return True
        if time.monotonic() >= deadline:
            return False
        await page.wait_for_timeout(800)


# ---------------- Аккаунт Яндекса: вход по QR и создание встречи ----------------

class NotLoggedIn(Exception):
    pass


class YandexAccount:
    """Один сохранённый профиль браузера с входом в Яндекс. Одновременно им пользуется один вызов."""

    def __init__(self):
        self.lock = asyncio.Lock()

    async def _ctx(self, p):
        config.YANDEX_PROFILE.mkdir(parents=True, exist_ok=True)
        return await p.chromium.launch_persistent_context(
            str(config.YANDEX_PROFILE), headless=True, args=chromium_args(config.ROOT),
            env=browser_env(), locale="ru-RU", user_agent=UA, viewport={"width": 1100, "height": 800})

    @staticmethod
    async def _logged(ctx) -> bool:
        cookies = await ctx.cookies("https://yandex.ru")
        return any(c["name"] == "Session_id" and c["value"] and c["value"] != "noauth" for c in cookies)

    async def is_logged_in(self) -> bool:
        async with self.lock, async_playwright() as p:
            ctx = await self._ctx(p)
            try:
                return await self._logged(ctx)
            finally:
                await ctx.close()

    async def login_qr(self, send_qr: Callable[[bytes], Awaitable[None]], wait_sec: int = 240) -> bool:
        """Показать QR Яндекс ID, дождаться сканирования телефоном. QR обновляется сам — шлём новый снимок раз в 100 с."""
        async with self.lock, async_playwright() as p:
            ctx = await self._ctx(p)
            try:
                if await self._logged(ctx):
                    return True
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://passport.yandex.ru/auth?retpath=https%3A%2F%2Ftelemost.yandex.ru%2F",
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(4000)
                await click_any(page, COOKIE_BUTTONS, timeout=1000)
                if "magic" not in page.url:
                    await click_any(page, [r"QR"], timeout=3000)
                    await page.wait_for_timeout(4000)
                sent_at = 0.0
                deadline = time.monotonic() + wait_sec
                while time.monotonic() < deadline:
                    if await self._logged(ctx):
                        return True
                    if time.monotonic() - sent_at > 100 and "passport" in page.url:
                        await send_qr(await page.screenshot())
                        sent_at = time.monotonic()
                    await page.wait_for_timeout(2000)
                return await self._logged(ctx)
            finally:
                await ctx.close()

    async def create_meeting(self) -> str:
        async with self.lock, async_playwright() as p:
            ctx = await self._ctx(p)
            found: list[str] = []

            async def on_response(resp):
                try:
                    if "json" in (resp.headers.get("content-type") or ""):
                        link = parse_link(await resp.text())
                        if link:
                            found.append(link)
                except Exception:
                    pass

            try:
                if not await self._logged(ctx):
                    raise NotLoggedIn()
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                page.on("response", on_response)
                await page.goto("https://telemost.yandex.ru/", wait_until="domcontentloaded", timeout=60000)
                await click_any(page, COOKIE_BUTTONS, timeout=0)
                if not await click_create(page, timeout=25000):
                    # Второй заход: страница могла недогрузиться или показать проверку. Снимок — в logs для разбора.
                    await page.screenshot(path=str(config.LOG_DIR / "create-nobutton-1.png"))
                    log.warning("нет кнопки создания, url=%s, текст: %s", page.url, (await body_text(page))[:300])
                    await page.goto("https://telemost.yandex.ru/", wait_until="domcontentloaded", timeout=60000)
                    if not await click_create(page, timeout=25000):
                        await page.screenshot(path=str(config.LOG_DIR / "create-nobutton-2.png"))
                        log.warning("нет кнопки создания повторно, url=%s, текст: %s", page.url, (await body_text(page))[:300])
                        raise RuntimeError("не нашёл кнопку создания встречи («Новый звонок»)")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    for pg in ctx.pages:
                        if "passport.yandex" in pg.url:
                            raise NotLoggedIn()
                        link = parse_link(pg.url)
                        if link:
                            return link
                    if found:
                        return found[0]
                    link = parse_link(await body_text(page))
                    if link:
                        return link
                    await page.wait_for_timeout(1000)
                await page.screenshot(path=str(config.LOG_DIR / "create-failed.png"))
                raise RuntimeError("Телемост не отдал ссылку на встречу за 45 с")
            finally:
                await ctx.close()

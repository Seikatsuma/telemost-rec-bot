"""Гейт «нет каши»: хронология «кто когда говорил» на живой встрече.

Разделить голоса по дорожкам нельзя — Телемост смешивает звук всех участников в один
поток (проверено 18.09: один ssrc, один неизменный csrc, уровней громкости нет). Зато он
сам сообщает, кто говорит: в раздаче слотов у каждого участника стоит признак речевой
активности `vad`. Его и пишем в журнал — это единственный источник хронологии.

Двое говорят по заранее известному сценарию, включая намеренное наложение. Бот пишет один
общий файл и журнал речи. Сверяем журнал со сценарием: порядок реплик, метки времени,
сохранилось ли наложение, верно ли названы люди.

Запуск: venv/bin/python tests/live_chronology.py
"""
import asyncio
import base64
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-chrono-rec")
config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"
config.BOT_CAMERA = False

import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from oggopus import OggOpusWriter  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

sys.argv = [sys.argv[0], "--bot-nice", "0", "--seconds", "1"]
import diag_holes_live as H  # noqa: E402

OUT = pathlib.Path("/tmp/telemost-chrono")
OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"):
    f.unlink()

SCRIPT = [
    (0, "Тест Анна", True),
    (10, "Тест Анна", False),
    (12, "Тест Борис", True),
    (22, "Тест Борис", False),
    (24, "Тест Анна", True),
    (28, "Тест Борис", True),      # намеренное наложение
    (34, "Тест Анна", False),
    (34, "Тест Борис", False),
]
EXPECTED = [(0, 10, "Тест Анна"), (12, 22, "Тест Борис"),
            (24, 34, "Тест Анна"), (28, 34, "Тест Борис")]
TOTAL = 37
TOLERANCE = 2.5   # секунды: сюда входит и задержка нажатия кнопки в браузере


class Person:
    def __init__(self, p, name):
        self.p, self.name = p, name

    async def join(self, url):
        args = [a for a in chromium_args(config.ROOT) if "fake-video" not in a]
        self.b = await self.p.chromium.launch(headless=True, args=args, env=browser_env())
        ctx = await self.b.new_context(locale="ru-RU", user_agent=UA,
                                       viewport={"width": 640, "height": 400})
        await ctx.add_init_script("window.__recKeepMic = true;")
        await ctx.add_init_script(H.SENDER_JS)
        await ctx.add_init_script(path=str(ROOT / "inject.js"))
        self.page = await ctx.new_page()
        return await telemost.join(self.page, url, self.name, mute=True)

    async def mic(self, on):
        return await telemost.click_any(
            self.page, [r"Включить микрофон"] if on else [r"Выключить микрофон"], timeout=5000)

    async def close(self):
        await self.b.close()


async def main() -> int:
    url = await telemost.YandexAccount().create_meeting()
    print("встреча:", url, flush=True)
    vad: list[tuple[float, str, bool]] = []
    writer = {"w": None}
    t0 = [None]

    async with async_playwright() as p:
        bb = await p.chromium.launch(
            headless=True,
            args=[a for a in chromium_args(config.ROOT) if "fake-video" not in a] + ["--mute-audio"],
            env=browser_env())
        ctx = await bb.new_context(locale="ru-RU", user_agent=UA,
                                   viewport={"width": 640, "height": 360})

        def on_frame(source, ssrc, rtp_ts, at_ms, b64):
            if writer["w"] is None:
                writer["w"] = OggOpusWriter(OUT / "meeting.ogg")
            writer["w"].write_packet(base64.b64decode(b64))

        def on_event(source, kind, payload):
            if kind != "vad" or t0[0] is None:
                return
            vad.append((time.monotonic() - t0[0], payload.get("name") or payload.get("id"),
                        bool(payload.get("speaking"))))

        await ctx.expose_binding("__recFrame", on_frame)
        await ctx.expose_binding("__recEvent", on_event)
        await ctx.add_init_script(path=str(ROOT / "inject_encoded.js"))
        page = await ctx.new_page()
        print("бот:", await telemost.join(page, url, config.BOT_NAME), flush=True)

        anna, boris = Person(p, "Тест Анна"), Person(p, "Тест Борис")
        print("Анна:", await anna.join(url), flush=True)
        print("Борис:", await boris.join(url), flush=True)
        who = {"Тест Анна": anna, "Тест Борис": boris}
        await asyncio.sleep(4)
        # Видео останавливаем ТОЛЬКО когда участники уже в звонке: если сделать это сразу
        # после входа бота, Телемост пересогласовывает соединение и звук не приходит вовсе
        # (замер 18.09 — ноль кадров).
        dropped = await page.evaluate("() => window.__rec.dropVideo('stop')")
        print(f"  входящее видео остановлено, дорожек: {dropped}", flush=True)
        await asyncio.sleep(2)

        print("\n--- сценарий пошёл ---", flush=True)
        t0[0] = time.monotonic()
        for at, name, on in SCRIPT:
            delay = t0[0] + at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            ok = await who[name].mic(on)
            print(f"  {at:>3} с: {name} {'говорит' if on else 'молчит'}"
                  f"{'' if ok else '  (КНОПКА НЕ НАЙДЕНА)'}", flush=True)
        await asyncio.sleep(max(0, t0[0] + TOTAL - time.monotonic()))

        snap = await page.evaluate("() => window.__rec.snapshot('Запись встречи')")
        print(f"\nсостояние бота: кадров {snap.get('frames')}, байт {snap.get('bytes')}, "
              f"дорожек {snap.get('tracks')}, ошибки {snap.get('errors')}", flush=True)
        await anna.close()
        await boris.close()
        await bb.close()

    if writer["w"]:
        writer["w"].close()

    print("\n================ ЖУРНАЛ РЕЧИ ================")
    for t, name, on in vad:
        print(f"  {t:5.1f} с  {name}: {'начал говорить' if on else 'замолчал'}")

    # Собираем отрезки речи на человека и сглаживаем границы.
    # Признак речевой активности дребезжит: человек делает вдох — приходит «замолчал»,
    # через секунду «снова говорит». Без сглаживания одна реплика распадается на куски
    # (замер 18.09: у Бориса лишний отрезок в 1.2 с). Поэтому близкие отрезки одного
    # человека склеиваем, слишком короткие выбрасываем — это обычная практика для VAD.
    MERGE_GAP = 1.5     # с: пауза короче — та же реплика
    MIN_SEGMENT = 1.0   # с: короче — не реплика, а щелчок

    raw: list[tuple[float, float, str]] = []
    open_at: dict[str, float] = {}
    for t, name, on in vad:
        if on:
            open_at.setdefault(name, t)
        elif name in open_at:
            raw.append((open_at.pop(name), t, name))
    end = vad[-1][0] if vad else TOTAL
    for name, t in open_at.items():
        raw.append((t, end, name))
    raw.sort()

    built: list[tuple[float, float, str]] = []
    for a, b, name in raw:
        prev = next((i for i in range(len(built) - 1, -1, -1) if built[i][2] == name), None)
        if prev is not None and a - built[prev][1] <= MERGE_GAP:
            built[prev] = (built[prev][0], b, name)
        else:
            built.append((a, b, name))
    built = [s for s in built if s[1] - s[0] >= MIN_SEGMENT]
    built.sort()
    if len(raw) != len(built):
        print(f"\n(сглаживание: было {len(raw)} отрезков, стало {len(built)})")

    print("\nчто построил бот:")
    for a, b, name in built:
        print(f"  {a:5.1f} – {b:5.1f}  {name}")
    print("\nсценарий:")
    for a, b, name in EXPECTED:
        print(f"  {a:5.1f} – {b:5.1f}  {name}")

    ok = True
    if writer["w"] is None or not (OUT / "meeting.ogg").exists():
        print("\nПРОВАЛ: запись не получилась")
        ok = False
    if len(built) != len(EXPECTED):
        print(f"\nПРОВАЛ: отрезков речи {len(built)}, ждали {len(EXPECTED)}")
        ok = False
    else:
        for (ga, gb, gname), (ea, eb, ename) in zip(built, EXPECTED):
            d = max(abs(ga - ea), abs(gb - eb))
            good = d <= TOLERANCE and gname == ename
            print(f"  {'ok' if good else 'ПРОВАЛ'}: {ename} — расхождение {d:.1f} с, "
                  f"имя {'верное' if gname == ename else 'НЕВЕРНОЕ (' + gname + ')'}")
            if not good:
                ok = False
    over = [x for x in built if x[2] == "Тест Борис" and x[0] >= 24]
    if over and any(a < over[0][1] and b > over[0][0] and n != "Тест Борис" for a, b, n in built):
        print("  ok: наложение речи сохранено двумя отрезками")
    else:
        print("  ПРОВАЛ: наложение речи не сохранилось")
        ok = False
    print("\nИТОГ:", "хронология верна, каши нет" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))

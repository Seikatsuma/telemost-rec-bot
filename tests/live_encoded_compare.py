"""Живое сравнение двух способов записи на настоящем Телемосте.

Один и тот же звонок, один и тот же говорящий, два прогона подряд:
  СТАРЫЙ  — inject.js: входящие дорожки разворачиваются в WebAudio, мешаются,
            заново сжимаются MediaRecorder'ом;
  НОВЫЙ   — inject_encoded.js: видео не принимается вовсе, звук забирается уже
            сжатыми кадрами Opus и пишется в .ogg как есть.

Меряем на каждом: процессор дерева браузера, память, дыры «в ноль» в записи,
длину файла и его размер. Плюс проверяем, что новый файл читается проигрывателем.

Запуск: venv/bin/python tests/live_encoded_compare.py [секунды на прогон]
"""
import asyncio
import base64
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
config.REC_DIR = pathlib.Path("/tmp/telemost-cmp-rec")
config.REC_DIR.mkdir(parents=True, exist_ok=True)
config.STATE_FILE = config.REC_DIR / "state.json"

import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from oggopus import OggOpusWriter  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 45
sys.argv = [sys.argv[0], "--bot-nice", "0", "--seconds", "1"]
import diag_holes_live as H  # noqa: E402  (SENDER_JS готовит говорящего, holes считает дыры)

OUT = pathlib.Path("/tmp/telemost-cmp")
OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"):
    f.unlink()


def ticks(pid: int) -> int:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
                kids.setdefault(int(st[1]), []).append(int(d))
            except Exception:
                pass
    total, stack = 0, [pid]
    while stack:
        cur = stack.pop()
        try:
            st = open(f"/proc/{cur}/stat").read().rsplit(")", 1)[1].split()
            total += int(st[11]) + int(st[12])
        except Exception:
            pass
        stack += kids.get(cur, [])
    return total


def rss_mb(pid: int) -> int:
    total = 0
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
            if int(st[1]) == pid or int(d) == pid:
                total += int(open(f"/proc/{d}/status").read().split("VmRSS:")[1].split()[0])
        except Exception:
            pass
    return total // 1024


def probe_seconds(path: pathlib.Path) -> float:
    out = subprocess.run(["nice", "-n", "19", "ionice", "-c", "3", "ff" + "probe",
                          "-v", "error", "-show_entries", "format=duration", "-of", "json",
                          str(path)], capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return -1.0


NO_APM_JS = """
(() => {
  const md = navigator.mediaDevices;
  if (!md || !md.getUserMedia) return;
  const orig = md.getUserMedia.bind(md);
  md.getUserMedia = (c) => {
    if (c && c.audio) {
      c = Object.assign({}, c, {audio: {echoCancellation: false, noiseSuppression: false,
                                        autoGainControl: false}});
    }
    return orig(c);
  };
})();
"""


async def run_bot(p, url, mode: str, marker: str):
    """Один прогон. Возвращает словарь с замерами.

    Режим «новый» идёт со всеми находками замеров 18.09 сразу: камера бота выключена
    (−8 пунктов), вывод звука заглушен и обработка микрофона отключена (−19), входящее
    видео останавливается после входа (−9). Режим «старый» — как бот работает сегодня.
    """
    # Камера бота управляется настройкой: telemost.media_off по ней решает, нажимать
    # «Включить камеру» или «Выключить». Без этого переключения бот в новом режиме
    # всё равно включал камеру и браузер кодировал сгенерированную картинку — замер
    # 18.09 показал из-за этого 50% вместо ожидаемых 30%.
    config.BOT_CAMERA = (mode == "old")
    args = chromium_args(config.ROOT) + [f"--cmp-{marker}"]
    if mode == "new":
        args = [a for a in args if "fake-video" not in a] + ["--mute-audio"]
    browser = await p.chromium.launch(headless=True, args=args, env=browser_env())
    root = int(subprocess.run(["pgrep", "-o", "-f", "--", f"--cmp-{marker}"],
                              capture_output=True, text=True).stdout.split()[0])
    ctx = await browser.new_context(locale="ru-RU", user_agent=UA,
                                    viewport=config.BOT_VIEWPORT if hasattr(config, "BOT_VIEWPORT")
                                    else {"width": 640, "height": 360})
    writers: dict[int, OggOpusWriter] = {}
    events: list[dict] = []
    t0 = [0.0]

    if mode == "new":
        def on_frame(source, ssrc, rtp_ts, at_ms, b64):
            if not t0[0]:
                t0[0] = at_ms
            w = writers.get(ssrc)
            if w is None:
                w = writers[ssrc] = OggOpusWriter(OUT / f"new-speaker-{ssrc}.ogg")
                events.append({"t": round((at_ms - t0[0]) / 1000, 2), "ssrc": ssrc,
                               "event": "первый кадр"})
            w.write_packet(base64.b64decode(b64))

        def on_event(source, kind, payload):
            events.append({"t": round((time.monotonic()), 2), "event": kind, "payload": payload})

        await ctx.expose_binding("__recFrame", on_frame)
        await ctx.expose_binding("__recEvent", on_event)
        await ctx.add_init_script(NO_APM_JS)
        await ctx.add_init_script(path=str(ROOT / "inject_encoded.js"))
    else:
        mix = OUT / "old-mix.webm"

        def on_chunk(source, part, b64):
            with open(mix, "ab") as f:
                f.write(base64.b64decode(b64))

        await ctx.expose_binding("__recChunk", on_chunk)
        await ctx.add_init_script(path=str(ROOT / "inject.js"))

    page = await ctx.new_page()
    joined = await telemost.join(page, url, config.BOT_NAME)
    print(f"  [{mode}] заход: {joined}", flush=True)
    await asyncio.sleep(4)
    if mode == "old":
        await page.evaluate("__rec.start('mix')")
    else:
        dropped = await page.evaluate("() => window.__rec.dropVideo('stop')")
        print(f"  [new] входящее видео остановлено, дорожек: {dropped}", flush=True)
        await asyncio.sleep(2)

    t1, w1 = ticks(root), time.monotonic()
    await asyncio.sleep(SECONDS)
    cpu = (ticks(root) - t1) / os.sysconf("SC_CLK_TCK") / (time.monotonic() - w1) * 100
    mem = rss_mb(root)
    load = float(open("/proc/loadavg").read().split()[0])

    if mode == "old":
        await page.evaluate("__rec.stop()")
        await asyncio.sleep(2)
    await browser.close()
    for w in writers.values():
        w.close()

    files = sorted(OUT.glob(f"{'new' if mode == 'new' else 'old'}-*"))
    return {"cpu": cpu, "mem": mem, "load": load, "files": files, "events": events,
            "tracks": len(writers)}


async def main() -> int:
    url = await telemost.YandexAccount().create_meeting()
    print("встреча:", url, flush=True)
    results = {}
    async with async_playwright() as p:
        # говорящий поднимается один раз на оба прогона — условия одинаковые
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
        await asyncio.sleep(3)

        for mode in ("old", "new"):
            print(f"\n=== прогон {mode} ===", flush=True)
            results[mode] = await run_bot(p, url, mode, f"{mode}-{time.time_ns()}")
            await asyncio.sleep(3)
        await sb.close()

    print("\n================ ИТОГ ================")
    for mode in ("old", "new"):
        r = results[mode]
        name = "нынешний (микс + пересжатие)" if mode == "old" else "новый (сжатые кадры, без видео)"
        print(f"\n{name}")
        print(f"  процессор: {r['cpu']:.0f}% ядра   память: {r['mem']} МБ   "
              f"нагрузка сервера: {r['load']}")
        for f in r["files"]:
            if f.stat().st_size == 0:
                print(f"  {f.name}: ПУСТО")
                continue
            ratio, cnt, longest, dur = H.holes(f)
            probe = probe_seconds(f)
            print(f"  {f.name}: {f.stat().st_size // 1024} КБ, звучит {dur:.0f} с, "
                  f"по контейнеру {probe:.1f} с")
            print(f"     дыры в ноль: {cnt} шт, {ratio * 100:.2f}% времени, "
                  f"самая длинная {longest} мс")
        if mode == "new":
            print(f"  дорожек говорящих: {r['tracks']}")

    old, new = results["old"], results["new"]
    if old["cpu"] > 0:
        print(f"\nпроцессор: {old['cpu']:.0f}% → {new['cpu']:.0f}% "
              f"({new['cpu'] / old['cpu'] * 100:.0f}% от прежнего)")
    print(f"память: {old['mem']} МБ → {new['mem']} МБ")
    return 0


sys.exit(asyncio.run(main()))

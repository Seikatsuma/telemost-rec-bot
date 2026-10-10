"""Провалы «в ноль» у бота на живом Телемосте — чистый опыт.

Говорящий подаёт речь в звонок напрямую (WebAudio → MediaStream, без фейкового микрофона и шумодава),
значит его звук без дыр. Бот пишет двумя способами: штатная смесь inject.js (WebAudio) и MediaRecorder прямо
с дорожки. Вариант задаётся аргументом: nice для процессов браузера бота (как у службы — 10) и камера бота.
Считаем «дыры цифровой тишины» (< -80 дБ дольше 60 мс) — их в живой речи не бывает.
Запуск: venv/bin/python -u tests/diag_holes_live.py --bot-nice 10 --camera 1
"""
import argparse
import asyncio
import base64
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

import config  # noqa: E402
import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--bot-nice", type=int, default=10)
ap.add_argument("--camera", type=int, default=1)
ap.add_argument("--seconds", type=int, default=60)
args = ap.parse_args()
OUT = pathlib.Path(f"/tmp/telemost-holes-n{args.bot_nice}-c{args.camera}")
OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"):
    f.unlink()

SPEECH = OUT / "speech.ogg"
subprocess.run(["nice", "-n", "19", "ff" + "mpeg", "-loglevel", "error", "-y", "-i", str(ROOT / "tests/fixtures/speech-ru.ogg"),
                "-t", "70", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k", str(SPEECH)], check=True)
SPEECH_B64 = base64.b64encode(SPEECH.read_bytes()).decode()

SENDER_JS = """
(() => {
  const b64 = "%s";
  const md = navigator.mediaDevices, orig = md.getUserMedia.bind(md);
  md.getUserMedia = async (c) => {
    const real = await orig(c);
    if (!c || !c.audio) return real;
    const ctx = new AudioContext({sampleRate: 48000});
    const bytes = Uint8Array.from(atob(b64), ch => ch.charCodeAt(0));
    const buf = await ctx.decodeAudioData(bytes.buffer);
    const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true;
    const dest = ctx.createMediaStreamDestination(); src.connect(dest); src.start();
    const out = new MediaStream([...dest.stream.getAudioTracks(), ...real.getVideoTracks()]);
    real.getAudioTracks().forEach(t => t.stop());
    return out;
  };
})();
""" % SPEECH_B64

DIRECT_JS = """
(() => {
  const Orig = window.RTCPeerConnection; window.__direct = [];
  function W(...a) { const pc = new Orig(...a);
    pc.addEventListener('track', ev => { if (ev.track.kind !== 'audio') return;
      const idx = window.__direct.length;
      const rec = new MediaRecorder(new MediaStream([ev.track]), {mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 64000});
      let chain = Promise.resolve();
      rec.ondataavailable = e => { if (!e.data.size) return; chain = chain.then(async () => { const b = new Uint8Array(await e.data.arrayBuffer()); let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); await window.__directChunk(idx, btoa(s)); }); };
      window.__direct.push(rec); });
    return pc; }
  W.prototype = Orig.prototype; Object.setPrototypeOf(W, Orig); window.RTCPeerConnection = W;
})();
"""


def holes(path) -> tuple[float, int, float, float]:
    raw = subprocess.run(["nice", "-n", "19", "ff" + "mpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    w = 160  # 10 мс
    n = len(x) // w
    if n < 100:
        return 0, 0, 0, 0
    db = 20 * np.log10(np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(1)) + 1e-9)
    total, cnt, longest, i = 0, 0, 0, 0
    first = next((k for k in range(n) if db[k] > -50), 0)
    last = n - next((k for k in range(n) if db[n - 1 - k] > -50), 0)
    i = first
    while i < last:
        if db[i] < -80:
            j = i
            while j < last and db[j] < -80:
                j += 1
            if j - i >= 6:
                total += j - i
                cnt += 1
                longest = max(longest, (j - i) * 10)
            i = j
        else:
            i += 1
    span = max(1, last - first)
    return total / span, cnt, longest, span / 100


def tree_pids(root):
    out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True).stdout.split("\n")
    kids = {}
    for line in out:
        if line.strip():
            p, pp = map(int, line.split())
            kids.setdefault(pp, []).append(p)
    res, stack = [], [root]
    while stack:
        p = stack.pop()
        res.append(p)
        stack += kids.get(p, [])
    return res


async def main():
    config.BOT_CAMERA = bool(args.camera)
    url = await telemost.YandexAccount().create_meeting()
    async with async_playwright() as p:
        bot_args = chromium_args(config.ROOT) + [f"--diag-bot-{time.time_ns()}"]
        bot_b = await p.chromium.launch(headless=True, args=bot_args, env=browser_env())
        marker = bot_args[-1]
        root = int(subprocess.run(["pgrep", "-o", "-f", "--", marker], capture_output=True, text=True).stdout.split()[0])
        if args.bot_nice:
            subprocess.run(["renice", "-n", str(args.bot_nice), "-p", *map(str, tree_pids(root))], capture_output=True)
        ctx = await bot_b.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 800, "height": 500})
        await ctx.add_init_script(DIRECT_JS)
        await ctx.add_init_script(path=str(config.ROOT / "inject.js"))

        def mix(source, part, b64):
            with open(OUT / "mix.webm", "ab") as f:
                f.write(base64.b64decode(b64))

        def direct(source, idx, b64):
            with open(OUT / f"direct{idx}.webm", "ab") as f:
                f.write(base64.b64decode(b64))
        await ctx.expose_binding("__recChunk", mix)
        await ctx.expose_binding("__directChunk", direct)
        bot = await ctx.new_page()
        print("бот:", await telemost.join(bot, url, config.BOT_NAME), flush=True)
        if args.bot_nice:  # renice новых процессов (рендерер страницы появился после запуска)
            subprocess.run(["renice", "-n", str(args.bot_nice), "-p", *map(str, tree_pids(root))], capture_output=True)

        sp_b = await p.chromium.launch(headless=True, args=[a for a in chromium_args(config.ROOT) if "fake-video" not in a], env=browser_env())
        sctx = await sp_b.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 640, "height": 400})
        await sctx.add_init_script("window.__recKeepMic = true;")
        await sctx.add_init_script(SENDER_JS)
        await sctx.add_init_script(path=str(config.ROOT / "inject.js"))
        sp = await sctx.new_page()
        print("говорящий:", await telemost.join(sp, url, "Тест Анна", mute=False), flush=True)
        await telemost.click_any(sp, [r"Включить микрофон"], timeout=1500)
        await asyncio.sleep(5)
        await bot.evaluate("__rec.start('mix')")
        await bot.evaluate("window.__direct.forEach(r => r.start(5000))")
        t0 = time.monotonic()
        loads = []
        while time.monotonic() - t0 < args.seconds:
            await asyncio.sleep(5)
            loads.append(float(open("/proc/loadavg").read().split()[0]))
        await bot.evaluate("__rec.stop()")
        await bot.evaluate("window.__direct.forEach(r => r.state !== 'inactive' && r.stop())")
        await asyncio.sleep(3)
        await sp_b.close()
        await bot_b.close()
    print(f"вариант: nice бота {args.bot_nice}, камера {args.camera}, нагрузка сервера {min(loads):.1f}–{max(loads):.1f}")
    for f in sorted(OUT.glob("*.webm")):
        r, c, longest, dur = holes(f)
        print(f"  {f.name}: {dur:.0f} с звука, дыр в ноль {c} шт, {r*100:.1f}% времени, самая длинная {longest} мс", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

"""Диагностика провалов звука на живом Телемосте: где теряется звук — в сети/у отправителя или при записи у бота.

Бот + один говорящий участник (tests/fixtures/speech-ru.ogg), 70 с речи.
У бота параллельно: (1) штатная запись inject.js (смесь через WebAudio), (2) MediaRecorder прямо на каждую
входящую дорожку, минуя WebAudio. Раз в 5 с — getStats входящего звука: concealedSamples (сколько звука
WebRTC «додумал» из-за потерь пакетов), packetsLost, jitter. Плюс загрузка CPU браузеров.
Итог: доля провалов в каждой записи против доли concealment — если провалы записи ≈ concealment, виновата
доставка; если провалов в записи больше — виноват способ записи.
Запуск: nice -n 10 venv/bin/python -u tests/diag_audio_live.py
"""
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
import selftest  # noqa: E402
import telemost  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402

OUT = pathlib.Path("/tmp/telemost-diag")
OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"):
    f.unlink()

DIRECT_JS = """
(() => {
  window.__direct = []; window.__pcs = [];
  const Orig = window.RTCPeerConnection;
  function W(...a) {
    const pc = new Orig(...a); window.__pcs.push(pc);
    pc.addEventListener('track', ev => {
      if (ev.track.kind !== 'audio') return;
      const idx = window.__direct.length;
      const rec = new MediaRecorder(new MediaStream([ev.track]), {mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 64000});
      let chain = Promise.resolve(); rec.ondataavailable = e => { if (!e.data.size) return; chain = chain.then(async () => { const b = new Uint8Array(await e.data.arrayBuffer()); let str = ''; for (let i = 0; i < b.length; i += 0x8000) str += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); await window.__directChunk(idx, btoa(str)); }); };
      window.__direct.push({rec, started: performance.now()});
    });
    return pc;
  }
  W.prototype = Orig.prototype; Object.setPrototypeOf(W, Orig); window.RTCPeerConnection = W;
  window.__stats = async () => {
    const r = {recv: 0, conc: 0, silentConc: 0, lost: 0, packets: 0, jitter: 0, frames: 0};
    for (const pc of window.__pcs) {
      (await pc.getStats()).forEach(s => {
        if (s.type === 'inbound-rtp' && s.kind === 'video') r.frames += s.framesDecoded || 0;
        if (s.type === 'inbound-rtp' && s.kind === 'audio') {
          r.recv += s.totalSamplesReceived || 0; r.conc += s.concealedSamples || 0;
          r.silentConc += s.silentConcealedSamples || 0; r.lost += s.packetsLost || 0;
          r.packets += s.packetsReceived || 0; r.jitter = Math.max(r.jitter, s.jitter || 0);
        }
      });
    }
    return r;
  };
})();
"""


SENDER_JS = """
(() => {
  const md = navigator.mediaDevices, orig = md.getUserMedia.bind(md);
  md.getUserMedia = async (c) => {
    const st = await orig(c);
    const t = st.getAudioTracks()[0];
    if (t && !window.__senderRec) {
      const rec = new MediaRecorder(new MediaStream([t.clone()]), {mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 64000});
      let chain = Promise.resolve();
      rec.ondataavailable = e => { if (!e.data.size) return; chain = chain.then(async () => { const b = new Uint8Array(await e.data.arrayBuffer()); let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); await window.__senderChunk(btoa(s)); }); };
      window.__senderRec = rec;
      window.__senderConstraints = JSON.stringify(c);
    }
    return st;
  };
})();
"""


def load(p):
    raw = subprocess.run(["nice", "-n", "19", "ff" + "mpeg", "-loglevel", "error", "-i", str(p), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def dropouts(p) -> tuple[float, int, float]:
    """Доля времени в коротких провалах (< -55 дБ, 50–600 мс, посреди звука), их число, длительность записи."""
    x = load(p)
    w = 800
    n = len(x) // w
    if n < 20:
        return 0, 0, 0
    db = 20 * np.log10(np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(1)) + 1e-9)
    total, cnt, i = 0, 0, 0
    while i < n:
        if db[i] < -55:
            j = i
            while j < n and db[j] < -55:
                j += 1
            L = j - i
            if 1 <= L <= 12 and i >= 6 and j + 6 < n and db[i - 6:i].max() > -35 and db[j:j + 6].max() > -35:
                total += L
                cnt += 1
            i = j
        else:
            i += 1
    return total / n, cnt, n * 0.05


def cpu_of(pids):
    out = subprocess.run(["ps", "-o", "pid=,ppid=,pcpu=,comm=", "-A"], capture_output=True, text=True).stdout
    return out


async def main():
    ref_drop = dropouts(selftest._speech_wav())
    print(f"исходная речь: провалы {ref_drop[0]*100:.1f}% времени, {ref_drop[1]} шт. за {ref_drop[2]:.0f} с", flush=True)
    url = await telemost.YandexAccount().create_meeting()
    print("встреча", url, flush=True)
    async with async_playwright() as p:
        bot_b = await p.chromium.launch(headless=True, args=chromium_args(config.ROOT), env=browser_env())
        ctx = await bot_b.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 800, "height": 500})
        await ctx.add_init_script(DIRECT_JS)
        await ctx.add_init_script(path=str(config.ROOT / "inject.js"))

        def mix_chunk(source, part, b64):
            with open(OUT / "mix.webm", "ab") as f:
                f.write(base64.b64decode(b64))

        def direct_chunk(source, idx, b64):
            with open(OUT / f"direct{idx}.webm", "ab") as f:
                f.write(base64.b64decode(b64))
        await ctx.expose_binding("__recChunk", mix_chunk)
        await ctx.expose_binding("__directChunk", direct_chunk)
        bot = await ctx.new_page()
        print("бот:", await telemost.join(bot, url, config.BOT_NAME), flush=True)

        sp_args = [a for a in chromium_args(config.ROOT) if not a.startswith("--use-file-for-fake-audio-capture")]
        sp_args.append(f"--use-file-for-fake-audio-capture={selftest._speech_wav()}")
        sp_b = await p.chromium.launch(headless=True, args=sp_args, env=browser_env())
        sctx = await sp_b.new_context(locale="ru-RU", user_agent=UA, viewport={"width": 640, "height": 400})
        await sctx.add_init_script("window.__recKeepMic = true;")
        await sctx.add_init_script(SENDER_JS)
        await sctx.add_init_script(path=str(config.ROOT / "inject.js"))

        def sender_chunk(source, b64):
            with open(OUT / "sender_raw.webm", "ab") as f:
                f.write(base64.b64decode(b64))
        await sctx.expose_binding("__senderChunk", sender_chunk)
        sp = await sctx.new_page()
        print("говорящий:", await telemost.join(sp, url, "Тест Анна", mute=False), flush=True)
        await telemost.click_any(sp, selftest.MIC_ON, timeout=1500)
        if "--speaker-camera-off" in sys.argv:
            cam = sp.locator('button[title="Выключить камеру"], button[aria-label="Выключить камеру"]').first
            if await cam.is_visible():
                await cam.evaluate("b => b.click()")
            print("  камера говорящего выключена", flush=True)
        await asyncio.sleep(3)

        await bot.evaluate("__rec.start('diag')")
        await bot.evaluate("window.__direct.forEach(d => d.rec.start(5000))")
        await sp.evaluate("window.__senderRec && window.__senderRec.start(5000)")
        s0 = await bot.evaluate("__stats()")
        print("  микрофон говорящего запрошен с:", await sp.evaluate("window.__senderConstraints || 'нет'"), flush=True)
        t0 = time.monotonic()
        cpu_samples = []
        while time.monotonic() - t0 < 70:
            await asyncio.sleep(5)
            s = await bot.evaluate("__stats()")
            ps = subprocess.run(["bash", "-c", "ps -eo pcpu,args | grep -i 'chrom' | grep -v grep | awk '{s+=$1} END {print s}'; cat /proc/loadavg"],
                                capture_output=True, text=True).stdout.split()
            cpu_samples.append(float(ps[0] or 0))
            d_recv = s["recv"] - s0["recv"]
            print(f"  {time.monotonic()-t0:4.0f}с: дорожек {await bot.evaluate('window.__direct.length')}, додумано WebRTC "
                  f"{100*(s['conc']-s0['conc'])/max(1,d_recv):.1f}% (из них тишина {100*(s['silentConc']-s0['silentConc'])/max(1,d_recv):.1f}%), "
                  f"потеряно пакетов {s['lost']-s0['lost']} из {s['packets']-s0['packets']}, jitter {s['jitter']*1000:.0f} мс, "
                  f"видеокадров декодировано {s['frames']-s0['frames']}, CPU браузеров {ps[0]}%, load {ps[1]}", flush=True)
        await bot.evaluate("__rec.stop()")
        await bot.evaluate("window.__direct.forEach(d => d.rec.state !== 'inactive' && d.rec.stop())")
        await sp.evaluate("window.__senderRec && window.__senderRec.state !== 'inactive' && window.__senderRec.stop()")
        await asyncio.sleep(3)
        await sp_b.close()
        await bot_b.close()

    print("\nИТОГ по записям (провалы посреди звука):")
    for f in sorted(OUT.glob("*.webm")):
        r, c, dur = dropouts(f)
        print(f"  {f.name}: {dur:.0f} с, провалы {r*100:.1f}% времени, {c} шт.")
    print(f"  исходная речь: {ref_drop[0]*100:.1f}% , {ref_drop[1]} шт. за {ref_drop[2]:.0f} с (для сравнения — естественные паузы)")


asyncio.run(main())

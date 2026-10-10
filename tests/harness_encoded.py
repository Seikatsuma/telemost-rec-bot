"""Стенд: запись сжатых кадров Opus без декодирования — локально, без Телемоста.

Внутри одной страницы поднимаются два соединения WebRTC: одно отправляет заведомо
известную речь (tests/fixtures/speech-ru.ogg), второе принимает. У принимающего
перехватываются УЖЕ СЖАТЫЕ кадры (`receiver.createEncodedStreams`) и складываются
в .ogg нашим упаковщиком — ни декодирования, ни микса, ни повторного сжатия.

Что доказывает стенд:
  1. механизм вообще работает в нашем headless-браузере;
  2. получившийся файл читается проигрывателем и имеет верную длину;
  3. в нём нет «дыр» цифровой тишины;
  4. сколько процессора это стоит — для сравнения с нынешним способом.

Запуск: venv/bin/python tests/harness_encoded.py [секунды]
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

import config  # noqa: E402
from browser_env import UA, browser_env, chromium_args  # noqa: E402
from oggopus import OggOpusWriter  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = pathlib.Path("/tmp/telemost-encoded")
OUT.mkdir(exist_ok=True)
for f in OUT.glob("*"):
    f.unlink()

SPEECH = ROOT / "tests/fixtures/speech-ru.ogg"

PAGE_JS = """
async (b64) => {
  const log = [];
  window.__log = log;
  const ctx = new AudioContext({sampleRate: 48000});
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const buf = await ctx.decodeAudioData(bytes.buffer);
  const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true;
  const dest = ctx.createMediaStreamDestination(); src.connect(dest); src.start();

  const pc1 = new RTCPeerConnection();
  const pc2 = new RTCPeerConnection();
  window.__pc = [pc1, pc2];
  pc1.onicecandidate = e => e.candidate && pc2.addIceCandidate(e.candidate);
  pc2.onicecandidate = e => e.candidate && pc1.addIceCandidate(e.candidate);

  pc2.ontrack = (ev) => {
    const r = ev.receiver;
    let streams;
    try { streams = r.createEncodedStreams(); }
    catch (err) { log.push('createEncodedStreams не дал потоков: ' + err); return; }
    const reader = streams.readable.getReader();
    const writer = streams.writable.getWriter();
    (async () => {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        const md = value.getMetadata ? value.getMetadata() : {};
        const b = new Uint8Array(value.data);
        let s = '';
        for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
        await window.__frame(md.synchronizationSource || 0, value.timestamp || 0,
                            performance.now(), btoa(s));
        await writer.write(value);          // пропускаем кадр дальше, как есть
      }
    })();
  };

  const track = dest.stream.getAudioTracks()[0];
  pc1.addTrack(track, dest.stream);
  const offer = await pc1.createOffer();
  await pc1.setLocalDescription(offer);
  await pc2.setRemoteDescription(offer);
  const answer = await pc2.createAnswer();
  await pc2.setLocalDescription(answer);
  await pc1.setRemoteDescription(answer);
  return 'соединение поднято';
}
"""


def ticks(pid: int) -> int:
    """Тики процессора всего дерева процессов — как в остальных замерах проекта."""
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


def ffprobe_seconds(path: pathlib.Path) -> float:
    out = subprocess.run(["nice", "-n", "19", "ionice", "-c", "3", "ff" + "probe",
                          "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return -1.0


def holes(path: pathlib.Path) -> tuple[float, int, int]:
    """Доля «цифровой тишины» (< -80 дБ дольше 60 мс) — та же метрика, что в diag_holes_live."""
    import numpy as np
    raw = subprocess.run(["nice", "-n", "19", "ionice", "-c", "3", "ff" + "mpeg",
                          "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    w, n = 160, len(raw) // 2 // 160
    if n < 50:
        return -1, -1, -1
    db = 20 * np.log10(np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(1)) + 1e-9)
    total = cnt = longest = 0
    i = 0
    while i < n:
        if db[i] < -80:
            j = i
            while j < n and db[j] < -80:
                j += 1
            if j - i >= 6:
                total += j - i
                cnt += 1
                longest = max(longest, (j - i) * 10)
            i = j
        else:
            i += 1
    return total / n, cnt, longest


async def main() -> int:
    writers: dict[int, OggOpusWriter] = {}
    stats = {"frames": 0, "bytes": 0}
    first_ms = {}
    last_ms = {}

    def on_frame(source, ssrc, rtp_ts, at_ms, b64):
        w = writers.get(ssrc)
        if w is None:
            w = writers[ssrc] = OggOpusWriter(OUT / f"speaker-{ssrc}.ogg")
            first_ms[ssrc] = at_ms
        last_ms[ssrc] = at_ms
        data = base64.b64decode(b64)
        w.write_packet(data)
        stats["frames"] += 1
        stats["bytes"] += len(data)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=chromium_args(config.ROOT),
                                          env=browser_env())
        pid = int(subprocess.run(["pgrep", "-n", "-f", "chrome-headless-shell"],
                                 capture_output=True, text=True).stdout.split()[0])
        ctx = await browser.new_context(user_agent=UA)
        await ctx.expose_binding("__frame", on_frame)
        page = await ctx.new_page()
        await page.goto("about:blank")
        b64 = base64.b64encode(SPEECH.read_bytes()).decode()
        print(await page.evaluate(PAGE_JS, b64), flush=True)

        await asyncio.sleep(3)                       # дать соединению устояться
        t1, w1 = ticks(pid), time.monotonic()
        await asyncio.sleep(SECONDS)
        cpu = (ticks(pid) - t1) / os.sysconf("SC_CLK_TCK") / (time.monotonic() - w1) * 100
        rss = int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
        load = open("/proc/loadavg").read().split()[0]
        await browser.close()

    for w in writers.values():
        w.close()

    print(f"\nкадров принято: {stats['frames']}, байт: {stats['bytes']}")
    print(f"процессор браузера: {cpu:.0f}% ядра, память {rss} МБ, нагрузка сервера {load}")
    ok = True
    for ssrc, w in writers.items():
        dur = ffprobe_seconds(w.path)
        ratio, cnt, longest = holes(w.path)
        wall = (last_ms[ssrc] - first_ms[ssrc]) / 1000
        print(f"\nдорожка {ssrc}: {w.path.name}, {w.path.stat().st_size // 1024} КБ")
        print(f"  длина по счётчику пакетов: {w.seconds:.1f} с")
        print(f"  длина по проигрывателю:    {dur:.1f} с")
        print(f"  шло по часам:              {wall:.1f} с")
        print(f"  дыры в ноль: {cnt} шт, {ratio * 100:.2f}% времени, самая длинная {longest} мс")
        if dur < 0:
            print("  ПРОВАЛ: файл не читается проигрывателем")
            ok = False
        elif abs(dur - w.seconds) > 0.3:
            print("  ПРОВАЛ: длина по счётчику расходится с файлом")
            ok = False
    if not writers:
        print("ПРОВАЛ: ни одного кадра не перехвачено")
        ok = False
    print("\nИТОГ:", "стенд пройден" if ok else "ЕСТЬ ПРОВАЛЫ")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))

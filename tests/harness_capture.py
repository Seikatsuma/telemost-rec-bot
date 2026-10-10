"""Стенд без Телемоста: проверяет inject.js в том же Chromium, что и бот.

Локальная страница поднимает два RTCPeerConnection: «участник» шлёт тон 440 Гц,
«бот» принимает (его PC перехвачен inject.js). Поддельный сигнальный сервер шлёт
сообщения Телемоста о входе и выходе. Проверяем: счёт людей, запись, паузу, стоп,
что файл декодируется ffmpeg и в нём есть звук нужной длины.

Запуск: venv/bin/python tests/harness_capture.py
"""
import asyncio
import base64
import json
import pathlib
import subprocess
import sys

from aiohttp import web
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from browser_env import chromium_args, browser_env  # noqa: E402

OUT = pathlib.Path("/tmp/telemost-harness")

PAGE = """<!doctype html><html><body>harness<script>
window.runCall = async () => {
  const ws = new WebSocket('ws://127.0.0.1:' + location.port + '/ws');
  await new Promise(r => ws.onopen = r);
  ws.send(JSON.stringify({uid: '1', hello: {participantId: 'bot-self'}}));
  const actx = new AudioContext();
  const osc = actx.createOscillator(); osc.frequency.value = 440;
  const gain = actx.createGain(); gain.gain.value = 0.5;
  const d = actx.createMediaStreamDestination(); osc.connect(gain).connect(d); osc.start();
  const a = new RTCPeerConnection(), b = new RTCPeerConnection();
  a.onicecandidate = e => e.candidate && b.addIceCandidate(e.candidate);
  b.onicecandidate = e => e.candidate && a.addIceCandidate(e.candidate);
  d.stream.getTracks().forEach(t => a.addTrack(t, d.stream));
  await a.setLocalDescription(await a.createOffer()); await b.setRemoteDescription(a.localDescription);
  await b.setLocalDescription(await b.createAnswer()); await a.setRemoteDescription(b.localDescription);
  window.__ws = ws;
  return 'ok';
};
</script></body></html>"""


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    sockets = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text=PAGE, content_type="text/html"))
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    fails = []

    def check(name, ok, detail=""):
        print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    async def send(msg):
        await sockets[0].send_str(json.dumps(msg))
        await asyncio.sleep(0.3)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=chromium_args(ROOT), env=browser_env())
        ctx = await browser.new_context()
        await ctx.add_init_script(path=str(ROOT / "inject.js"))

        async def on_chunk(source, part, b64):
            with open(OUT / f"{part}.webm", "ab") as f:
                f.write(base64.b64decode(b64))

        await ctx.expose_binding("__recChunk", on_chunk)
        page = await ctx.new_page()
        await page.goto(f"http://127.0.0.1:{port}/")
        await page.evaluate("runCall()")
        await asyncio.sleep(1.5)

        snap = await page.evaluate("__rec.snapshot('Запись встречи')")
        check("свой id из hello", snap["selfId"] == "bot-self", str(snap["selfId"]))
        check("перехвачена входящая дорожка", snap["tracks"] >= 1, str(snap["tracks"]))

        await send({"uid": "2", "updateDescription": {"description": [
            {"id": "bot-self", "meta": {"name": "Запись встречи"}},
            {"id": "u1", "meta": {"name": "Иван"}}]}})
        snap = await page.evaluate("__rec.snapshot('Запись встречи')")
        check("люди без бота = 1", snap["humans"] == ["Иван"], str(snap["humans"]))

        await send({"uid": "3", "upsertDescription": {"description": [{"id": "u2", "meta": {"name": "Мария"}}]}})
        snap = await page.evaluate("__rec.snapshot('Запись встречи')")
        check("вошёл второй = 2", len(snap["humans"]) == 2, str(snap["humans"]))

        await page.evaluate("__rec.start('p1')")
        await asyncio.sleep(6)
        snap = await page.evaluate("__rec.snapshot('Запись встречи')")
        check("звук слышен (уровень > 0.1)", snap["level"] > 0.1, f"{snap['level']:.2f}")

        await page.evaluate("__rec.pause()")
        await asyncio.sleep(4)  # пауза не должна попасть в файл
        await page.evaluate("__rec.resume()")
        await asyncio.sleep(4)
        res = await page.evaluate("__rec.stop()")
        check("стоп дождался последнего куска", res == "stopped", res)

        await send({"uid": "4", "removeDescription": {"descriptionId": ["u1", "u2"]}})
        snap = await page.evaluate("__rec.snapshot('Запись встречи')")
        check("все вышли = 0", snap["humans"] == [], str(snap["humans"]))
        check("ошибок в странице нет", not snap["errors"], str(snap["errors"]))
        await browser.close()
    await runner.cleanup()

    f = OUT / "p1.webm"
    check("файл записи есть", f.exists() and f.stat().st_size > 10000, str(f.stat().st_size if f.exists() else 0))
    if f.exists():
        dur = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(f), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        import re
        t = re.findall(r"time=(\d+):(\d+):([\d.]+)", dur)
        secs = int(t[-1][0]) * 3600 + int(t[-1][1]) * 60 + float(t[-1][2]) if t else 0
        mean = re.search(r"mean_volume: ([-\d.]+) dB", dur)
        check("длина ≈ 10 с без паузы (8–12)", 8 <= secs <= 12, f"{secs:.1f} с")
        check("в файле звук, не тишина (> -40 дБ)", mean and float(mean.group(1)) > -40, mean.group(1) if mean else "?")
    print("ИТОГ:", "всё прошло" if not fails else f"провалено {len(fails)}: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Сквозная проверка логики Meeting на поддельном Телемосте (настоящий нужен вход в Яндекс).

Поддельная страница повторяет то, на что опирается бот: поле имени, кнопку «Подключиться»,
сигнальный WebSocket с hello/updateDescription/upsertDescription/removeDescription
и входящую WebRTC-дорожку со звуком. Сценарий:
 1) бот зашёл, людей нет → записи нет;
 2) вошёл человек → запись; все вышли на 4 с (< пауза) и вернулись → та же запись;
 3) все вышли дольше паузы → запись закрыта одним файлом нужной длины, одно сообщение «встреча закончилась»,
    бот остаётся в звонке и сторожит ссылку;
 4) ссылку прислали снова → бот зашёл, люди есть → новая запись; бот зашёл с выключенными камерой и микрофоном;
 5) сервер рвёт соединение посреди записи → бот заходит заново, запись продолжается вторым куском;
 6) стоп → последняя запись закрыта.
Запуск: venv/bin/python tests/e2e_fake_telemost.py
"""
import asyncio
import json
import pathlib
import shutil
import sys

from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

TMP = pathlib.Path("/tmp/telemost-e2e")
shutil.rmtree(TMP, ignore_errors=True)
config.REC_DIR = TMP / "rec"
config.REC_DIR.mkdir(parents=True)
config.GRACE_SEC = 8

import meeting as meeting_mod  # noqa: E402
import telemost  # noqa: E402
import postprocess  # noqa: E402
meeting_mod.DROP_SEC = 10

PAGE = """<!doctype html><html><body>
<div id="pre"><input placeholder="Ваше имя"><button id="go">Подключиться</button></div>
<div id="pre2"><button title="Выключить микрофон" onclick="this.title='Включить микрофон'">м</button><button title="Включить камеру" onclick="this.title='Выключить камеру'">к</button></div>
<div id="in" style="display:none">Встреча идёт <button title="Выйти из встречи" onclick="window.__left=1; fetch('/left')">выйти</button></div>
<script>
document.getElementById('go').onclick = async () => {
  const name = document.querySelector('input').value;
  if (!name) return;
  document.getElementById('pre').style.display = 'none';
  document.getElementById('in').style.display = '';
  const ws = new WebSocket('ws://' + location.host + '/ws');
  await new Promise(r => ws.onopen = r);
  ws.send(JSON.stringify({uid: 'h', hello: {participantId: 'self-' + Math.random()}}));
  const actx = new AudioContext();
  const osc = actx.createOscillator(); osc.frequency.value = 330;
  const g = actx.createGain(); g.gain.value = 0;
  const d = actx.createMediaStreamDestination(); osc.connect(g).connect(d); osc.start();
  const present = new Set();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.updateDescription) { present.clear(); m.updateDescription.description.forEach(x => present.add(x.id)); }
    if (m.upsertDescription) m.upsertDescription.description.forEach(x => present.add(x.id));
    if (m.removeDescription) m.removeDescription.descriptionId.forEach(x => present.delete(x));
    g.gain.value = present.size ? 0.5 : 0;  // звук есть, только пока во встрече люди
  });
  const a = new RTCPeerConnection(), b = new RTCPeerConnection();
  a.onicecandidate = e => e.candidate && b.addIceCandidate(e.candidate);
  b.onicecandidate = e => e.candidate && a.addIceCandidate(e.candidate);
  d.stream.getTracks().forEach(t => a.addTrack(t, d.stream));
  await a.setLocalDescription(await a.createOffer()); await b.setRemoteDescription(a.localDescription);
  await b.setLocalDescription(await b.createAnswer()); await a.setRemoteDescription(b.localDescription);
};
</script></body></html>"""

# Оболочка мессенджера, как в настоящем Телемосте с 21.09.26: сам звонок — во вложенном кадре
# /private-join/<id>, поверх висит накладка Orb-Overlay2 (она съедает обычный клик Playwright),
# рядом чужие кнопки, на которые бот нажать НЕ должен. Так проверка идёт по тому же пути, что и бой.
SHELL = """<!doctype html><html><body style="margin:0">
<div class="Orb-Overlay2" style="position:fixed;inset:0;z-index:9"></div>
<button aria-label="Завершить звонок">з</button><button>Войти</button>
<div>1 участник</div>
<iframe src="/private-join/{id}" style="width:100%;height:620px;border:0"></iframe>
</body></html>"""

sockets: list[web.WebSocketResponse] = []
people: dict[str, str] = {}
joins = 0
lefts = 0


async def ws_handler(request):
    global joins
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    joins += 1
    sockets.append(ws)
    await ws.send_str(json.dumps({"uid": "u", "updateDescription": {"description": [
        {"id": k, "meta": {"name": v}} for k, v in people.items()]}}))
    async for _ in ws:
        pass
    return ws


async def broadcast(msg):
    for ws in list(sockets):
        if not ws.closed:
            await ws.send_str(json.dumps(msg))


async def enter(pid, name):
    people[pid] = name
    await broadcast({"uid": "x", "upsertDescription": {"description": [{"id": pid, "meta": {"name": name}}]}})


async def leave(pid):
    people.pop(pid, None)
    await broadcast({"uid": "x", "removeDescription": {"descriptionId": [pid]}})


async def speaking(pid, on: bool):
    """Раздача слотов: признак речевой активности участника — то, что бот пишет в журнал."""
    await broadcast({"uid": "x", "slotsConfig": {"slots": [{"participant": {"participantId": pid}, "vad": on}]}})


def voiced(path) -> float:
    """Секунды со звуком: длина минус тишина (пауза не должна попадать в файл, хвост опроса — тишина)."""
    import re, subprocess
    err = subprocess.run(["nice", "-n", "19", "ionice", "-c", "3", "ff" + "mpeg", "-hide_banner", "-i", str(path), "-af",
                          "silencedetect=n=-40dB:d=0.5", "-f", "null", "-"], capture_output=True, text=True).stderr
    silence = sum(float(x) for x in re.findall(r"silence_duration: ([\d.]+)", err))
    return postprocess.duration(path) - silence


async def main() -> int:
    app = web.Application()
    app.router.add_get("/j/{id}", lambda r: web.Response(
        text=SHELL.replace("{id}", r.match_info["id"]), content_type="text/html"))
    app.router.add_get("/private-join/{id}", lambda r: web.Response(text=PAGE, content_type="text/html"))
    app.router.add_get("/ws", ws_handler)

    async def left(request):
        global lefts
        lefts += 1
        return web.Response(text="ok")
    app.router.add_get("/left", left)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    notes: list[str] = []
    closed: list[pathlib.Path] = []
    fails = []

    def check(name, ok, detail=""):
        print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
        if not ok:
            fails.append(name)

    async def notify(chat, text):
        notes.append(text)
        print("   [в Telegram]", text.splitlines()[0], flush=True)

    async def finalize(seg_dir):
        audio = await asyncio.to_thread(postprocess.build_audio, seg_dir)
        closed.append(audio)

    m = meeting_mod.Meeting(f"http://127.0.0.1:{port}/j/5550001", 1, notify, finalize)
    task = asyncio.create_task(m.run())

    async def wait_for(cond, sec):
        for _ in range(int(sec * 4)):
            if cond():
                return True
            await asyncio.sleep(0.25)
        return cond()

    check("1. бот зашёл", await wait_for(lambda: m.phase.startswith("во встрече"), 60), m.phase)
    await asyncio.sleep(3)
    check("1. без людей записи нет", m.segment is None, m.phase)
    # Владелец 18.09: о входе бота не сообщать — «встреча создана, можно заходить» уже сказано в карточке.
    check("1. о входе бота в Telegram ничего не пришло", not notes, str(notes))

    await enter("p1", "Иван")
    check("2. вошёл человек → запись", await wait_for(lambda: m.segment is not None and m.phase == "идёт запись", 10), m.phase)
    # Раздача слотов: Иван «говорит» ~2.5 с — должно попасть в журнал записи (timeline.jsonl).
    await asyncio.sleep(2)
    await speaking("p1", True)
    await asyncio.sleep(2.5)
    await speaking("p1", False)
    await asyncio.sleep(3.5)
    await leave("p1")
    check("2. все вышли → пауза", await wait_for(lambda: "пауз" in m.phase, 6), m.phase)
    await asyncio.sleep(4)
    await enter("p1", "Иван")
    check("2. вернулся в паузу → та же запись", await wait_for(lambda: m.phase == "идёт запись", 6) and not closed, f"{m.phase}, закрыто {len(closed)}")
    await asyncio.sleep(6)
    await leave("p1")
    check("3. пауза истекла → запись закрыта", await wait_for(lambda: len(closed) == 1, 25), f"закрыто {len(closed)}")
    if closed and closed[0]:
        d = voiced(closed[0])
        check("3. звучащих секунд ≈ 14, пауза не записана (12–18: опрос раз в 2 с на входе и выходе)", 12 <= d <= 18, f"{d:.1f} с")
    else:
        check("3. файл собран", False)
    # Журнал «кто говорил» — в папке первой записи: сырые события + свёрнутый speakers.json.
    seg_dirs = sorted(p for p in config.REC_DIR.iterdir() if p.is_dir())
    one_dir = len(seg_dirs) == 1
    check("3. папка записи есть", one_dir, str(seg_dirs))
    if one_dir:
        seg_dir = seg_dirs[0]
        tl = seg_dir / "timeline.jsonl"
        evs = [json.loads(x) for x in tl.read_text(encoding="utf-8").splitlines()] if tl.exists() else []
        check("3. журнал событий: vad Ивана + rec + вошёл",
              any(e.get("kind") == "vad" and e.get("name") == "Иван" and e.get("on") for e in evs)
              and any(e.get("kind") == "rec" and e.get("op") == "start" for e in evs)
              and any(e.get("kind") == "join" and e.get("name") == "Иван" for e in evs),
              f"{len(evs)} событий")
        sp = seg_dir / "speakers.json"
        seg_named = [s for s in (json.loads(sp.read_text(encoding="utf-8")).get("segments") if sp.exists() else [])
                     if s["name"] == "Иван" and s["audio_start_s"] is not None]
        check("3. speakers.json: реплика Ивана с меткой по звуку", bool(seg_named), sp.exists() and sp.read_text(encoding="utf-8")[:200] or "нет файла")
    # Сторож: после записи бот остаётся в звонке и ждёт возвращения людей (владелец 17.09).
    check("3. бот остался в звонке и сторожит ссылку", not task.done() and lefts == 0, f"вышел={task.done()}, нажатий={lefts}")
    ended = [n for n in notes if "закончилась" in n]
    check("3. одно сообщение «встреча закончилась»", len(ended) == 1, f"{len(ended)}: {notes}")
    if ended:
        e = ended[0]
        check("3. в нём что дальше: запись с расшифровкой и что можно вернуться",
              "пришлю" in e and "расшифровк" in e and "запишу снова" in e, e)

    await enter("p2", "Мария")
    check("4. вернулись по той же ссылке → новая запись", await wait_for(lambda: m.segment is not None and m.phase == "идёт запись", 10), m.phase)
    await asyncio.sleep(3)
    # Кнопки панели — в кадре звонка, как в настоящем Телемосте: смотрим туда же, куда и бот.
    titles = await (await telemost.call_frame(m.page)).evaluate(
        "() => [...document.querySelectorAll('#pre2 button')].map(b => b.title)")
    check("4. микрофон бота выключен, камера с картинкой включена", titles == ["Включить микрофон", "Выключить камеру"], str(titles))
    await asyncio.sleep(6)
    joins_before = joins
    for ws in list(sockets):
        await ws.close()
    sockets.clear()
    check("5. бот перезашёл после обрыва", await wait_for(lambda: joins > joins_before, meeting_mod.DROP_SEC + 40), f"заходов {joins}")
    check("5. запись продолжается", await wait_for(lambda: m.phase == "идёт запись" and m.segment and m.segment.parts >= 2, 20),
          f"{m.phase}, кусков {m.segment.parts if m.segment else 0}")
    await asyncio.sleep(6)
    m.request_stop()
    await asyncio.wait_for(task, 60)
    check("6. стоп → вторая запись закрыта", await wait_for(lambda: len(closed) == 2, 20), f"закрыто {len(closed)}")
    if len(closed) == 2 and closed[1]:
        d = postprocess.duration(closed[1])
        check("6. вторая запись из двух кусков, звук есть (≥ 10 с)", d >= 10, f"{d:.1f} с")
    # 7. Ссылка, на которую никто не пришёл за срок сторожа, — бот уходит и говорит об этом.
    # Поддельный Телемост один на все встречи: участница из шага 4 должна выйти, иначе бот увидит её
    # в новой встрече и начнёт писать (так тест и завис 18.09).
    await leave("p2")
    notes.clear()
    saved_idle = config.IDLE_LEAVE_SEC
    # Окно считается от создания встречи (21.09) — запас на запуск браузера под нагрузкой: 24.09 при
    # окне 8 с бот вошёл уже после его конца и ушёл, не дождавшись участника.
    config.IDLE_LEAVE_SEC = 25
    m = meeting_mod.Meeting(f"http://127.0.0.1:{port}/j/5550002", 1, notify, finalize)
    task = asyncio.create_task(m.run())
    check("7. никто не пришёл → бот ушёл сам", await wait_for(lambda: task.done(), 60), m.phase)
    check("7. и сказал, что никто не зашёл и нужна новая встреча",
          any("никто не зашёл" in n and "новую" in n for n in notes), str(notes))

    # 8. Встреча состоялась, потом срок сторожа истёк — уход молчаливый: «закончилась» уже сказано.
    notes.clear()
    m = meeting_mod.Meeting(f"http://127.0.0.1:{port}/j/5550003", 1, notify, finalize)
    task = asyncio.create_task(m.run())
    await wait_for(lambda: m.phase.startswith("во встрече"), 60)
    await enter("p3", "Олег")
    await wait_for(lambda: m.phase == "идёт запись", 10)
    await asyncio.sleep(3)
    await leave("p3")
    check("8. встреча закончилась → бот ушёл после срока сторожа", await wait_for(lambda: task.done(), 60), m.phase)
    check("8. лишнего «никто не зашёл» после состоявшейся встречи нет",
          not any("никто не зашёл" in n for n in notes), str(notes))
    config.IDLE_LEAVE_SEC = saved_idle

    # 9. Встреча длиннее потолка одной записи (боевой — 4 ч) — запись режется на части подряд,
    #    человек никуда не уходит, «закончилась» не приходит, звук не теряется.
    notes.clear()
    closed_before = len(closed)
    saved_max = config.MAX_SEGMENT_SEC
    config.MAX_SEGMENT_SEC = 10
    m = meeting_mod.Meeting(f"http://127.0.0.1:{port}/j/5550004", 1, notify, finalize)
    task = asyncio.create_task(m.run())
    await wait_for(lambda: m.phase.startswith("во встрече"), 60)
    await enter("p4", "Долгий")
    check("9. человек в звонке без перерыва → запись порезана на части по потолку",
          await wait_for(lambda: len(closed) >= closed_before + 2, 90), f"частей {len(closed) - closed_before}")
    check("9. запись после разреза продолжается", await wait_for(lambda: m.phase == "идёт запись", 10), m.phase)
    check("9. «встреча закончилась» при разрезе не приходило", not any("закончилась" in n for n in notes), str(notes))
    pieces = [c for c in closed[closed_before:] if c]
    check("9. в каждой части есть звук (≥ 8 с)", pieces and all(postprocess.duration(c) >= 8 for c in pieces),
          str([round(postprocess.duration(c)) for c in pieces]))
    await leave("p4")
    config.MAX_SEGMENT_SEC = saved_max

    # Недоделанные встречи гасим до закрытия сервера — иначе cleanup ждёт их соединений вечно.
    if not task.done():
        m.request_stop()
        try:
            await asyncio.wait_for(task, 60)
        except Exception:
            pass

    # Регрессия 08.10: финальный кусок звука терялся (R.part обнулялся до recorder.stop) —
    # у каждой закрытой записи должен лежать ≥1 webm, у совсем короткой — единственный кусок.
    # Замер только после остановки: у ещё идущей записи первый кусок может просто не дойти.
    no_audio = [d.name for d in config.REC_DIR.iterdir() if d.is_dir() and not list(d.glob("part*.webm"))]
    check("10. финальный кусок не потерян: во всех записях есть webm", not no_audio, str(no_audio))

    await runner.cleanup()
    print("ИТОГ:", "всё прошло" if not fails else f"провалено {len(fails)}: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))

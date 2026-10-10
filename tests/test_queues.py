"""Аудио не ждёт расшифровку чужой записи; расшифровка переживает перезапуск бота.

Тяжёлые шаги подменены: сборка аудио и расшифровка — заглушки, Groq и Telegram не зовутся.
Запуск: venv/bin/python tests/test_queues.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import postprocess as P  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    fails += (not ok)


def make_seg(root, name, **extra):
    d = root / name
    d.mkdir()
    meta = {"url": "x", "chat_id": 1, "started": time.time(), "label": name, "names": []} | extra
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "audio.m4a").write_bytes(b"x")
    return d


async def main():
    root = pathlib.Path(tempfile.mkdtemp())
    config.GROQ_API_KEY = config.GROQ_API_KEY or "test"
    events: list[tuple[float, str]] = []
    t0 = time.monotonic()
    slow = {"первая"}

    P.build_audio = lambda d: d / "audio.m4a"
    P.duration = lambda a: 600.0
    P.split_for_telegram = lambda a: [a]
    P.summarize = lambda text: ""

    def fake_transcribe(audio):
        time.sleep(3 if audio.parent.name in slow else 0.1)   # «Groq держит» первую запись
        return "текст"
    P.transcribe = fake_transcribe

    async def send_audio(chat, f, title, caption):
        events.append((time.monotonic() - t0, f"аудио {f.parent.name}"))

    async def send_doc(chat, f, caption):
        events.append((time.monotonic() - t0, f"расшифровка {f.parent.name}"))

    async def send_text(chat, text):
        events.append((time.monotonic() - t0, f"текст: {text}"))

    a, b = make_seg(root, "первая"), make_seg(root, "вторая")
    first = asyncio.create_task(P.process_segment(a, send_audio, send_text, send_doc, lambda c: True))
    await asyncio.sleep(0.3)
    second = asyncio.create_task(P.process_segment(b, send_audio, send_text, send_doc, lambda c: True))
    await asyncio.gather(first, second)

    when = {name: t for t, name in events}
    check("аудио второй записи ушло, пока расшифровка первой ещё ждёт",
          when.get("аудио вторая", 99) < when.get("расшифровка первая", 0),
          " → ".join(f"{n} {t:.1f}с" for t, n in events))
    check("аудио второй не ждало 3 с «занятого Groq»", when.get("аудио вторая", 99) < 1.5,
          f"{when.get('аудио вторая', 99):.1f} с")
    check("обе расшифровки в итоге дошли",
          "расшифровка первая" in when and "расшифровка вторая" in when)
    meta_a = json.loads((a / "meta.json").read_text(encoding="utf-8"))
    check("после расшифровки запись помечена законченной", meta_a.get("sent") and meta_a.get("text_done") is True)

    # Перезапуск посреди ожидания Groq: аудио ушло, расшифровка — нет.
    events.clear()
    c = make_seg(root, "после_перезапуска", sent=True, text_done=False)
    check("такая запись считается недоделанной", c in P.unfinished_segments.__wrapped__(root)
          if hasattr(P.unfinished_segments, "__wrapped__") else
          (config.__setattr__("REC_DIR", root) or c in P.unfinished_segments()))
    await P.process_segment(c, send_audio, send_text, send_doc, lambda c: True)
    kinds = [n for _, n in events]
    check("после перезапуска досылается только расшифровка, аудио повторно не шлётся",
          kinds == ["расшифровка после_перезапуска"], str(kinds))

    # Старые записи без флага не трогаем: расшифровку заново не делаем.
    events.clear()
    d = make_seg(root, "старая", sent=True)
    await P.process_segment(d, send_audio, send_text, send_doc, lambda c: True)
    check("старая доставленная запись (без флага) не обрабатывается повторно", not events, str(events))

    print("ИТОГ:", "всё прошло" if not fails else f"провалено {fails}")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))

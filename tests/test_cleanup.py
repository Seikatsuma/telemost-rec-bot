"""Автоочистка: удаляется ТОЛЬКО старое и доставленное; всё остальное остаётся нетронутым.

Работает во временной папке, боевые записи не трогает.
Запуск: venv/bin/python tests/test_cleanup.py
"""
import json
import os
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


root = pathlib.Path(tempfile.mkdtemp())
config.REC_DIR = root / "rec"
config.REC_DIR.mkdir()
config.REC_KEEP_DAYS = 30
now = time.time()
DAY = 86400


def seg(name, age_days, **meta):
    d = config.REC_DIR / name
    d.mkdir()
    (d / "audio.m4a").write_bytes(b"x")
    m = d / "meta.json"
    if meta.get("_broken"):
        m.write_text("{битый", encoding="utf-8")
    else:
        m.write_text(json.dumps(meta), encoding="utf-8")
    t = now - age_days * DAY
    os.utime(m, (t, t))
    return d


old_sent = seg("старая_доставлена", 31, sent=True)
old_sent_text = seg("старая_с_расшифровкой", 40, sent=True, text_done=True)
fresh = seg("свежая_доставлена", 29, sent=True)
old_unsent = seg("старая_не_отправлена", 60, sent=False)
old_text_pending = seg("старая_ждёт_расшифровку", 60, sent=True, text_done=False)
broken = seg("старая_битая", 60, _broken=True)
outside = root / "рядом_не_трогать.txt"
outside.write_text("x")

removed = P.cleanup_old_recordings(now=now)
names = sorted(d.name for d in removed)

check("старая доставленная (31 день) — удалена", not old_sent.exists())
check("старая с расшифровкой (40 дней) — удалена", not old_sent_text.exists())
check("свежая (29 дней) — оставлена", fresh.exists())
check("не отправленная — оставлена, сколько бы ей ни было", old_unsent.exists())
check("ждущая расшифровку — оставлена", old_text_pending.exists())
check("битая запись — оставлена, а не удалена наугад", broken.exists())
check("файлы вне папки записей не тронуты", outside.exists())
check("удалено ровно две", len(removed) == 2, str(names))

config.REC_KEEP_DAYS = 0
seg("ещё_старая", 100, sent=True)
check("REC_KEEP_DAYS=0 — не удаляется ничего", P.cleanup_old_recordings(now=now) == [])

print("ИТОГ:", "всё прошло" if not fails else f"провалено {fails}")
sys.exit(1 if fails else 0)

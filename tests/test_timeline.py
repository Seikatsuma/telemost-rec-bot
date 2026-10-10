"""Сведение журнала «кто когда говорил» в speakers.json — без браузера, по готовым событиям.

Проверяемое: сглаживание дребезга (паузы <1.5 с склеить, реплики <1 с выкинуть — пороги
замерены 18.09), вычитание паузы записи из меток звука, две реплики внахлёст -> overlap,
метки по звуку считаются от начала собранного аудио (все куски подряд).

Запуск: venv/bin/python tests/test_timeline.py
"""
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import timeline  # noqa: E402
import postprocess  # noqa: E402

TMP = pathlib.Path("/tmp/telemost-timeline")
shutil.rmtree(TMP, ignore_errors=True)
config.REC_DIR = TMP
SEG = TMP / "m-20261008-120000"
SEG.mkdir(parents=True)

T0 = 1_759_000_000_000  # базовое unix-время, мс
fails = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def ev(source=None, kind=None, payload=None):
    """Событие, как оно приходит из страницы в write_event."""
    timeline.write_event(source, kind, payload)


def lines() -> list[dict]:
    return [json.loads(x) for x in (SEG / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]


# --- Сценарий записи: part001 0–60 с; пауза 20–30 с (все вышли); реплики внутри ---
part = f"{SEG.name}|part001"
ev(None, "rec", {"op": "start", "part": part, "at": T0})
ev(None, "participants", {"part": part, "people": [["p1", "Иван"], ["p2", "Мария"]], "at": T0})
# Иван говорит 2–10 с; Мария 6–14 с — наложение 6–10.
ev(None, "vad", {"part": part, "id": "p1", "name": "Иван", "speaking": True, "at": T0 + 2000})
ev(None, "vad", {"part": part, "id": "p2", "name": "Мария", "speaking": True, "at": T0 + 6000})
ev(None, "vad", {"part": part, "id": "p1", "name": "Иван", "speaking": False, "at": T0 + 10000})
ev(None, "vad", {"part": part, "id": "p2", "name": "Мария", "speaking": False, "at": T0 + 14000})
# Дребезг: короткий вдох Ивана на 16 с (0.5 с — выкинуть), потом реплика 17.2–18 с (склеиться не должна: <1с)
ev(None, "vad", {"part": part, "id": "p1", "name": "Иван", "speaking": True, "at": T0 + 16000})
ev(None, "vad", {"part": part, "id": "p1", "name": "Иван", "speaking": False, "at": T0 + 16500})
# Пауза записи 20–30 с (все вышли). В паузе звук не пишется — vad событие в ней без аудиометок.
ev(None, "rec", {"op": "pause", "part": part, "at": T0 + 20000})
ev(None, "vad", {"part": part, "id": "p2", "name": "Мария", "speaking": True, "at": T0 + 25000})
ev(None, "vad", {"part": part, "id": "p2", "name": "Мария", "speaking": False, "at": T0 + 26000})
ev(None, "rec", {"op": "resume", "part": part, "at": T0 + 30000})
# Иван снова 40–50 с.
ev(None, "vad", {"part": part, "id": "p1", "name": "Иван", "speaking": True, "at": T0 + 40000})
ev(None, "vad", {"part": part, "id": "p1", "name": "Иван", "speaking": False, "at": T0 + 50000})
ev(None, "participants", {"part": part, "people": [["p1", "Иван"]], "at": T0 + 55000})
ev(None, "rec", {"op": "stop", "part": part, "at": T0 + 60000})

ls = lines()
check("журнал: есть join обоих и leave Марии",
      [x.get("kind") for x in ls].count("join") == 2 and
      any(x.get("kind") == "leave" and x.get("name") == "Мария" for x in ls))
check("журнал: vad-события на месте", sum(1 for x in ls if x.get("kind") == "vad") == 10)
check("журнал: rec старт/пауза/резюме/стоп",
      [x.get("op") for x in ls if x.get("kind") == "rec"] == ["start", "pause", "resume", "stop"])

out = timeline.build_speakers(SEG)
check("speakers.json записан", out is not None and (SEG / "speakers.json").exists())

segs = { (s["name"], round(s["audio_start_s"] or -1), round(s["audio_end_s"] or -1)): s
         for s in out["segments"] if s["audio_start_s"] is not None }
# Аудио-метки = стеновые минус пауза 10 с: Мария 6–14 -> аудио 6–14;
# её реплика в паузе (25–26) аудиометок не имеет и выкидывается из этого словаря;
# Иван 40–50 стены -> аудио 30–40 (пауза вычтена).
ivan = [s for s in out["segments"] if s["name"] == "Иван"]
maria = [s for s in out["segments"] if s["name"] == "Мария"]
check("Иван: 2 реплики (дребезг 0.5 с выкинут)", len(ivan) == 2, str([ (s["wall_start_ms"]-T0)/1000 for s in ivan]))
check("Иван: метки по звуку 2–10 и 30–40",
      [ (round(s["audio_start_s"]), round(s["audio_end_s"])) for s in ivan ] == [(2, 10), (30, 40)],
      str([ (s["audio_start_s"], s["audio_end_s"]) for s in ivan ]))
check("Мария: реплика 6–14 по звуку", maria and (round(maria[0]["audio_start_s"]), round(maria[0]["audio_end_s"])) == (6, 14),
      str([ (s["audio_start_s"], s["audio_end_s"]) for s in maria ]))
check("Мария: реплика во время паузы осталась в журнале без меток звука",
      any(s["audio_start_s"] is None for s in maria))
check("наложение 6–10 отмечено у обоих",
      ivan[0]["overlap"] and maria[0]["overlap"])
check("состав: оба имени", sorted(out["speakers"]) == ["Иван", "Мария"], str(out["speakers"]))
check("часть записи описана", out["parts"] and out["parts"][0]["name"].endswith("part001"))

# Вышел на полуслове: Иван говорит в part002 и уходит без «молчит» — реплика должна
# закрыться на «вышел» (78 с), а не тянуться до конца журнала (90 с).
part2 = f"{SEG.name}|part002"
ev(None, "rec", {"op": "start", "part": part2, "at": T0 + 70000})
ev(None, "vad", {"part": part2, "id": "p1", "name": "Иван", "speaking": True, "at": T0 + 72000})
ev(None, "participants", {"part": part2, "people": [["p2", "Мария"]], "at": T0 + 78000})
ev(None, "rec", {"op": "stop", "part": part2, "at": T0 + 90000})
# Стоп part003 потерян (падение страницы): часть закрывается последним событием (104 с),
# а не стартом part004 (160 с) — иначе 56 мёртвых секунд едут в метки звука.
part3 = f"{SEG.name}|part003"
part4 = f"{SEG.name}|part004"
ev(None, "rec", {"op": "start", "part": part3, "at": T0 + 100000})
ev(None, "vad", {"part": part3, "id": "p1", "name": "Иван", "speaking": True, "at": T0 + 102000})
ev(None, "vad", {"part": part3, "id": "p1", "name": "Иван", "speaking": False, "at": T0 + 104000})
ev(None, "rec", {"op": "start", "part": part4, "at": T0 + 160000})
ev(None, "vad", {"part": part4, "id": "p1", "name": "Иван", "speaking": True, "at": T0 + 162000})
ev(None, "vad", {"part": part4, "id": "p1", "name": "Иван", "speaking": False, "at": T0 + 166000})
ev(None, "rec", {"op": "stop", "part": part4, "at": T0 + 170000})

out = timeline.build_speakers(SEG)
m2 = [s for s in out["segments"] if s["name"] == "Иван" and T0 + 70000 < s["wall_start_ms"] < T0 + 90000]
check("вышел на полуслове: реплика закрыта на выходе (78 с), не до конца журнала",
      m2 and round((m2[0]["wall_end_ms"] - T0) / 1000) == 78, str(m2))
p3 = next(p for p in out["parts"] if p["name"].endswith("part003"))
check("потерянный стоп: часть закрыта последним событием (104 с), не новым стартом (160)",
      round((p3["end_wall_ms"] - T0) / 1000) == 104, str(p3))
# Звук к part004: 50 (part001: 60−10 паузы) + 20 (part002) + 4 (part003) = 74 с сдвига.
i4 = [s for s in out["segments"] if s["name"] == "Иван" and s["wall_start_ms"] > T0 + 160000]
check("метки part004 без мёртвого времени: 76–80 по звуку",
      i4 and (round(i4[0]["audio_start_s"]), round(i4[0]["audio_end_s"])) == (76, 80),
      str([(s["audio_start_s"], s["audio_end_s"]) for s in i4]))

# Имена в расшифровке: реплика Deepgram получает имя того, кто по журналу Телемоста
# говорил в эти секунды; на краях допуск 3 с (признак отстаёт от речи); дальше — без имени.
utts = [{"start": 2.0, "end": 9.0},    # внутри Ивана 2–10
        {"start": 32.0, "end": 39.0},  # внутри Ивана 30–40
        {"start": 6.0, "end": 13.0},   # большее накрытие Марии 6–14 (4 с Ивана против 7 её)
        {"start": 40.5, "end": 42.0},  # пусто, но Иван кончается в 40 — ближайшая в допуске
        {"start": 45.0, "end": 47.0}]  # ближайшая в 5 с — за допуском, имени нет
labels = postprocess._label_utterances(utts, SEG)
check("имена по журналу: накрытие → имя, край ≤3 с → имя, дальше → None",
      labels == ["Иван", "Иван", "Мария", "Иван", None], str(labels))
check("без speakers.json — все None", postprocess._label_utterances(utts, TMP / "m-nodir") == [None] * 5)

# Пустая папка без журнала — build_speakers молча ничего не делает.
empty = TMP / "m-empty"
empty.mkdir()
check("нет журнала -> None, файла нет", timeline.build_speakers(empty) is None and not (empty / "speakers.json").exists())

print("ИТОГ:", "всё прошло" if not fails else f"провалено {len(fails)}: {fails}")
sys.exit(1 if fails else 0)

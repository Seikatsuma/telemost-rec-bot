"""Журнал «кто когда говорил» одной записи: сырые события -> свёрнутый файл для расшифровки.

Откуда берётся: Телемост в сигнальном канале шлёт раздачу слотов (slotsConfig), где у каждого
участника стоит признак речевой активности vad — тот самый, от которого в окне «загорается»
плитка говорящего. inject.js превращает его в события и шлёт сюда через биндинг __recEvent.

Что лежит в папке записи:
- timeline.jsonl — сырой журнал событий, по строке JSON на событие:
    {"wall_ms": 1759..., "kind": "join"|"leave"|"vad"|"rec", ...}
  vad: {"id","name","on":true|false}; rec: {"op":"start"|"pause"|"resume"|"stop","part":"seg|partNNN"}.
  wall_ms — миллисекунды unix-времени; общие часы у страницы и у нас (та же машина).
- .roster.json — последний известный состав (нужен, чтобы из полного списка участников
  делать события «вошёл/вышел»; при рестарте дифф посчитается от пустого).
- speakers.json — свёрнуто на закрытии записи: реплики каждого со сглаживанием и метками
  в секундах ОТ НАЧАЛА СОБРАННОГО АУДИО (то, во что потом ляжет разметка расшифровки).

Пороги сглаживания — замер 18.09.26 на живом Телемосте (agent.md): признак дребезжит
(вдох -> «замолчал» -> через секунду «говорит»); паузы короче MERGE_GAP_MSEC склеиваем,
реплики короче MIN_SEG_MSEC отбрасываем.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import config

log = logging.getLogger("timeline")

MERGE_GAP_MSEC = 1500
MIN_SEG_MSEC = 1000

TIMELINE_NAME = "timeline.jsonl"
SPEAKERS_NAME = "speakers.json"
ROSTER_NAME = ".roster.json"


def _seg_dir(part: str) -> Path | None:
    """Папка записи из имени части "<папка>|<partNNN>" — как у write_chunk:
    событие может прийти после закрытия записи, поэтому адресуемся по имени, а не «в текущую»."""
    seg_name, _, _ = part.partition("|")
    if not seg_name or "/" in seg_name or ".." in seg_name:
        return None
    d = config.REC_DIR / seg_name
    return d if d.is_dir() else None


def _append(seg_dir: Path, obj: dict) -> None:
    try:
        with open(seg_dir / TIMELINE_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("журнал записи %s", seg_dir)


def _roster(seg_dir: Path) -> dict:
    try:
        return json.loads((seg_dir / ROSTER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_event(source, kind: str, payload) -> None:
    """Биндинг __recEvent из страницы. payload.part — адресат ("seg|partNNN"), добавляет inject.js."""
    try:
        if not isinstance(payload, dict):
            return
        seg_dir = _seg_dir(str(payload.get("part") or ""))
        if seg_dir is None:
            return
        wall = int(payload.get("at") or time.time() * 1000)
        if kind == "vad":
            _append(seg_dir, {"wall_ms": wall, "kind": "vad", "id": str(payload.get("id") or ""),
                              "name": str(payload.get("name") or ""), "on": bool(payload.get("speaking"))})
        elif kind == "participants":
            people = {str(p[0]): str(p[1]) for p in payload.get("people") or [] if p}
            old = _roster(seg_dir)
            for pid, name in people.items():
                if pid not in old:
                    _append(seg_dir, {"wall_ms": wall, "kind": "join", "id": pid, "name": name})
            for pid, name in old.items():
                if pid not in people:
                    _append(seg_dir, {"wall_ms": wall, "kind": "leave", "id": pid, "name": name})
            (seg_dir / ROSTER_NAME).write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")
        elif kind == "rec":
            _append(seg_dir, {"wall_ms": wall, "kind": "rec", "op": str(payload.get("op") or ""),
                              "part": str(payload.get("part") or "")})
    except Exception:
        log.exception("событие %s", kind)


# ---------------- сведение в speakers.json ----------------

def _parts(events: list[dict], last_wall: int) -> list[dict]:
    """Интервалы записи по событиям rec: [start, end] и паузы внутри (пока все вышли,
    MediaRecorder стоит на паузе и это время в файл не попадает — вычитаем)."""
    parts: list[dict] = []
    seen = 0  # метка последнего события — чем закрыть часть, если её «стоп» потерян
    for e in events:
        w = e["wall_ms"]
        if e.get("kind") == "rec":
            op = e.get("op")
            if op == "start":
                # Стопа нет (падение страницы): конец — последнее событие, а не новый старт,
                # иначе мёртвое время перезахода посчитается записанным звуком.
                if parts and parts[-1]["end"] is None:
                    parts[-1]["end"] = seen or w
                parts.append({"name": e.get("part", ""), "start": w, "end": None, "pauses": []})
            elif parts:
                if op == "pause":
                    parts[-1]["pauses"].append([w, None])
                elif op == "resume" and parts[-1]["pauses"] and parts[-1]["pauses"][-1][1] is None:
                    parts[-1]["pauses"][-1][1] = w
                elif op == "stop":
                    parts[-1]["end"] = w
        seen = w
    for p in parts:
        if p["end"] is None:
            p["end"] = last_wall
        for ps in p["pauses"]:
            if ps[1] is None:
                ps[1] = p["end"]
    return parts


def _audio_at(wall_ms: int, parts: list[dict]) -> float | None:
    """Секунды от начала собранного аудио; None — время не записывалось (пауза, до старта)."""
    off = 0
    for p in parts:
        if p["start"] <= wall_ms <= p["end"]:
            if any(s <= wall_ms < e for s, e in p["pauses"]):
                return None
            paused = sum(min(wall_ms, e) - s for s, e in p["pauses"] if s <= wall_ms)
            return round((off + wall_ms - p["start"] - paused) / 1000, 2)
        off += p["end"] - p["start"] - sum(e - s for s, e in p["pauses"])
    return None


def _smooth(raw: list[dict]) -> list[dict]:
    """По человеку: склеить реплики через паузу < MERGE_GAP_MSEC, выкинуть < MIN_SEG_MSEC."""
    out = []
    for s in sorted(raw, key=lambda x: x["start"]):
        if out and out[-1]["name"] == s["name"] and s["start"] - out[-1]["end"] < MERGE_GAP_MSEC:
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    out.sort(key=lambda x: x["start"])
    return [s for s in out if s["end"] - s["start"] >= MIN_SEG_MSEC]


def build_speakers(seg_dir: Path) -> dict | None:
    """timeline.jsonl -> speakers.json. Вызывается на закрытии записи (и при досылке после сбоя)."""
    tl = seg_dir / TIMELINE_NAME
    if not tl.exists():
        return None
    events = []
    for line in tl.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    last_wall = max((e.get("wall_ms", 0) for e in events), default=0)
    parts = _parts(events, last_wall)

    names: dict[str, str] = {}
    open_seg: dict[str, dict] = {}
    raw: list[dict] = []
    for e in events:
        if e.get("name") and e.get("id"):
            names[e["id"]] = e["name"]
        kind = e.get("kind")
        if kind == "leave":  # вышел на полуслове — признак «молчит» уже не придёт
            pid = e.get("id") or e.get("name") or ""
            if pid in open_seg:
                s = open_seg.pop(pid)
                s["end"] = e["wall_ms"]
                raw.append(s)
            continue
        if kind != "vad":
            continue
        pid = e["id"] or e["name"]
        if e.get("name"):
            names[pid] = e["name"]
        if e["on"] and pid not in open_seg:
            open_seg[pid] = {"name": pid, "start": e["wall_ms"], "end": e["wall_ms"]}
        elif not e["on"] and pid in open_seg:
            s = open_seg.pop(pid)
            s["end"] = e["wall_ms"]
            raw.append(s)
    for s in open_seg.values():  # кто-то говорил на момент конца журнала
        s["end"] = last_wall
        raw.append(s)
    for s in raw:
        s["name"] = names.get(next((p for p, n in names.items() if s["name"] in (p, n)), s["name"]), s["name"])

    segments = []
    for s in _smooth(raw):
        seg = {"name": s["name"], "wall_start_ms": s["start"], "wall_end_ms": s["end"],
               "audio_start_s": _audio_at(s["start"], parts), "audio_end_s": _audio_at(s["end"], parts),
               "overlap": False}
        segments.append(seg)
    for a in segments:
        for b in segments:
            if a is not b and a["wall_start_ms"] < b["wall_end_ms"] and b["wall_start_ms"] < a["wall_end_ms"]:
                a["overlap"] = True
                break

    people = [{"id": pid, "name": n} for pid, n in names.items()]
    out = {"version": 1,
           "speakers": sorted({s["name"] for s in segments}),
           "parts": [{"name": p["name"], "start_wall_ms": p["start"], "end_wall_ms": p["end"]} for p in parts],
           "segments": segments,
           "people": people,
           "events": len(events)}
    (seg_dir / SPEAKERS_NAME).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out

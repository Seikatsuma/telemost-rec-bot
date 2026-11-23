"""Закрытая запись → один аудиофайл → Telegram; дальше, если есть ключи, расшифровка и конспект.

Звук склеивается из кусков part001.webm, part002.webm… (новый кусок — после перезахода бота).
Выход — .m4a (AAC 64 кбит/с, моно): играет в Telegram на iPhone сразу, ~29 МБ в час.
Расшифровка — Deepgram целиком (диаризация + таймкоды; имена спикеров подставляются из
speakers.json журнала Телемоста), при сбое — Groq кусками по минуте (без ролей).
Конспект — DeepSeek, если есть ключ, иначе модель Groq.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import requests

import config
import timeline
from browser_env import low_priority

log = logging.getLogger("post")

# Две очереди (18.09.26). Раньше была одна на всё, и расшифровка шла внутри неё. После того как
# расшифровка научилась ждать лимиты Groq по часу, аудио СЛЕДУЮЩЕЙ записи (вторая часть длинной
# встречи, вторая встреча) стояло бы за ней часами — против «аудио сразу» (владелец 18.09).
_audio_lock = asyncio.Lock()  # сборка аудио — тяжёлая, по одной за раз: 2 ядра на весь сервер
_text_lock = asyncio.Lock()   # расшифровка — по одной за раз: лимит Groq общий на весь ключ

SendAudio = Callable[[int, Path, str, str], Awaitable[None]]
SendText = Callable[[int, str], Awaitable[None]]
SendDoc = Callable[[int, Path, str], Awaitable[None]]


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(low_priority(cmd), capture_output=True, text=True, timeout=timeout)


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def build_audio(seg_dir: Path) -> Path | None:
    parts = sorted(p for p in seg_dir.glob("part*.webm") if p.stat().st_size > 0)
    if not parts:
        return None
    out = seg_dir / "audio.m4a"
    inputs: list[str] = []
    for p in parts:
        inputs += ["-err_detect", "ignore_err", "-i", str(p)]
    filt = "".join(f"[{i}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", filt,
              "-map", "[a]", "-ac", "1", "-c:a", "aac", "-b:a", "64k", str(out)])
    if r.returncode != 0 or not out.exists():
        # Битый хвост последнего куска (сбой процесса посреди записи) — склеиваем по одному, пропуская негодные.
        log.warning("склейка не удалась (%s), пробую по кускам", r.stderr[-300:])
        good = []
        for i, p in enumerate(parts):
            wav = seg_dir / f"fix{i:03d}.wav"
            if _run(["ffmpeg", "-loglevel", "error", "-y", "-err_detect", "ignore_err", "-i", str(p),
                     "-ac", "1", "-ar", "48000", str(wav)]).returncode == 0 and duration(wav) > 0.5:
                good.append(wav)
        if not good:
            return None
        lst = seg_dir / "fix.txt"
        lst.write_text("".join(f"file '{g.name}'\n" for g in good))
        r = _run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                  "-c:a", "aac", "-b:a", "64k", str(out)])
        for g in good:
            g.unlink(missing_ok=True)
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            return None
    return out


def split_for_telegram(audio: Path) -> list[Path]:
    limit = config.TG_PART_MB * 1024 * 1024
    if audio.stat().st_size <= limit:
        return [audio]
    total = duration(audio)
    n = int(audio.stat().st_size // limit) + 1
    step = total / n
    out = []
    for i in range(n):
        p = audio.with_name(f"audio-part{i + 1}.m4a")
        _run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(i * step), "-t", str(step), "-i", str(audio), "-c", "copy", str(p)])
        out.append(p)
    return out


def fmt_dur(sec: float) -> str:
    m = int(round(sec / 60))
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{max(m, 1)} мин"


# ---------------- расшифровка и конспект ----------------

GROQ_ASR_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
# Подсказка-«образец разговорной речи»: с ней Whisper не выкидывает слова-паразиты и короткие реплики.
# Замер на звонках: полнота 92.1 % против 76.7 % без неё; по эталону «Трамвай» turbo+подсказка 96.6 % (Deepgram 94.7).
CALLS_PROMPT = ("Ну вот, смотри, там как бы, да? Ну то есть, это самое, "
                "вот именно так, да. Понятно, ну ладно.")
# Куски по 60 с с перекрытием 2 с. Замер 17.09 по эталону: целиком 3 мин — 71 %, по 120 с — 83 %, по 90 и 60 с — 96.6 %
# (на длинном куске Whisper пропускает целые фразы). Метка времени в расшифровке — раз в PARAGRAPH_SEC.
PIECE_SEC = 60
OVERLAP_SEC = 2
PARAGRAPH_SEC = 600


def _norm(w: str) -> str:
    return "".join(ch for ch in w.lower().replace("ё", "е") if ch.isalnum())


def _drop_overlap(prev: str, nxt: str, probe: int = 15) -> str:
    """Убрать из начала nxt слова, повторяющие конец prev (перекрытие кусков)."""
    pw, nw = [_norm(w) for w in prev.split()][-probe:], nxt.split()
    nn = [_norm(w) for w in nw[:probe]]
    for k in range(min(len(pw), len(nn)), 0, -1):
        if pw[-k:] == nn[:k] and any(pw[-k:]):
            return " ".join(nw[k:])
    return nxt


# Сколько один кусок готов ждать Groq суммарно. Бесплатный тариф: 20 запросов в минуту, 7 200 секунд
# звука в час, 28 800 в сутки (console.groq.com/docs/rate-limits, 18.09.26). Куски по ~62 с → в час
# влезает ~1 ч 55 мин встречи; на встрече длиннее Groq отвечает 429 с retry-after в минуты. Раньше
# бот делал 8 попыток по ≤120 с и помечал кусок «не распознано» — теряли середину длинной встречи.
# Теперь ждём столько, сколько просит Groq: расшифровка приходит позже, но целиком (аудио уже отправлено).
ASR_PATIENCE_SEC = 3600
ASR_MAX_WAIT_SEC = 1200

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
# Реплики с таймкодами (utterances — резка по паузам). Диаризацию НЕ просим (владелец 08.10:
# «по ролям не нужно — работа уже сделана»): кто говорил, знает журнал Телемоста по аккаунту,
# а голосовой разбор — лишняя и менее точная работа поверх. smart_format — читаемость
# (числа цифрами); filler_words выключены — «ну/вот» в протоколе встречи шум, не диктовка.
DEEPGRAM_PARAMS = ("language=ru&punctuate=true&smart_format=true"
                   "&utterances=true&filler_words=false")

# Допуск на границы реплики: признак vad зажигается позже нажатия микрофона и гаснет раньше
# конца фразы (расхождение 1.3–1.7 с — замер 18.09 на живом Телемосте).
VAD_NEAR_SEC = 3.0


def _label_utterances(utterances: list[dict], seg_dir: Path) -> list[str | None]:
    """Имя участника Телемоста для каждой реплики — по журналу speakers.json
    (audio_*_s и start/end Deepgram — одна шкала: секунды от начала аудио).

    Имя реплики — человек с наибольшим накрытием по времени; без накрытия — ближайшая
    реплика того же журнала в пределах VAD_NEAR_SEC (задержки признака на краях фразы).
    Совсем рядом никого или нет журнала — None.
    """
    try:
        sp = json.loads((seg_dir / timeline.SPEAKERS_NAME).read_text(encoding="utf-8"))
        segs = [s for s in sp.get("segments") or []
                if s.get("audio_start_s") is not None and s.get("audio_end_s") is not None]
    except Exception:
        return [None] * len(utterances)
    labels = []
    for u in utterances:
        us = float(u.get("start", 0.0))
        ue = float(u.get("end", us))
        cur: dict[str, float] = {}
        for s in segs:
            ov = min(ue, s["audio_end_s"]) - max(us, s["audio_start_s"])
            if ov > 0:
                cur[s["name"]] = cur.get(s["name"], 0.0) + ov
        if cur:
            labels.append(max(cur.items(), key=lambda kv: kv[1])[0])
            continue
        near, gap = None, VAD_NEAR_SEC
        for s in segs:
            g = max(us - s["audio_end_s"], s["audio_start_s"] - ue)
            if g < gap:
                near, gap = s["name"], g
        labels.append(near)
    return labels


def _ts(sec: float) -> str:
    s = int(sec)
    return f"[{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}]"


def _deepgram_asr(audio: Path, sleep=time.sleep) -> str:
    """Файл целиком в Deepgram -> абзацы «[время] Имя: реплика» (имя — из журнала Телемоста;
    где журнал молчит — «Спикер ?», без журнала — без подписи).
    429/5xx — повтор с ожиданием до ASR_PATIENCE_SEC; текст пуст или ответ битый — ошибка."""
    url = f"{DEEPGRAM_URL}?model={config.DEEPGRAM_ASR_MODEL}&{DEEPGRAM_PARAMS}"
    waited, attempt = 0.0, 0
    while True:
        with open(audio, "rb") as f:
            r = requests.post(url, headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}",
                                            "Content-Type": "audio/mp4"},
                              data=f, timeout=(30, 1800))
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504):
            asked = float(r.headers.get("retry-after") or 0)
            wait = min(asked or 30 * (attempt + 1), ASR_MAX_WAIT_SEC)
            if waited + wait > ASR_PATIENCE_SEC:
                raise RuntimeError(f"Deepgram занят дольше {ASR_PATIENCE_SEC // 60} мин ({r.status_code})")
            log.warning("deepgram %s, жду %.0f с (всего ждал %.0f с)", r.status_code, wait, waited)
            sleep(wait)
            waited += wait
            attempt += 1
            continue
        raise RuntimeError(f"Deepgram {r.status_code}: {r.text[:200]}")
    res = r.json()["results"]
    utterances = res.get("utterances") or []
    if not utterances:
        return ((res.get("channels") or [{}])[0].get("alternatives") or [{}])[0].get("transcript", "").strip()
    names = _label_utterances(utterances, audio.parent)
    # Журнал никого не назвал — абзацы без подписи говорящего вовсе (выдуманный номер не нужен).
    labels = [n or "Спикер ?" for n in names] if any(names) else [""] * len(utterances)
    paragraphs: list[str] = []
    cur_label, cur_start, cur_parts = None, 0.0, []
    for u, label in zip(utterances, labels):
        txt = (u.get("transcript") or "").strip()
        if not txt:
            continue
        if label != cur_label:
            if cur_parts:
                head = f"{_ts(cur_start)} {cur_label}: " if cur_label else f"{_ts(cur_start)} "
                paragraphs.append(head + ' '.join(cur_parts))
            cur_label, cur_start, cur_parts = label, float(u.get("start", 0.0)), [txt]
        else:
            cur_parts.append(txt)
    if cur_parts:
        head = f"{_ts(cur_start)} {cur_label}: " if cur_label else f"{_ts(cur_start)} "
        paragraphs.append(head + ' '.join(cur_parts))
    return "\n\n".join(paragraphs)


def _groq_asr(path: Path, sleep=time.sleep) -> str:
    """Один кусок в Groq. 429/5xx — повтор с ожиданием по retry-after, пока не исчерпано ASR_PATIENCE_SEC."""
    waited, attempt = 0.0, 0
    while True:
        with open(path, "rb") as f:
            r = requests.post(GROQ_ASR_URL, headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                              files={"file": (path.name, f, "audio/ogg")},
                              data={"model": config.GROQ_ASR_MODEL, "language": "ru", "temperature": "0",
                                    "prompt": CALLS_PROMPT, "response_format": "json"},
                              timeout=120)
        if r.status_code == 200:
            return (r.json().get("text") or "").strip()
        if r.status_code in (429, 500, 502, 503, 504):
            asked = float(r.headers.get("retry-after") or 0)
            wait = min(asked or 5 * (attempt + 1), ASR_MAX_WAIT_SEC)
            if waited + wait > ASR_PATIENCE_SEC:
                raise RuntimeError(f"Groq занят дольше {ASR_PATIENCE_SEC // 60} мин (последний ответ {r.status_code})")
            log.warning("groq %s, жду %.0f с (всего ждал %.0f с)", r.status_code, wait, waited)
            sleep(wait)
            waited += wait
            attempt += 1
            continue
        r.raise_for_status()


def transcribe(audio: Path) -> str:
    """Расшифровка: Deepgram целиком (роли + таймкоды), при сбое — Groq кусками по минуте."""
    if config.DEEPGRAM_API_KEY:
        try:
            text = _deepgram_asr(audio)
            if text:
                return text
            log.warning("deepgram вернул пусто — ухожу на Groq")
        except Exception:
            log.exception("deepgram не справился — ухожу на Groq")
    return _groq_transcribe(audio)


def _groq_transcribe(audio: Path) -> str:
    """Расшифровка через Groq кусками по минуте; абзац с меткой времени — каждые 10 минут."""
    if not config.GROQ_API_KEY:
        return ""
    total = duration(audio)
    paragraphs: list[tuple[float, list[str]]] = []
    tmp = audio.with_name("asr-piece.ogg")
    prev = ""
    start = 0.0
    while start < total:
        # Моно 16 кГц Opus 32 кбит/с: минута ≈ 240 КБ, распознаётся так же, как WAV (замер 17.09).
        _run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-t", str(PIECE_SEC + OVERLAP_SEC), "-i", str(audio),
              "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", str(tmp)])
        text = ""
        if tmp.exists() and duration(tmp) > 1:
            try:
                text = _groq_asr(tmp)
            except Exception as e:
                log.exception("распознавание куска с %d с", start)
                text = f"(не распознано: {type(e).__name__})"
        if text:
            text = _drop_overlap(prev, text) if prev else text
            prev = text or prev
            para_start = start - start % PARAGRAPH_SEC
            if not paragraphs or paragraphs[-1][0] != para_start:
                paragraphs.append((para_start, []))
            if text:
                paragraphs[-1][1].append(text)
        start += PIECE_SEC
    tmp.unlink(missing_ok=True)
    return "\n\n".join(f"[{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}] " + " ".join(parts)
                        for t, parts in paragraphs if parts)


SUMMARY_PROMPT = """Ниже расшифровка встречи, распознанная автоматически (в словах возможны ошибки).
Сделай очень короткий конспект по-русски: только то, что есть в тексте, ничего не додумывай.
Весь ответ — не длиннее 700 знаков. Простой текст без markdown. Разделы без содержания не выводи.

О чём: одно предложение.
Главное:
• 2–5 коротких пунктов
Решения:
• …
Задачи:
• кто — что — срок"""


def summarize(transcript: str) -> str:
    """Короткий конспект: DeepSeek, если есть ключ, иначе модель Groq (тогда хватает одного бесплатного ключа)."""
    if len(transcript) < 200:
        return ""
    if config.DEEPSEEK_API_KEY:
        url, key, model, limit = "https://api.deepseek.com/chat/completions", config.DEEPSEEK_API_KEY, "deepseek-chat", 200_000
    elif config.GROQ_API_KEY:
        url, key, model, limit = GROQ_CHAT_URL, config.GROQ_API_KEY, config.GROQ_CHAT_MODEL, 60_000
    else:
        return ""
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "max_tokens": 1200, "temperature": 0.3,
                            "messages": [{"role": "system", "content": SUMMARY_PROMPT},
                                         {"role": "user", "content": transcript[:limit]}]},
                      timeout=300)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ---------------- весь путь одной записи ----------------

async def process_segment(seg_dir: Path, send_audio: SendAudio, send_text: SendText, send_doc: SendDoc,
                          want_text: Callable[[int], bool], recovered: bool = False) -> None:
    meta_path = seg_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("sent") and meta.get("text_done", True):
        return
    chat = int(meta["chat_id"])
    if meta.get("sent"):
        # Аудио уже ушло, а расшифровка не дошла (перезапуск посреди ожидания Groq) — только она.
        await _send_transcript(seg_dir, meta, chat, send_text, send_doc, want_text)
        return
    async with _audio_lock:
        if (seg_dir / "timeline.jsonl").exists() and not (seg_dir / "speakers.json").exists():
            try:
                await asyncio.to_thread(timeline.build_speakers, seg_dir)
            except Exception:
                log.exception("журнал говорящих %s", seg_dir)
        audio = await asyncio.to_thread(build_audio, seg_dir)
        dur = duration(audio) if audio else 0
        started = datetime.fromtimestamp(meta["started"])
        label = meta.get("label") or "Встреча"
        title = f"{label} · {started:%d.%m %H:%M:%S}"
        if not audio or dur < 3:
            await send_text(chat, f"🔇 {label}: звука не было — присылать нечего")
        else:
            caption = f"🎧 {title} · {fmt_dur(dur)}"
            if meta.get("names"):
                caption += "\n👥 " + ", ".join(meta["names"])
            if recovered:
                caption += "\n(после перезапуска бота)"
            files = await asyncio.to_thread(split_for_telegram, audio)
            for i, f in enumerate(files):
                t = title if len(files) == 1 else f"{title} · часть {i + 1}/{len(files)}"
                await send_audio(chat, f, t, caption if i == 0 else t)
        meta["sent"] = True
        # Расшифровка ещё впереди: флаг, чтобы после перезапуска бот её дослал, а не потерял.
        meta["text_done"] = not (audio and dur >= 20 and want_text(chat)
                                 and (config.GROQ_API_KEY or config.DEEPGRAM_API_KEY))
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # Очередь аудио отпущена — следующая запись уже может собираться и уходить.
    await _send_transcript(seg_dir, meta, chat, send_text, send_doc, want_text)


async def _send_transcript(seg_dir: Path, meta: dict, chat: int, send_text: SendText, send_doc: SendDoc,
                           want_text: Callable[[int], bool]) -> None:
    """Расшифровка и конспект одной записи — своей очередью, после того как аудио уже отправлено."""
    if meta.get("text_done", True):
        return
    audio = seg_dir / "audio.m4a"
    dur = duration(audio) if audio.exists() else 0
    started = datetime.fromtimestamp(meta["started"])
    label = meta.get("label") or "Встреча"
    title = f"{label} · {started:%d.%m %H:%M:%S}"
    async with _text_lock:
        if audio.exists() and dur >= 20 and want_text(chat) and (config.GROQ_API_KEY
                                                                 or config.DEEPGRAM_API_KEY):
            try:
                text = await asyncio.to_thread(transcribe, audio)
                if text:
                    safe = re.sub(r'[\\/:*?"<>|]+', " ", label).strip()
                    txt = seg_dir / f"Расшифровка {safe} {started:%d.%m %H-%M-%S}.txt"
                    # Расшифровка — только файлом, в подписи только чья она (текста в чате не нужно).
                    # Короткий конспект — в начале того же файла, не в чате.
                    try:
                        summary = await asyncio.to_thread(summarize, text)
                    except Exception:
                        log.exception("конспект %s", seg_dir)
                        summary = ""
                    head = f"{title} · {fmt_dur(dur)}\n"
                    if meta.get("names"):
                        head += "Участники: " + ", ".join(meta["names"]) + "\n"
                    if summary:
                        head += "\nКОНСПЕКТ\n" + summary + "\n"
                    txt.write_text(head + "\nРАСШИФРОВКА\n" + text + "\n", encoding="utf-8")
                    caption = f"📝 Расшифровка · {title}"
                    await send_doc(chat, txt, caption)
            except Exception:
                log.exception("расшифровка %s", seg_dir)
                await send_text(chat, f"⚠️ {label}: расшифровка не получилась — аудио выше")
        meta["text_done"] = True
        (seg_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def cleanup_old_recordings(now: float | None = None) -> list[Path]:
    """Удалить записи, которые давно доставлены (разрешение владельца 18.09.26: через REC_KEEP_DAYS).

    Трогаем только папки внутри REC_DIR, где meta.json говорит: аудио отправлено и расшифровка не
    ждёт досылки. Возраст — по времени последней записи meta.json (она пишется при отправке).
    Недоставленное, битое или непонятное не удаляется никогда.
    """
    if config.REC_KEEP_DAYS <= 0:
        return []
    now = now or time.time()
    limit = config.REC_KEEP_DAYS * 86400
    removed = []
    root = config.REC_DIR.resolve()
    for meta in sorted(config.REC_DIR.glob("*/meta.json")):
        d = meta.parent
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            if not m.get("sent") or m.get("text_done") is False:
                continue
            if now - meta.stat().st_mtime < limit:
                continue
            if d.resolve().parent != root:
                continue
            shutil.rmtree(d)
            removed.append(d)
            log.info("удалена запись старше %d дн.: %s", config.REC_KEEP_DAYS, d.name)
        except Exception:
            log.exception("очистка записи %s", d)
    return removed


def unfinished_segments() -> list[Path]:
    """Записи, где что-то не дошло: аудио (sent нет) или расшифровка (text_done=False)."""
    out = []
    for meta in sorted(config.REC_DIR.glob("*/meta.json")):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            if not m.get("sent") or m.get("text_done") is False:
                out.append(meta.parent)
        except (json.JSONDecodeError, OSError):
            continue
    return out

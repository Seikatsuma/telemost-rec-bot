"""Небольшое состояние на диске: хозяева бота, какие встречи бот сторожит, настройки чатов.
Пишется атомарно (tmp + rename), чтобы обрыв питания не оставил битый файл."""
import json
import os
import threading

import config

_lock = threading.Lock()


def load() -> dict:
    try:
        with open(config.STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("owners", [])
    data.setdefault("meetings", {})  # id -> {url, chat_id, added_at}
    data.setdefault("chats", {})  # chat_id -> {"text": bool}
    return data


def save(data: dict) -> None:
    with _lock:
        tmp = str(config.STATE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, config.STATE_FILE)


def update(fn) -> dict:
    data = load()
    fn(data)
    save(data)
    return data

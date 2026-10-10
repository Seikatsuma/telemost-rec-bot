"""Расшифровка переживает лимиты Groq: ждёт столько, сколько просит сервер, и не теряет куски.

Настоящий Groq не зовётся: requests.post подменён ответами-заглушками, ожидание не настоящее.
Запуск: venv/bin/python tests/test_groq_patience.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import postprocess as P  # noqa: E402

fails = 0


def check(name, ok, detail=""):
    global fails
    print(("OK   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    fails += (not ok)


class Resp:
    def __init__(self, code, retry_after=None, text="привет"):
        self.status_code, self.headers = code, ({"retry-after": str(retry_after)} if retry_after else {})
        self._text = text

    def json(self):
        return {"text": self._text}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def run(responses):
    seq, slept = list(responses), []
    P.requests.post = lambda *a, **k: seq.pop(0)
    with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
        try:
            return P._groq_asr(pathlib.Path(f.name), sleep=slept.append), slept
        except Exception as e:
            return e, slept


out, slept = run([Resp(200)])
check("сразу ответил — текст без ожидания", out == "привет" and not slept)

out, slept = run([Resp(429, 900), Resp(429, 900), Resp(200)])
check("часовой лимит: ждёт по 15 мин, как просит Groq, и получает текст",
      out == "привет" and slept == [900, 900], f"ждал {slept}")

out, slept = run([Resp(429, 5000), Resp(200)])
check("просят ждать больше 20 мин — ждёт 20 и пробует снова", out == "привет" and slept == [1200], f"{slept}")

out, slept = run([Resp(503)] * 3 + [Resp(200)])
check("временный сбой сервера без retry-after — короткие паузы и текст", out == "привет" and slept == [5, 10, 15], f"{slept}")

out, slept = run([Resp(429, 1200)] * 10)
check("занят дольше часа — сдаётся с понятной ошибкой, а не висит вечно",
      isinstance(out, RuntimeError) and "дольше 60 мин" in str(out) and sum(slept) <= P.ASR_PATIENCE_SEC,
      f"{out}; ждал {sum(slept):.0f} с")

out, slept = run([Resp(400)])
check("неверный запрос — сразу ошибка, без бессмысленных повторов", isinstance(out, RuntimeError) and not slept)

print("ИТОГ:", "всё прошло" if not fails else f"провалено {fails}")
sys.exit(1 if fails else 0)

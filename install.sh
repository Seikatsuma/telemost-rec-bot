#!/usr/bin/env bash
# Установка бота записи встреч Яндекс Телемоста. Запуск: ./install.sh (из папки бота).
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

say "1/5 Проверяю Python и ffmpeg"
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || fail "нет python3 (нужен 3.10 или новее)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail "нужен Python 3.10 или новее, сейчас $("$PY" --version)"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || fail "нет модуля venv: sudo apt install python3-venv"
command -v ffmpeg >/dev/null || fail "нет ffmpeg: sudo apt install ffmpeg"

say "2/5 Создаю окружение venv и ставлю библиотеки"
[ -d venv ] || "$PY" -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

say "3/5 Настройки"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Создан файл .env — впишите в него BOT_TOKEN и DEEPGRAM_API_KEY (или GROQ_API_KEY)."
else
    echo ".env уже есть — не трогаю."
fi

say "4/5 Скачиваю браузер Chromium для бота и проверяю, что он запускается"
venv/bin/playwright install chromium
# Проверка тем же способом, каким бот запускает браузер (учитывает BROWSER_SYSLIBS из .env).
if ! venv/bin/python - <<'EOF'
import asyncio
from playwright.async_api import async_playwright
from browser_env import browser_env
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, args=["--no-sandbox"], env=browser_env())
        await b.close()
asyncio.run(main())
EOF
then
    echo
    echo "Chromium не запускается — не хватает системных библиотек. Выполните один раз:"
    echo "    sudo $(pwd)/venv/bin/playwright install-deps chromium"
    echo "и запустите ./install.sh ещё раз. (Нет sudo — укажите папку с библиотеками в BROWSER_SYSLIBS в .env.)"
    exit 1
fi

say "5/5 Проверяю запись звука на локальном стенде (без Телемоста и Telegram)"
venv/bin/python tests/harness_capture.py | tail -1

cat <<EOF

Готово. Дальше:
  1. Впишите BOT_TOKEN и DEEPGRAM_API_KEY (или GROQ_API_KEY) в файл .env
  2. Запустите бота:              venv/bin/python bot.py
     или службой (перезапуск сам): см. README.md, раздел «Запуск службой»
  3. В Telegram напишите боту /start, затем /login и отсканируйте QR приложением Яндекс
  4. Нажмите «🎙 Новая встреча»
EOF

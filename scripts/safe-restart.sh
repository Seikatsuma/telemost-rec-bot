#!/usr/bin/env bash
# Перезапуск бота записи БЕЗ обрыва чужой записи.
# 18.09.26 перезапуск «вручную» пришёлся на середину встречи владельца: запись разбилась на две
# части и ~30 с разговора пропали. Этот скрипт перезапускает, только когда записи нет:
#   - записью считается кусок part*.webm, менявшийся за последние 3 минуты;
#   - по умолчанию ЖДЁТ окончания записи (проверка раз в минуту, до 6 часов), потом перезапускает;
#   - --no-wait: не ждать, а отказаться с кодом 3, если запись идёт.
set -u
cd "$(dirname "$0")/.."
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
WAIT=1; ACTION=restart
for a in "$@"; do
  [ "$a" = "--no-wait" ] && WAIT=0
  [ "$a" = "--stop" ] && ACTION=stop      # остановить (для работ с аккаунтом Яндекса), запуск потом вручную
  [ "$a" = "--start" ] && ACTION=start
done
if [ "$ACTION" = "start" ]; then
  systemctl --user start telemost-rec-bot; sleep 30
  echo "служба: $(systemctl --user is-active telemost-rec-bot)"; exit 0
fi

recording() { find data/rec -name 'part*.webm' -mmin -3 2>/dev/null | head -1; }

for i in $(seq 1 360); do
  busy=$(recording)
  [ -z "$busy" ] && break
  meeting=$(basename "$(dirname "$busy")" | cut -d- -f1)
  if [ "$WAIT" = 0 ]; then
    echo "ОТКАЗ: идёт запись встречи $meeting — перезапуск оборвал бы её. Запусти без --no-wait, чтобы дождаться."
    exit 3
  fi
  [ "$i" = 1 ] && echo "идёт запись встречи $meeting — жду её окончания (проверка раз в минуту)"
  sleep 60
done
[ -n "$(recording)" ] && { echo "ОТКАЗ: запись идёт уже 6 часов — перезапуск не выполнен"; exit 3; }

L=$(wc -l < logs/bot.log)
systemctl --user "$ACTION" telemost-rec-bot
[ "$ACTION" = "stop" ] && { echo "служба остановлена (запуск: scripts/safe-restart.sh --start)"; exit 0; }
sleep 45
echo "служба: $(systemctl --user is-active telemost-rec-bot)"
tail -n +$((L+1)) logs/bot.log | grep -E "запущен|в звонке|ERROR|Traceback" | cut -c1-120

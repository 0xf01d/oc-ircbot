#!/bin/sh
# smoke_local.sh — end-to-end round trip without Docker.
#
# Starts the mock IRC server (plaintext), the real bot.py, and brain.py with
# the stub brain; asserts the mock records the brain's PRIVMSG. This is the
# same seam the compose/CI smoke uses, just without containers.
set -eu
cd "$(dirname "$0")/.."
D=$(mktemp -d)
PORT=${SMOKE_PORT:-16667}

printf '# none\n' > "$D/bot.trust"
printf '# none\n' > "$D/bot.reserved"
: > "$D/bot.startup"
: > "$D/events.log"

python3 tests/mock_irc_server.py --port "$PORT" --inject "#test hello bot" \
    --out "$D/mock.out" --expect stub-ok --timeout 30 > "$D/mock.log" 2>&1 &
MOCK=$!
sleep 1

IRC_HOST=127.0.0.1 IRC_PORT="$PORT" IRC_TLS=0 IRC_NICK=ocbot IRC_IDENT=ocbot \
  BOT_LOG="$D/bot.log" BOT_FIFO="$D/bot.send" BOT_EVENTS="$D/events.log" \
  BOT_TRUST="$D/bot.trust" BOT_RESERVED="$D/bot.reserved" \
  BOT_RESERVED_STORE="$D/reserved.tsv" BOT_ENV="$D/.env" BOT_STARTUP="$D/bot.startup" \
  python3 -u bot.py > "$D/bot.stdout" 2>&1 &
BOT=$!

BOT_EVENTS="$D/events.log" BOT_FIFO="$D/bot.send" BRAIN_FROM_START=1 \
  BRAIN_POLL=0.3 BRAIN_BATCH_WINDOW=1 BRAIN_RATE=0 BRAIN_GLOBAL_RATE=0 \
  OC_BRAIN_CMD="python3 tests/stub_brain.py" \
  python3 -u brain.py > "$D/brain.log" 2>&1 &
BRAIN=$!

trap 'kill "$BOT" "$BRAIN" 2>/dev/null || true' EXIT INT TERM
wait "$MOCK"
echo "SMOKE PASS: mock recorded -> $(cat "$D/mock.out")"

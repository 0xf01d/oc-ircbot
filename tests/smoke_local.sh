#!/bin/sh
# smoke_local.sh — end-to-end round trip without Docker.
#
# Starts the mock IRC server (plaintext), the real bot.py, a stub opencode
# serve, and the REAL brain.py attached to it; asserts the mock records the
# brain's PRIVMSG. Same seam as the compose/CI smoke, just without containers:
# tail -> prompt_async -> SSE -> idle -> messages -> FIFO.
set -eu
cd "$(dirname "$0")/.."
D=$(mktemp -d)
PORT=${SMOKE_PORT:-16667}
SPORT=${SMOKE_SERVE_PORT:-46099}

printf '# none\n' > "$D/bot.trust"
printf '# none\n' > "$D/bot.reserved"
: > "$D/bot.startup"
: > "$D/events.log"
mkdir -p "$D/workspace"

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

python3 -u tests/stub_opencode_serve.py --host 127.0.0.1 --port "$SPORT" \
  > "$D/serve.log" 2>&1 &
SERVE=$!

BOT_EVENTS="$D/events.log" BOT_FIFO="$D/bot.send" IRC_NICK=ocbot \
  NETWORKS_FILE="$D/no-networks.json" \
  BRAIN_SERVE_URL="http://127.0.0.1:$SPORT" BRAIN_WORKDIR="$D/workspace" \
  BRAIN_MEMORY_DIR="$D/workspace/memory" BRAIN_HEARTBEAT="$D/hb" \
  BRAIN_AGENT_STATUS="$D/agent.status" BRAIN_FROM_START=1 \
  BRAIN_POLL=0.3 BRAIN_BATCH_WINDOW=1 \
  python3 -u brain.py > "$D/brain.log" 2>&1 &
BRAIN=$!

trap 'kill "$BOT" "$BRAIN" "$SERVE" 2>/dev/null || true' EXIT INT TERM
wait "$MOCK"
echo "SMOKE PASS: mock recorded -> $(cat "$D/mock.out")"

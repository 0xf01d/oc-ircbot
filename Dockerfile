# One Dockerfile, three final images — pick with --target (compose sets it):
#   docker build --target bot      -t oc-ircbot-bot .
#   docker build --target brain    -t oc-ircbot-brain .
#   docker build --target watchdog -t oc-ircbot-watchdog .
#
# bot and watchdog are stdlib-only Python (python:3.12-slim already ships
# ca-certificates); only the brain adds the Node-based OpenCode CLI.
# Containers run with compose `init: true` (tini as PID 1): zombies reaped,
# SIGTERM forwarded to the entrypoints.
#
# Every service runs INSIDE a detached tmux session (tmuxsup.py): attach to
# watch it live — `docker compose exec brain tmux attach -t brain` — while
# `docker logs` keeps working (pipe-pane mirrors the pane to stdout).

FROM python:3.12-slim AS runtime-base
WORKDIR /app
RUN mkdir -p /data
# ncurses-base already ships the tmux-256color terminfo entry.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tmux \
 && rm -rf /var/lib/apt/lists/*
ENV TERM=tmux-256color

FROM runtime-base AS bot
COPY bot.py leak_guard.py util.py nets.py networks.py bot.say tmuxsup.py ./
COPY bot.trust bot.trust.ircnet bot.reserved bot.startup ./
ENV BOT_LOG=/data/bot.log \
    BOT_FIFO=/data/bot.send \
    BOT_EVENTS=/data/events.log \
    BOT_RESERVED_STORE=/data/reserved_recent.tsv
# nets.py supervises the bot(s): restart backoff on a dead child, and a clean
# `QUIT` through every FIFO on SIGTERM — so `docker stop` parts the bot
# instead of dropping the connection. tmuxsup wraps it in the session.
CMD ["python3", "-u", "tmuxsup.py", "bot", "python3", "-u", "nets.py"]

FROM runtime-base AS watchdog
COPY watchdog.py util.py tmuxsup.py ./
ENV BRAIN_HEARTBEAT=/data/brain.heartbeat
CMD ["python3", "-u", "tmuxsup.py", "watchdog", "python3", "-u", "watchdog.py"]

# Persistent OpenCode sessions: Python (stdlib supervisor) + the Node-based
# OpenCode CLI. node:22-slim + apt python3 is fewer lines than the reverse
# (nodesource setup) and keeps the image small.
FROM node:22-slim AS brain
# This stage is not FROM runtime-base, so it re-states the /app layout the
# other images inherit; without it `COPY ./` lands code at / and the CI
# serve-bind check (`/app/tests/...`) finds nothing.
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates tmux openssh-client \
 && rm -rf /var/lib/apt/lists/*
# The OpenCode CLI (Node package). Pulls its own binary at install time.
RUN npm install -g opencode-ai
COPY brain.py policy.py networks.py memory_log.py util.py opencode.json tmuxsup.py ./
# tests/ ships the stub serve so the image is smoke-testable offline.
COPY tests/ ./tests/
ENV TERM=tmux-256color \
    BOT_EVENTS=/data/events.log \
    BOT_FIFO=/data/bot.send \
    BRAIN_WORKDIR=/data/workspace \
    BRAIN_MEMORY_DIR=/data/workspace/memory \
    BRAIN_HEARTBEAT=/data/brain.heartbeat \
    BRAIN_AGENT_STATUS=/data/agent.status \
    BRAIN_MODEL=opencode/deepseek-v4.1-flash-free
CMD ["python3", "-u", "tmuxsup.py", "brain", "python3", "-u", "brain.py"]

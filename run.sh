#!/usr/bin/env bash
# Supervisor loop for Joblin.
#
# Run this *inside* the bot's tmux window instead of launching the bot
# directly:
#
#     ./run.sh                          # new way
#     # uv run python -m joblin    # old way (no auto-update/restart)
#
# Each iteration pulls the latest code, syncs deps, and runs the bot in the
# foreground. When the bot stops, the loop pulls again and restarts it — so a
# redeploy is just "stop the bot and let the loop pick up the new code" (see
# ./redeploy.sh). Because everything stays in this one pane, the tmux
# scrollback (your log) is continuous across restarts.
#
#   - Ctrl-C in this window stops the bot AND this loop (full shutdown).
#   - ./redeploy.sh (or any signal to just the bot) -> pull + restart.
#   - A crash also triggers an automatic pull + restart after a short pause.
set -uo pipefail
cd "$(dirname "$0")"

# Ctrl-C / SIGINT: shut down for real instead of looping back around.
trap 'echo; echo "=== supervisor: SIGINT — shutting down ==="; exit 0' INT

while true; do
    echo "=== $(date '+%F %T') supervisor: git pull ==="
    git pull --ff-only || echo "!!! git pull failed — starting with the code on disk"

    echo "=== $(date '+%F %T') supervisor: uv sync ==="
    uv sync || echo "!!! uv sync failed — starting anyway"

    echo "=== $(date '+%F %T') supervisor: starting Joblin (Ctrl-C here to stop for good) ==="
    uv run python -m joblin
    code=$?

    echo "=== $(date '+%F %T') supervisor: Joblin exited (code $code); restarting in 3s ==="
    sleep 3
done

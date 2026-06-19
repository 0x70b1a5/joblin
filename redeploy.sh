#!/usr/bin/env bash
# Redeploy farmtracker: stop the running bot so the supervisor loop (run.sh)
# pulls the latest code and starts it again.
#
# Run this from anywhere on the VPS — you do NOT need to attach to tmux. The
# new logs appear in the bot's tmux window, continuous with the old ones.
#
# Requires the bot to have been launched with ./run.sh.
set -euo pipefail

# Match the bot process (this also covers the `uv run ...` launcher). The bash
# supervisor's own command line doesn't contain this string, so it survives and
# does the pull + restart.
if pkill -f 'python -m farmtracker'; then
    echo "Sent stop signal to farmtracker — run.sh will pull the latest code and restart it."
    echo "Watch it come back up with:  tmux attach   (then Ctrl-B D to detach)"
else
    echo "No running farmtracker process found."
    echo "Make sure it's running under ./run.sh in its tmux window."
    exit 1
fi

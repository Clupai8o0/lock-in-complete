#!/usr/bin/env bash
# Launches the orchestrator (main.py) and the Flask dashboard together.
# Ctrl-C cleanly stops both. Logs interleave on this terminal.

set -u

cd "$(dirname "$0")"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PY=${PYTHON:-python3}

pids=()
cleanup() {
  echo
  echo "[run.sh] stopping (${pids[*]})"
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait "${pids[@]}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "[run.sh] starting orchestrator (main.py)"
"$PY" main.py &
pids+=($!)

echo "[run.sh] starting dashboard (dashboard/app.py)"
"$PY" dashboard/app.py &
pids+=($!)

echo "[run.sh] PIDs: orchestrator=${pids[0]} dashboard=${pids[1]}"
echo "[run.sh] press Ctrl-C to stop both"

# Exit as soon as either child dies, so a crash takes the whole stack down
# instead of leaving an orphan running.
wait -n "${pids[@]}"

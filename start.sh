#!/bin/sh
set -eu

python saas_worker.py &
worker_pid=$!
gunicorn --workers 1 --threads 4 --bind "0.0.0.0:${PORT:-5051}" saas:app &
web_pid=$!

shutdown() {
  kill "$worker_pid" "$web_pid" 2>/dev/null || true
  wait "$worker_pid" "$web_pid" 2>/dev/null || true
}
trap shutdown EXIT INT TERM

wait "$web_pid"

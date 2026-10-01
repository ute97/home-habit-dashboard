#!/bin/sh
set -eu

mkdir -p /data
chown app:app /data
options_path=
if [ -f /data/options.json ]; then
  options_path=/tmp/habit-dashboard-options.json
  cp /data/options.json "$options_path"
  chown app:app "$options_path"
fi
for file in /data/habits.sqlite3 /data/habits.sqlite3-shm /data/habits.sqlite3-wal; do
  if [ -f "$file" ]; then
    chown app:app "$file"
  fi
done

if [ -n "$options_path" ]; then
  exec su-exec app:app env APP_OPTIONS_PATH="$options_path" python3 /app/server.py
fi
exec su-exec app:app python3 /app/server.py

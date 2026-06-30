#!/bin/bash
set -e

# Clean stale X lock files
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
mkdir -p /tmp/.X11-unix

# Start Xvfb
Xvfb :1 -screen 0 1280x800x24 -nolisten tcp &
sleep 1
export DISPLAY=:1

# VNC for initial interactive setup (disable with VNC_ENABLED=false after first boot)
if [ "${VNC_ENABLED:-true}" != "false" ]; then
    x11vnc -display :1 -passwd "${VNC_PASSWORD:-obsidian}" -listen 0.0.0.0 -forever -shared -noxdamage 2>/dev/null &
    echo "VNC available on port 5900 — password: ${VNC_PASSWORD:-obsidian}"
fi

echo "Starting Obsidian (user-data-dir=/obsidian-config)..."
exec /opt/obsidian/obsidian \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --user-data-dir=/obsidian-config

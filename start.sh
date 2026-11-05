#!/usr/bin/env bash
# DonJon — one-command launcher (Linux / PipeWire or PulseAudio)
set -e
cd "$(dirname "$0")"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

if [ ! -d ".venv" ]; then
  echo "→ creating Python environment"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r backend/requirements.txt

missing=0
for cmd in mixxx ffmpeg pactl Xvfb xdotool gcc convert; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "⚠ missing: $cmd"
    missing=1
  fi
done
if [ "$missing" = "1" ]; then
  echo ""
  echo "Install the missing pieces (Arch):"
  echo "  sudo pacman -S mixxx ffmpeg libpulse xorg-server-xvfb xdotool gcc imagemagick"
  echo ""
fi

if command -v ip >/dev/null 2>&1; then
  LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
else
  LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
LAN_IP=${LAN_IP:-127.0.0.1}
PORT=${DONJON_PORT:-8080}
if [ -z "${DONJON_ROOM:-}" ]; then
  DONJON_ROOM=$(shuf -i 100000-999999 -n 1 2>/dev/null || python -c 'import secrets;print(secrets.randbelow(900000)+100000)')
fi
export DONJON_ROOM

echo "============================================================"
echo "  DonJon — AI Party DJ + shared speakers"
echo "============================================================"
echo "  Host console : http://localhost:${PORT}"
echo "  Guests       : http://${LAN_IP}:${PORT}"
echo "  Room code    : ${DONJON_ROOM}"
echo ""
echo "  Open the host console on THIS machine. Guests open the LAN"
echo "  address on their phones and tap “Enable synced sound”."
echo "  Mixxx runs hidden in the background; close this terminal"
echo "  or press Ctrl+C to stop everything."
echo "============================================================"

export PYTHONUNBUFFERED=1
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}"

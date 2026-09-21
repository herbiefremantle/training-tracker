#!/bin/sh
# Start the tracker so a phone on the same Wi-Fi can open it. Laptop must stay on.
# (The normal command in the README only listens on this computer.)
cd "$(dirname "$0")" || exit 1
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
NAME=$(scutil --get LocalHostName 2>/dev/null)
echo
echo "  Open this on your phone (same Wi-Fi):"
[ -n "$IP" ]   && echo "    http://$IP:8000"
[ -n "$NAME" ] && echo "    http://$NAME.local:8000   (stays the same if your IP changes)"
[ -z "$IP" ] && [ -z "$NAME" ] && echo "    (couldn't detect an address - check System Settings > Wi-Fi > Details)"
echo
echo "  Anyone on this network can open it - there's no login. Ctrl-C to stop."
echo
# caffeinate -i: don't idle-sleep while the server runs (closing the lid still sleeps the Mac)
exec caffeinate -i .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

#!/bin/bash
# Ring receiver set-up on your Mac. Run it yourself: it makes the signing secret.
#   bash setup.sh            make the secret (once), start the service, open the tailnet address
#   bash setup.sh --copy     put the secret on the clipboard again for 2 minutes
#   bash setup.sh --status   show if the parts run
# The secret is never printed. It goes to the clipboard, and the iPhone can paste it (Universal Clipboard).
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
RING_HOME="${RING_HOME:-$HOME/.ring-router}"
PORT="${RING_PORT:-8791}"
HTTPS_PORT="${RING_HTTPS_PORT:-9446}"
LABEL="local.ring-router"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

copy_secret() {
  pbcopy < "$RING_HOME/secret"
  ( sleep 120; printf '' | pbcopy ) >/dev/null 2>&1 &
  echo "The secret is on the clipboard for 2 minutes. Paste it in the Pebble app: Sign requests."
}

address() {
  local name
  name="$(tailscale status --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
  echo "https://$name:$HTTPS_PORT/ring"
}

if [ "${1:-}" = "--copy" ]; then copy_secret; exit 0; fi
if [ "${1:-}" = "--status" ]; then
  launchctl list | grep -q "$LABEL" && echo "service: loaded" || echo "service: NOT loaded"
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null && echo "receiver: answers" || echo "receiver: NO answer"
  [ -s "$RING_HOME/secret" ] && echo "secret: present" || echo "secret: MISSING"
  ls "$RING_HOME/models"/*.bin >/dev/null 2>&1 && echo "speech model: present" || echo "speech model: MISSING"
  echo "address for the Pebble app: $(address)"
  echo "notes waiting: $(ls "$RING_HOME/inbox" 2>/dev/null | wc -l | tr -d ' ')"
  exit 0
fi

mkdir -p "$RING_HOME" && chmod 700 "$RING_HOME"
if [ ! -s "$RING_HOME/secret" ]; then
  ( umask 077; openssl rand -hex 32 > "$RING_HOME/secret" )
  echo "Made a new signing secret."
fi
chmod 600 "$RING_HOME/secret"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$REPO/receiver.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>RING_HOME</key><string>$RING_HOME</string>
    <key>RING_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$RING_HOME/launchd.err</string>
  <key>StandardOutPath</key><string>$RING_HOME/launchd.out</string>
</dict></plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$PORT" >/dev/null
sleep 1
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null && echo "The receiver runs." || echo "The receiver does NOT answer. See $RING_HOME/launchd.err"
echo "Address for the Pebble app (tailnet only): $(address)"
copy_secret

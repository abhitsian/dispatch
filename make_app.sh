#!/usr/bin/env bash
# Build a real /Applications/Dispatch.app bundle that wraps the source in
# ~/claude-apps/dispatch. Idempotent — re-run after edits to refresh the bundle
# (the launcher always runs the latest source; you only need to re-run this
# when the bundle metadata or launcher itself changes).
set -euo pipefail

APP_NAME="Dispatch"
# Source dir = wherever this script lives (the git clone). Derived, not
# hardcoded, so the bundle works no matter where the repo is cloned — the
# launcher execs THIS clone, so `git pull` is all it takes to be on latest.
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="${1:-/Applications}"
APP_BUNDLE="$INSTALL_ROOT/${APP_NAME}.app"
LOG_FILE="$HOME/Library/Logs/Dispatch.log"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "ERROR: source directory not found: $SOURCE_DIR" >&2
    exit 1
fi
if [ ! -x "$SOURCE_DIR/.venv/bin/python" ]; then
    echo "ERROR: venv python not found: $SOURCE_DIR/.venv/bin/python" >&2
    echo "  hint: cd $SOURCE_DIR && python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# Stop any currently-running instance so the new bundle can claim port 8765
# cleanly the first time it launches. Kill by port (robust) rather than by
# argv pattern — a process started with a relative `app.py` path won't match
# a "dispatch/app.py" pattern, which is how stale instances survived before.
for pid in $(lsof -ti :8765 2>/dev/null); do kill "$pid" >/dev/null 2>&1 || true; done
sleep 1

# Rebuild from scratch — small enough that this is faster than diffing.
if [ -d "$APP_BUNDLE" ]; then
    rm -rf "$APP_BUNDLE"
fi
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Ship the app icon. The thin launcher used to omit this, leaving a generic
# Dock/⌘-Tab icon — copy the committed .icns so the bundle is recognizable.
if [ -f "$SOURCE_DIR/Dispatch.icns" ]; then
    cp "$SOURCE_DIR/Dispatch.icns" "$APP_BUNDLE/Contents/Resources/Dispatch.icns"
else
    echo "WARN: Dispatch.icns not found in $SOURCE_DIR — bundle will have a generic icon" >&2
fi

# ---------- Info.plist ----------
cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>Dispatch</string>
    <key>CFBundleIdentifier</key>
    <string>com.vaibhav.dispatch</string>
    <key>CFBundleName</key>
    <string>Dispatch</string>
    <key>CFBundleDisplayName</key>
    <string>Dispatch</string>
    <key>CFBundleIconFile</key>
    <string>Dispatch.icns</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <!--
      LSUIElement was true earlier (menu-bar-only), but macOS 26 with notch
      eats the icon into overflow. We ship as a regular Dock app + auto-open
      a browser dashboard, so the user has a real visible surface.
    -->
    <key>NSMicrophoneUsageDescription</key>
    <string>Dispatch needs microphone access so you can transmit radio messages to your Claude sessions.</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <false/>
    <key>NSSupportsSuddenTermination</key>
    <false/>
</dict>
</plist>
EOF

# ---------- launcher ----------
# IMPORTANT: when launched via Launch Services (double-click, Spotlight, open),
# the environment is minimal — no \$PATH inheritance from the user's shell.
# We rebuild PATH so ffmpeg / whisper / claude / say can all be found at
# runtime by subprocess.run() inside the python app.
cat > "$APP_BUNDLE/Contents/MacOS/Dispatch" <<EOF
#!/bin/bash
set -u
export HOME="\${HOME:-$HOME}"
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:\$HOME/.local/bin"
export LANG="\${LANG:-en_US.UTF-8}"
export LC_ALL="\${LC_ALL:-en_US.UTF-8}"

LOG="\$HOME/Library/Logs/Dispatch.log"
mkdir -p "\$(dirname "\$LOG")"

# breadcrumb so failures are diagnosable from the log
{ echo "----"; echo "launched \$(date) PATH=\$PATH"; } >> "\$LOG" 2>&1

if /usr/bin/curl -fsS --max-time 1 http://127.0.0.1:8765/health >/dev/null 2>&1; then
    /usr/bin/osascript -e 'display notification "Dispatch is already running — look for the icon in the menu bar." with title "Dispatch"' >/dev/null 2>&1 || true
    echo "already running (health check OK), exiting" >> "\$LOG"
    exit 0
fi

echo "execing $SOURCE_DIR/.venv/bin/python $SOURCE_DIR/app.py" >> "\$LOG"
exec "$SOURCE_DIR/.venv/bin/python" -u "$SOURCE_DIR/app.py" >> "\$LOG" 2>&1
EOF
chmod +x "$APP_BUNDLE/Contents/MacOS/Dispatch"

# Nudge Launch Services so Spotlight + Finder pick the new bundle up.
touch "$APP_BUNDLE"
/usr/bin/lsregister -f "$APP_BUNDLE" >/dev/null 2>&1 || \
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP_BUNDLE" >/dev/null 2>&1 || true

echo "built  $APP_BUNDLE"
echo "logs   $LOG_FILE"
echo
echo "launch via Spotlight ('Dispatch') or double-click in $INSTALL_ROOT."

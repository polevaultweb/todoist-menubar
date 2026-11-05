#!/bin/sh
# Set up the virtualenv and a launch agent that starts the app at login and restarts it if it crashes.
# Re-run after moving the folder. PYTHON=/path/to/python3 and LABEL=com.example.todoist-menubar override the defaults.
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
PYTHON=${PYTHON:-python3}
LABEL=${LABEL:-todoist-menubar}
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/todoist-menubar.log"

if [ ! -x "$DIR/.venv/bin/python3" ]; then
    echo "Creating virtualenv with $("$PYTHON" --version)"
    "$PYTHON" -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install --quiet --upgrade -r "$DIR/requirements.txt"

if [ -z "${TODOIST_API_TOKEN:-}" ] && ! security find-generic-password -s todoist-api-token >/dev/null 2>&1; then
    echo "No Todoist token found. Add one (Todoist Settings > Integrations > Developer > API token):"
    echo "  security add-generic-password -a \"\$USER\" -s todoist-api-token -w"
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/.venv/bin/python3</string>
        <string>$DIR/todoist_menubar.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Running. Log: $LOG"
echo "Restart after changes: launchctl kickstart -k gui/\$(id -u)/$LABEL"

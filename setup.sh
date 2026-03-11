#!/bin/bash
# CISO Approval Triage Bot — Setup Script
# Creates venv, installs deps, copies env template, creates launchd plist

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up CISO Approval Triage Bot..."

# Create virtual environment
if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

echo "Installing dependencies..."
source venv/bin/activate
pip install -r requirements.txt

# Copy env template if .env doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env file — please fill in your API tokens"
fi

# Create logs directory
mkdir -p "$SCRIPT_DIR/logs"

# Create prompts directory if missing
mkdir -p "$SCRIPT_DIR/prompts"

SERVICE_NAME="com.ciso-approval-bot"

# Create launchd plist
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/${SERVICE_NAME}.plist << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/venv/bin/python3</string>
        <string>${SCRIPT_DIR}/bot.py</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/logs/bot.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/logs/bot_error.log</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
PLIST

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit .env with your API tokens"
echo "2. Load the service: launchctl load ~/Library/LaunchAgents/${SERVICE_NAME}.plist"
echo "3. Check logs: tail -f ${SCRIPT_DIR}/logs/bot.log"
echo ""
echo "To stop: launchctl unload ~/Library/LaunchAgents/${SERVICE_NAME}.plist"

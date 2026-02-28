#!/usr/bin/env bash
set -euo pipefail

# Deploy bot Android app to phone via Tailscale + Termux ADB
# Usage: ./android/deploy.sh [--skip-build]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKIP_BUILD=false

for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=true ;;
  esac
done

export ANDROID_HOME="${ANDROID_HOME:-/home/kafar/android-sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

PHONE_IP="100.93.228.102"
PHONE_PORT="8022"
ADB="adb -s localhost:5555"
SSH="ssh -p $PHONE_PORT $PHONE_IP"
PACKAGE="com.claudebot.app"

# --- Build ---
if [ "$SKIP_BUILD" = false ]; then
  echo "==> Building debug APK..."
  cd "$SCRIPT_DIR"
  ./gradlew assembleDebug
fi

APK="$SCRIPT_DIR/app/build/outputs/apk/debug/app-debug.apk"
if [ ! -f "$APK" ]; then
  echo "ERROR: APK not found at $APK"
  exit 1
fi
echo "==> APK: $APK ($(du -h "$APK" | cut -f1))"

# --- Send to phone, install, launch ---
PHONE_DEST="/sdcard/Download/claudebot.apk"

echo "==> Sending APK to phone via SCP..."
scp -P "$PHONE_PORT" "$APK" "$PHONE_IP:$PHONE_DEST"

echo "==> Installing via ADB (Termux)..."
$SSH "$ADB install -r $PHONE_DEST" 2>&1 | tail -3

echo "==> Launching app..."
$SSH "$ADB shell am force-stop $PACKAGE" 2>/dev/null || true
$SSH "$ADB shell monkey -p $PACKAGE -c android.intent.category.LAUNCHER 1" 2>/dev/null

echo "==> Deploy complete!"

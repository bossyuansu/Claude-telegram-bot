#!/usr/bin/env bash
set -euo pipefail

# Run Android instrumentation e2e tests on a Gradle managed device.
# Usage:
#   ./android/run-e2e.sh
#   ./android/run-e2e.sh com.claudebot.app.ui.SessionFilterE2ETest
#   ./android/run-e2e.sh com.claudebot.app.ui.SessionFilterE2ETest#switchFilter_thenBackToAll_restoresAllMessages

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

DEVICE="${E2E_DEVICE:-pixel2api30}"
TASK=":app:${DEVICE}DebugAndroidTest"
SELECTOR="${1:-}"

ARGS=("$TASK")
if [[ -n "$SELECTOR" ]]; then
  ARGS+=("-Pandroid.testInstrumentationRunnerArguments.class=$SELECTOR")
fi

echo "==> Running managed-device e2e tests on $DEVICE"
if [[ -n "$SELECTOR" ]]; then
  echo "==> Test selector: $SELECTOR"
fi

./gradlew "${ARGS[@]}"

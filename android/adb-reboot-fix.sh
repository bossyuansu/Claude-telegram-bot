#!/usr/bin/env bash
# Re-enable ADB TCP mode after phone reboot.
# Requires phone to be USB-connected to this Windows/WSL machine.
# Then establishes the SSH tunnel for wireless ADB.
#
# Usage: ./android/adb-reboot-fix.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== ADB Reboot Recovery ==="

# Step 1: Check if Windows ADB sees the phone via USB
echo "[1/3] Checking USB connection via Windows ADB..."
USB_OUTPUT=$(cd /mnt/c && /mnt/c/Windows/System32/cmd.exe /c "adb devices" 2>&1 | grep -E "^\S+\s+device$" || true)

if [ -z "$USB_OUTPUT" ]; then
    echo "ERROR: No phone detected via USB."
    echo "  - Is the phone plugged in via USB?"
    echo "  - Is USB debugging enabled on the phone?"
    echo "  - Did you approve the 'Allow USB debugging' prompt on the phone?"
    exit 1
fi

DEVICE_ID=$(echo "$USB_OUTPUT" | awk '{print $1}')
echo "  Found device: $DEVICE_ID"

# Step 2: Enable ADB TCP mode
echo "[2/3] Enabling ADB TCP mode (port 5555)..."
TCP_OUTPUT=$(cd /mnt/c && /mnt/c/Windows/System32/cmd.exe /c "adb tcpip 5555" 2>&1)

if echo "$TCP_OUTPUT" | grep -q "restarting in TCP mode"; then
    echo "  ADB TCP mode enabled successfully."
else
    echo "ERROR: Failed to enable TCP mode:"
    echo "  $TCP_OUTPUT"
    exit 1
fi

# Step 3: Wait briefly for adbd to restart
echo "[3/4] Waiting for adbd to restart..."
sleep 2

# Step 4: Connect phone's own Termux ADB to itself
echo "[4/5] Connecting Termux ADB to local port..."
PHONE_TS_IP="100.93.228.102"
PHONE_SSH_PORT="8022"
ssh -o ConnectTimeout=5 -p "$PHONE_SSH_PORT" "$PHONE_TS_IP" "adb connect 127.0.0.1:5555" 2>&1 || true

# Verify Termux ADB sees the device
TERMUX_DEVICES=$(ssh -o ConnectTimeout=5 -p "$PHONE_SSH_PORT" "$PHONE_TS_IP" "adb devices" 2>&1)
if echo "$TERMUX_DEVICES" | grep -q "127.0.0.1:5555"; then
    echo "  Termux ADB connected to device."
else
    echo "WARNING: Termux ADB didn't connect. Deploy script may not work."
    echo "  $TERMUX_DEVICES"
fi

# Step 5: Establish SSH tunnel for remote ADB access
echo "[5/5] Establishing SSH tunnel..."
bash "$SCRIPT_DIR/adb-tunnel.sh"

echo ""
echo "=== Done! Phone is ready for wireless ADB. You can unplug USB. ==="

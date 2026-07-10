#!/usr/bin/env bash
# Establish ADB connection to phone via SSH tunnel over Tailscale.
# Works regardless of which WiFi network the phone is on.
#
# Usage: ./android/adb-tunnel.sh          # connect
#        ./android/adb-tunnel.sh stop     # disconnect

PHONE_TS_IP="100.93.228.102"
PHONE_SSH_PORT="8022"
LOCAL_ADB_PORT="5556"
REMOTE_ADB_PORT="5555"

stop_tunnel() {
    # Kill existing SSH tunnel
    pkill -f "ssh.*-L ${LOCAL_ADB_PORT}:127.0.0.1:${REMOTE_ADB_PORT}.*${PHONE_TS_IP}" 2>/dev/null
    adb disconnect "127.0.0.1:${LOCAL_ADB_PORT}" 2>/dev/null
    echo "ADB tunnel stopped."
}

if [ "${1:-}" = "stop" ]; then
    stop_tunnel
    exit 0
fi

# Check Tailscale connectivity
if ! tailscale ping "$PHONE_TS_IP" -c 1 --timeout 5s >/dev/null 2>&1; then
    # Fallback: try basic ping
    if ! timeout 3 bash -c "echo >/dev/tcp/${PHONE_TS_IP}/${PHONE_SSH_PORT}" 2>/dev/null; then
        echo "ERROR: Phone ($PHONE_TS_IP) is unreachable. Is Tailscale running on the phone?"
        exit 1
    fi
fi

# Kill stale tunnel if any
stop_tunnel

# Check if ADB TCP mode is active on phone
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes -p "$PHONE_SSH_PORT" "$PHONE_TS_IP" "true" 2>/dev/null; then
    echo "ERROR: Cannot SSH to phone (${PHONE_TS_IP}:${PHONE_SSH_PORT}). Is Termux SSH running?"
    exit 1
fi

# Create SSH tunnel: local:5556 -> phone:localhost:5555
ssh -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -f -N -L "${LOCAL_ADB_PORT}:127.0.0.1:${REMOTE_ADB_PORT}" \
    -p "$PHONE_SSH_PORT" "$PHONE_TS_IP" 2>&1

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to create SSH tunnel."
    exit 1
fi

# Connect ADB through tunnel
sleep 1
if timeout 8 adb connect "127.0.0.1:${LOCAL_ADB_PORT}" 2>&1 | grep -q "connected"; then
    echo "ADB connected via SSH tunnel (127.0.0.1:${LOCAL_ADB_PORT} -> phone:${REMOTE_ADB_PORT})"
    adb -s "127.0.0.1:${LOCAL_ADB_PORT}" shell getprop ro.product.model
else
    echo "ERROR: ADB connect failed. Is 'adb tcpip 5555' enabled on the phone?"
    echo "You may need to plug in USB and run: adb tcpip 5555"
    stop_tunnel
    exit 1
fi

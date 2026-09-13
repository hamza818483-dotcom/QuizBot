#!/bin/sh
# 2026-09-13: Best-effort Cloudflare WARP SOCKS5 proxy starter.
#
# Why this exists: HuggingFace Space's free tier has an unstable outbound
# connection to YouTube's CDN (confirmed root cause of persistent
# 'SSL: UNEXPECTED_EOF_WHILE_READING' errors on /cut <yt-link> -- fails
# even on the very first webpage fetch, on every retry). Cloudflare WARP
# is a genuinely free VPN (registers itself, no account/payment) whose
# IPs YouTube does not blacklist the way it blocks most datacenter
# proxies. This script tries to bring up a local SOCKS5 proxy at
# 127.0.0.1:40000 for yt-dlp to use.
#
# NON-FATAL BY DESIGN: HF Space containers may lack the NET_ADMIN
# capability WARP's tunnel needs. If ANY step here fails, this script
# just exits -- it never blocks or crashes the main app (started
# alongside it, not after it, in the Dockerfile CMD). app.py checks for
# the ready-marker file below at request time and falls back to a direct
# connection if it's missing, so /cut works exactly as before this
# script existed when WARP isn't available in this environment.

REDIR() { echo "[warp] $*"; }

READY_FILE="/app/data/warp_ready"
rm -f "$READY_FILE" 2>/dev/null

if ! command -v warp-cli >/dev/null 2>&1; then
    REDIR "warp-cli not found on PATH -- WARP was not installed at build time, skipping."
    exit 0
fi
REDIR "warp-cli found: $(command -v warp-cli)"

# warp-cli is only a client; it talks to the warp-svc daemon over a
# local socket. The daemon is not started automatically by installing
# the package -- it must be launched here as a background process
# before any warp-cli subcommand will succeed.
if command -v warp-svc >/dev/null 2>&1; then
    REDIR "starting warp-svc daemon..."
    warp-svc >/app/logs/warp-svc.log 2>&1 &
    WARP_SVC_PID=$!
    # Give the daemon a moment to create its socket before the CLI hits it.
    for i in 1 2 3 4 5; do
        if warp-cli --accept-tos status >/dev/null 2>&1; then
            REDIR "warp-svc daemon is responding (pid $WARP_SVC_PID)."
            break
        fi
        sleep 1
    done
else
    REDIR "warp-svc binary not found -- cannot start daemon, aborting."
    exit 0
fi

# Idempotent registration -- 'registration new' errors harmlessly if a
# registration already exists (persisted under /var/lib/cloudflare-warp
# if that path happens to be a mounted volume; ephemeral otherwise, which
# is fine, it just re-registers on the next container start).
REDIR "registering..."
warp-cli --accept-tos registration new 2>&1 | sed 's/^/[warp] /'

REDIR "setting proxy mode..."
warp-cli --accept-tos mode proxy 2>&1 | sed 's/^/[warp] /'
warp-cli --accept-tos proxy port 40000 2>&1 | sed 's/^/[warp] /'

REDIR "connecting..."
warp-cli --accept-tos connect 2>&1 | sed 's/^/[warp] /'

# Give the tunnel a few seconds to come up, then verify it actually
# routes through Cloudflare before telling app.py it's usable.
for i in 1 2 3 4 5 6; do
    sleep 2
    STATUS=$(warp-cli --accept-tos status 2>&1)
    REDIR "status check $i: $STATUS"
    TRACE=$(curl -s --max-time 3 -x socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace 2>&1)
    if echo "$TRACE" | grep -q "warp=on"; then
        mkdir -p /app/data
        echo "socks5://127.0.0.1:40000" > "$READY_FILE"
        REDIR "WARP is up and routing -- ready file written."
        exit 0
    fi
    REDIR "trace check $i did not show warp=on yet: $TRACE"
done
# Never came up cleanly -- leave READY_FILE absent, app.py falls back to
# direct connection.
REDIR "WARP never came up cleanly after retries -- falling back to direct connection."
exit 0

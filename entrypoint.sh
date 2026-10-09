#!/bin/bash
set -e

# The manager always uses the browser bundled with this release. Environment
# overrides must not silently replace it or restart SDK background updates.
export CLOAKBROWSER_BINARY_PATH=/opt/cbm/cloakbrowser/chrome
export CLOAKBROWSER_AUTO_UPDATE=false
export CLOAKBROWSER_VERSION="$(python -c 'import json; print(json.load(open("/opt/cbm/browser-versions.json"))["cloakbrowser"]["version"])')"

if [ -z "${KEEPASSXC_DATABASE_PASSWORD:-}" ]; then
    echo "ERROR: KEEPASSXC_DATABASE_PASSWORD must be set to a non-empty value." >&2
    echo "Set it in docker-compose.yml or pass -e KEEPASSXC_DATABASE_PASSWORD=... to docker run." >&2
    exit 1
fi

# Initialize data directories
mkdir -p /data/profiles
mkdir -p /tmp/cbm
chmod 700 /tmp/cbm

# Kill stale processes from previous container runs
pkill -f 'Xvnc :[0-9]' 2>/dev/null || true
pkill -f 'cloakbrowser.*chrome' 2>/dev/null || true
pkill -f 'chromium.*fingerprint' 2>/dev/null || true
pkill -f 'keepassxc.*?/data/profiles/' 2>/dev/null || true
pkill -f keepassxc-proxy 2>/dev/null || true
pkill -f xclip 2>/dev/null || true

# Clean Chrome lock files left on the persistent volume
find /data/profiles -maxdepth 2 -name 'SingletonLock' -delete 2>/dev/null || true
find /data/profiles -maxdepth 2 -name 'SingletonCookie' -delete 2>/dev/null || true
find /data/profiles -maxdepth 2 -name 'SingletonSocket' -delete 2>/dev/null || true

# Remove X11 lock files from previous displays
rm -f /tmp/.X1*-lock 2>/dev/null || true
find /tmp/cbm -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null || true

# Start FastAPI (serves built React + API)
cd /app
echo ""
echo "  CloakBrowser Manager running at http://localhost:8080"
echo ""
exec uvicorn backend.main:app --host 0.0.0.0 --port 8080 --log-level warning

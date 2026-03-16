#!/bin/bash
# Emergency restore: reinstalls clean indicatorStatusIcon.js from the Ubuntu package.
# Run this when AppIndicator tray icons have disappeared due to a bad patch.
#
# Usage: bash emergency_restore.sh

set -e

TARGET="/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com/indicatorStatusIcon.js"
PACKAGE="gnome-shell-extension-appindicator"
TMP_DEB="/tmp/panel_arranger_emergency.deb"
TMP_DIR="/tmp/panel_arranger_emergency"

echo "=== Panel Arranger emergency restore ==="
echo ""
echo "Current file state:"
grep -c "panel-arranger" "$TARGET" 2>/dev/null \
    && echo "  !! $TARGET contains panel-arranger patches (this is the problem)" \
    || echo "  OK — no panel-arranger markers found"
echo ""

echo "Downloading clean package..."
apt-get download "$PACKAGE" -o Dir::Cache::Archives="$TMP_DIR" 2>/dev/null \
    || { mkdir -p "$TMP_DIR"; apt-get download "$PACKAGE" 2>&1 | tail -1; }

# apt-get download puts the .deb in the current dir; move it
DEB=$(find . "$TMP_DIR" -maxdepth 2 -name "${PACKAGE}_*.deb" 2>/dev/null | head -1)
if [ -z "$DEB" ]; then
    echo "ERROR: Could not find downloaded .deb. Try running manually:"
    echo "  apt-get download $PACKAGE && dpkg-deb --fsys-tarfile *.deb | tar -xf - ./usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com/indicatorStatusIcon.js"
    exit 1
fi
echo "Found: $DEB"

echo "Extracting original indicatorStatusIcon.js..."
mkdir -p "$TMP_DIR/extract"
dpkg-deb --fsys-tarfile "$DEB" \
    | tar -xf - -C "$TMP_DIR/extract" \
    "./usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com/indicatorStatusIcon.js"

EXTRACTED="$TMP_DIR/extract/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com/indicatorStatusIcon.js"

echo "Verifying extracted file..."
if grep -q "panel-arranger" "$EXTRACTED"; then
    echo "ERROR: Extracted file still contains panel-arranger markers. Something is wrong."
    exit 1
fi
echo "  OK — extracted file is clean"

echo ""
echo "Copying clean file (requires authentication)..."
pkexec cp "$EXTRACTED" "$TARGET"

echo ""
echo "SUCCESS. indicatorStatusIcon.js has been restored to the original package version."
echo ""
echo "Log out and back in (or run: gnome-shell --replace &) to reload GNOME Shell."
echo ""
echo "Cleaning up..."
rm -rf "$TMP_DIR" "$DEB" 2>/dev/null || true
echo "Done."

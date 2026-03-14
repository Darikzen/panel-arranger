#!/bin/bash
# Install Panel Arranger
set -e

INSTALL_DIR="/opt/panel-arranger"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "Installing Panel Arranger..."

sudo mkdir -p "$INSTALL_DIR"
sudo cp panel_arranger.py "$INSTALL_DIR/"
sudo chmod 755 "$INSTALL_DIR/panel_arranger.py"

mkdir -p "$DESKTOP_DIR"
cp panel-arranger.desktop "$DESKTOP_DIR/"
# Fix path in desktop file
sed -i "s|Exec=.*|Exec=python3 $INSTALL_DIR/panel_arranger.py|" "$DESKTOP_DIR/panel-arranger.desktop"

update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo "Done! You can launch 'Panel Arranger' from your app menu,"
echo "or run: python3 $INSTALL_DIR/panel_arranger.py"

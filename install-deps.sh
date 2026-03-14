#!/usr/bin/env bash
# Install system dependencies for panel-arranger on Ubuntu 24.04 / Debian.
set -e

sudo apt install -y \
    python3 \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gtk-4.0 \
    gir1.2-adw-1 \
    gnome-shell-extensions \
    policykit-1

echo "Done. Run with: python3 panel_arranger.py"

#!/bin/bash
set -e

if [[ $EUID -ne 0 ]]; then
   echo "Please run as root (use sudo)."
   exit 1
fi

echo "Removing ruv-gui..."
rm -f /usr/local/bin/ruv-gui
rm -f /usr/share/applications/ruv-gui.desktop
rm -f /usr/share/icons/hicolor/256x256/apps/ruv-gui.png

# Remove systemd boot service if present
if systemctl list-unit-files | grep -q ruv-boot.service; then
    echo "Disabling and removing boot service..."
    systemctl disable ruv-boot.service 2>/dev/null || true
    rm -f /etc/systemd/system/ruv-boot.service
    systemctl daemon-reload
fi

# Remove all profiles and configuration
read -p "Remove all saved profiles and configuration in /etc/ruv? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf /etc/ruv
fi

echo "Uninstall complete."

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

# Remove man page
if [ -f /usr/share/man/man1/ruv.1.gz ]; then
    rm -f /usr/share/man/man1/ruv.1.gz
    echo "Man page removed."
fi

# Update icon cache (optional but good practice)
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

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

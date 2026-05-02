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
if [ -f /usr/share/man/man1/ruv-gui.1.gz ]; then
    rm -f /usr/share/man/man1/ruv-gui.1.gz
    echo "Man page removed."
fi
if [ -f /usr/share/man/man1/ruv.1.gz ]; then
    rm -f /usr/share/man/man1/ruv.1.gz
    echo "Legacy man page removed."
fi

# Update icon cache
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

# Remove systemd boot service if present
if systemctl is-enabled ruv-boot.service >/dev/null 2>&1 || [ -f /etc/systemd/system/ruv-boot.service ]; then
    echo "Disabling and removing boot service..."
    systemctl disable ruv-boot.service 2>/dev/null || true
    systemctl stop ruv-boot.service 2>/dev/null || true
    rm -f /etc/systemd/system/ruv-boot.service
    systemctl daemon-reload
fi

# CORRECTED: cache file is now in /var/cache/ruv/
CACHE_FILE="/var/cache/ruv/co_cache.json"
CACHE_DIR="/var/cache/ruv"
if [ -f "$CACHE_FILE" ]; then
    echo "Removing Curve Optimizer cache..."
    rm -f "$CACHE_FILE"
    # remove the cache directory if it's empty
    if [ -d "$CACHE_DIR" ] && [ -z "$(ls -A "$CACHE_DIR")" ]; then
        rmdir "$CACHE_DIR" 2>/dev/null || true
    fi
fi

# Remove all profiles and configuration
read -p "Remove all saved profiles, configuration in /etc/ruv, and cache? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf /etc/ruv
    # also remove any remaining cache
    rm -rf /var/cache/ruv
fi

echo "Uninstall complete."

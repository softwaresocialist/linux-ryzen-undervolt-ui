#!/bin/bash
set -e

if [[ $EUID -ne 0 ]]; then
   echo "Please run as root (use sudo)."
   exit 1
fi

echo "Removing ruv-gui..."
rm -f /usr/local/bin/ruv-gui
rm -f /usr/share/applications/ruv-gui.desktop
rm -f /usr/share/icons/hicolor/scalable/apps/ruv-gui.svg
rm -f /usr/share/polkit-1/actions/com.softwaresocialist.ruv.policy

# Optionally remove profiles
read -p "Remove all saved profiles in /etc/ruv/profiles? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf /etc/ruv
fi

echo "Uninstall complete."

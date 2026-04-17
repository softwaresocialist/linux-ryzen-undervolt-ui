#!/bin/bash
set -e

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)."
   exit 1
fi

echo "Checking dependencies..."
command -v python3 >/dev/null 2>&1 || { echo "Python3 is required. Abort."; exit 1; }
python3 -c "import PyQt6" 2>/dev/null || { echo "PyQt6 is not installed."; exit 1; }
command -v pkexec >/dev/null 2>&1 || { echo "pkexec (polkit) is required."; exit 1; }

echo "Checking for ryzen_smu driver..."
if [ ! -f "/sys/kernel/ryzen_smu_drv/version" ]; then
    echo "WARNING: ryzen_smu driver is not loaded."
    echo "The tool will not work until the driver is installed and loaded."
    echo "Please install the driver from: https://github.com/amkillam/ryzen_smu"
    echo "Then load it with: sudo modprobe ryzen_smu"
    echo "Press Enter to continue installation anyway, or Ctrl+C to abort."
    read -r
else
    echo "ryzen_smu driver detected."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Install main script
echo "Installing ruv-gui script..."
install -Dm 755 "$SCRIPT_DIR/ruv_gui.py" /usr/local/bin/ruv-gui

# Desktop integration
echo "Installing desktop file and icon..."
install -Dm 644 "$SCRIPT_DIR/ruv-gui.desktop" /usr/share/applications/ruv-gui.desktop
if [ -f "$SCRIPT_DIR/ruv-gui.png" ]; then
    install -Dm 644 "$SCRIPT_DIR/ruv-gui.png" /usr/share/icons/hicolor/256x256/apps/ruv-gui.png
    gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

# Install man page
echo "Installing man page..."
if [ -f "$SCRIPT_DIR/ruv.1" ]; then
    install -Dm 644 "$SCRIPT_DIR/ruv.1" /usr/share/man/man1/ruv.1
    gzip -f /usr/share/man/man1/ruv.1
    echo "Man page installed as /usr/share/man/man1/ruv.1.gz"
else
    echo "Warning: ruv.1 not found in $SCRIPT_DIR – skipping man page installation."
fi

echo "Installation complete!"
echo "You can now run 'ruv-gui' from the application menu or 'ruv' from the terminal."
echo "View the manual with: man ruv"

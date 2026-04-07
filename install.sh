#!/bin/bash
set -e

echo "=== Ryzen Undervolt Tool Installer (GUI only) ==="

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)." 
   exit 1
fi

# Check dependencies
echo "Checking dependencies..."
command -v python3 >/dev/null 2>&1 || { echo "Python3 is required. Abort."; exit 1; }
python3 -c "import PyQt6" 2>/dev/null || { echo "PyQt6 is not installed. Run: pip install PyQt6"; exit 1; }
command -v pkexec >/dev/null 2>&1 || { echo "pkexec (polkit) is required. Install polkit package."; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Install main script
echo "Installing ruv-gui script..."
install -Dm 755 "$SCRIPT_DIR/ruv_gui.py" /usr/local/bin/ruv-gui

# Desktop integration
echo "Installing desktop file and icon..."
install -Dm 644 "$SCRIPT_DIR/ruv-gui.desktop" /usr/share/applications/ruv-gui.desktop
if [ -f "$SCRIPT_DIR/ruv-gui.svg" ]; then
    install -Dm 644 "$SCRIPT_DIR/ruv-gui.png" /usr/share/icons/hicolor/scalable/apps/ruv-gui.svg
    gtk-update-icon-cache -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

echo "Installation complete!"

# Linux Ryzen Undervolt UI

Early development – expect bugs. AI‑assisted code.

## What is it?

A Linux GUI to set per‑core voltage offsets (undervolt) for Ryzen CPUs via the `ryzen_smu` kernel driver.

## Prerequisites

- Ryzen CPU (3000 series or newer)
- Python 3.8+ with PyQt6 installed (`pip install PyQt6`)
- polkit (`pkexec`) – usually preinstalled
- The `ryzen_smu` kernel driver (install separately, see step 1)

## Installation

### 1. Install the ryzen_smu driver

```bash
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu
sudo make dkms-install
reboot
```
2. Install the GUI tool
bash

git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
sudo ./install.sh

The installer copies the script to /usr/local/bin/ruv-gui, adds a desktop file and icon, installs a Polkit policy (password once per session), and creates /etc/ruv/profiles.
3. Run the GUI

From the application menu: "Ryzen Undervolt Tool"
Or terminal: 
```bash
ruv-gui
```
Uninstall
```bash
sudo ./uninstall.sh
```
Usage

    Set an offset in mV (e.g., -15)

    Select cores

    Click "Apply to Selected Cores"

    Use profiles to save/load settings

    "Set as Boot Profile" creates a systemd service to apply at startup

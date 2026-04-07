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

https://github.com/amkillam/ryzen_smu

## 2. Install the GUI tool
```bash
git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
sudo ./install.sh
```
## 3. Run the GUI

From the application menu: "Ryzen Undervolt Tool"
Or terminal: 
```bash
ruv-gui
```
## Uninstall
```bash
sudo ./uninstall.sh
```

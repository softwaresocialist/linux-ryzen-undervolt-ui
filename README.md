# Linux Ryzen Undervolt UI

Early development – expect bugs. AI‑assisted code.

## What is it?

Linux Undervolt Tool for Ryzen CPUs using the Ryzen SMU kernel driver.
Allows reading and setting voltage offsets per core.

## DISCLAIMER

WARNING: This tool writes to the SMU (System Management Unit) of your Ryzen CPU.
Incorrect offsets may cause system instability or damage. Use at your own risk.
Only processors that are supported by the Ryzen SMU driver should work.

## Prerequisites

- Ryzen CPU
- Python 3.8+ with PyQt6 installed
- polkit (`pkexec`)
- The `ryzen_smu` kernel driver

## Installation

### 1. Install the Ryzen SMU driver

https://github.com/amkillam/ryzen_smu

## 2. Install the GUI tool
```bash
git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
chmod +x install.sh
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
chmod +x uninstall.sh
sudo ./uninstall.sh
```
<img width="930" height="758" alt="Bildschirmfoto_20260409_170510" src="https://github.com/user-attachments/assets/6374d400-1896-41c0-ad30-fc7bdbf28e60" />




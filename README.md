# Linux Ryzen Undervolt UI
Early development – expect bugs. AI‑assisted code.
<img width="796" height="627" alt="Bildschirmfoto_20260418_210747" src="https://github.com/user-attachments/assets/a81fbd61-5320-4c51-a436-96e0e42cb273" />

## What is it?

Linux Undervolt Tool for Ryzen CPUs using the Ryzen SMU kernel driver.
Allows reading and setting voltage offsets per core.
## DISCLAIMER

WARNING:
Incorrect offsets may cause system instability or damage. Use at your own risk.
Only processors that are supported by the Ryzen SMU driver should work.
## Prerequisites

    - Ryzen CPU

    - Python 3.8+ with PyQt6 installed

    - polkit (pkexec)

    - The ryzen_smu kernel driver

## Installation
### 1. Install the Ryzen SMU driver
https://github.com/amkillam/ryzen_smu#installation
### 2. Install and remove the tool
#### 2.1 Install
```bash
git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
chmod +x install.sh
sudo ./install.sh
```
#### 2.2 Remove
```bash
cd linux-ryzen-undervolt-ui
chmod +x uninstall.sh
sudo ./uninstall.sh
```
### Run the GUI

From the application menu: "Ryzen Undervolt Tool"
Or terminal:
```bash
ruv-gui
```
### CLI Usage

The tool also works from the command line. All commands require root privileges (use sudo).

#### Core Specification Syntax
Many commands accept flexible core specifications:

    Single core: 0, 2, 5

    Comma-separated list: 0,2,4

    Range (inclusive): 0-7 (all cores from 0 to 7)

    Combination: 0,2-5,7 (cores 0, 2, 3, 4, 5, and 7)

### Core Operations
##### List current offsets for all cores
```bash
sudo ruv-gui status
```
##### Get offset for specific core(s)
```bash
sudo ruv-gui get <cores>
```
##### Set offset for a specific number of cores
```bash
sudo ruv-gui set <cores> <offset>
```
##### Reset all cores to 0 mV
```bash
sudo ruv-gui reset
```
### Profile Management
##### List all saved profiles
```bash
sudo ruv-gui profile list
```
##### Save current offsets as a new profile
```bash
sudo ruv-gui profile save <name>
```
##### Apply a saved profile
```bash
sudo ruv-gui profile apply <profile-name>
```
##### Read a profile (outputs JSON)
```bash
sudo ruv-gui profile read <profile-name>
```
##### Update specific cores in a profile
```bash
sudo ruv-gui profile update <name> --cores <cores> --offset <offset>
```
##### Update a profile and apply immediately
```bash
sudo ruv-gui profile update <name> --cores <cores> --offset <offset> --apply
```
##### Delete a profile (also resets offsets)
```bash
sudo ruv-gui profile delete <name>
```
### Managing Boot Profiles
##### Enable a Profile at Boot
```bash
sudo ruv-gui boot enable <profile-name>
```
##### Check Current Boot profile Status
```bash
sudo ruv-gui boot status
```
##### Remove Boot profile and service
```bash
sudo ruv-gui boot disable
```

# Linux Ryzen Undervolt UI
Early development – expect bugs. AI‑assisted code.
<img width="798" height="625" alt="Bildschirmfoto_20260417_123451" src="https://github.com/user-attachments/assets/31f787ec-7684-4846-bdc6-bc6ef26d8bed" />
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

    - polkit (pkexec)

    - The ryzen_smu kernel driver

## Installation
### 1. Install the Ryzen SMU driver

https://github.com/amkillam/ryzen_smu
### 2. Install the tool

```bash
git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
chmod +x install.sh
sudo ./install.sh
```

### 3. Run the GUI

From the application menu: "Ryzen Undervolt Tool"
Or terminal:
```bash
ruv-gui
```
## CLI Usage

The tool also works from the command line. All commands require root privileges (via sudo for example).

### List current offsets
```bash
sudo ruv-gui status
```
### Get offset for a specific core
```bash
sudo ruv-gui get <core-id>
```
### Set offset for a single core (in mV)
```bash
sudo ruv-gui set <core-id> <offset>
```
### Apply the same offset to multiple cores
```bash
sudo ruv-gui apply-list <core-id...> <offset>
```
### Reset all offsets to 0 mV
```bash
sudo ruv-gui reset
```
### List all saved profiles
```bash
sudo ruv-gui profile list
```
### Save current offsets as a new profile
```bash
sudo ruv-gui profile save <name>
```
### Apply a saved profile by name
```bash
sudo ruv-gui apply <profile-name>
```
### Delete a profile
```bash
sudo ruv-gui profile delete <name>
```
### Update specific cores in a profile
```bash
sudo ruv-gui profile update <name> --cores 0 2 4 --offset -30
```
### Update profile and apply immediately
```bash
sudo ruv-gui profile update <name> --cores 0 2 4 --offset -30 --apply
```
### Read a profile file (outputs JSON)
```bash
sudo ruv-gui read-profile /etc/ruv/profiles/mystable.json
```
### Enable automatic profile apply at boot
```bash
sudo ruv-gui boot enable <profile-name>
```
### Disable and remove boot service
```bash
sudo ruv-gui boot disable
```
### Check boot service status
```bash
sudo ruv-gui boot status
```



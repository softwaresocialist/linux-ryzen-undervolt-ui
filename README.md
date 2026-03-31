# Linux Ryzen undervolt UI
THIS IS STILL IN ACTIVE DEVELOPMENT AND NOT MEANT TO BE USED BY REGULAR USERS

I ONLY TESTED THIS ON MY 5700x3d
# What is it ?
This is a linux implementation of the PBO2 undervolting tool with a gui and cli.
# How to use it ?
1. Clone this repository: https://github.com/amkillam/ryzen_smu and install the Ryzen SMU driver that makes comunication with Ryzen SMU (System Management Unit) possible
```pwsh
git clone https://github.com/amkillam/ryzen_smu
cd ryzen_smu
sudo make dkms-install
```
Now make a reboot. The dkms-install should make a new module into you system called "ryzen_smu". It will autostart next time you reboot your system. Without it the provided Python script will not function.

2. Clone and run the python file
```pwsh
git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
python3 ruv_gui.py



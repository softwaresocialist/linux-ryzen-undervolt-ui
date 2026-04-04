# Linux Ryzen undervolt UI
This is still in early development. EXPECT BUGS
# What is it ?
This is a linux implementation of the PBO2 undervolting tool with a gui and cli.
# How to use it ?
1. Clone this repository: https://github.com/amkillam/ryzen_smu and install the Ryzen SMU driver that makes comunication with Ryzen SMU (System Management Unit) possible
```pwsh
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu
sudo make dkms-install
```
Now make a reboot. The dkms-install should make a new module into you system called "ryzen_smu". It will autostart next time you reboot your system. Without it the provided Python script will not function.

2. Clone and run the python file
```pwsh
git clone https://github.com/softwaresocialist/linux-ryzen-undervolt-ui.git
cd linux-ryzen-undervolt-ui
python3 ruv_gui.py
```


<img width="807" height="529" alt="Bildschirmfoto_20260404_140831" src="https://github.com/user-attachments/assets/93a5a128-c5aa-45fd-9671-95775fb269a8" />

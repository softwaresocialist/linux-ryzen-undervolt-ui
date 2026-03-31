# Linux Ryzen undervolt UI
THIS IS STILL IN ACTIVE DEVELOPMENT AND NOT MEANT TO BE USED BY REGULAR USERS 
# What is it ?
This is a linux implementation of the PBO2 undevolting tool used to undervolt Ryzen CPUs in Windows. More info on how to do it in Windows is here: https://github.com/PrimeO7/How-to-undervolt-AMD-RYZEN-5800X3D-Guide-with-PBO2-Tuner
# How to use it ?
1. Clone this repository: https://github.com/amkillam/ryzen_smu and install the Ryzen SMU driver that makes comunication with Ryzen SMU (System Management Unit) possible
```pwsh
git clone https://github.com/amkillam/ryzen_smu
cd ryzen_smu
sudo make dkms-install
```
Now make a reboot. The dkms-install should make a new module into you system called "ryzen_smu". It will autostart next time you reboot your system. Without it the provided Python script will not function.

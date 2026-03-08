<h1 align="center">SnapACE</h1>
<p align="center">
  <a aria-label="License" href="https://github.com/BlackFrogKok/SnapAce/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/BlackFrogKok/SnapAce">
  </a>
  <a aria-label="Last commit" href="https://github.com/BlackFrogKok/SnapAce/commits/">
    <img src="https://img.shields.io/github/last-commit/BlackFrogKok/SnapAce">
  </a>
  <img src="https://img.shields.io/badge/stage-beta-orange">
</p>
<p align="center">
This project provides integration of the Anycubic ACE PRO with the Snapmaker U1 printer as a filament storage.
</p>

[Версия на русском (RU)](README.ru.md)

## Pinout and Wiring
**You will need to make a cable to connect the ACE to a USB**

<img src="./.github/img/pinout.png" alt="drawing" width="70%"/>

> [!IMPORTANT]
>VCC (24 V) for logic is NOT required — ACE powers itself. Connect via USB to a regular port.

## Installation Instructions

1.  **Custom Firmware:** Install the latest [Paxx12](https://github.com/paxx12/SnapmakerU1-Extended-Firmware) custom firmware to gain SSH access to your Snapmaker U1.
2.  **Enable Debug Mode:**
    *   Connect to your printer via SSH.
    *   Execute the following command to enable debug mode:
        ```bash
        touch /oem/.debug
        ```
> [!NOTE]
> This mode allows user files to persist after a reboot.
> [!WARNING]
> Enabling debug mode will reset your Wi-Fi settings. You will need to reconnect to your Wi-Fi network after the printer reboots.
3.  **Install Extra Modules:**
    *   Copy `ace.py` and `filament_feed.py` from this repository to `/home/lava/klipper/klippy/extras/` on your printer.
> [!IMPORTANT]
> Rename the stock `filament_feed.py` to `filament_feed_stock.py` before copying the new one.
4.  **Install Kinematics Module:**
    *   Copy `extruder.py` from this repository to `/home/lava/klipper/klippy/kinematics/`.
> [!IMPORTANT]
> Rename the stock `extruder.py` to `extruder_stock.py` before copying the new one.
5.  **Configure Klipper:**
    *   Copy `ace.cfg` (if provided) to the custom config directory: `/config/extended/klipper/`.
6.  **Calibrate Feeding Length:**
    *   Connect all four PTFE tubes between the ACE Pro gates and the U1.
    *   Measure the approximate length of the PTFE line.
    *   Subtract approximately 5cm from this measurement.
    *   Open `ace.cfg` and find the `feed_length:` variable.
    *   Enter your calculated value (e.g., if the line is 80cm, set `feed_length: 750`).
    *   *Goal:* The filament should stop approximately 5cm away from the toolhead after being fed from the ACE Pro gate.
7.  **Restart:** Restart your printer to apply the changes.

## Connection example
<p>
    <img src="./.github/img/ACE_front.jpg" width="200" hspace="5" alt="alt text"/>
    <img src="./.github/img/ACE_behind.jpg" width="200" hspace="5" alt="alt text"/>
</p>

## Support me
<p align="center"> 
  <a href="https://ko-fi.com/blackfrogkok" target="_blank"> <img src="https://ko-fi.com/img/githubbutton_sm.svg"/> </a> 
</p>

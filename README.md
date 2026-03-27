# mUlt1ACE

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Stage: Beta](https://img.shields.io/badge/stage-beta-orange)]()

Multi-ACE Pro integration for the Snapmaker U1 3D printer. Connect up to 4 Anycubic ACE Pro units for expanded filament management - auto-detected, hot-swappable, no reboot required.

> ⚠️ **Beta Software** - This project is in active development. Use at your own risk. While it has been tested successfully, it may cause unexpected behavior. Always backup your original files before installation. See [Installation Step 3](#3-backup-original-files).

Based on [SnapAce by BlackFrogKok](https://github.com/BlackFrogKok/SnapAce).

## Features

**Multi-ACE Support**
- Auto-detection of all connected ACE Pro units via USB Vendor/Product ID
- Hot-swap between ACE units without reboot (`ACE_LOAD` Klipper Makros)
- Full unload/load cycle with a single command 
- Active device selection persists across reboots
- Supports up to 4 ACE Pro units (expandable via USB hubs or daisy chain)

**Improved Filament Handling**
- Reactive sensor polling during preload - stops feeding immediately when filament reaches toolhead sensor
- Per-toolhead configurable feed lengths (different PTFE tube lengths per head)
- Load retry with mini-retract if filament doesn't reach the sensor
- Unload retry with full toolhead re-heat cycle if filament is stuck
- Automatic `feed_assist` management prevents conflicts between toolheads

**Bug Fixes**
- Fixed `feed_assist` not being disabled before loading a different toolhead (caused timeout errors)
- Fixed touchscreen sending duplicate unload commands (state_mismatch errors)
- RFID data pushed to display after ACE switch

**Compatibility**
- Works on **Snapmaker U1 Stock Firmware V1.2.0** (no custom firmware required)
- Also compatible with [paxx12 Extended Firmware](https://github.com/paxx12/SnapmakerU1-Extended-Firmware)
- Requires SSH access (built into Firmware V1.2.0)

## Cable Building Guide (Solder-Free)

The ACE Pro connects to the Snapmaker U1 via USB using a Molex Micro-Fit 3.0 connector. No soldering required.

### What You Need

- **1x Molex Micro-Fit 3.0 Male 2x3 connector with pre-crimped wires** ([AliExpress](https://de.aliexpress.com) - search for "Micro-Fit Molex MX3.0 43025 43020 Male 2x3 20AWG")

- A screw terminal USB adapter - [Amazon example](https://amzn.to/4uZ61jo)

### Pinout

Connect the following pins from the ACE Pro Molex connector to USB:

```
ACE Pro Molex (2x3)          USB Type-A

┌──────|──|───────┐
│  1   2   3      │          Pin 6 (VCC)  - NOT CONNECTED
│  4   5   6      │          Pin 2 (D+)   - ACE D+
└─────────────────┘          Pin 3 (D-)   - ACE D-
                             Pin 5 (GND)  - ACE GND
```

Refer to the [original SnapAce pinout diagram](https://github.com/BlackFrogKok/SnapAce/blob/main/.github/img/pinout.png) for the exact Molex pin positions.

> **Important:** VCC is not needed - the ACE Pro has its own power supply.

### Assembly

1. Connect D-, D+, and GND from the Molex connector to D-, D+, and GND on the USB connector (screw terminal)
2. **Twist D+ and D- wires together** (2-3 twists per cm) to reduce electromagnetic interference
3. Connect to the Snapmaker U1 via a USB extension cable
4. Additional ACE Pro units connect via the **daisy chain** cable (included with ACE Pro) - no additional USB cables needed

### Recommended Setup

```
[Snapmaker U1] ──USB──> [ACE Pro #1]  ──DaisyChain──> [ACE Pro #2]
                                      ──DaisyChain──> [ACE Pro #3]
                                      ──DaisyChain──> [ACE Pro #4]
```

## Installation on Stock Firmware V1.2.0

### 1. Enable Root Access

On the Snapmaker U1 touchscreen, enable Root Access in the settings (available in Firmware V1.2.0+).

### 2. Enable Debug Mode (File Persistence)

Connect via SSH and enable debug mode so your files survive reboots:

```bash
ssh root@<YOUR-U1-IP>
# Password: snapmaker

touch /oem/.debug
reboot
```

> **Note:** This may reset your Wi-Fi settings. Reconnect via the touchscreen after reboot.

### 3. Backup Original Files

```bash
cp /home/lava/klipper/klippy/extras/filament_feed.py /home/lava/klipper/klippy/extras/filament_feed_stock.py
cp /home/lava/klipper/klippy/kinematics/extruder.py /home/lava/klipper/klippy/kinematics/extruder_stock.py
```

### 4. Install mUlt1ACE Files

Create the required directories:
```bash
mkdir -p /home/lava/printer_data/config/extended/
mkdir -p /home/lava/printer_data/config/extended/mUlt1ACE/
```

Copy the following files to your Snapmaker U1 using WinSCP or `scp`:

| File | Destination |
|------|-------------|
| `ace.py` | `/home/lava/klipper/klippy/extras/ace.py` |
| `filament_feed.py` | `/home/lava/klipper/klippy/extras/filament_feed.py` |
| `extruder.py` | `/home/lava/klipper/klippy/kinematics/extruder.py` |
| `ace.cfg` | `/home/lava/printer_data/config/extended/ace.cfg` |

> **Note:** `extruder.py` is unchanged from the original SnapAce project.

### 5. Configure Klipper

Add the following line to your `printer.cfg`:

```ini
[include extended/ace.cfg]
```

### 6. Calibrate

Measure the PTFE tube length from each ACE Pro gate to each toolhead. Set `retract_length` to the full tube length. Set `feed_length` and `load_length` to approximately 3/4 of the tube length - the reactive sensor polling handles the remaining distance automatically.

> **Tested setup:** The default config values below were tested with ACE Pro stock PTFE tubes, ~5cm cable directly at the Snapmaker U1 entrance, and Bambu-style splitters.

### 7. Reboot

```bash
reboot
```

After reboot, verify the ACE is detected:

```
ACE_LIST
```

## Configuration Reference

### ace.cfg

```ini
[save_variables]
filename: /home/lava/printer_data/config/extended/mUlt1ACE/ace_vars.cfg

[ace]
# serial: not needed - auto-detected via USB Vendor/Product ID
# Only set this as fallback if auto-detection fails:
# serial: /dev/serial/by-path/platform-fed00000.usb-usb-0:1.3.3:1.0
baud: 115200

# --- Feed & Retract ---
feed_speed: 80              # ACE feed speed (mm/s)
retract_speed: 80           # ACE retract speed (mm/s)
retract_length: 1950        # Full retract distance - set to full PTFE tube length (mm)
feed_length: 1500           # Preload feed distance - approx. 3/4 of tube length (mm)

# --- Load Phase (used by filament_feed.py) ---
load_length: 1500           # Load feed distance - approx. 3/4 of tube length (mm)
load_retry: 3               # Number of retries if sensor not reached
load_retry_retract: 50      # Mini-retract before retry (mm)

# --- Dryer ---
max_dryer_temperature: 70   # Max dryer temp (°C)

# --- Per-Toolhead Overrides (optional) ---
# Use these if PTFE tube lengths differ per toolhead:
# feed_length_0: 1500
# feed_length_1: 1600
# feed_length_2: 1400
# feed_length_3: 1500
# load_length_0: 1500
# load_retry_0: 3
# load_retry_retract_0: 50

# --- GCode Macros ---
[gcode_macro ACE_drying_on]
gcode:
    ACE_START_DRYING TEMP=55 DURATION=240

[gcode_macro ACE_drying_off]
gcode:
    ACE_STOP_DRYING

[gcode_macro switch_to_ace1]
gcode:
    ACE_SWITCH TARGET=1 AUTOLOAD=1

[gcode_macro switch_to_ace2]
gcode:
    ACE_SWITCH TARGET=2 AUTOLOAD=1
```

## GCode Commands

| Command | Description |
|---------|-------------|
| `ACE_LIST` | List all detected ACE devices with active marker |
| `ACE_SWITCH TARGET=n` | Switch to ACE unit n (1-based, no reboot) |
| `ACE_SWITCH TARGET=n AUTOLOAD=1` | Full unload → switch → load cycle |
| `ACE_FEED INDEX=n LENGTH=x SPEED=s` | Feed filament from gate n |
| `ACE_RETRACT INDEX=n LENGTH=x SPEED=s` | Retract filament to gate n |
| `ACE_ENABLE_FEED_ASSIST INDEX=n` | Enable feed assist for gate n |
| `ACE_DISABLE_FEED_ASSIST` | Disable feed assist |
| `ACE_START_DRYING TEMP=t DURATION=d` | Start dryer at t°C for d minutes |
| `ACE_STOP_DRYING` | Stop dryer |

## How It Works

### Auto-Detection

On startup, mUlt1ACE scans `/dev/serial/by-path/` and identifies ACE Pro devices by their USB Vendor ID (`28e9`) and Product ID (`018a`) via sysfs. No hardcoded serial paths needed.

### Reactive Sensor Polling

Unlike the original SnapAce which feeds a fixed distance blindly, mUlt1ACE uses `how_wait=0` to start the feed and then polls the toolhead filament sensor every 0.105 seconds. When filament is detected, `_stop_feeding()` is called immediately - minimizing overshoot regardless of PTFE tube length.

### ACE Switch with AUTOLOAD

When `ACE_SWITCH TARGET=2 AUTOLOAD=1` is called:

1. **Unload Phase** - For each toolhead: checks the filament sensor. If filament is in the head, runs the full Snapmaker unload sequence (heat, extract, retract). If not, skips.
2. **Disconnect** - Closes serial connection to current ACE.
3. **Connect** - Opens serial connection to new ACE, waits for heartbeat.
4. **RFID Push** - Sends new ACE's RFID spool data to the touchscreen display.
5. **Load Phase** - For each gate with filament: runs the full Snapmaker load sequence.

### Persistent Selection

The active ACE device path is saved to `ace_vars.cfg` using Klipper's `save_variables` system. After a reboot, mUlt1ACE automatically reconnects to the last active ACE.

## Differences from Original SnapAce

| Feature | SnapAce | mUlt1ACE |
|---------|---------|----------|
| ACE units | 1 | Up to 4 (auto-detected) |
| Serial config | Required (hardcoded path) | Auto-detected via USB ID |
| ACE switching | Not supported | Hot-swap, no reboot |
| Preload sensor | No sensor check (blind feed) | Reactive polling, instant stop |
| Load feed | 100mm at speed 20 (hardcoded) | Configurable length & speed |
| Load retry | None | Configurable with mini-retract |
| Unload retry | None | Full re-heat + extract cycle |
| Per-toolhead config | None | All parameters overridable |
| Feed assist conflict | Not handled | Auto-disable before load |
| Touchscreen double-tap | state_mismatch error | Silently ignored |
| RFID after switch | Not applicable | Auto-pushed to display |
| Firmware requirement | paxx12 Extended | Stock V1.2.0 (or paxx12) |

## Troubleshooting

**ACE not detected after reboot**
- Check USB cable connection
- Verify with `ls /dev/serial/by-path/` - ACE devices should be listed
- Check `dmesg | tail -20` for USB errors

**"Invalid JSON/UTF-8 from ACE PRO" messages**
- USB signal integrity issue - twist D+ and D- wires, improve cable shielding
- Try a shorter USB extension cable

**Load timeout on first attempt, works on second**
- Check if `feed_assist` from a previous toolhead is interfering
- Check klippy.log for `feed_loading` entries

**"Filament Unloading Anomaly" on display**
- Should be fixed in mUlt1ACE - machine state is reset after unload
- If still occurring, check if the issue happens with stock load/unload (touchscreen) as well

**File changes lost after reboot**
- Verify `.debug` flag: `ls -la /oem/.debug`
- Check overlay is active: `mount | grep overlay`

## Credits

- [BlackFrogKok/SnapAce](https://github.com/BlackFrogKok/SnapAce) - Original ACE Pro integration for Snapmaker U1
- [paxx12/SnapmakerU1-Extended-Firmware](https://github.com/paxx12/SnapmakerU1-Extended-Firmware) - Custom firmware with SSH access
- [agrloki/ValgACE](https://github.com/agrloki/ValgACE) - ACE Pro Klipper driver for Creality (protocol reference)
- Snapmaker community on Discord (#u1-printer channel)

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0). See [LICENSE](LICENSE) for details.

## Support

If you find this project useful:

- ⭐ Star this repository
- 🐛 Report bugs via [Issues](../../issues)


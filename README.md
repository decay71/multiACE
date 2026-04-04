# mUlt1ACE

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/K3K610R4F9)

## multiACE v0.80b "First Light"

**Multi-ACE Pro support for Snapmaker U1 with Klipper**

> ⚠️ **Beta Software** - This is a community-driven development project for enabling multiple Anycubic ACE Pro filament changers on Snapmaker printers. While carefully tested, it relies on community feedback and testing to mature. Use at your own risk. Please report issues and share your experience to help improve multiACE for everyone.

> **Important Note:** Both the Snapmaker U1 and the Anycubic ACE Pro have their own quirks with filament loading/unloading, RFID detection (possibly related to tag sticker positioning), and occasional mechanical issues. Not every problem encountered is a multiACE issue - many are inherent to the underlying hardware. This is a beta release, not a production-ready solution. Whether these U1 and ACE Pro limitations can be resolved in the future remains to be seen.

## What is multiACE?

multiACE extends the [SnapACE](https://github.com/BlackFrogKok/SnapACE) software to support **multiple ACE Pro units** on a single Snapmaker U1 printer. Switch between ACE units to use different filament sets - for example, PLA on ACE 0 and PETG on ACE 1 - without physically swapping spools.

## Typical Workflow

### Single Material (e.g. PLA on ACE 0)

1. Insert spools into ACE 0
2. Press **ACEB__Load_0** → loads all filled slots
3. Print normally

### Multiple Materials (e.g. PLA on ACE 0, PETG on ACE 1)

1. Insert PLA spools into ACE 0, PETG spools into ACE 1
2. Load PLA toolheads (T0-T2) via display
3. Press **ACEA__Switch_1** → switch to ACE 1
4. Load PETG into desired toolhead (e.g. T3) via display or **ACEC__Load_T3**
5. Toolchanges during print automatically switch between ACEs

### Switching Complete Filament Sets

1. Press **ACEC__Unload_All** → unloads everything
2. Press **ACEB__Load_1** → switch to ACE 1 and load all

### Switching ACE Units

Use the Fluidd macros **ACEA__Switch_0..3** to switch between ACE units.

> **Note on macro names:** The macro names use letter prefixes (ACEA, ACEB, ACEC...) to ensure they appear in a logical order in Fluidd's alphabetical macro list. If you prefer different names, you can rename them anytime in `config/extended/ace.cfg`.

## Features

- **Multi-ACE Support** - Connect up to 4 ACE Pro units simultaneously
- **ACE Switching** - Switch between ACE units via Fluidd macros or console
- **Auto-Load** - Load all filled slots from selected ACE with one command
- **Unload All** - Unload all toolheads, automatically switching to correct ACE for retract
- **RFID Handling** - Automatic RFID detection and display across ACE switches
- **Manual Filament Support** - Works with both RFID and non-RFID spools
- **Per-ACE Dryer Settings** - Configurable temperature and duration per ACE
- **Normal Mode** - Switch back to stock Snapmaker operation at any time (only original files active, no ACE code running). Useful for filaments the ACE Pro cannot handle, such as TPU/TPE
- **Auto-Feed Control** - Automatic during print, disabled outside print to prevent unwanted preloads
- **Print-Start Safety Check** - Warns if a needed ACE is offline
- **PAXX Firmware Compatible** - Works with PAXX firmware which provides display mirroring, allowing full load/unload control from your computer
- **Clean Install/Uninstall** - One-command scripts with automatic backup and restore

## Requirements

- Snapmaker U1 printer
- Snapmaker firmware or PAXX firmware (tested with Snapmaker 1.2 and PAXX 12-14)
- 1-4 Anycubic ACE Pro units connected via USB (tested with 3)
- SSH access to the printer
- Fluidd web interface
- PTFE tube splitters (1-to-N per toolhead) - also allows switching to Normal Mode without recabling

## Hardware Setup

### Cable Building Guide (Solder-Free)

The ACE Pro connects to the Snapmaker U1 via USB using a Molex Micro-Fit 3.0 connector. No soldering required.

**What You Need:**
- 1x Molex Micro-Fit 3.0 Male 2x3 connector with pre-crimped wires - [AliExpress](https://de.aliexpress.com/item/1005010370245711.html)
- 1x USB Type-A screw terminal adapter - [Amazon](https://www.amazon.com/dp/B0825TWRW7)

**Pinout:**

```
ACE Pro Molex (2x3) - front view          Connection
         ||  <- clip
   ┌────────────┐
   │ [1] [2] [3] │                        Pin 2 (D-)  -> USB D-
   │ [4] [5] [6] │                        Pin 3 (D+)  -> USB D+
   └────────────┘                         Pin 5 (GND) -> USB GND
                                          Pin 6 (VCC) -> NOT CONNECTED
```

Refer to the [SnapAce pinout diagram](https://github.com/BlackFrogKok/SnapAce/blob/main/.github/img/pinout.png) for the exact Molex pin positions.

> **Important:** Pin 6 (VCC) is not connected - the ACE Pro has its own power supply.

**Assembly:**
1. Connect D-, D+, and GND from the Molex connector to D-, D+, and GND on the USB connector
2. Twist D+ and D- wires together (2-3 twists per cm) to reduce electromagnetic interference
3. If using a cut USB cable: wrap the exposed section with aluminum foil overlapping the cable shield
4. Additional ACE units connect via the daisy chain cable (included with ACE Pro) - no additional USB cables needed for units 2+

### ACE Connection Overview

Each ACE Pro connects to the printer via **two interfaces**:
- **USB** - Serial communication (commands, status, RFID)
- **PTFE tubes** - Filament path from ACE slots to toolheads

All ACE units are wired **in parallel** - each ACE slot feeds the **same toolhead** as the corresponding slot on every other ACE. This allows switching entire filament sets by switching the active ACE.

```
                    Splitter
ACE 0  Slot 0 ──────┐
ACE 1  Slot 0 ──────┤──── Head 0 (T0)
ACE 2  Slot 0 ──────┘

ACE 0  Slot 1 ──────┐
ACE 1  Slot 1 ──────┤──── Head 1 (T1)
ACE 2  Slot 1 ──────┘

ACE 0  Slot 2 ──────┐
ACE 1  Slot 2 ──────┤──── Head 2 (T2)
ACE 2  Slot 2 ──────┘

ACE 0  Slot 3 ──────┐
ACE 1  Slot 3 ──────┤──── Head 3 (T3)
ACE 2  Slot 3 ──────┘
```

### USB Connection

Each ACE Pro connects to the Snapmaker U1 via USB (data only - each ACE has its own power supply). The ACE units are daisy-chained through the USB ports on the back of each ACE:

```
Snapmaker U1 USB Port
        │
      ACE 0 ─── ACE 1 ─── ACE 2 ─── ACE 3
       (USB out → USB in, daisy chain)
```

> **Note:** VCC (5V) is not connected in the USB cable - only data lines. Each ACE Pro is powered by its own external power supply.

multiACE detects ACE units automatically by USB vendor/product ID (28e9:018a). The order of the daisy chain determines the ACE index (0, 1, 2, 3).

### PTFE Tube Splitters

Each toolhead needs a **splitter** that merges PTFE tubes from multiple ACE units into a single path to the extruder. With splitters installed, you can switch between ACE units **and** back to Normal Mode (stock feeders) without recabling.

- **3D print** a Y-splitter or multi-way splitter
- **Use commercial** PTFE tube connectors with multiple inputs

> **Tip:** Keep all PTFE tube lengths as equal as possible between ACE units. Adjust `load_length` per toolhead in `ace.cfg` if needed.

### RFID Spool Tags

The ACE Pro reads RFID tags from Anycubic spools to automatically detect filament type, color, and brand. For third-party spools without RFID, you can write compatible tags yourself:

- **Tags** - Use NFC NTAG 213 or 215 stickers
- **iPhone** - TagMySpool app
- **Android** - RFID ACE app

Spools without RFID tags work fine - you can set the filament type and color manually via the Snapmaker display.

### Recommended Setup

| ACE Units | Use Case | Setup |
|-----------|----------|-------|
| 2 ACEs | Material switching (e.g. PLA + PETG) | 2-way splitters, direct USB |
| 2 ACEs | Extended color range (8 colors) | 2-way splitters, direct USB |
| 3-4 ACEs | Multi-material + colors | N-way splitters, USB hub |

## Installation

### Prerequisites

Before installing multiACE, ensure the following:

1. **Firmware** - Install Snapmaker firmware 1.2+ or PAXX firmware 12-14 on your Snapmaker U1
2. **Enable Root Access** - On the Snapmaker display, go to Settings > About > tap firmware version 10 times to unlock Advanced Mode, then enable Root Access
3. **Enable SSH** - Connect via SSH or serial console and run:
   ```
   touch /home/lava/.oem_debug
   ```
   After reboot, Wi-Fi password needs to be re-entered on the display. SSH is then available at `root@<printer-ip>`
4. **Verify SSH** - Connect from your computer:
   ```
   ssh root@<printer-ip>
   ```

### Quick Install (Recommended)

1. Download or clone this repository
2. Copy the `multiace/` folder to your printer via SCP/SFTP (e.g. WinSCP on Windows, or command line):
   ```
   scp -r multiace/ root@<printer-ip>:/tmp/multiace/
   ```
3. SSH into the printer and run:
   ```
   bash /tmp/multiace/install_multiace.sh
   ```
4. Reboot the printer
5. multiACE starts in **Multi mode** - all connected ACE units are detected automatically

### Manual Install

If you prefer manual installation:

1. Copy Klipper extras to the printer:
   ```
   cp klipper/extras/ace.py /home/lava/klipper/klippy/extras/
   cp klipper/extras/filament_feed_ace.py /home/lava/klipper/klippy/extras/
   cp klipper/extras/filament_switch_sensor_ace.py /home/lava/klipper/klippy/extras/
   cp klipper/kinematics/extruder_ace.py /home/lava/klipper/klippy/kinematics/
   ```

2. Copy config files:
   ```
   cp config/extended/ace.cfg /home/lava/printer_data/config/extended/
   mkdir -p /home/lava/printer_data/config/extended/multiace
   cp config/extended/multiace/ace_vars.cfg /home/lava/printer_data/config/extended/multiace/
   cp config/extended/multiace/ace_mode_switch.sh /home/lava/printer_data/config/extended/multiace/
   chmod +x /home/lava/printer_data/config/extended/multiace/ace_mode_switch.sh
   ```

3. Activate ACE file swap:
   ```
   bash /home/lava/printer_data/config/extended/multiace/ace_mode_switch.sh ace
   ```

4. Delete Python cache:
   ```
   rm -rf /home/lava/klipper/klippy/extras/__pycache__/
   rm -rf /home/lava/klipper/klippy/kinematics/__pycache__/
   ```

5. Reboot the printer

### Uninstall

Run the uninstall script (installed automatically to the printer):
```
bash /home/lava/printer_data/config/extended/multiace/uninstall_multiace.sh
```

Or from the install folder:
```
bash /tmp/multiace/uninstall_multiace.sh
```

Then reboot. The printer returns to stock operation.

## Fluidd Macros

All operations are available as macro buttons in Fluidd, sorted alphabetically:

| Macro | Description |
|-------|-------------|
| **ACEA__Switch_0..3** | Switch to ACE 0-3 (no autoload) |
| **ACEB__Load_0..3** | Switch to ACE and load all filled slots |
| **ACEC__Unload_All** | Unload all toolheads |
| **ACEC__Unload_T0..T3** | Unload individual toolhead |
| **ACEC__Load_T0..T3** | Load individual toolhead from active ACE |
| **ACED__Dry_Start_0..3** | Start drying on ACE (uses config settings) |
| **ACED__Dry_Stop** | Stop drying on current ACE |
| **ACEE__Autofeed_Off/ON** | Disable/enable auto-feed |
| **ACEF__Mode_Normal** | Switch to stock mode (no ACE) |
| **ACEF__Mode_Multi** | Switch to multi-ACE mode |

## Configuration

All settings are in `config/extended/ace.cfg` under the `[ace]` section:

```ini
[ace]
# ACE feed/retract settings
feed_speed: 80          # Feed speed (mm/s)
retract_speed: 30       # Retract speed (mm/s, lower = cleaner winding)
retract_length: 1950    # Distance from extruder to splitter (mm)
load_length: 2100       # ACE feed distance for load (mm)

# Retry settings
load_retry: 1           # Number of load retries
load_retry_retract: 50  # Mini-retract before retry (mm)

# Temperature
swap_default_temp: 250  # Fallback temp when no config available
max_dryer_temperature: 70

# Purge (for in-layer color swap, future feature)
extra_purge_length: 50  # Extra extrusion after flush (mm), 0 = disabled

# Dryer defaults (per-ACE overrides possible)
dryer_temp: 55          # Default drying temperature
dryer_duration: 240     # Default drying duration (minutes)

# Optional: Per-ACE dryer overrides
# dryer_temp_0: 55
# dryer_temp_1: 45
# dryer_duration_0: 240
# dryer_duration_1: 180

# Optional: Per-Toolhead overrides
# load_length_0: 2100
# load_length_1: 2050
# retract_length_0: 1950
# retract_length_1: 1900
```

### Configuration Recommendations

**feed_length** - Set to `0` (disabled). The preload phase wastes time when loading ACE slots and leads to inconsistent filament positions in the PTFE tubes.

**load_length** - Set to approximately **110% of your actual PTFE tube length** (from ACE to splitter). The load phase is sensor-controlled and will stop when filament is detected, so a longer value is safe and ensures reliable loading.

**retract_speed** - Keep low (default `30`). The ACE Pro sometimes winds filament loosely at higher speeds, causing tangles on the spool. Additionally, consider printing a spool guide upgrade such as [this ACE Pro roller guide](https://www.printables.com/model/1237589-20-anycubic-ace-pro-upgrade-kit-to-new-s1-version) to improve winding quality.

**retract_length** - Measure the actual distance from your extruder sensor to the PTFE splitter and subtract ~100mm. The retract only needs to pull the filament back past the splitter junction, not the full tube length.

## Known Limitations

- **Unload before first use** - After a fresh install or when upgrading from a previous version, unload all toolheads before using multiACE. Filament loaded from a previous installation may cause unexpected behavior since multiACE has no knowledge of the previous state. Use **ACEC__Unload_All** or unload via display before starting.
- **Toolchange Time** - When a toolchange during print requires switching to a different ACE, the toolchange is extended by up to ~5 seconds (serial reconnect). Toolchanges within the same ACE are not affected.
- **ACE USB Reset** - Inactive ACE units periodically reset their USB connection (~3s cycle). This is normal ACE Pro firmware behavior and does not affect operation. Visible in `dmesg` but harmless.
- **Display Attach Toolhead** - Attaching a toolhead via the Snapmaker display triggers auto-feed. This is stock Snapmaker behavior and cannot be suppressed.
- **Unload All clears display** - After **ACEC__Unload_All**, manually set filament types and colors are cleared. This is by design - reload and set filament info again after unload.

## Troubleshooting

### Reset to clean state

If things get out of sync (wrong filament displayed, unexpected behavior), reset everything:

1. Unload all toolheads via display (make sure no filament is stuck in any head)
2. In Fluidd console: `ACE_CLEAR_HEADS`
3. Power-cycle the printer (full off/on, not just Klipper restart)
4. After reboot, start fresh with loading from ACE 0

### Klipper won't start after install
- Check if `ace.cfg` is included: `grep ace.cfg /home/lava/printer_data/config/printer.cfg`
- Check if `multiace/ace_vars.cfg` exists
- Run uninstall and reinstall

### ACE not detected
- Check USB connection: `ls /dev/serial/by-path/`
- ACE Pro should show as vendor `28e9`, product `018a`
- Try power-cycling the ACE

### Old code running despite update
- Delete Python cache: `rm -rf /home/lava/klipper/klippy/extras/__pycache__/`
- Check file timestamp in console: `multiACE v0.80b (file: ...)`

### Serial errors on console
- Serial errors during ACE switch are logged silently. If errors persist, check USB cables.

## Roadmap

### Next Version
- Bug fixes based on community feedback
- Custom Fluidd UI panel for ACE management
- Maybe one day: [Full vision](https://youtube.com/video/gJVQikjtDNs)

## License

This project is based on [SnapACE](https://github.com/BlackFrogKok/SnapACE) and [Klipper](https://github.com/Klipper3d/klipper), both licensed under GPL-3.0. multiACE is therefore also GPL-3.0.

##AI-Assisted Development Notice

This project includes AI-assisted content research documentation, parts of code).
All content is reviewed by humans before inclusion.

## Credits

- **[SnapACE](https://github.com/BlackFrogKok/SnapACE)** by BlackFrogKok - Foundation for ACE Pro Klipper integration
- **[DuckACE](https://github.com/utkabobr/DuckACE)** - ACE Pro reverse engineering and protocol documentation
- **[ACE Research](https://github.com/printers-for-people/ACEResearch)** by Printers for People - ACE Pro protocol research
- **[3D Print Forum](https://forum.drucktipps3d.de/)** - Tips, tricks, and community knowledge
- **Snapmaker** - Printer hardware and firmware
- **Anycubic** - ACE Pro filament changer
- **Community** - Testing, feedback, and bug reports (hopefully!)

# mUlt1ACE

## What's new in multiACE 0.90b "Vibrant Fungi"

**This is NO AMS-like solution with 1000s of reliable swaps, and I don't think it ever will be — but it recovers to a pause if it fails, so you can solve the problem and continue.**

**In-print color swaps up to 16 colors** - layer-boundary and mid-layer swaps during an active print are now stable enough for real prints rather than tests. The USB rewrite, the hardened load/unload path, and the FA/Load toggles together close the failure modes that previously made mid-print swaps fragile. 

**Swaptimizer** - `--optimize` on the post-process script reassigns T indices to minimize mid-print swaps and prints an ACE/Slot-sorted loadout to follow when you load cartridges. Typical savings on a 5+ color print: 20–30 %.

The real star is **`--layer`**: it detects whether every layer of the print stays within ≤4 colors, and if so, rewrites the gcode so swaps only ever happen at layer boundaries (Belady-optimal). That typically means an order of magnitude fewer swaps than naive assignment, and no mid-layer toolchange interruptions at all. Example: a 7-color Toad print from MakerWorld drops from 120 mid-print swaps to 3 layer-boundary swaps - at ~3:48 per swap, that's ~7.4 hours of print time back.

**Auto-Load Spools** - The Post-Processing Script Loads spools across all Aces, and unloads where needed. Full Automatic.

**Rewritten USB engine** - cross-ACE toolchanges now run at stock speed *with* feed_assist on every head. The start-ACE-only restriction from 0.81b is gone: every connected ACE stays fully available for the duration of a print, and the reset-cycle edge cases that forced the 0.81b workaround are handled at the engine level.

**Hardened load / unload** - several failure modes that previously stalled prints are now handled instead of just reported. Additional extrude retries retract, so the extruder gears release and re-grip. Failures snapshot the pause state (active extruder, per-head target temps) before raising, and resume was overridden with a multiACE safety layer.

Many color changes possible without U1 load / unload errors - all caught by the sensor / retry logic. If it isn't caught, solve it and resume.

**FA / Load handling on / off** - feed_assist is now togglable per-ACE and separately for print-time and load-time (`fa_print_disable` / `fa_load_disable`). Useful for ACEs where FA interferes with the load mechanics of a specific material.



[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/K3K610R4F9)

## multiACE v0.90b "Vibrant Fungi"

**Multi-ACE Pro support for Snapmaker U1 with Klipper**

> ⚠️ **Beta Software** - This is a community-driven development project for enabling multiple Anycubic ACE Pro filament changers on Snapmaker printers. While carefully tested, it relies on community feedback and testing to mature. Use at your own risk. Please report issues and share your experience to help improve multiACE for everyone.

> **Important Note:** Both the Snapmaker U1 and the Anycubic ACE Pro have their own quirks with filament loading/unloading, RFID detection (possibly related to tag sticker positioning), and occasional mechanical issues. Not every problem encountered is a multiACE issue - many are inherent to the underlying hardware. This is a beta release, not a production-ready solution. Whether these U1 and ACE Pro limitations can be resolved in the future remains to be seen.

## What is multiACE?

multiACE extends the [SnapACE](https://github.com/BlackFrogKok/SnapACE) software to support **multiple ACE Pro units** on a single Snapmaker U1 printer. Switch between ACE units to use different filament sets - for example, PLA on ACE 0 and PETG on ACE 1 - without physically swapping spools.

## Typical Workflow

### In-Print Color Swaps (layer / mid-layer)

Color swaps during an active print can be triggered two ways:

1. **Insert `ACE_SWAP_HEAD` at a layer** via your slicer's "Change Filament" / "Insert Custom G-code at Layer" feature - good for a handful of accent swaps without reslicing.
2. **Run the slicer gcode through the included post-processing script** - rewrites slicer-emitted virtual toolheads `T4..T15` into the correct `ACE_SWAP_HEAD` commands automatically.

Both paths use the same hardened load/unload logic as normal toolchanges. See [How to Do Toolswaps](#how-to-do-toolswaps) below for the exact command format and post-processing setup.

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

- **In-Print Color Swaps** - Layer-boundary and mid-layer color swaps during an active print, triggered from slicer gcode or via post-processing script
- **Full Cross-ACE Feed_Assist** - All connected ACEs stay fully available during a print, feed_assist on every head, at stock toolchange speed (rewritten USB engine)
- **Hardened Load / Unload** - Retract-recovery between extrude retries, pause-state snapshot before failure, safer resume path (pre-heat before travel, Z-hop before XY)
- **FA / Load Toggle per ACE** - Disable feed_assist per-ACE and separately for print-time / load-time (`fa_print_disable` / `fa_load_disable`)
- **Multi-ACE Support** - Connect up to 4 ACE Pro units simultaneously
- **ACE Switching** - Switch between ACE units via Fluidd macros or console
- **Auto-Load** - Load all filled slots from selected ACE with one command
- **Unload All** - Unload all toolheads, automatically switching to correct ACE for retract
- **RFID Handling** - Automatic RFID detection and display across ACE switches
- **Manual Filament Support** - Works with both RFID and non-RFID spools
- **Per-ACE Dryer Settings** - Configurable temperature and duration per ACE
- **Normal Mode** - Switch back to stock Snapmaker operation at any time (only original files active, no ACE code running). Useful for filaments the ACE Pro cannot handle, such as TPU/TPE
- **PAXX Firmware Compatible** - Works with PAXX firmware which provides display mirroring, allowing full load/unload control from your computer
- **Clean Install/Uninstall** - One-command scripts with automatic backup and restore

## Requirements

- Snapmaker U1 printer
- Snapmaker firmware or PAXX firmware (tested with Snapmaker 1.2, 1.3 and PAXX 12-14, 12-15)
- 1-4 Anycubic ACE Pro units connected via USB (tested with 3)
- SSH access to the printer
- Fluidd web interface
- PTFE tube splitters (1-to-N per toolhead) - also allows switching to Normal Mode without recabling

## Q & A

**Why go back to "poop" printing?**
You don't have to. The main goal of this software is still clean filament handling, not color-swap printing. You don't need to produce 1000-color swap prints that poop out more filament than the part weighs (and mulitACE isn't designed for that) - but adding 1-12 extra colors on top of standard multi-material work is nice without much added waste. Or just adding Gold and Silver to your CMYKW Full Spectrum print.

**You still use tip forming - why not a cutter?**
Tip forming is one of several ACE/U1 load-unload quirks. The first approach here is to solve them in software: sensor-reading retries, hardened recovery paths, changes to tip forming. If you want to build a physical cutter, I'll gladly build the routines for it - get in touch. And if something does fail, you can still walk to the printer, sort it out by hand, and hit resume.
It doesn't produce as much poop as a cutter, that's an advantage.

**Is it fast?**
No. Tip forming instead of a cutter, plus the bowden length, retries, puts a single color swap at up to 3-4 minutes. And since swaps don't happen at the park position, every change adds directly to print time. That said, stock Snapmaker per-layer color changes aren't any faster and require manual intervention every time - here it's at least automated. Park-position swaps are an option for the future.

**Does it work with ACE 2, AnkerMake Vivid, or other changers?**
No. But the ACE2 is reverse engineered now, I'll look into it. If you know another changer that is *proven* reliably better than the ACE Pro (and Klipper compatible), let me know - I've seen no trustworthy tests on the Vivid, and self-built machines are much pricier. multiACE aims for a solution anyone can set up.

**Can I use multiACE with just one ACE Pro?**
Yes. With a single ACE, multiACE still manages loads, unloads, and auto-feed cleanly and adds the hardened retry/resume path. `ace_device_count` defaults to `1`, no extra config needed.

**Does it work with / without RFID tags?**
Yes. Anycubic-RFID (or self written) spools work fine - or set filament type and color manually via the Snapmaker display. RFID and non-RFID spools can be mixed across slots and ACEs.

**Can I still use TPU / TPE?**
Yes, two ways: switch to **Normal Mode** (stock feeders, no ACE) for a full TPU session, or use **Load Off** on a per-toolhead basis while the rest of the heads keep using the ACE. Both keep swap/unload state consistent.

**Do I need PAXX firmware?**
No. Stock Snapmaker firmware 1.2+ works. PAXX adds display mirroring so load/unload is fully controllable from the computer - convenient, not required.

**What happens if an ACE is powered off or disconnected at startup?**
multiACE waits up to 20s for all expected devices (per `ace_device_count`) before locking the path-to-index mapping. A device missing at that point will be flagged, and a pre-print safety check warns if a needed ACE is offline.

**Will a failed load during print ruin the whole print?**
Not automatically. Load failures trigger a pause (not a full abort), snapshot the pause state (active extruder, target temps), and route through a hardened resume path. In many cases - loose filament, gear slip - the retract-between-retries recovery clears the problem before the pause is even raised.

**Can I go back to stock operation?**
Yes. `ACEF__Mode_Normal` switches to stock Snapmaker operation (no ACE code running). The uninstall script reverts everything with one command.

**Will this work reliably?**
No. Maybe yes. This is beta software, errors can and will show up. I've thoroughly tested it, but now it's up to you! Test and report errors - you're part of the team.

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

> **Important:** Do **not** connect Pin 6 (VCC) - the ACE Pro has its own power supply, and connecting VCC can damage your printer. Molex cables have no standardized color coding - always measure continuity before connecting.

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

> **Before Snapmaker firmware updates:** run `bash uninstall_multiace.sh` first, install the firmware update, then reinstall multiACE. Snapmaker firmware updates overwrite the multiACE Klipper files (`filament_feed.py`, `extruder.py`, `filament_switch_sensor.py`); without uninstalling first you end up in a half-stock half-multiACE state where neither path works reliably.

### Prerequisites

Before installing multiACE, ensure the following:

1. **Firmware** - Install Snapmaker firmware 1.2+ or PAXX firmware 12-14+ on your Snapmaker U1
2. **Enable Root Access** - On the Snapmaker display, go to Settings > About > tap firmware version 10 times to unlock Advanced Mode, then enable Root Access 
3. **Enable SSH** - Connect via SSH or serial console and run:
   ```
   touch /oem/.debug
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
| **ACEF__Mode_Normal** | Switch to stock mode (no ACE) |
| **ACEF__Mode_Multi** | Switch to multi-ACE mode |
| **ACEG__Status** | Show active ACE, detected devices, head mapping, build tag |
| **ACEG__List** | List all detected ACE devices |

## How to Do Toolswaps

multiACE supports up to **16 logical filaments** (4 toolheads × up to 4 ACE units). The core in-print swap command is:

```
ACE_SWAP_HEAD HEAD=<0..3> ACE=<0..3>
```

This swaps the filament on `HEAD` to the matching slot of the given `ACE`, reusing the hardened load/unload path. There are two ways to get these commands into your print gcode.

### Option 1 - Manual G-code Insertion (per layer)

For a quick 1-2 swap print without reslicing the whole project, use your slicer's **"Insert Custom G-code at Layer"** feature (Orca / Prusa: "Change Filament At Layer"; Bambu: "Pause / Custom G-code at layer").

Example: at layer 42, swap the filament on head 0 to the spool in ACE 1, slot 0:

```
; layer 42
ACE_SWAP_HEAD HEAD=0 ACE=1
```

Use this when you want full manual control over where and how often swaps happen. Good for color accents, signatures, or single-layer labels.

### Option 2 - Automatic Post-Processing (`post_process_virtual_toolheads.py`)

For a real multi-color print where the slicer already thinks in tool changes, let the included post-processing script do the conversion. The script maps slicer-emitted **virtual toolheads T4..T15** to the correct `ACE_SWAP_HEAD` commands and cleans up the heater/pre-extrude commands so nothing collides with multiACE's own swap flow.

**Filament order in the slicer** - set up your project with up to 16 filaments: **T0..T3** are the four "primary" filaments physically loaded on your active ACE (head 0..3), and **T4..T15** are the swap-in filaments on the other ACE slots. The mapping is position-based: `T4` = ACE 1 / head 0, `T5` = ACE 1 / head 1, … `T7` = ACE 1 / head 3, `T8..T11` = ACE 2 heads 0..3, `T12..T15` = ACE 3 heads 0..3. Assign your slicer's colors/materials in that order so the post-processing script can translate every toolchange into the right `ACE_SWAP_HEAD HEAD=X ACE=Y`.

**Setup** - in your slicer's post-processing field, point it at:

```
python3 /path/to/multiace/tools/post_process_virtual_toolheads.py 
```

**Flags:**

- `--optimize` - Swaptimizer: reassign T indices to minimize mid-print swaps, print an ACE/Slot-sorted loading order.
- `--layer` - upgrade of `--optimize`: if every layer stays within ≤4 colors, rewrite the gcode so swaps only happen at layer boundaries. Silently falls back to `--optimize` when infeasible.
- `--no-auto-load` - turn off auto-load feature

**What it does:**

- Rewrites bare `T4..T15` in the print body into `T<head%4>` + `ACE_SWAP_HEAD HEAD=X ACE=Y`
- Skips redundant swaps when a head already holds the requested color
- Runs an **optimizer**: prints a recommended ACE loadout (which 4 colors should live on the "primary" slots so fewer swaps are needed) to both the slicer's post-process dialog and `multiace_postprocess.log`

**Upload the processed gcode via Fluidd** - after the slicer exports, upload the resulting `.gcode` through Fluidd (Jobs → Upload) and start the print from there. Fluidd sends the rewritten commands to Klipper directly, so the ACE-aware gcode reaches multiACE exactly as the script produced it.

This is the path used for full multi-material prints. The optimizer output is useful even if you edit the loadout by hand afterwards - it tells you which color changes cost you the most swaps.

## Configuration

All settings live in `config/extended/ace.cfg` under the `[ace]` section (the Fluidd macros live in the same file below). For a fresh multi-ACE install only `ace_device_count` has to be changed - everything else has sensible defaults.

### Required

```ini
[ace]
ace_device_count: 1          # Number of physical ACE Pro devices (1..8)
```

At startup multiACE waits up to 20s for all expected devices before locking the path-to-index mapping, so a unit mid-USB-reset-cycle at boot never causes index drift. **Required for multi-ACE** - without an explicit count a single missing unit can lock the wrong mapping.

### Logging / Debug

```ini
state_debug: true            # per-toolchange / per-load audit log
usb_debug: true              # per-scan / per-connect serial-layer log
fa_debug: true               # feed_assist trace (useful during 0.90b bring-up)
# log_dir: /home/lava/printer_data/logs   # default is usually fine
```

Separate files under `printer_data/logs/multiace_*.log`. Keep them on while the release is in beta - they're essential for post-mortem analysis and cost nothing at runtime.

### Serial / Feed / Retract

```ini
baud: 115200
feed_speed: 80               # mm/s
retract_speed: 80            # mm/s
load_length: 2100            # ACE feed distance into the bowden (mm)
retract_length: 1950         # sensor-to-splitter distance (mm)
```

Set `load_length` to roughly **110 % of your PTFE length** - the phase is sensor-stopped, so overshoot is safe. `retract_length` = measured extruder-sensor-to-splitter distance minus ~100 mm; the retract only needs to pass the splitter junction, not the full tube. Low `retract_speed` helps the ACE wind the spool tighter; a spool guide upgrade like [this roller guide](https://www.printables.com/model/1237589-20-anycubic-ace-pro-upgrade-kit-to-new-s1-version) improves winding quality further.

### Load / Unload Retry (multiACE hardening)

```ini
load_retry: 3               # FEED_AUTO LOAD retries if sensor not reached

extrusion_retry: 7           # outer retries after wheel check fails (0 = disabled)

unload_retry: 3              # unload re-heat / re-run attempts
```

### Dryer

```ini
dryer_temp: 55               # °C
dryer_duration: 240          # minutes
max_dryer_temperature: 70    # safety cap

# Per-ACE overrides (optional):
# dryer_temp_0: 55
# dryer_temp_1: 45
# dryer_duration_0: 240
```

### Feed-Assist (FA) Gate

```ini
# Per-ACE FA exclusion (comma-separated 0-based ACE indices).
# fa_print_disable: no FA during print - extruder pulls filament alone
# fa_load_disable:  no FA during load - manual insert (e.g. TPU)
# fa_print_disable: 0,2
# fa_load_disable: 1
```

### Toolchange / Swap

```ini
extra_purge_length: 50       # extra mm after flush during toolchange
swap_default_temp: 250       # fallback swap temp when no heater/RFID target

# swap_retract_length: 900   # mid-print swap retract (default = retract_length)
```

### Per-ACE Length Overrides (optional)

When bowden lengths differ per ACE, override them in a dedicated `[ace N]` section:

```ini
[ace 0]
load_length: 2100
retract_length: 1950
load_length_2: 2200          # slot-specific override (ACE 0, slot 2)

[ace 1]
load_length: 2050
```

Lookup priority: `[ace N] load_length_Y` → `[ace N] load_length` → `[ace] load_length`. Same for `retract_length`. Speeds stay global.

## Known Limitations
- **Don't turn off automatic load in display** - Throws errors.
- **Maybe some purge-finetuning is needed**, have to look into that.
- **Unload before first use** - After a fresh install or when upgrading from a previous version, unload all toolheads before starting multiACE. Filament loaded from a previous installation can cause unexpected behavior since multiACE has no knowledge of the previous state. Use **ACEC__Unload_All** or unload via the display first.
- **Unload All clears display info** - After **ACEC__Unload_All**, manually set filament types and colors are cleared. By design - reload and set filament info again after unloading.

## Tips

Small things that make a big difference in practice - mostly mechanical, a few config-related:

- **Use spools the ACE actually likes.** Cardboard spools are the classic offender: they soak up humidity, swell, jam in the ACE channel, and the feed wheel still registers motion because only the spool itself is turning. Rewind to plastic or print a spool adapter. Spools that are too large or too small for the ACE Pro hub bind just as easily - stay close to standard 1 kg plastic cores.
- **Kobra 3 / S1 guide upgrade.** On the newer Anycubic roller-guide system the original feeder parts often skip; print the updated guide parts for much smoother winding. <!-- TODO: add link --> *(link to follow)*
- **Tune the retry parameters for your setup.** `load_retry`, `extrusion_retry` and `unload_retry` are there to be adjusted. The defaults catch most soft failure modes without user intervention, but bumping them up further on a problematic spool/setup is worth experimenting with.
- **Fit a larger purge bin.** Multi-color prints produce more purge than single-material runs. A bigger aftermarket or printed purge bin saves a trip to the printer mid-print.
- **Stable splitter / PTFE connections.** A splitter that shifts under feed pressure, or a PTFE joint sitting 1–2 mm short, can cost you a load with no obvious cause. Make sure every junction is fully seated, collets are locked, and the splitter is mounted on something that doesn't flex.

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
- Check file timestamp in console: `multiACE v0.90b (file: ...)`

### Serial errors on console
- Serial errors during ACE switch are logged silently. If errors persist, check USB cables.

### Reporting issues

When reporting a problem, please include the following logs from your printer. They are essential for diagnosing the issue:

1. **multiACE state log** - per-action audit trail (toolchanges, loads, unloads, FA events):
   ```
   cat /home/lava/printer_data/logs/multiace_state.log
   ```
2. **multiACE USB log** - serial connect/disconnect and scan events:
   ```
   cat /home/lava/printer_data/logs/multiace_usb.log
   ```
3. **Klipper log** - the last ~200 lines around the time of the issue:
   ```
   tail -200 /home/lava/printer_data/logs/klippy.log
   ```

Also mention:
- **Time of error** - exact timestamp so we can find it in the logs
- **What you did** - which button / macro / gcode you triggered
- **What happened before** - was this mid-print, during load, after a restart, etc.
- **Expected behavior** - what should have happened instead
- How many ACE units you have connected
- Whether your spools have RFID tags or not
- Whether Developer Mode is enabled (`ls /oem/.debug`)

## License

This project is based on [SnapACE](https://github.com/BlackFrogKok/SnapACE) and [Klipper](https://github.com/Klipper3d/klipper), both licensed under GPL-3.0. multiACE is therefore also GPL-3.0.

## AI-Assisted Development Notice

This project includes AI-assisted content (research, documentation, parts of code).
All content is reviewed by humans before inclusion.

## Credits

- **[SnapACE](https://github.com/BlackFrogKok/SnapACE)** by BlackFrogKok - Foundation for ACE Pro Klipper integration
- **[DuckACE](https://github.com/utkabobr/DuckACE)** - ACE Pro reverse engineering and protocol documentation
- **[ACE Research](https://github.com/printers-for-people/ACEResearch)** by Printers for People - ACE Pro protocol research
- **[3D Print Forum](https://forum.drucktipps3d.de/)** - Tips, tricks, and community knowledge
- **Snapmaker** - Printer hardware and firmware
- **Anycubic** - ACE Pro filament changer
- **Community** - Testing, feedback, and bug reports (hopefully!)

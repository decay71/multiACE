# multiACE+

**Multi-MMU support for Snapmaker U1 and Klipper**

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/K3K610R4F9)

🆕 **New: ready-made cable.** Connects an ACE Pro or ACE 2 to the U1 or other printers.

[![Get the Cable](cablebutton.png)](https://postapocalyptic-diy.com/shop/) [![Buy on eBay](ebaybutton.png)](https://www.ebay.de/itm/820289097531)

[![Guides & Downloads](visitbutton.png)](https://postapocalyptic-diy.com/multiace/)

## 🏆 Tier 1 winner in the Snapmaker Innovation Fund

multiACE won Tier 1 in the Hardware Mod category of the Snapmaker Innovation Fund. Thank you to everyone who voted, tested, reported bugs, sent logs, bought a cable or a coffee, and helped other users in the forum and on GitHub. Thank you also to **Snapmaker** for the Innovation Fund and for an open printer that makes a project like this possible. This release is the first one built after the win, and a lot of it comes from you.

> [!NOTE]
> To get pre-releases through the update button, add `update_prerelease: true` under `[ace]` in ace.cfg and restart Klipper.


## What makes it multiACE+

multiACE was built for exactly one device, the ACE Pro, and later the ACE 2. multiACE+ puts a driver layer between the engine and the hardware: every changer is a driver that tells the engine what it can do (sensors, feed assist, drying, tag reading, moving lanes on their own), and loading, unloading, swaps, recovery, the preflight and the web UI work the same on top of it.

### multiACE+ plugin for OrcaSlicer [multiACE+ in Orca Hub](https://cloud.orcaslicer.com/app/plugins/plugin-hub/3a9455df-8d2c-45a1-b95b-e3fa7e2d85f8)
Send a sliced file from OrcaSlicer straight to the printer and assign it in the same window: the plugin runs the preflight itself, shows what is loaded on the printer, and offers the plans (as loaded, optimize, layer, color) in multi and head mode, either reordering the loaded spools only or using all slots with a load proposal. The preview takes the colours of the chosen spools, the plan shows its background swaps, and a weight check warns before a spool runs short. Install it from the OrcaSlicer plugin hub or from the Config tab.

- **One driver per changer.** Each one can be switched on or off. Want to bring another filament changer to multiACE+? The [driver guide](multiace/docs/DRIVER_GUIDE.md) shows how, using the CFS Nano as the example. New drivers are very welcome as pull requests.
- **Devices tab.** Every unit gets a fixed place, MMU 1 to 4, and keeps it when you plug it into another USB port. Give your units names, take one out, or press *Locate* to see which one is which. A unit that is missing keeps its place and its spools.
- **Up to four units, mixed** in any combination of the drivers below.

**First new drivers:**

- **Creality CFS Nano** (opt-in). A cheap four-slot unit that connects through a USB adapter. It knows which slots hold filament, loads and unloads, and lets the filament run freely while printing. No feed assist and no tag reading.
- **ace2k** (opt-in). [tobecwb's ace2k firmware](https://github.com/tobecwb/ace2k) turns an ACE 2 Pro into a Klipper-style controller. multiACE drives it with its own driver: feed assist that follows the extruder, tag reading when a spool is inserted, the dryer, and every lane can move on its own. That makes **background swaps in multi mode** possible for the first time: a parked head unloads and reloads while another head prints. Flash it from the Devices tab, with tobecwb's permission.



## What's new in 1.94 (pre-release)

### multiACE+ plugin for OrcaSlicer
New: send and assign straight from OrcaSlicer, see [What makes it multiACE+](#what-makes-it-multiace). Needs multiACE+ 1.94 or newer.

For developers who want to build or update their own integration, the interface is documented here:
- [LOADOUT_API.md](multiace/docs/LOADOUT_API.md) - reading the loadout, running the preflight in a plugin, the queue
- [SEND_TO_MULTIACE.md](multiace/docs/SEND_TO_MULTIACE.md) - sending a file to the printer
- [ENGINE_API.md](multiace/docs/ENGINE_API.md) - the gcode and runtime contract

### Firmware flashing (thanks to @Tareku99)
The ACE firmware flash in the Devices tab is simpler now: each unit flashes from its own card, shows the progress there, and an unplugged unit can be forgotten right next to its name.

### Other improvements
- Every multi print goes through the assignment pipeline; the optimised plans got a scope switch.
- The filament picker offers materials the display knows but the firmware database lacks.
- A Spoolman address works without http://.
- RESUME after a strand broke above the MMU gate no longer needs the old slot refilled.
- ace2k images 0.16.0 and 0.17.0-alpha.1 and stock ACE 2 firmware 1.1.34 in the flash list.

## What's new in multiACE+ 1.91 

This release would look very different without the people who joined in. **[stefanodangelo](https://github.com/stefanodangelo)** built a whole new part of the web UI, the Print Hub, almost on his own, and brought tests and CI with it. **[docx-tech](https://github.com/docx-tech)** pushed background swaps further than anyone, with ideas, code and  long test prints. **[tobecwb](https://github.com/tobecwb/ace2k)** wrote ace2k, the firmware that makes background swaps in multi mode possible, and let us ship his flasher. **[physicsG](https://github.com/physicsG)** built the Bowden path calibration, and **[Tareku99](https://github.com/Tareku99)** brought multiACE into the paxx Extended Firmware as a ready-made package, organised its macros in Fluidd and Mainsail and opened it up for generic USB adapters. Thank you, all of you. Every contribution is named below, right where it appears.

**New look.** Redesigned web UI with Dark, Light and the previous Classic style, switchable in the Config tab.

**Slicer integration**
- Upload the plain slicer file to the web UI. The preflight matches the slicer colours to the spools that are loaded, plans the swaps (or suggests a better loadout), shows the estimate and starts the print.
- Orca and Snapmaker Orca: with the multiACE printer profile (download in the web UI), filament 5 to 20 stand for the slots of all MMUs, so you can pick a spool right in the slicer.
- A file sent straight from the slicer prints with a single ACE in multi mode; anything else goes through the preflight.
- **Coming in the next release: an Orca plugin**, so the slicer sees your loaded spools and sends the print to multiACE directly.

**Print Hub** *(by [stefanodangelo](https://github.com/stefanodangelo))*
- Control tab with webcam, print panel and console.
- History, Favorites and a print queue: prepare a file now, start it later, and the loadout is checked again before it runs.
- 3D G-code preview and slicer thumbnails in the preflight, History, Favorites and Queue.
- Estimate card and swap timeline in the preflight, learned from how long your own swaps really take.
- Push and browser notifications for errors and warnings.
- See the changes before a config save, and remember the last filament you picked.
- Test harness with a mock printer and automatic tests for every change.

**Background swaps**
- **Background swaps in multi mode** on ace2k units *(firmware by [tobecwb](https://github.com/tobecwb/ace2k))*.
- **The arriving head waits in the dock** until its background swap has gripped and primed the new filament, instead of driving over the bed with a half loaded nozzle and dropping a blob. *(by docx-tech)*
- **Less purge in the dock**: the amount primed at the park position can be set on its own, without touching the purge of a normal swap. *(by docx-tech)*
- **Shorter background swaps**: pulling the old filament, gripping and priming the new one now run as one chain instead of waiting after every move, and a head that will be picked up soon stops ready at the sensor instead of being cut off mid-prime. *(by stefanodangelo)*
- **Choose how much happens in the background**: everything, unload and transport to the sensor, or only the unload.
- The heater goes back to what the slicer asked for after a background swap, and filament waiting at the sensor is no longer lost after a restart.

**docx-tech's work in detail** *([docx-tech](https://github.com/docx-tech))*

Most of what makes background swaps practical today was found or built by docx-tech:
- the arrival hold above (three pull requests, from the first draft to holding until the prime is done),
- the separate dock purge,
- the **lean flush**: a faster, cleaner flush after a swap with one brush pass and no extra trips (built from his pull request),
- no needless cool-down when the next colour stays on the same head (built from his pull request),
- the ideas behind the transport mode, the hot pre-purge before the tip forming and the lower purge minimum,
- the USB connection section of this README,
- and long test runs with full logs. In his background swap runs 92 of 103 swaps finished in the dock, and his own setup prints the Axolotl test model in 5 h 12 min, by tuning head change speed and cutting purge where the wipe tower already cleans. His numbers are the benchmark the rest of this section was measured against.

**Bowden path calibration** *(by [physicsG](https://github.com/physicsG))*

New in this release: multiACE measures the length of every filament path from the MMU to the toolhead, using the unit's own encoder, and sets the load length for each slot. No more guessing tube lengths.

**Setup and integration**
- multiACE as a ready-made package for the paxx Extended Firmware, so the firmware installs and updates it for you. *(by [Tareku99](https://github.com/Tareku99))*
- multiACE macros are grouped in Fluidd and Mainsail. *(by Tareku99)*
- Generic USB-RS485 adapters for the ACE 2 are recognised when you allow it. *(by Tareku99)*
- Flash the ace2k firmware from the Devices tab, with the flasher by tobecwb.

**Also:** one simple purge slider, a power loss with a missing MMU now pauses instead of cancelling, restart buttons wherever a restart is needed, stock firmware 2.1.0 checked.

<details>
<summary>Earlier releases (1.27b, 1.10b)</summary>

## What's new in multiACE 1.27b 

**A big thank you to our new collaborators.** Three developers joined the project and brought their own features with them:

- **Simon-CR** - the ACE2-Open community firmware for the ACE 2 Pro, which lets multiACE read and write spool tags. multiACE now also runs on other Klipper printers, not only the Snapmaker U1 (his Voron is the first one), and a watchdog notices when an ACE silently stops answering and reconnects it.
- **Tareku99** - multiACE as a ready-made package for the paxx Extended Firmware, so the firmware installs and updates it for you. A big thank you to [paxx12](https://github.com/paxx12) for the plan to ship this multiACE integration with the rolling releases of the Extended Firmware.
- **Godless50** - the ACE Pro gets its community firmware: flash it straight from the web UI, and read the tags of your non Anycubic spools on an ACE Pro too. Auto-dry can also follow an external humidity sensor.



**Read spool tags on the ACE Pro.** With the community firmware, press *Read* in the filament picker and the spool's tag is identified and matched to your spool list. (Needs the community firmware on the ACE Pro.)

**Online updates from GitHub.** The Update button now fetches releases directly from GitHub.

**More robust connection.** If an ACE drops off, multiACE keeps trying to reconnect it in the background; a disconnected unit is shown as offline instead of a confusing state, and you get no pause when nothing is printing.

**TRSYNC back to stock.** multiACE no longer patches Klipper's homing timeout. If you see 0003-0528 "Timer too close" during homing or bed mesh, set `trsync_timeout: 0.250` in the `[ace]` section of ace.cfg and restart Klipper; the value only ever raises the window, never lowers it.

**Also:** clearer messages when a spool has no tag code, small web UI and log fixes.


## What's new in multiACE 1.10b

**Multifilament optimisation.** One colour may sit on several heads: put a second spool
of the same colour on a free slot and the preflight spreads that colour over both heads,
so fewer swaps are needed (a 6 colour print with 28 swaps came down to 7). Works in head
mode and in multi mode, with a max-copies setting and a strict colour switch; the plans
re-plan live in the browser. Manual heads take part in the multi preflight as pinned heads.
(Idea: Dragg30 /snapmaker forum)

**Calibrate button in the PA dialog**

**Manual heads.** in multi mode now appear in preflight

**RFID tags:** **read** the UID of any tag, read and **write** for open tags (ACE2-Open units). Not every tag's content can be read, but the card UID can be read from every tag (Bambu, Snapmaker, Anycubic, OpenSpool, blank), so any spool can be identified. OpenSpool and blank NTAG tags are read automatically when the spool is inserted; the picker gets Read tag and Write to tag buttons (OpenSpool or Anycubic format, optionally with the card UID as SKU so every ACE (V1, V2 Stock) recognises the spool).
**Tag reading hardened.** 

**Spool inserted mid-print** The unit's pull-in is stopped right after the bite so it cannot push a second strand into the combiner while the neighbour lane prints; the tag is read at the end of the print if possible.

**ACE2-Open firmware - flash from the web UI.** Flash an ACE 2 Pro straight from the
Config tab: stock 1.1.31, or patch automatically to **ACE2-Open** build. A big thank you to
**[Simon-CR](https://github.com/Simon-CR/ace2-pro-firmware-research)** - his ACE 2 Pro
firmware research and the ACE2-Open UID passthrough are what make everything below
possible. (Flashing is at your own risk - never during a print, never unplug.)

**ACE2-Open flash target 1.1.60O.** The web flash offers 1.1.60O (the newest build that
is verified to run); 

**Spools bound by card UID.** A spool can be identified by its chip's serial, not only by
the tag's SKU - several codes per spool, learned on hand assignment, shared with Spoolman
and SpoolLink.

**Pressure advance per spool.** The stock flow calibration is stored on the spool and
applied automatically whenever that spool is loaded; synced with Spoolman using the same
field as the [pechex/SpoolLink mod](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/pull/649).
The preflight can run the calibration per head at print start (#115).

**API documentation** for slicers, hosts and scripts now ships with multiACE - see
[API documentation](#api-documentation-for-slicers-hosts-and-scripts).

**Also:** stock firmware 2.0.0 and 1.6.0 support, a load fix against crushed tips on reloads,
auto-dry soft start, preflight and web UI fixes, installer fixes.

**Nozzle wiper version 2 - new, larger purge bin** (unchanged from 0.99.8b):
**https://makerworld.com/en/models/3084827** Wiper ·
**https://makerworld.com/en/models/3040955** Bin & Bin XL


</details>

## How it works

> Use at your own risk. multiACE is not an AMS with thousands of reliable swaps every time, but when something fails it stops in a resumable pause instead of ruining the print.

multiACE+ runs up to four filament changers (MMU 1 to 4) on a Snapmaker U1 with its four toolheads. Three modes, switchable in the web UI:

- **Multi**: slot N of every MMU feeds head N through a splitter. Switch whole filament sets, or let the preflight swap colours on a head during the print.
- **Head**: each head is wired to its own MMU, and that head can reach all four of its slots. The other heads can stay on the stock side feeders.
- **Normal**: stock Snapmaker operation, no MMU code running.

## Features

- **Colour swaps during the print**, at layer changes or in the middle of a layer.
- **Background swaps at the park position**: a parked head unloads and reloads while another head prints. Head mode with ACE units, multi mode with ace2k units. Keep the wipers clean, purge from the dock can build up there.
- **Custom temperatures and tip forming** per material, with a step editor in the web UI (see https://postapocalyptic-diy.com/temp-and-tip-tuning/). Please share your results in the sticky issue.
- **Quad Replenish**: when a spool runs out mid-print, multiACE loads a matching spool from another slot or MMU and continues.
- **Spool management**: your spools with material, colour, vendor and remaining weight, linked to the slots. Use while printing is booked automatically. Spools are assigned by hand or recognised by their tag (code or chip UID), and can be synced with Spoolman or SpoolLink.
- **Humidity-controlled drying**: an ACE 2 or ace2k dries by its humidity reading instead of a timer and can take ACE Pro units along.
- **Purge per colour pair** from the slicer's flush matrix, with one slider to scale it.
- **Air-Print Detection**: checks every load and watches the back-pressure while printing; pauses on clogs and chewing; possible air is only logged while it is still being measured.
- **Devices tab**: fixed places MMU 1 to 4, names, locate, firmware flashing for ACE 2 (stock, ACE2-Open, ace2k) and ACE Pro (community firmware).
- **Print Hub**: control, history, favorites and a print queue.
- **Compact panel view** to embed in Fluidd as a camera, so the MMU status stays visible next to the print.
- **Hardened load and unload** with retries, clear error messages and resumable pauses.
- **Auto-load**: the needed spools are loaded at print start, partly in parallel.
- **Online updates** from the web UI.
- **paxx Extended Firmware**: works with it and is available as a package for it.
- **Clean install and uninstall** with automatic backup and restore.

The ACE 2 flash engine is based on hakimio's OTA updater and is used with his permission. Thanks to hakimio for the reverse engineering that made it possible.

## Supported MMUs

Up to four units in total, mixed with ACE Pro, CFS Nano or ace2k as you like. ACE 2 Pro units need firmware 1.1.31, ACE2-Open or ace2k. All Ace Units can be flashed from the Devices tab (at your own risk, never during a print).

## 🌐 Reactive Web UI

A full real-time control panel for your MMUs. https://printer-ip/multiace/

- **Live dashboard** with a wiring overlay that shows which slot is feeding which toolhead
- **Editable command queue** - pause, drop pending load / unload / swap jobs before they run
- **Saveable filament loadouts** - snapshot the current spool configuration and re-apply it on the next print with a single click
- Dryer settings per MMU, slot picker, and a multi-language UI (English, German, Chinese)

See it in action: https://youtu.be/9uLE1uydWmo


## 🌐 Web-Preflight
- **Upload the unprocessed G-code to the multiACE web UI.** Print in the loaded order or rearrange the spools to the suggested loadout to save swaps. The needed spools are loaded automatically, no need to preload.

##  In-Print Color Swaps (layer / mid-layer)

Color swaps during an active print can be triggered two ways:

1. **Through the web preflight** (recommended): upload the slicer file, multiACE plans and inserts the swaps.
2. **By hand**: insert `ACE_SWAP_HEAD` at a layer with your slicer's "Insert Custom G-code at Layer" feature, good for a few accent colours without reslicing.

Both paths use the same hardened load/unload logic as normal toolchanges. See [How to Do Toolswaps](#how-to-do-toolswaps) below for the exact command format and post-processing setup.

## 📺 On-Printer Touchscreen (community, beta)


multiACE's own surfaces are browser-based. If you'd rather drive it from the
printer itself, [physicsG's HelixScreen fork](https://github.com/physicsG/helixscreen)
adds multiACE support to [HelixScreen](https://github.com/prestonbrown/helixscreen),
replacing the stock U1 touchscreen UI.


## FOrca Slicer support (community)

Support for FOrca Slicer (https://github.com/jiyang1018/FOrcaSlicer) Gcode, Upload via WebUI or directly sent from new 
FOrca Slicer ([releases](https://github.com/jiyang1018/FOrcaSlicer/releases))


## Requirements

- Snapmaker U1 printer
- Stock Snapmaker firmware 1.4 to 2.1, or paxx Extended Firmware
- 1 to 4 MMUs over USB: ACE Pro, ACE 2 Pro (stock, ACE2-Open or ace2k), Creality CFS Nano (opt-in)
- SSH access to the printer for the first install
- PTFE splitters per toolhead for multi mode (also lets you go back to normal mode without recabling)

## Q & A

**Why go back to "poop" printing?**
You don't have to. The main goal of this software is still clean filament handling, not color-swap printing. You don't need to produce 1000-color swap prints that poop out more filament than the part weighs (and multiACE isn't designed for that) - but adding 1-12 extra colors on top of standard multi-material work is nice without much added waste. Or just adding Gold and Silver to your CMYKW Full Spectrum print.

**Does it work with ACE 2, CFS or other changers?**
ACE Pro and ACE 2 (stock, ACE2-Open, ace2k) can run side by side, and the Creality CFS Nano has an opt-in driver. Other changers need a driver, see [What makes it multiACE+](#what-makes-it-multiace). Drivers are welcome as pull requests.

**Can I use multiACE with just one ACE Pro?**
Yes. With a single ACE, multiACE still manages loads, unloads, and auto-feed cleanly and adds the hardened retry/resume path. No extra config needed.

**Does it work with / without RFID tags?**
Yes. Anycubic tags, self-written tags (OpenSpool, Anycubic format) and, with community firmware, other tags work fine - or set filament type and color manually via the Snapmaker display. Spools with and without tags can be mixed across slots and MMUs.

**Can I still use TPU / TPE?**
Yes. Use a manual head (hand-fed, multi mode), a stock side feeder head (head mode), or switch to normal mode.

**Do I need PAXX firmware?**
No. Stock Snapmaker firmware 1.4 to 2.1 works. PAXX adds display mirroring so load/unload is fully controllable from the computer - convenient, not required.

**Will a failed load during print ruin the whole print?**
Not automatically. Load failures trigger a pause (not a full abort), snapshot the pause state (active extruder, target temps), and route through a hardened resume path. In many cases - loose filament, gear slip - the retract-between-retries recovery clears the problem before the pause is even raised.

**Can I go back to stock operation?**
Yes. Normal mode switches to stock Snapmaker operation (no ACE code running). The uninstall script reverts everything with one command.

## Hardware Setup

Cable Setup see:

https://postapocalyptic-diy.com/ace-pro-1-2-cable-guide/



### MMU Connection Overview

Each MMU connects to the printer through **USB** (commands, status, tags) and **PTFE tubes** (filament path to the toolheads).

**Multi mode:** all MMUs are wired **in parallel**. Slot N of every MMU feeds head N through a splitter.

```
                    Splitter
MMU 1  Slot 1 ──────┐
MMU 2  Slot 1 ──────┤──── Head 1
MMU 3  Slot 1 ──────┘

MMU 1  Slot 2 ──────┐
MMU 2  Slot 2 ──────┤──── Head 2
MMU 3  Slot 2 ──────┘

        ... and so on for slot 3 / head 3 and slot 4 / head 4
```

**Head mode:** each head gets its own MMU, and a 4-to-1 combiner joins the four slots of that MMU into the head.

```
MMU 1  Slot 1-4 ──── 4:1 combiner ──── Head 1
MMU 2  Slot 1-4 ──── 4:1 combiner ──── Head 2
(heads without an MMU use the stock side feeder)
```

### USB Connection
Each unit connects to the Snapmaker U1 via USB (data only - each unit has its own power supply). The USB connection setup depends on whether you are using the ACE Pro v1 or ACE Pro v2.

ACE Pro v1: Supports daisy-chaining.

ACE Pro v2: Does not support daisy-chaining. Each unit must connect directly to a dedicated USB Hub. Each ACE Pro 2 requires the supplied K3 Cable paired with an RS485-to-USB adapter. Refer to the [Cable Guide](https://postapocalyptic-diy.com/ace-pro-1-2-cable-guide/) for complete wiring details.

ACE Pro (daisy chain)
```
Snapmaker U1 USB Port
        │
      MMU 1 ─── MMU 2 ─── MMU 3 ─── MMU 4
        (USB out → USB in, daisy chain)
```

ACE 2 Pro (USB hub)
```
Snapmaker U1 USB Port
        │
   [ USB Hub ]
  ┌─────┼─────┬─────┐
  │     │     │     │
MMU 1 MMU 2 MMU 3 MMU 4
  (K3 Cable + RS485-to-USB adapter per unit)
```
> **Note:** VCC (5V) is not connected in the USB cable - only data lines. Each ACE Pro is powered by its own external power supply.


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

| MMUs | Use case | Setup |
|-----------|----------|-------|
| 2 | Material switching (e.g. PLA + PETG) | 2-way splitters, direct USB |
| 2 | Extended color range (8 colors) | 2-way splitters, direct USB |
| 3-4 | Multi-material + colors | N-way splitters, USB hub |

## Installation

> **Before Snapmaker firmware updates:** run `bash uninstall_multiace.sh` first, install the firmware update, then reinstall multiACE. Snapmaker firmware updates overwrite the multiACE Klipper files (`filament_feed.py`, `extruder.py`, `filament_switch_sensor.py`); without uninstalling first you end up in a half-stock half-multiACE state where neither path works reliably.

### Prerequisites

Before installing multiACE, ensure the following:

1. **Firmware** - Stock Snapmaker firmware 1.4 to 2.1, or paxx Extended Firmware
2. **Enable Root Access** - On the Snapmaker display, go to Settings to unlock Advanced Mode, then enable Root Access 
3. **Enable SSH** - Connect via SSH or serial console and run:
   ```
   touch /oem/.debug
   ```
   After reboot, Wi-Fi password needs to be re-entered on the display. SSH is then available at `root@<printer-ip>`
4. **Verify SSH** - Connect from your computer:
   ```
   ssh root@<printer-ip>
   ```

### Install

1. Download or clone this repository
2. Copy the `multiace/` folder to your printer via SCP/SFTP (e.g. WinSCP on Windows, or command line):
   ```
   scp -r multiace/ root@<printer-ip>:/tmp/multiace/
   ```
3. SSH into the printer and run:
   ```
   bash /tmp/multiace/install_multiace.sh
   ```
4. Install with WEB UI https://printer-ip/multiace/
   ```
   bash /tmp/multiace/install_multiace.sh --install-web
   
   ```
5. Reboot the printer
6. multiACE starts in **Multi mode**. Connected units get their places in the Devices tab of the web UI.


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

All operations are also available as macros, grouped in Fluidd and Mainsail. The numbers in the macro names count from 0, so `_0` is MMU 1 and `T0` is head 1.

| Macro | Description |
|-------|-------------|
| **ACEA__Switch_0..3** | Switch to MMU 1-4 (no autoload) |
| **ACEB__Load_0..3** | Switch to an MMU and load all filled slots |
| **ACEC__Unload_All** | Unload all toolheads |
| **ACEC__Unload_T0..T3** | Unload one toolhead |
| **ACEC__Load_T0..T3** | Load one toolhead from the active MMU |
| **ACED__Dry_Start_0..3** | Start drying on an MMU |
| **ACED__Dry_Stop** | Stop drying |
| **ACEF__Mode_Normal / _Multi / _Head** | Switch the operating mode |
| **ACEG__Status / ACEG__List** | Show the active unit, devices, head mapping and build |
| **ACEH__Update_Check / _Apply** | Check for and install an update |

Most of this is easier in the web UI; the macros are there for the console and for your own macros.

## How to Do Toolswaps

multiACE supports up to **16 filaments** (4 toolheads x up to 4 MMUs). The swap command is:

```
ACE_SWAP_HEAD HEAD=<0..3> ACE=<0..3> [SLOT=<0..3>]
```

It swaps the filament on `HEAD` to the given MMU (and slot), with the same hardened load and unload as every other change. The numbers count from 0 here as well.

### Web preflight (recommended)

Upload the plain slicer file to the web UI. The preflight reads the slicer's colours and tool changes, matches them to the loaded spools, plans every swap (including background swaps), shows the estimate and starts the print. With the multiACE printer profile for Orca (download in the web UI), filament 5 to 20 in the slicer stand for the slots of all MMUs.

### By hand (a few accent colours)

Use your slicer's **"Insert Custom G-code at Layer"** feature. Example: at layer 42, swap head 1 to MMU 2:

```
; layer 42
ACE_SWAP_HEAD HEAD=0 ACE=1
```

## Configuration

Most settings can be changed in the Config tab of the web UI, which writes them to `config/extended/ace.cfg` (`[ace]` section). Every option there has a short description. Units no longer need a device count: they get their places in the Devices tab.

A few you may want to look at:

```ini
[ace]
feed_speed: 80               # mm/s
retract_speed: 80            # mm/s
load_length: 2100            # MMU feed distance into the bowden (mm), or let the Bowden calibration set it per slot
retract_length: 1950         # pull back far enough to clear the splitter (mm)
swap_retract_length: 900     # retract during a swap (0 = retract_length)
load_retry: 3                # load retries if the toolhead sensor is not reached
extrusion_retry: 7           # retries when no flow is measured after a load
unload_retry: 3              # unload attempts
dryer_temp: 55               # °C
dryer_duration: 240          # minutes
```

Per-MMU or per-slot lengths go into an `[ace N]` section (N counts from 0); the Bowden calibration writes them for you:

```ini
[ace 0]
load_length: 2100
load_length_2: 2200          # MMU 1, slot 3
```

## Known Limitations
- **Don't turn off automatic load in display** - Throws errors.
- **Unload before first use** - After a fresh install, unload all toolheads before starting multiACE. Filament loaded from a previous installation can cause unexpected behavior since multiACE has no knowledge of the previous state. Use **ACEC__Unload_All** or unload via the display first.
- **Unload All clears display info** - After **ACEC__Unload_All**, manually set filament types and colors are cleared. By design - reload and set filament info again after unloading.

## Tips

Small things that make a big difference in practice - mostly mechanical, a few config-related:

- **Use spools the ACE actually likes.** Cardboard spools are the classic offender: they soak up humidity, swell, jam in the ACE channel, and the feed wheel still registers motion because only the spool itself is turning. Rewind to plastic or print a spool adapter. Spools that are too large or too small for the ACE Pro hub bind just as easily - stay close to standard 1 kg plastic cores.
- **Kobra 3 / S1 guide upgrade.** On the newer Anycubic roller-guide system the original feeder parts often skip; print the updated guide parts for much smoother winding. <!-- TODO: add link --> *(link to follow)*
- **Tune the retry parameters for your setup.** `load_retry`, `extrusion_retry` and `unload_retry` are there to be adjusted. The defaults catch most soft failure modes without user intervention, but bumping them up further on a problematic spool/setup is worth experimenting with.
- **Fit a larger purge bin.** Multi-color prints produce more purge than single-material runs. A bigger aftermarket or printed purge bin saves a trip to the printer mid-print.
- **Stable splitter / PTFE connections.** A splitter that shifts under feed pressure, or a PTFE joint sitting 1-2 mm short, can cost you a load with no obvious cause. Make sure every junction is fully seated, collets are locked, and the splitter is mounted on something that doesn't flex.

## Troubleshooting

### Reset to clean state

If things get out of sync (wrong filament displayed, unexpected behavior), reset everything:

1. Unload all toolheads via display (make sure no filament is stuck in any head)
2. In Fluidd console: `ACE_CLEAR_HEADS`
3. Power-cycle the printer (full off/on, not just Klipper restart)
4. After reboot, load again from the web UI

### Klipper won't start after install
- Check if `ace.cfg` is included: `grep ace.cfg /home/lava/printer_data/config/printer.cfg`
- Check if `multiace/ace_vars.cfg` exists
- Run uninstall and reinstall

### MMU not detected
- Check the Devices tab: is the unit listed, and does it have a place?
- Check the USB connection: `ls /dev/serial/by-path/`
- Power-cycle the unit

### Old code running despite update
- New code needs a restart of the Klipper service (or a reboot); a firmware restart from Fluidd does not reload it.
- The startup line in the console shows what runs: `multiACE ... Version 1.90 build=... core=...`

### Serial errors on console
- Short serial errors are retried and reconnected automatically. If they keep coming, check the USB cables and the hub.

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
- Which MMUs you have connected (Devices tab)
- Whether your spools have RFID tags or not
- Whether Developer Mode is enabled (`ls /oem/.debug`)

## ℹ️ Before you install

multiACE is a **community project** - built by hobbyists, for hobbyists. A quick orientation before SSH'ing into your printer:

- multiACE needs **root access** to your Snapmaker U1 (`touch /oem/.debug` + reboot). With root enabled and custom code running, **this may affect your manufacturer warranty**. Snapmaker support generally cannot help with a modified printer.
- The installer **modifies live Klipper files** under `/home/lava/klipper/klippy/extras/` and `/kinematics/` (filament feed, switch-sensor, extruder). Stock files are backed up as `*_pre_multiace.py` and the included `uninstall_multiace.sh` restores everything cleanly.
- The project is **not endorsed or supported by Snapmaker, Anycubic, or the PAXX upstream maintainers**.
- This software comes **without warranty** - formally covered by GPL-3.0 §15-17. Translation: I do my best, but the responsibility for using it stays with you.

If any of that doesn't sit right, no worries - your printer keeps working with stock Snapmaker firmware as it is. If you're on board: have fun, and feedback / issues are always welcome.

## API documentation (for slicers, hosts and scripts)

multiACE can be driven from outside - a slicer plugin, a print host or a plain
script. The contract lives in `docs/`:

- [`ENGINE_API.md`](multiace/docs/ENGINE_API.md) - the gcode command vocabulary, the
  `ace` status object, push events and versioning (Moonraker REST/WebSocket; nothing
  imports the engine, so your program keeps its own licence).
- [`LOADOUT_API.md`](multiace/docs/LOADOUT_API.md) - the web backend's HTTP API: read what
  is actually loaded (spools, head wiring, nozzle sizes) before assigning filaments, and
  hand a sliced file back.
- [`SEND_TO_MULTIACE.md`](multiace/docs/SEND_TO_MULTIACE.md) - the upload endpoint in
  detail (inbox semantics, status codes).

## License

This project is based on [SnapACE](https://github.com/BlackFrogKok/SnapACE) and [Klipper](https://github.com/Klipper3d/klipper), both licensed under GPL-3.0. multiACE is therefore also GPL-3.0.

## AI-Assisted Development Notice

This project includes AI-assisted content (research, documentation, parts of code).
All content is reviewed by humans before inclusion.

## Contributors

- **[stefanodangelo](https://github.com/stefanodangelo)** - Print Hub (control, history, favorites, queue), estimates and swap timeline, 3D preview and thumbnails, notifications, shorter background swaps, test harness and CI.
- **[docx-tech](https://github.com/docx-tech)** - arrival hold for background swaps, dock purge setting, lean flush, no needless cool-down on the same head, long test runs and benchmarks, USB section of this README.
- **[tobecwb](https://github.com/tobecwb/ace2k)** - the ace2k firmware and its flasher.
- **[physicsG](https://github.com/physicsG)** - Bowden path calibration, multiACE support in his HelixScreen fork.
- **[Tareku99](https://github.com/Tareku99)** - paxx Extended Firmware package, macro groups in Fluidd and Mainsail, generic USB adapters.
- **[Simon-CR](https://github.com/Simon-CR)** - ACE2-Open community firmware (reading and writing tags), other Klipper printers, connection watchdog.
- **[Godless50](https://github.com/Godless50)** - ACE Pro community firmware and flashing from the web UI, tag reading on the ACE Pro, external humidity for auto-dry.
- **[hfoi589](https://github.com/hfoi589)** - feed-assist re-arm that checks the real slot state, and the first browser-side preflight.
- **Popstar (forum.snapmaker.com)** - the default PLA swap temperature of 220 °C.

## Credits

- **[ Hakimio](https://github.com/hakimio)** for ACE Pro 2 reverse engineering and support, firmware flash
- **[Simon-CR](https://github.com/Simon-CR/ace2-pro-firmware-research)** - ACE 2 Pro firmware research and the ACE2-Open UID passthrough that makes reading and writing foreign RFID tags possible
- **[pechex](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/pull/649)** - the per-spool pressure-advance schema (`pressure_advance_matrix`) multiACE shares for Spoolman interop
- **[tobecwb](https://github.com/tobecwb/ace2k)** - the ace2k firmware for the ACE 2 Pro and its flasher
- **[SnapACE](https://github.com/BlackFrogKok/SnapACE)** by BlackFrogKok - Foundation for ACE Pro Klipper integration
- **[DuckACE](https://github.com/utkabobr/DuckACE)** - ACE Pro reverse engineering and protocol documentation
- **[ACE Research](https://github.com/printers-for-people/ACEResearch)** by Printers for People - ACE Pro protocol research
- **Snapmaker** - Printer hardware and firmware
- **Anycubic** - ACE Pro filament changer
- **Community** - Testing, feedback, and bug reports (hopefully!)

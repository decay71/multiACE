# ACE 2 Pro firmware flashing

An ACE 2 Pro can run one of three firmwares with multiACE. All of them are
flashed from the web UI: **Config, Devices, ACE firmware**. This page says
what each one is, where it comes from and how to get from one to another.

For the ACE Pro (Gen 1) see [GEN1_FLASH.md](GEN1_FLASH.md).

## The firmwares

| firmware | from | what it gives you | driver |
|---|---|---|---|
| **Stock 1.1.31** | Anycubic | The reference. Reads Anycubic tags. | `ace_v2` (default) |
| **Stock 1.1.34** | Anycubic | Newer stock build. Flashing it and going back to 1.1.31 is tested; multiACE itself is tested on 1.1.31. | `ace_v2` |
| **ACE2-Open 1.1.60O** | [Simon-CR/ace2-pro-firmware-research](https://github.com/Simon-CR/ace2-pro-firmware-research) | Stock 1.1.31 plus a community patch: the card UID of any tag, OpenSpool and other tag formats, tag writing, automatic tag read when a spool is inserted, and a fix for one bay reporting its neighbour's tag. | `ace_v2` |
| **ace2k 0.15.0** | [tobecwb/ace2k](https://github.com/tobecwb/ace2k) | A different firmware altogether: the unit becomes a Klipper-style controller. Feed assist that follows the extruder, every lane moves on its own (this is what makes background swaps in multi mode possible), tag reading on insert, dryer. No tag writing. | `ace2k` (opt-in) |

ACE2-Open and ace2k are community projects, not Anycubic firmware. Neither
Anycubic nor multiACE takes responsibility for a flash.

## What you need

**Stock.** multiACE ships no Anycubic firmware and links to none. You need
Anycubic's update file yourself:

- `ACE2_V1.1.31_20260306.swu` (encrypted, the card asks for the password),
  or the same firmware as a raw `.bin`
- `ACE2_V1.1.34_20260430.bin`

The card only flashes files whose bytes match one of these builds. Any
other file is refused.

**ACE2-Open.** There is no file to download. Upload the stock **1.1.31**
file and choose the patch to ACE2-Open; the backend applies the patch to
that file before flashing. The patch only fits 1.1.31, so a unit on 1.1.34
needs the 1.1.31 file for this as well. After the flash the unit reports
`V1.1.60O`.

**ace2k.** Choose the version and press **Load from GitHub**. The image is
fetched from tobecwb's release page and checked against its checksum. The
flash itself is done by tobecwb's own flasher, which ships with multiACE
with his permission.

## Paths

| from | to | how |
|---|---|---|
| stock 1.1.31 / 1.1.34 | the other stock build | upload the file, flash |
| stock | ACE2-Open | upload 1.1.31, choose the patch, flash |
| ACE2-Open | stock 1.1.31 | upload 1.1.31 without the patch, flash |
| stock or ACE2-Open | ace2k | Load from GitHub, flash |
| ace2k | newer ace2k | Load from GitHub, flash |
| ace2k | stock 1.1.31 | upload 1.1.31, flash |
| ace2k | ACE2-Open | two steps: back to stock 1.1.31 first, then patch |

A unit that runs ace2k only takes stock 1.1.31 back, not 1.1.34.

## What happens on an ace2k flash

- Before the first write the flasher saves the unit's factory calibration.
  The unit keeps its calibration itself; the saved file is your backup and
  can be downloaded from the Devices tab.
- When a unit changes between stock and ace2k, multiACE sets its port in
  `[ace] ace2k_ports`, switches `enable_ace2k` on and restarts Klipper. The
  unit keeps its place (MMU 1-4), its name and its settings.
- The ace2k driver is opt-in. Without `enable_ace2k: true` an ace2k unit is
  not used.

## Before you flash

- Never during a print.
- Do not touch power or USB cables while the flash runs.
- Stop a running dry cycle first.
- Use **Dry run** to check the connection and the file without writing
  anything.

## If something goes wrong

- **The unit reports the new version but shows no slots.** It runs, but
  answers no application command. Choose another firmware and flash again,
  no power cycle needed.
- **The flash was interrupted** (power loss, cable pulled, Klipper
  restart). The bootloader is not overwritten, so the unit can be flashed
  again. If it no longer connects in multiACE, the card cannot reach it.
  In that case stop Klipper and run the ace2k flasher on the printer by
  hand, with the stock 1.1.31 `.bin` or an ace2k image:

  ```bash
  python3 /home/lava/multiace_web/backend/vendor/ace2k_flash.pyz --port /dev/serial/by-path/<port of the unit> <image>
  ```

  Then start Klipper again.

## Credits

- **[tobecwb](https://github.com/tobecwb/ace2k)** wrote ace2k and its
  flasher.
- **[Simon-CR](https://github.com/Simon-CR/ace2-pro-firmware-research)**
  did the ACE 2 Pro firmware research and wrote ACE2-Open.

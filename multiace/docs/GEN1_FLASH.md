# Gen 1 (ACE Pro) firmware flashing

This note is for reviewing the `feat/gen1-flasher-v1` branch: what it
changes, why the Gen-1 transport is a separate branch inside the firmware
tab, and the traps that must stay documented. The short version: an ACE
Pro (Gen 1) can now be flashed from the same Config-tab card as an ACE 2,
with the same tested-images gate philosophy, without a single new gcode
command.

## What the branch changes

| file | change |
|---|---|
| `web/backend/ace1_flash.py` | **new.** Gen-1 IAP flasher: `FF AA \| len(u16 LE) \| payload \| crc16(MCRF4XX) \| FE` framing, JSON-RPC (`get_info`, `get_status`, `iap_version`, `iap_upgrade`, `iap_upgrade_finish`), `0x55 \| addr(u32 LE) \| n \| data` chunks from `0x08024000` (64 B), its own `KNOWN_FIRMWARE` list and `check_known` gate. |
| `web/backend/main.py` | the `/api/acefw/*` routes route by the protocol Klipper reports: `v1` goes to `ace1_flash`, `v2` keeps `ace2_ota` unchanged. `/api/acefw/versions` gains `gen1_versions`. |
| `klipper/extras/ace.py` | `ACE_FW_RELEASE` no longer refuses a V1 - the gate now accepts both generations (no new command; `ACE_FW_RESUME` was already generation-agnostic). |
| `web/frontend/app.js`, `index.html` | the firmware card lists both generations; selecting a V1 switches the allowlist and hides the Gen-2-only fields (`.swu` password, ACE2-Open patch, force). |
| `i18n/en.json`, `i18n/de.json` | the few new strings. `zh.json` has no acefw block upstream, so it keeps falling back to English. |
| `tests/gen1_flasher_selfcheck.py` | **new.** The project has no test suite, so this is the test-in-a-script: protocol vectors plus a full simulated flash over a fake transport (no hardware, no pyserial). |
| `docs/GEN1_FLASH.md` | this note. |

## Why a separate transport, not a mode in `ace2_ota.py`

The two generations share the framing bytes and the idea of an IAP
sequence, but nothing on the wire:

* **Gen 1 has no OTA updater.** The stock Gen-1 firmware answers JSON-RPC
  over the framed serial link; there is no equivalent of the ACE 2
  protobuf-style commands `2/3/4`. The Gen-1 commands are
  `iap_upgrade{size,crc,version}` + 64-byte `0x55` chunks + `iap_upgrade_finish`.
* **The gates differ.** A Gen-1 version string does **not** identify an
  image: stock and the OpenCubic CFW both report `1.3.863`. The Gen-1
  allowlist is therefore keyed by image id and gated on the exact md5
  (plus size and CRC), while the ACE 2 list is keyed by version.
* Keeping them apart means a Gen-2 change can never move the Gen-1 wire,
  and the Gen-1 flasher can be reviewed against the bench flasher line by
  line.

## The flow (both generations)

1. Klipper releases the port: `ACE_FW_RELEASE ACE=n`. The command
   disconnects the unit and holds every reconnect path (`_open_ace`
   refuses a held index), so nothing fights the flasher for the serial
   port. `ACE_FW_RESUME` hands it back; the web card does both
   automatically.
2. The backend opens the port and flashes with the matching engine
   (Gen 1: `ace1_flash`, Gen 2: `ace2_ota`), with progress in the card.
3. After the commit the unit reboots; the flasher waits for it to answer
   again (Gen 1: `get_info`, then `get_status` as the application probe).
4. **Rollback = re-flash a known image.** There is no separate undo: pick
   the stock entry in the same card and flash it. Gen-1 flashing never
   overwrites the bootloader, so a failed boot falls back to update mode
   and can be re-flashed.

## Traps (keep these documented)

* **Never flash via `/etc/init.d/S60klipper start|restart`.** That path
  runs the printer's own update check and re-flashes the stock ACE
  firmware over a modified image (observed on hardware: the device went
  back to `V1.3.863` and `/tmp` was wiped). Use the web card, and a
  Moonraker `POST /printer/firmware_restart` to bring Klipper back.
* **Release the port first.** A flasher that opens the port while Klipper
  holds it fights the heartbeat and the flash fails. `ACE_FW_RELEASE` is
  the one supported path; it is not persisted, so a Klipper restart
  mid-flash fails the flash loudly instead of stranding a unit.
* **Stop a running dry cycle before flashing.** The release stops the
  cycle multiACE started, but a manual one survives until the flash
  reboots the unit (the reporter's procedure stops it explicitly with
  `drying_stop`). Stop it from the web UI / `ACE_EXT_RAW` first.
* **Keep the link busy.** A Gen-1 unit self-resets after roughly 3.3 s
  without traffic (an unconnected ACE Pro USB-reboots every ~3.5 s until
  something talks to it). The chunk stream keeps the link busy during the
  write; `get_status` pings cover the waits (`prime`, the post-commit
  poll). Do not insert long pauses into the write loop.
* **One image per gate entry, no licence claims.** `ace1_flash.KNOWN_FIRMWARE`
  carries a `source` line naming whose release asset the exact bytes are.
  multiACE ships none of these images and states nothing about their
  licence - OpenCubic ships no licence file, so a source line is the
  honest maximum.

## The Gen-1 tested-images list

| entry id | image | size | CRC-16 | md5 |
|---|---|---|---|---|
| `1.3.871-tunnel` | Godless50/ACE-PRO-v1.-NFC-UID asset `ACE_V1.3.863_tunnel_ops.bin` (OpenCubic CFW base + UID stub + RC522 tunnel, reports CV1.3.871) | 114632 | `0x1AC9` | `219df3df77f7c7e1e15a79d580a2379e` |
| `1.3.863-stock` | clean stock `ACE_V1.3.863_20250518.bin` (rollback target) | 105652 | `0xDEFB` | `dcd04589dcadd5b4feab66d33e772531` |

To add an image: flash a dry run with it, read size/CRC/MD5 off the
result, verify on hardware, then add the entry - and only then fill its
`tested` note. The same rule as `ace2_ota.KNOWN_FIRMWARE`.

## Open items for the bench

* The Gen-1 entries carry `"tested": ""` on purpose - the PR adds the
  images with their source lines, but no multiACE hardware has flashed
  them through this transport yet.
* The reporter never confirmed a `856 -> 863` direct update; their unit
  was already on 1.3.863. Whether the bootloader validates the announced
  version against the running one is untested.
* `get_status` as the Gen-1 application probe is inferred from the bench
  flasher's post-flash polling, not from bootloader/app research like the
  ACE 2's `ace_app_alive`. A unit that answers `get_info` but no
  `get_status` is reported (not hidden), but the exact meaning needs a
  hardware datapoint.

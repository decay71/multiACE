# multiACE Engine API

This document defines the **stable interface** that external software (a
print host, a slicer plugin, an orchestration script) uses to drive the
multiACE engine. It is both the technical contract and the basis for keeping a
separate program at arm's length from the GPL-licensed engine.

Companion documents: `LOADOUT_API.md` (the HTTP API of the multiACE web
backend: read the loadout, hand a file back) and `SEND_TO_MULTIACE.md` (the
upload endpoint in detail).

## 1. Boundary & licensing

multiACE is a Klipper `[extras]` module and is **GPL-3.0**. Any code that
imports it (`import ace`, subclassing its objects, etc.) becomes a derivative
work and inherits the GPL.

An external program stays a **separate work** by talking to the engine only
through **arm's-length IPC** - never importing engine modules:

- **Moonraker REST/WebSocket** is the bus.
- The **gcode command vocabulary** (section 3) and the **`ace` status object**
  (section 4) are the entire contract.

As long as the external program communicates only via these, it is not a
derivative of multiACE and may carry its own licence. Recipients of the
engine still get its source (GPL §3); the external program does not.

## 2. Transport

| Direction | Mechanism |
|-----------|-----------|
| Command   | `POST /printer/gcode/script` with `{"script": "ACE_… …"}` |
| Read state| `GET /printer/objects/query?ace` |
| Live state| `POST /printer/objects/subscribe` for `ace` (+ `print_stats`) over the Moonraker WebSocket |

Commands are plain gcode; parameters are `KEY=VALUE` tokens. Moonraker enforces
print-state rules (busy/paused/printing) on its end. Commands should be treated
as **idempotent-safe to retry** on transport errors.

## 3. Command vocabulary (stable contract)

These are the commands an orchestrator relies on. `[..]` = optional.

### Index base - read this before writing a script

Every **machine-readable** index is **0-based**: the gcode parameters below
(`HEAD=`, `ACE=`, `SLOT=`), the status object of section 4, and the fields of
the events in section 5. So HEAD/SLOT 0-3 and ACE 0-3 (device index).

**Human-readable output is offset.** Console messages, `klippy.log`, the web UI
and the pause/error texts multiACE raises on the touchscreen add
`[ace] display_index_base`, which the shipped config sets to **1**. (The
touchscreen's own tile labels are the stock firmware's numbering and are not
ours to shift.) The same slot therefore reads

| where | how it appears |
|---|---|
| gcode you send | `SLOT=1` |
| status object / events | `"slot": 1` |
| log line, web UI, multiACE popup | `Slot 2` |

Nothing is converted on the way in: an index you send is always taken as
0-based, whatever the logs display. Set `display_index_base: 0` in `[ace]` to
make logs and UI match the API - that changes presentation only, never the
values on the wire.

### Filament routing

| Command | Parameters | Effect |
|---------|-----------|--------|
| `ACE_LOAD_HEAD` | `HEAD=n [ACE=n] [SLOT=n]` | Load a toolhead from an ACE slot. A head without ACE (feeder, manual) loads via its side feeder; `ACE`/`SLOT` are ignored there. |
| `ACE_UNLOAD_HEAD` | `HEAD=n [RETRACT_LENGTH=mm] [KEEP_HEAT=temp]` | Unload a head back to its ACE. `KEEP_HEAT>0` holds the hotend at `temp` (no cold cool-down). A head without ACE gets the tip-pull only; the strand is taken out at the side port. |
| `ACE_UNLOAD_ALL_HEADS` | - | Unload every loaded head, heads without ACE included (tip-pull only). |
| `ACE_SWAP_HEAD` | `HEAD=n ACE=n [SLOT=n] [ANTI_OOZE=mm] [INITIAL=1]` | Mid-print swap: unload current, load new. A no-op when the head already holds that ACE/slot. `ANTI_OOZE` = the end-of-swap retract, sized to the un-retract the following gcode will push back (default `swap_anti_ooze_retract`). `INITIAL=1` marks a swap inside the auto-load block at print start (parks at the discard position instead of restoring a print position). The flush length comes from `ACE_SET_PURGE`, not from a swap parameter. Skipped (log only) on a head without ACE (feeder, manual). |
| `ACE_SWITCH` | `TARGET=n [AUTOLOAD=1]` | Make ACE `TARGET` the active device. |
| `ACE_RETRACT` | `INDEX=n LENGTH=mm [SPEED=mm/s]` | Low-level retract of a slot. |
| `ACE_FEED` | (feeds active slot) | Low-level feed. |
| `ACE_CLEAR_HEADS` | `[HEAD=n]` | Clear head→ACE/slot mapping (all or one head). |

### Purge / waste control

| Command | Parameters | Effect |
|---------|-----------|--------|
| `ACE_SET_PURGE` | `LENGTH=mm` \| `RESET=1` \| `MATRIX=0|1 [PERSIST=0]` | Set the swap/load flush length for upcoming flush(es). `LENGTH` is 0-200 mm; `LENGTH=0` = stock default (80 mm). `RESET=1` reverts to the `swap_purge_length` config value. `MATRIX=0|1` ignores/honors per-pair `LENGTH` stamps and WRITES the `purge_matrix` config line (write-through); with `PERSIST=0` the change is RAM-only and the config value returns at restart. Settings whose RAM value deviates from their config line are listed in the status field `settings_volatile`. Intended for per-colour-pair purge fed from the slicer. Every settings setter (`ACE_SET_PICKUP_CLEANING`, `ACE_SET_AIRPRINT_DETECTION`, `ACE_SET_QUAD_REPLENISH`, `ACE_SET_CONFIRM_COMMANDS`, `ACE_SET_SPOOLMAN`, `MULTIACE_SET_LANGUAGE`, `ACE_BG_SET_HEAD`) follows the same write-through contract: it edits its config line and accepts `PERSIST=0`. |

### Per-head mode

| Command | Parameters | Effect |
|---------|-----------|--------|
| `ACE_SET_HEAD_MANUAL` | `HEAD=n ENABLE=0\|1` | Toggle manual bypass for a head (no ACE feed/retract/feed-assist/RFID; loads via its side feeder or the display manual load). Multi mode. Persisted. |
| `ACE_SET_HEAD_ACE` | `HEAD=n ACE=n` | Head mode: wire a head to the ACE that feeds it (1:1 - one ACE per head, never shared). Persisted. |
| `ACE_SET_HEAD_FEEDER` | `HEAD=n ENABLE=0\|1` | Head mode: the head loads/unloads via the printer's own side feeder and the ACE never touches it. Persisted. |

The operating mode itself (`normal` / `multi` / `head`, status field `mode`) is
switched by the user via the web UI; a host should read it and plan
accordingly rather than switch it. In **multi** mode slot *N* feeds head *N* on
every unit; in **head** mode each ACE-driven head is wired to one ACE
(`head_ace`) and swaps between that unit's four slots, while `head_feeder`
heads print one fixed colour from the side feeder.

### Background swap (head mode, experimental)

| Command | Parameters | Effect |
|---------|-----------|--------|
| `ACE_BG_SWAP` | `HEAD=n SLOT=n [ACE=n] [TEMP=] [ANTI_OOZE=] [PURGE=mm] [QUIET=1] [FORCE=1]` | Unload and reload a **parked** head's ACE slot while another head prints, so the arrival toolchange becomes a no-op. Requires the head to be opted in (`ACE_BG_SET_HEAD HEAD=n ENABLE=1`, a hardware declaration that the dock is open below). `QUIET=1` turns every refusal into a log line (the arrival then swaps inline). Requires the `[ace_bg_swap]` section. **Not yet part of the stable contract** - parameters may still change. |

### Feed assist

| Command | Effect |
|---------|--------|
| `ACE_ENABLE_FEED_ASSIST` / `ACE_DISABLE_FEED_ASSIST` | Enable/disable feed assist. |

### Dryer

| Command | Parameters |
|---------|-----------|
| `ACE_START_DRYING` | `TEMP=… [DURATION=…]` |
| `ACE_STOP_DRYING` | `[ACE=n]` |
| `ACE_DRY` | `ACE=n [TEMP=] [DURATION=]` |

### Batch / orchestration helpers

| Command | Parameters | Effect |
|---------|-----------|--------|
| `ACE_SEQ` | `PLAN=… [UNLOAD=0\|1]` | Scripted load/unload sequence. `PLAN` tokens: `H:A`=load HEAD from ACE, `A0`=all from ACE 0, `U`=unload all, `U0`=unload head 0. `UNLOAD` (default 1) runs a final unload-all. |
| `ACE_PRELOAD` | same syntax as `ACE_SEQ` | Preload heads/slots; `UNLOAD` defaults to 0 (no final unload). Use to **pre-stage** the next colour while printing. |

### Diagnostic (NOT part of the stable contract)

`ACE_HEAD_STATUS`, `ACE_LIST`, `ACE_USB_STATS`, `ACE_DEBUG`, `ACE_TEST`, and the
low-level `A_*` protocol pokes (`A_FEED`, `A_STATUS`, `A_FEEDCHECK`, `A_RAW`, …)
are for humans/debugging. They expose raw protocol behaviour and **may change
without notice** - do not build an orchestrator on them.

## 4. Status object (`ace`)

All indices below are 0-based, independent of `display_index_base` (see
section 3).

`GET /printer/objects/query?ace` returns:

```jsonc
{
  "api_version": 1,                 // engine contract version (section 6)
  "status": "ready|busy|unknown",   // active device aggregate
  "temp": 0,
  "dryer_status": { ... },
  "gate_status": [ ... ],           // active device, per-slot gate flag
  "active_device": 0,
  "device_count": 1,
  "swap_phase": "idle",             // idle|unload|load|flush|done (finer than swap_in_progress)
  "last_swap_result": null,         // { head, ace, slot, status, ts } of the most recent swap, or null
  "event_seq": 0,                   // monotonic; bumps on every emitted engine event
  "head_source": { "0": {ace_index, slot, type, color, brand, ...} | null, ... },
  "head_manual": { "0": false, "1": false, "2": false, "3": false },
  "mode": "multi",                  // normal|multi|head
  "head_ace":    { "0": 0, "1": 1, "2": 2, "3": 3 },   // head mode: head -> ACE wiring
  "head_feeder": { "0": false, ... },                  // head mode: side-feeder heads
  "settings_volatile": [],          // settings whose RAM value deviates from the config line
  "swap_in_progress": false,
  "aces": [
    {
      "idx": 0,
      "connected": true,
      "protocol": "v1|v2",
      "status": "ready|busy|unknown",
      "temp": 0, "humidity": null,
      "dryer_status": { ... },
      "gate_status": [ ... ],
      "feed_assist": -1,            // armed slot, -1 = none
      "slots": [
        { "index": 0, "status": "empty|ready|…", "sku": "",
          "material": "", "rfid": 0, "brand": "", "color": [r,g,b] }
        // color is the RAW device triple. [0,0,0] is ambiguous on its
        // own - it is the tag's black only when "material" is set,
        // otherwise the slot declares no colour. Engine-internal
        // captures already carry that resolved: "" = unknown,
        // "000000" = a declared black.
      ]
    }
  ]
}
```

Key fields for orchestration:
- **`api_version`** - engine contract version; gate capability on this (section 6).
- **`head_source[h]`** - which ACE/slot currently feeds head `h` (null = unloaded).
- **`head_manual[h]`** - head is in manual bypass (skip it in plans).
- **`swap_in_progress`** - a swap is running; don't issue conflicting commands.
- **`swap_phase`** - finer swap state (`idle|unload|load|flush|done`); a host can
  pre-stage on `flush` or wait for `idle` before the next action.
- **`last_swap_result`** - `{head, ace, slot, status, ts}` of the most recent swap
  (`status='ok'` or a failure tag such as `unload_failed`/`load_failed`/`slot_empty`).
- **`event_seq`** - monotonic counter bumped on every emitted event (below); compare
  across polls to detect a missed update.
- **`aces[i].status`** / **`slots[].status`** - device/slot busy vs idle, used to
  wait for an operation to finish (e.g. a rollback returning to `ready`).
- **`aces[i].slots[].rfid` / `material` / `color`** - spool identity.
- **`mode`** / **`head_ace`** / **`head_feeder`** - the routing topology (section 3,
  per-head mode); read it before planning which slot can serve which head.

The object carries further fields (spool table and bindings, Spoolman and
SpoolLink state, tag read/write progress, per-spool pressure advance, dryer
automation, calibration). They are **additive** (section 6): a host may ignore
them, and their shape is not yet frozen - build on the fields listed above.

Filament type/colour is otherwise a **free string** pushed via the stock
`SET_PRINT_FILAMENT_CONFIG` (the engine does not enforce a material whitelist).

## 5. Events / observability

Two complementary channels:

**a) Status-object transitions (subscribe-based).** Subscribe to the `ace` object
(and `print_stats`) and react to `swap_phase`, `last_swap_result`, `swap_in_progress`,
slot `status`, `head_source`. `event_seq` lets you detect a missed update between
polls. This is sufficient for the section 7 flow.

**b) Push events (low-latency).** For timing-sensitive actions (pre-purge / pre-stage
before a swap) the engine also emits machine-readable lines on Moonraker's
`gcode_response` stream - subscribe to it over the WebSocket. Format:

```
multiace_event <name> key=val key=val ... seq=<n>
```

Emitted events:

| Event | When | Fields |
|-------|------|--------|
| `swap_imminent` | a swap has started, before the unload | `head ace slot from_ace from_slot seq` |
| `slot_ready`    | the new slot is loaded and wiped, swap about to finish | `head ace slot seq` |
| `swap_done`     | swap completed successfully | `head ace slot status seq` |
| `swap_failed`   | swap exited via recovery/abort (paired with a paused error) | `head ace slot status seq` |
| `resistance_suspect` | a passing load-time flow measurement sat far above the lane's own baseline (back-pressure / partial blockage suspect); with `resistance_pause` on, the second suspect on a lane or head pauses the print | `head ace slot site delta baseline strikes head_strikes seq` |
| `airlog_chew` | the in-print flow sampler (`[ace] airlog: true`) saw sustained huge back-pressure while extruding (chew / air-print suspect); with `resistance_pause` on this pauses the print | `head delta run seq` |

`status` on `swap_failed` carries the failure tag (`unload_failed`, `load_failed`,
`slot_empty`, …). `error` is the generic fallback for an exit that names no tag of
its own - it means "the swap failed", nothing more; the paused error message that
comes with it is the specific one. `ok` is never a legal value on this event.
All push events are best-effort: a transport hiccup never disturbs
the swap, so treat the status object (channel a) as the source of truth and the push
events as latency hints.

Every event is additionally mirrored to `klippy.log` as
`[multiACE] multiace_event <name> …`. The `gcode_response` stream is live-only - Moonraker keeps roughly the last 1000 console lines and nothing on disk - so the log
is where an event can still be read after the fact. Use it for post-mortems and bug
reports; do not poll the log as a transport.

## 6. Versioning

The `ace` status object exposes **`api_version`** (currently `1`). It bumps only on
a **breaking** change to the command vocabulary (section 3) or the status-object
shape (section 4); additive changes (new optional fields, new push events) do **not**
bump it. A host should read `api_version` once and refuse / degrade if it sees a
major version it does not support.

## 7. Typical external-host flow (example)

1. Subscribe to `ace` + `print_stats`.
2. Ingest slicer gcode, derive the toolchange/swap plan and a per-colour-pair
   purge map.
3. While printing colour A, `ACE_PRELOAD` the next colour into a free slot.
4. Before each swap: `ACE_SET_PURGE LENGTH=<pair>`; then `ACE_SWAP_HEAD …`;
   then `ACE_SET_PURGE RESET=1`.
5. Wait for `swap_in_progress=false` and the slot `status` to settle before the
   next action.

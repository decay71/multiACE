# Flexi mode

Flexi is a fourth multiACE operating mode (`ace__mode = 'flexi'`) next to
normal, multi and head. Any ACE slot can feed any toolhead through PTFE
splitters (joiners), with up to 4 units (ACE Pro and ACE Pro 2, mixed). Flexi
reuses the multiACE loader, unloader, preflight and web UI; it adds:

- per-slot routes (`ACE:slot` → toolhead) and splitter groups, edited in
  **Config → Paths**;
- a path ledger, so two slots never feed into the same shared tube;
- route discovery: feed one slot slowly until a toolhead sensor sees it, then
  pull it back (no heating; stops on an empty slot);
- runout backup limited to spares that are physically routed to the head, with
  a per-head backup order (**Prepare to print → Check runout backup**);
- *Load several*: pick slots and load them one after another;
- large G-code (up to 2 GB) analysis and preparation on the printer.

normal, multi and head keep their behaviour. Every Flexi-only rule sits behind
an explicit `_ace_mode == 'flexi'` check.

## Install

`install_multiace.sh` installs the Flexi modules with the rest of multiACE.
It also:

- adds `extended/klipper/flexi.cfg` if it is missing (the `[flexi]` section
  that loads Flexi's motion guards; PAXX12's `[include extended/klipper/*.cfg]`
  picks it up) and never overwrites an existing one;
- adds `flexi` to the mode list of PAXX12's `/etc/init.d/S58multiace-mode`
  (one word, backup kept as `S58multiace-mode.pre_flexi`; root only), so the
  ACE modules are applied at boot in Flexi as in multi/head.

`uninstall_multiace.sh` removes them again and restores that backup.

## First-time setup

1. **Config → Initial setup**: press **Flexi**. Klipper restarts once.
2. **Config → Paths**: enter which head each slot feeds, or use
   **Find routes automatically** (stay at the printer the first time).
3. Flexi does not know what is in the tubes on a fresh install, so every path
   starts as *uncertain*. Check the tubes by eye and record that on the
   dashboard (recovery panel) before the first load.
4. Load each head once from the dashboard and watch the first load. Long
   tubes may need a per-slot `load_length` / `retract_length` (the discovery
   table suggests values).

The web page has a built-in Help (top bar) with a chapter per topic.

## Safety rules in the code

- The Flexi motor gate classifies every ACE request method as motion or
  non-motion (`FLEXI_MOTION_METHODS` / `FLEXI_NON_MOTION_METHODS` in
  `ace_flexi_engine.py`); a test fails if a new method is not classified.
  The raw debug commands (`A_FEED`, `A_ROLLBACK`, `A_SPEED`, `A_RAW`, `A_DRY`,
  `A_RFID_TEST`) are refused in Flexi only.
- `flexi_h1_adapter.py` / `flexi_h2_interceptor.py` guard ACE motor requests
  and the tip-forming moves; they are loaded by `[flexi]` in `flexi.cfg`.
- The stock online updater (`ACE_UPDATE_APPLY`, web **Update**) refuses on a
  Flexi build, because it would replace Flexi with a plain release.

## Changes that apply to every mode

These came out of Flexi testing and are tagged `@MULTIACE_FLEXI_SHARED`:

- runout backup policy (`ace_failover.py`) and its read-only preview;
- config writes are atomic, owned like their folder and always readable by
  Klipper (a root-owned 0600 `ace.cfg` halted Klipper);
- an ACE Pro on stock/CV firmware offers a manual tag read through the normal
  recognition cycle (`tag_read_mode` = `gen1_stock`);
- comms-loss alerts wait for the reconnect outcome while idle (an ACE Pro
  drops off USB for a moment when Klipper restarts);
- identical-spool twin binding, the Gen1 RC522 tunnel probe on every community
  build, the Fluidd macro layout keeping user categories in place.

## Finding Flexi code

Every place where original code calls into Flexi, branches on Flexi, or was
changed by the Flexi work carries a tag:

| Tag | Meaning |
|---|---|
| `@MULTIACE_FLEXI_HOOK` | call into a Flexi module or a Flexi-only branch |
| `@MULTIACE_FLEXI_PATCH` | original UI behaviour adjusted for Flexi |
| `@MULTIACE_FLEXI_COMPAT` | glue keeping original behaviour identical while Flexi data exists |
| `@MULTIACE_FLEXI_SHARED` | a change for every mode (list above) |

```bash
grep -rn "@MULTIACE_FLEXI_" multiace/
```

Files whose header says `Flexi-owned module` (`ace_flexi_*.py`, `flexi*.py`,
`flexi-*.js`, `flexi.cfg`) belong to Flexi entirely.

## Status

Developed and tested on a Snapmaker U1 (PAXX12) with two ACE Pro units and
splitters: loading, unloading, Unload All, route discovery and runout backup
selection. This branch merges Flexi onto multiACE 1.30b (`87e4b44`); the
merged web side still needs a printer test on this exact branch.

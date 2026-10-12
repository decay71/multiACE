# Gen-1 (ACE Pro) tag tunnel - `ACE_TAG_READ`

The ACE Pro's own reader understands **Anycubic tags only**: a third-party
spool (OpenSpool NDEF, a blank NTAG, a Bambu/Snapmaker MIFARE chip) arrives
with no SKU - or an SKU no table entry carries. The **community firmware**
(`CV1.3.87x`) adds an RC522 tunnel on the existing `filament_recognition`
command, so multiACE can drive the reader itself and read those tags
directly: the card UID always, and the OpenSpool material/colour when the
tag carries an NDEF record.

Companion documents:

* the tunnel contract (packed index, ops 0..8, antenna map, gotchas):
  `REPORT-RC522-TUNNEL-EN.md` in
  <https://github.com/Godless50/ACE-PRO-v1.-NFC-UID>
* the host implementation: `multiace/klipper/extras/ace_gen1_tunnel.py`
* the self-check: `tests/gen1_tag_tunnel_selfcheck.py`

## What it does

* **Detects the tunnel safely.** Two gates, both must pass: the runtime
  firmware string is a known tunnel build (`CV1.3.87x`), and a probe op
  actually answers through the stub (a `result.code` exists - a stock reply
  never has one). A stock unit (`V1.3.863`) and the earlier UID-only
  community build (`CV1.3.863`) see **no tunnel traffic at all**. If the
  firmware matches but does not answer, exactly one probe is sent, logged
  once, and the feature is dropped for that firmware.
* **Reads a tag.** `acquire` (op 7, holds the reader) → `SELECT` on the
  slot's reader channel (op 6, `slot_channel(slot)`) → NTAG `READ(0x30)` of
  the page (the exact host order: `TXMODE |= 0x80`, `RXMODE |= 0x80`,
  `BitFraming = 0`, FIFO writes, `TRANSCEIVE`, RX bits, FIFO reads) →
  `release` (op 8, mandatory). Page 0 yields the 7-byte UID with both
  ISO14443-3 BCC check bytes verified (the shared `ace_rc522` check); an
  NTAG capability container additionally triggers a bounded read of the
  OpenSpool user area (pages 4..39), decoded by the shared `ace_rc522`
  OpenSpool decoder.
* **Reads only on demand.** Every tunnel TRANSCEIVE blocks the firmware
  for about 7 s (its lamps blink, its gates flicker), and a full read
  holds about ten of them. So nothing reads automatically, neither at
  connect nor after an insert (a session during an insert broke the
  insert and took the unit off the bus, HW 2026-10-03): a tag is read
  only by `ACE_TAG_READ`, i.e. the web Read button. The result lands in
  multiACE's own per-unit store, never in `_info_per_ace`. Binding is
  **gated on attribution**: the two slots of an antenna pair share one RF
  path, so the card UID is offered to the **existing** tag-bind path
  (`_spool_bind_by_tag(..., unbind=False)`) **only when the partner slot
  (`slot ^ 1`) reads empty in the same status, or when the partner's
  card UID is already known** (its last tunnel read, or a UID code on the
  spool bound there). A known partner card is put to sleep with HLTA
  during the read, so the next SELECT answers with the other card; a
  result can then never be the partner's. An occupied partner with an
  unknown card still stores and surfaces the read but does not bind (a
  wrong first binding has no repair path on a Gen 1); the operator probe
  `ACE_TAG_READ` is the explicit override, always binds and also skips a
  known partner card.
  `unbind=False` on purpose: a tunnel read must never release a binding
  the vendor path owns.
* **Surfaces it.** `get_status`: the slot's `uid` / `tag_format` are filled
  from the tunnel read **only when the device delivered none** (a device
  value always wins), and each ACE carries an additive `tag_tunnel` block:

  ```json
  "tag_tunnel": {
    "enabled": true,
    "available": true,
    "reads": {
      "0": {"uid": "04225251C82A81", "format": "openspool",
            "material": "PETG", "color": "DE3530", "brand": "Creality",
            "bound": true, "age": 12.3}
    }
  }
  ```

## The command (operator probe)

```
ACE_TAG_READ ACE=<n> SLOT=<0..3> [PAGE=<n>]
```

* `PAGE` defaults to `0` (the UID page). Any page can be dumped.
* On a Gen 1 it prints the raw 16 bytes and the UID:

  ```
  [multiACE] ACE 0 slot 2 page 0: 04 22 52 FC 51 C8 2A 81 32 48 00 00 E1 10 6D 00
  [multiACE] ACE 0 slot 2: UID 04225251C82A81 (ntag) - third-party tag
  ```
* When no card answers and the lane may move (no print running, the slot
  feeds no head), it rotates the spool forward in 20 mm steps until a card
  answers (at most 650 mm, below the shortest path to a combiner), reads
  it and pulls the lane back by the same distance. Otherwise it reads in
  place.
* It is the only tunnel read; nothing reads automatically.
* On an ACE 2 the command keeps its previous meaning (rotate + read +
  bind); the Gen-1 branch is only taken for a non-V2 protocol.

## Always on

`ACE_TAG_READ` uses the tunnel on every ACE Pro whose firmware matches
the tunnel builds (`CV1.3.87x`) **and** answers a probe op through the
stub; a stock unit never receives tunnel traffic, so there is no switch. To go
without it, flash the stock firmware. The former `gen1_tag_tunnel` option
is still read and ignored, so an existing config line does not halt
Klipper; `ACE_SET_TAG_TUNNEL` only reports that the switch is gone.

## Limits

* **Community firmware required.** The tunnel exists only on the
  community build `CV1.3.87x` (verified reference `CV1.3.871`). Stock and
  the UID-only community image are never touched.
* **Searching for the tag.** The tag only answers while it faces the coil.
  When `ACE_TAG_READ` finds no card and the lane may
  move, it rotates the spool forward (`feed_filament`, 20 mm
  steps, at most 650 mm, after the slot reports `ready`), stop at the first
  card and pull the lane back by the distance moved (`unwind_filament`;
  the ACE Pro has no decoder, so it is the commanded distance). Never
  during a print, never on a slot that feeds a head. A read starts only
  once no slot of the unit reports `preload` (the firmware's own pull-in,
  17-25 s measured). The
  pull-back waits up to 60 s while the unit is busy with another slot
  (it answers FORBIDDEN then).
* **Unreadable card at the field edge.** A card that answers SELECT but
  whose page read fails is re-read further in (+20, +40 mm) and just
  before the hit (-20 mm), like the ACE 2 centring.
* **Whose card?** When the partner slot holds an unknown card, the
  partner lane is rotated (20 mm steps, at most 200 mm, then back): the
  card leaves the field -> it is the partner's (stored for the partner,
  our lane is searched with it skipped); it still answers -> ours, bind.
  For `ACE_TAG_READ` only; the partner must be idle
  and feed no head.
* **Unit-wide wait.** The ACE Pro refuses motor commands while any slot
  of the unit still moves, so a search starts only when every slot is
  ready or empty.
* **Firmware identity is vetted.** The community firmware reads tags on
  its own, but cannot say which bay of the shared antenna answered (or
  replays an old record). While the partner slot is occupied, our own
  read decides: our OpenSpool read replaces the firmware identity, and a
  card of ours whose user pages were ALL read without an OpenSpool record
  hides a firmware OpenSpool identity. Until such a read exists (pending,
  running, or only the UID came through) the firmware identity stays
  shown.
* **Never during an insert.** A tunnel session holds the reader the
  firmware needs to pull in and identify a new spool. No session starts
  while any slot of the unit reports `preload` or `shifting`, and a
  running session stops at its next op when an insert starts or a gate
  of the unit changes; the reader is released at once and the read is
  retried when the unit is quiet. A session overlapping an insert made
  the preload drop the new spool and crashed the ACE off the USB.
  Lanes the session moved itself are not counted: the ACE Pro reports
  our own search step as `shifting` too, and a real re-insert on such a
  lane still flips its gate. While a stopped read waits for its retry,
  the slot keeps the identity it showed before.
* **Only real moves count.** A search step counts only when the ACE
  answers `success`; `empty` ends the search. A spool pulled or
  re-inserted during the search ends it, and the new spool is never
  pulled back.
* **Survives a restart.** The reads are persisted (`ace__gen1_tag_reads`,
  save_variables) and restored at startup; an empty slot evicts them, a
  fresh read replaces them. A spool swapped while the printer was off
  keeps the old UID until its card is read again (same as the ACE 2).
* **Never during a print.** A tunnel session holds the unit's reader;
  `ACE_TAG_READ` stays available during a print (read in place, no
  rotation).
* **A TRANSCEIVE blocks the firmware ~6.9 s** on the tunnel build (every
  page read, data or not; FA log 2026-10-03). The reply wait for op 3 is
  10 s, and any op without a reply ends the session (release only), so
  no further op is queued into a unit that is still blocked.
* **One read per unit at a time.** A manual `ACE_TAG_READ` issued while
  another read runs on the same unit is queued behind it (at most 180 s)
  instead of being refused.
* **Two antennas, two bays each; reader channel = bit-swap.** Antenna 1
  covers slots 0 and 1 (reader channels 0 and 2), antenna 2 covers slots 2
  and 3 (channels 1 and 3). The reader **channel** for a slot is
  `0,1,2,3 -> 0,2,1,3` (`((slot & 1) << 1) | ((slot >> 1) & 1)`,
  `slot_channel()`); the **partner slot** sharing the antenna is
  `slot ^ 1`, whose channel is `channel ^ 2`. An earlier live run saw
  readers 0/2 and 1/3 answer with the same UID - that is the shared RF
  path, and the reason the automatic bind is gated on the partner slot
  reading empty or carrying a known card (see above). Two slots reading the same UID cannot both
  bind: the existing duplicate guard refuses the second one, and an
  unattributed read is stored but never bound.
* **Read cost.** One page read is ~25 tunnel commands (~0.5-1 s on the
  wire); the OpenSpool user-area read adds 9 more. A per-command timeout
  bounds every op; a read session is always closed with `release`, and a
  failed release is logged once (the unit may need a power cycle - it
  would otherwise stay paused and report `status=busy`).
* **UID form.** The UID is the ISO14443-3 one (`page0[0:3] +
  page1[0:3]`, BCC-verified): `04225251C82A81`. The tunnel report's UID
  column prints raw page bytes 0..6 (BCC0 instead of UID6) - matching the
  project's existing `card_uids`/phone-confirmed form is deliberate.
* **Gen 1 only.** V2 behaviour is untouched; the automatic tick is a
  hard no-op for a V2 unit.

## Tests

The repository has no CI beyond the release tarball; this is the
test-in-a-script, under `tests/` alongside the Gen-1 flasher self-check:

```sh
python3 tests/gen1_tag_tunnel_selfcheck.py
```

It imports the real `ace.py`, `ace_gen1_tunnel.py` and `ace_rc522.py` and
drives them on hand-built fakes (no Klipper, no hardware): the
packing/signed conversion, the slot -> reader-channel bit-swap map
(`0,1,2,3 -> 0,2,1,3`), the exact op sequence, the reply parsing
(bit-masked `result.code`), graceful degradation (stock firmware sends
nothing; a matching-but-silent firmware gets one probe; a dead link times
out bounded), both genuine live captures (`04 22 52 FC ...` and
`53 42 70 E9 ...`) yielding their bytes and UIDs, an OpenSpool NDEF decode
through the reused `ace_rc522` decoder, and the ace.py wiring (one
attempt per occupancy, the shared-antenna bind gate - partner occupied
with an unknown card -> stored but not bound, known card -> halted - own store, no
`_info_per_ace` writes, get_status surfacing, unchanged V2 path).

## Open points for the maintainer

1. **Surface third-party spools automatically?** The automatic read binds
   by card UID only when a table entry already carries that UID **and the
   partner slot on the shared antenna reads empty**; it never creates
   entries and never releases a vendor binding. If the maintainer prefers
   report-only (no bind call at all), the `bind=False` path is already the
   non-binding one.
2. **Web UI.** The new `tag_tunnel` status block and the slot `uid` /
   `tag_format` fill are already in `get_status`; the web backend passes
   the slot `uid`/`tag_format` through today. Whether to add a Config tab
   toggle and a "third-party tag" badge is a UI decision.
3. **Retry policy.** One attempt per insert is deliberate and cheap; a
   bounded retry ladder (or a "read on next rotation" hook) would raise the
   hit rate on spools whose tag parks away from the coil.
4. **Firmware acceptance.** The version pre-gate accepts `CV1.3.87x`.
   A later tunnel build (e.g. `CV1.3.872`) would pass the gate; the probe
   then decides. If a future build changes the op contract, the constant
   needs revisiting (the reference is `CV1.3.871`).

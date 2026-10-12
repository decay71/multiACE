# Reading the loadout from multiACE

For slicers and other tools that want to know **what is actually loaded on the
printer** before they assign filaments - and how to hand a sliced file back.

Companion documents: `ENGINE_API.md` (the gcode/runtime contract for driving the
engine) and `SEND_TO_MULTIACE.md` (the upload endpoint in detail).

- Base URL: `http://<printer-ip>/multiace/api`
- No auth token. The API is LAN-only, like Moonraker and Fluidd.
- Everything below is read-only unless stated otherwise.

---

## 1. The one call you probably want

```
GET /multiace/api/preflight/livedata
```

Returns the loaded spools and the head wiring in a single response - the exact
shape multiACE's own preflight consumes, so it can never drift from what the
printer really does.

```json
{
  "live_slots": [
    {"ace": 0, "slot": 0, "material": "PLA",  "color": "#26a69a"},
    {"ace": 0, "slot": 1, "material": "PETG", "color": "#000080"},
    {"ace": 1, "slot": 0, "material": "PLA",  "color": "#f4e2c1"}
  ],
  "head_ctx": {
    "mode": "multi",
    "head_nozzles": {"0": 0.2, "1": 0.4, "2": 0.6, "3": 0.8},
    "head_ace":   {"0": 0, "1": 1, "2": 2, "3": 3},
    "ace_heads":  [],
    "feeders":    [],
    "pickup_cleaning": false,
    "bg_available": true,
    "bg_heads": []
  }
}
```

**`live_slots` is filtered on purpose.** It lists only slots whose identity is
physically known - read from an RFID tag or set by the user. A label merely
inherited from a previous print job is *not* a slot identity and is left out, so
you never assign against a guess. Empty slots are omitted entirely; the absence
of a slot means "nothing usable there".

`color` is `#rrggbb`, lower case. `material` is a free string as the printer
knows it (`PLA`, `PETG`, `ABS`, …) - compare case-insensitively.

Returns **409** while any head is set to manual: the matcher is slot-based and a
hand-fed head has no slot. Treat it as "assignment unavailable", not an error.

---

## 2. Index bases - read this before you display anything

Internally every index is **0-based**: ACE 0…3, slot 0…3, head 0…3. Every field
in every API response uses those.

The printer's own UI may show them 1-based - that is a display setting
(`display_index_base`, exposed in `GET /api/state`). It changes **only** what
humans see, never the numbers on the wire.

So: use the API values as they are, and if you print them for a user, add the
offset the printer reports. Mixing the two produces off-by-one errors that look
like a wrong slot rather than a display bug - this has cost real debugging time
on both sides.

---

## 3. Which head can print which filament

Only relevant when nozzle sizes differ across heads; with a uniform machine you
can skip this section.

`head_ctx.head_nozzles` gives the diameter each head carries, straight from the
printer's own configuration:

```json
"head_nozzles": {"0": 0.2, "1": 0.4, "2": 0.6, "3": 0.8}
```

A filament sliced for one diameter can only print on a head carrying that
diameter, because the line widths are baked into the extrusions - multiACE
cannot re-assign across sizes, and will refuse rather than silently produce a
wrong-width print. Within one diameter the choice is free: several heads may
share it, and which ACE a spool sits in is decided by multiACE, not by you.

**In the gcode**, state each filament's nozzle in the standard header field,
indexed **per filament** (one entry per filament, not per extruder):

```
; nozzle_diameter = 0.2,0.4,0.6,0.8,0.2
```

If a filament has no entry, multiACE cannot tell which head it belongs to. It
will show it as unassigned and leave it unconstrained rather than guess.

---

## 4. Head wiring

```
"mode": "multi" | "head" | "normal"
```

- **`multi`** - the common case. Slot *N* feeds head *N*, across all units. A
  colour needed on head 2 can sit in slot 2 of any ACE.
- **`head`** - each head is wired to exactly one ACE (`head_ace` maps head →
  ACE), and prints several colours by swapping between that ACE's four slots.
  `ace_heads` lists the ACE-driven heads; `feeders` lists heads fed by the
  printer's own side feeder, each pinned to a single colour.
- **`normal`** - multiACE is not driving the feed at all.

`head_ace` maps **head → ACE**, not the reverse. Inverting it is an easy mistake
and produces a plausible-looking wrong answer.

---

## 5. Everything else

```
GET /multiace/api/state
```

The full dashboard state. Useful additions beyond `livedata`:

| Field | Meaning |
|---|---|
| `aces[].idx` / `connected` / `protocol` | the units, in index order |
| `aces[].slots[].state` | `empty`, `ready`, … - `empty` means no spool at the gate |
| `aces[].slots[].source` | `rfid`, `override`, `derived`, or absent - how the identity was established |
| `aces[].slots[].material` / `color` / `brand` / `sku` / `subtype` | the identity |
| `aces[].humidity` / `temp` / `dryer` | only ACE 2 units report humidity |
| `toolheads[]` | per head: what is loaded, and from which ACE/slot |
| `display_index_base` | display offset only - see §2 |

`source` is worth respecting: `derived` means the label came from a previous
print job rather than from the spool, which is exactly what `live_slots` filters
out. Use `rfid` and `override` as trustworthy, treat `derived` as a hint.

---

## 6. Sending a file back

```
POST /multiace/api/preflight/inbox     (multipart/form-data, field "file")
```

The file lands in a one-slot inbox; multiACE's web UI offers it for preflight as
soon as the printer is free. Nothing prints without the user confirming.

Always send the **original slicer export**. A file multiACE has already
processed is rejected with **409** - re-processing destroys the tool changes.

Full semantics, status codes and the one-shot delivery rule: see
`SEND_TO_MULTIACE.md`.

---

## 7. Division of labour

**Assignment stays with multiACE.** You do not need to work out which spool
serves which tool, and you should not try to: the decision is made against what
is loaded at the moment the user opens the preflight, which is not knowable at
slice time. Spools get moved, run out, and get swapped between slicing and
printing.

What your side supplies is the file and its declared requirements - colours,
materials, and the per-filament nozzle diameters from §3. What multiACE supplies
is the loadout (§1) so your UI can show the user what is available, and the
matching and rewriting when the file arrives.

The one thing multiACE cannot re-decide is the nozzle a filament was sliced for - that is baked into the extrusion widths. Everything else is negotiable at
print time, which is why it is settled there.

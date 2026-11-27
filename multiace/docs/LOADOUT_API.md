# Reading the loadout from multiACE

For slicers and other tools that want to know **what is actually loaded on the
printer** before they assign filaments - and how to hand a sliced file back.

Companion documents: `ENGINE_API.md` (the gcode/runtime contract for driving the
engine) and `SEND_TO_MULTIACE.md` (the upload endpoint in detail).

- Base URL: `http://<printer-ip>/multiace/api`
- No auth token. The API is LAN-only, like Moonraker and Fluidd.
- Everything below is read-only unless stated otherwise.

| Endpoint | Since |
|---|---|
| `GET /api/preflight/livedata`, `GET /api/preflight/pysrc` | 1.27 |
| `POST /api/preflight`, `/api/preflight/print`, `/api/preflight/inbox` | 1.27 |
| `POST /api/preflight/prefs` | 1.90 |
| `"deliver": "return"`, `GET /api/preflight/result` | 1.94 |
| `GET` and `POST /api/orca/options`, `GET /api/orca/spools` | 1.94 |

`GET /api/version` answers on every version; an endpoint the printer does
not have answers 404. Build for that: check, and fall back.

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
    "head_ace":   {"0": 0, "1": 1, "2": 2, "3": 3},
    "ace_heads":  [],
    "feeders":    [],
    "manual_heads": [],
    "head_nozzles": {"0": 0.4, "1": 0.4, "2": 0.4, "3": 0.4},
    "connected_aces": [0, 1],
    "bg_available": true,
    "bg_heads": []
  },
  "cost_params": {"main": {}, "per_ace": {}}
}
```

`head_ctx` carries more keys than shown, and gains keys between versions.
**Pass the whole object on unchanged** to the preflight code (section 8),
and read only the keys you need for your own display:

| Key | Meaning |
|---|---|
| `mode` | `multi`, `head` or `normal`, see section 4 |
| `head_ace`, `ace_heads`, `feeders` | head mode wiring, see section 4 |
| `manual_heads` | multi mode: hand-fed heads `[{head, material, color}]`. They print one fixed colour and their slot index is not available for MMU colours |
| `head_nozzles` | nozzle diameter per head, see section 3 |
| `head_nozzle_types` | `standard` or `high_flow` per head, firmware 1.6.0 and newer. A missing head means unknown, not standard |
| `connected_aces` | the MMUs connected right now. `null` means the printer could not tell |
| `concurrent_aces` | MMUs that can move one lane while another feeds (needed for background swaps in multi mode) |
| `empty_slots` | `[{ace, slot}]` with no spool at the gate |
| `max_copies`, `copies_strict` | how many spools one colour may use |
| `bg_available`, `bg_heads` | background swap engine loaded, heads enabled for it |
| `pickup_cleaning`, `purge_floor` | settings the rewrite stamps into the file |

`cost_params` are the inputs of the print time estimate, read from the
printer's configuration. Pass them on as they are.

**`live_slots` is filtered on purpose.** It lists only slots whose identity is
physically known - read from an RFID tag or set by the user. A label merely
inherited from a previous print job is *not* a slot identity and is left out, so
you never assign against a guess. Empty slots are omitted entirely; the absence
of a slot means "nothing usable there".

`color` is `#rrggbb`, lower case. `material` is a free string as the printer
knows it (`PLA`, `PETG`, `ABS`, …) - compare case-insensitively.

A hand-fed head does not block the call. It is listed in
`head_ctx.manual_heads` and takes part in the assignment as a head that
prints one fixed colour.

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

### Getting the rewritten file back instead

A slicer plugin that sends the file itself can let the printer do the
rewrite and fetch the result:

```
POST /multiace/api/preflight                 (multipart, field "file")   -> token + preview
POST /multiace/api/preflight/print           {"token", "mode", ..., "deliver": "return"}
GET  /multiace/api/preflight/print/status?job_id=...
GET  /multiace/api/preflight/result?job_id=...
```

With `"deliver": "return"` nothing is uploaded to Moonraker and nothing
starts. When the status reports `done` without `error`, `result` is true and
`size` is the byte count; the result is served **once** and deleted after
that (a second fetch answers 410, an unfetched file goes after ten minutes).
A failed job answers 409 with the reason. `deliver` defaults to `"print"`
(the printer uploads and starts the file) and cannot be combined with
`queue`.

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

## 8. Running the preflight in your own process

A plugin does not have to upload the file to have it rewritten. The printer
hands out the code its own preflight runs, and you run it where the file
is. This is what the browser preflight does, and what the multiACE+ plugin
for OrcaSlicer does.

```
GET /multiace/api/preflight/pysrc
```

```json
{"postprocess": "<source>", "core": "<source>", "swap_cost": "<source>"}
```

Three Python sources, plain standard library, Python 3.10 or newer:

- `postprocess`: `post_process_virtual_toolheads`, the rewriter
- `core`: `preflight_core`, the report and the pipeline
- `swap_cost`: the estimate model. Optional, older printers do not send it;
  without it there is no estimate, nothing else changes

**Fetch them on every run and do not keep a copy.** The rewrite and the
firmware on the printer belong together. A copy from another version
produces a file that printer may not print correctly, and nothing warns you.

### Loading

Execute each source into its own module object. `preflight_core` looks for
the estimate model in its module variable `_swap_cost`; set it when its own
import did not find one:

```python
import types

def load(sources):
    mods = {}
    for name, key in (("swap_cost", "swap_cost"),
                      ("post_process_virtual_toolheads", "postprocess"),
                      ("preflight_core", "core")):
        src = sources.get(key)
        if not src:
            continue
        mod = types.ModuleType(name)
        exec(compile(src, name + ".py", "exec"), mod.__dict__)
        mods[name] = mod
    core = mods["preflight_core"]
    if "swap_cost" in mods and getattr(core, "_swap_cost", None) is None:
        core._swap_cost = mods["swap_cost"]
    return mods["post_process_virtual_toolheads"], core
```

### The report

```python
pp, core = load(sources)
live = get("/api/preflight/livedata")

with open(path, "r", encoding="utf-8", errors="replace") as fh:
    colors, types_, num_aces, _used, plan, meta, header = \
        core.parse_meta(pp, fh, with_header=True)

report = core.build_report(
    pp, slicer_colors=colors, slicer_types=types_, num_aces=num_aces,
    plan_proxy=plan, live_slots=live["live_slots"],
    head_ctx=live["head_ctx"], token="", filename=name,
    size=os.path.getsize(path), meta=meta, header_text=header,
    cost_params=live.get("cost_params") or {})
```

`build_report` raises `ValueError` with a text for the user when the file
cannot be used at all, most often because multiACE has already processed
it. Show the text; do not retry.

The report comes in two shapes. Check `head_mode`:

| | `head_mode` false | `head_mode` true |
|---|---|---|
| When | multi mode, plain | head mode, and multi mode with colour copies, a hand-fed head or background swaps (`topology` then says `multi`) |
| What a colour is assigned to | a slot | a target |
| Candidates | `live_slots[]` `{ace, slot, material, color}` | `targets[]` `{id, kind, head, ace, slot, material, color}`; `id` is `slot-A-S` or `feeder-N`, `kind` is `ace` or `pin` |
| The printer's proposal | `plans.slicer.mapping[]` `{t, slot: {ace, slot}}` | `plans.loadout.mapping[]` `{t, target_id}` |

Both carry `slicer_colors[]` `{t, hex, name, material}` and, per plan,
`swaps`, `estimate` (`total_s`, `per_color[]` with `print_g` and `purge_g`)
and, where background swaps apply, `bg` (`bg_ok`, `unloads`).

Offer a colour only spools of the same material. In multi mode a colour for
head N can only come from slot N, of any MMU; a target already says which
head it feeds.

### The rewrite

```python
final = core.rewrite_pipeline(
    pp, src_path=path, tmp_a=path + ".a", tmp_b=path + ".b",
    slicer_colors=colors, slicer_types=types_, num_aces=num_aces,
    live_slots=live["live_slots"], head_ctx=live["head_ctx"],
    mode=mode, remap_override=remap, head_assignment=assignment,
    head_plan="loadout", meta=meta)
```

- `head_mode` false: `mode="slicer"` and `remap_override={str(t): ace * 4 +
  slot}`, only for colours whose slot differs from the proposal's default
  (`ace * 4 + slot != t`).
- `head_mode` true: `mode="head"` and `head_assignment={str(t): target_id}`
  for every colour. Keep `head_ctx` exactly as the printer sent it; its
  `mode` decides the wiring, the `mode` argument only names the pipeline.

The function works on the two temp paths and returns the one holding the
result; delete the other. `src_path` is not changed.

For bed mesh, timelapse or flow calibration, run
`core.prepend_print_prefs(final, other_path, bed_mesh, camera, flow_cal)`
on the result, and send the same three flags to the printer right before
the print starts:

```
POST /multiace/api/preflight/prefs   {"bed_mesh": true, "camera": false, "flow_cal": false}
```

The printer refuses them (409) once a print runs.

### Do not edit the result

The rewritten file is complete. Lines added, moved or removed afterwards
can break the load sequence at the start of the print and the pairing of
tool changes with their swaps. If your plugin needs something the rewrite
does not do, ask for it in the multiACE repository.

---

## 9. Staging a file in the print queue

Upload the rewritten file to Moonraker's queue folder, not started, then
register it:

```
POST /server/files/upload          root=gcodes, path=multiace_queue, print=false, file=<staged name>
POST /multiace/api/queue           {"filename", "path": "multiace_queue/<staged name>", "mode", "used_slots"}
```

Moonraker is reachable through the printer's web server on port 80, the
same host as `/multiace`. Give the staged file a unique name (the web UI
uses twelve random hex characters, an underscore and the file name).
`used_slots` is `core.used_ace_slots_file(path)`. If the second call
fails, delete the uploaded file again.

---

## 10. Printer settings for a send window

`GET /api/orca/options` returns the current value of the settings a
slicer plugin can offer before sending: `airprint`, `quad`, `noodle`,
`clean_bed`, `led_off`, `replenish` (stock auto replenish). A key the
printer does not report is left out.

`GET /api/orca/spools` returns `{"spools": {target id: {weight_g, pct,
sub}}}` per target (`slot-A-S`, `feeder-N`): weight and fill level of the
bound spool, and the filament's sub-type ("Support", "Silk"). Each key is
present only where known; a target with none of them is left out.

`POST /api/orca/options` takes the same keys (omit or `null` = leave as it
is) and sends only what differs from the current value. These are lasting
printer settings, not per-print flags. The per-print switches (`bed_mesh`,
`camera`, `flow_cal`) go into the body of `POST /api/preflight/print`.

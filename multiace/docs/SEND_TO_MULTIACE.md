# "Send to multiACE" - guide for slicer integrations

A slicer (or any other tool) can send a G-code file straight to multiACE. The
file lands in an inbox on the printer; the multiACE web UI offers it for
preflight automatically as soon as the printer is not printing. The analysis
then runs exactly like a manual upload - **in the user's browser** (Pyodide),
never on the printer's CPU. The printer only stores the file.

Companion documents: `LOADOUT_API.md` (reading what is loaded before you
assign filaments) and `ENGINE_API.md` (the gcode/runtime contract for driving
the engine).

## The one call

```
POST http://<printer-ip>/multiace/api/preflight/inbox
Content-Type: multipart/form-data
field: file   (file name ending in .gcode, .gco or .g)
```

Example:

```sh
curl -F "file=@model.gcode" http://192.168.1.50/multiace/api/preflight/inbox
```

No auth token, no API key - the API is reachable on the LAN only (like
Moonraker and Fluidd).

## Responses

| Status | Meaning |
|---|---|
| `200` | `{"ok": true, "name": "...", "size": ...}` - accepted |
| `400` | invalid file name / wrong extension / empty file |
| `409` | the file has already been processed by multiACE → **send the original slicer export** |
| `413` | too large (default limit 256 MB; raise it on the printer with `[ace] inbox_max_mb` in ace.cfg, takes effect without a restart) |

## Semantics

- **One waiting slot, newest wins.** A second POST silently replaces the
  waiting file.
- **Delivery is one-shot.** When the user opens the preflight, the inbox is
  emptied. If they abandon the preview, the slicer has to send again (or the
  user uploads by hand).
- **While the printer is printing** the file stays put; the web UI shows a
  banner and starts the preflight automatically once the printer is free.
- **The analysis happens at pick-up, not at send time** - the assignment is
  computed against the spools loaded at the moment the user looks at it.
- Status query: `GET /api/preflight/inbox` → `{pending, name, size, ts}`.
  Discard: `DELETE /api/preflight/inbox`.

## Rules

1. **Always send the original export**, never a file multiACE has already
   processed (files carrying `; multiACE processed:` or `; multiACE auto-load:`
   in the header are rejected with 409 - processing them twice would destroy
   the colour changes).
2. The printer needs a multiACE with inbox support (release 0.99.8b or newer).
   Check up front: `GET /api/preflight/inbox` → 404 means the build is too old.
3. Show errors to the user - 409 and 413 in particular carry an explanatory
   message in the `detail` field.

## What multiACE does with it

Receipt → banner "File from slicer: *name*" in the multiACE web UI →
preflight opens automatically (printer idle) or on click →
the usual preflight decision (assignment, loadout, print) by the user.
Nothing is **ever** printed without user interaction.

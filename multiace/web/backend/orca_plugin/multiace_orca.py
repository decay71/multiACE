# /// script
# requires-python = ">=3.10"
# dependencies = []
# [tool.orcaslicer.plugin]
# name = "multiACE+"
# description = "Preview. Assign slicer colours to the spools loaded in multiACE+ before a print is sent to a Snapmaker U1. Needs multiACE+ 1.94 or newer on the printer."
# version = "1.95.74"
# author = "decay71"
# ///
# SPDX-License-Identifier: GPL-3.0-only
"""multiACE+ plugin for OrcaSlicer 2.5.

Two parts:

* a page that shows the printer's multiACE web interface, and
* a window before a print is sent: one row per slicer colour with the
  spools loaded on the printer, prefilled with the printer's own proposal.

The plugin holds no assignment or rewrite logic. The printer analyses the
file (POST /multiace/api/preflight), rewrites it with the chosen assignment
(POST /multiace/api/preflight/print, deliver=return) and hands the result
back; OrcaSlicer then uploads and starts that file as usual. See
multiace/docs/LOADOUT_API.md, section 6.

The window step runs at Step.psGCodePostProcess. OrcaSlicer only calls it
when "multiACE+ - assign and send" is listed under the process option
"slicing_pipeline_plugin".
"""
import base64
import json
import os
import queue
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
import types
import uuid

try:
    import orca
except ImportError:      # imported outside OrcaSlicer (tests)
    orca = None

VERSION = "1.95.74"
PROCESSED_MARKER = b"; multiACE processed:"
SEND_NAME = "multiACE+ - assign and send"
OLD_SEND_NAME = "multiACE - assign and send"      # before 1.95.60
PAGE_NAME = "multiACE+"

HTTP_TIMEOUT = 20.0
UPLOAD_TIMEOUT = 600.0
JOB_TIMEOUT = 900.0
POLL_S = 0.5
QUEUE_DIR = "multiace_queue"


class PrinterError(Exception):
    """A request to the printer failed; str() is the text for the user."""


# --- address ---------------------------------------------------------------

def printer_base(print_host):
    """'http://<host>[:port]/multiace' from OrcaSlicer's print_host, or ''.

    multiACE is served by the printer's web server, not by Moonraker, so a
    Moonraker port (7125) is dropped."""
    raw = str(print_host or "").strip().strip('"').strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    host = parts.hostname or ""
    if not host:
        return ""
    if ":" in host:
        host = "[%s]" % host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port in (None, 80, 7125):
        netloc = host
    else:
        netloc = "%s:%d" % (host, port)
    scheme = parts.scheme if parts.scheme in ("http", "https") else "http"
    return "%s://%s/multiace" % (scheme, netloc)


# --- HTTP ------------------------------------------------------------------

def _detail(body):
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return body.decode("utf-8", "replace").strip()[:300]
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])
    return str(data)[:300]


class Printer:
    """The few multiACE calls the plugin makes."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        # Moonraker is served by the same web server, one level up
        self.origin = "%s://%s" % (parts.scheme, parts.netloc)

    def _open(self, req, timeout):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            err = PrinterError(_detail(e.read()) or "HTTP %d" % e.code)
            err.status = e.code
            raise err
        except Exception as e:
            # URLError/OSError, and whatever OrcaSlicer's permission audit
            # raises for a host the user has not allowed yet.
            err = PrinterError("printer not reachable at %s: %s"
                               % (req.full_url, getattr(e, "reason", e)))
            err.status = None
            raise err

    def grant(self):
        """Touch the bare printer address so OrcaSlicer asks ONCE for the
        whole host. A permission given for a URL with a path or query only
        covers that exact URL, and the status URL changes with every job.
        Call it on the UI thread: there the question is asked inline, on a
        worker thread the request is refused and asked afterwards."""
        parts = urllib.parse.urlsplit(self.base)
        host = parts.hostname or ""
        if ":" in host:
            host = "[%s]" % host
        try:
            urllib.request.urlopen(urllib.request.Request(
                "%s://%s" % (parts.scheme, host), method="HEAD"),
                timeout=5.0).close()
        except Exception:
            pass

    def _image(self, url, etag=None, timeout=4.0):
        """(bytes, content type, etag) of an image; bytes None when the
        printer says it has not changed."""
        req = urllib.request.Request(url)
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return (r.read(), r.headers.get("Content-Type")
                        or "image/jpeg", r.headers.get("ETag"))
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, None, etag
            raise PrinterError("HTTP %d" % e.code)
        except Exception as e:
            raise PrinterError(str(getattr(e, "reason", e)))

    def live_sources(self):
        """What the window can show live: the touchscreen, the cameras."""
        out = {"screen": False, "cams": [], "light": None,
               "panel": self.base + "/?panel=1"}
        try:
            out["screen"] = bool(self.get_json(
                "/api/screen-available", timeout=4.0).get("available"))
        except PrinterError:
            pass
        try:
            out["light"] = self.light()
        except PrinterError:
            pass
        try:
            info = self.get_json("/api/webcam/info", timeout=6.0)
            out["cams"] = [str(c.get("name") or "")
                           for c in info.get("cameras") or []
                           if c.get("kind") != "iframe"]
        except PrinterError:
            pass
        return out

    def light(self):
        """Chamber light in percent, None when the printer has none."""
        return self.get_json("/api/print-control", timeout=4.0).get("light")

    def set_light(self, on):
        self.post_json("/api/print-control",
                       {"verb": "light", "value": 100 if on else 0},
                       timeout=6.0)
        return self.light()

    def screen_frame(self, etag=None):
        return self._image(self.origin + "/screen/snapshot", etag)

    def cam_frame(self, name):
        return self._image(self.base + "/api/webcam/snapshot?cam="
                           + urllib.parse.quote(name or ""), timeout=8.0)

    def touch(self, action, x, y):
        req = urllib.request.Request(
            "%s/screen/touch?a=%s&x=%d&y=%d" % (
                self.origin, urllib.parse.quote(str(action)), int(x),
                int(y)), data=b"", method="POST")
        try:
            urllib.request.urlopen(req, timeout=3.0).close()
        except Exception:
            pass

    def get_json(self, path, timeout=HTTP_TIMEOUT):
        with self._open(urllib.request.Request(self.base + path),
                        timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_json(self, path, body, timeout=HTTP_TIMEOUT):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self._open(req, timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def upload(self, path, file_path, name, fields=None, base=None):
        """Multipart upload of one file (field "file"), streamed. fields:
        plain form fields sent before the file."""
        boundary = "multiace" + uuid.uuid4().hex
        safe = os.path.basename(name).replace('"', "") or "print.gcode"
        pre = "".join(
            "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n"
            "%s\r\n" % (boundary, k, v) for k, v in (fields or {}).items())
        head = (pre + "--%s\r\nContent-Disposition: form-data; "
                "name=\"file\"; "
                "filename=\"%s\"\r\nContent-Type: application/octet-stream"
                "\r\n\r\n" % (boundary, safe)).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")
        size = os.path.getsize(file_path)

        def body():
            yield head
            with open(file_path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            yield tail

        req = urllib.request.Request(
            (base or self.base) + path, data=body(), method="POST",
            headers={"Content-Type":
                     "multipart/form-data; boundary=" + boundary,
                     "Content-Length": str(len(head) + size + len(tail))})
        with self._open(req, UPLOAD_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    def download(self, path, dest):
        with self._open(urllib.request.Request(self.base + path),
                        UPLOAD_TIMEOUT) as r, open(dest, "wb") as out:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)

    # the calls
    def has_multiace(self):
        """True when the address answers as multiACE. A printer without it
        (404) is False; one that cannot be reached raises."""
        try:
            return isinstance(self.get_json("/api/version"), dict)
        except PrinterError as e:
            if getattr(e, "status", None) is None:
                raise
            return False

    def preflight(self, file_path, name):
        return self.upload("/api/preflight", file_path, name)

    def to_inbox(self, file_path, name):
        return self.upload("/api/preflight/inbox", file_path, name)

    def spools(self):
        """{target id: {weight_g, pct, sub}}: weight of the bound spool and
        the filament's sub-type, each where known; {} when the printer's
        multiACE does not offer it."""
        try:
            got = self.get_json("/api/orca/spools")
        except PrinterError:
            return {}
        got = got.get("spools") if isinstance(got, dict) else None
        return got if isinstance(got, dict) else {}

    def stage_in_queue(self, file_path, staged_name):
        """Put a finished file into the print queue folder on the printer
        (Moonraker upload, not started)."""
        return self.upload("/server/files/upload", file_path, staged_name,
                           fields={"root": "gcodes", "path": QUEUE_DIR,
                                   "print": "false"}, base=self.origin)

    def unstage(self, staged_name):
        req = urllib.request.Request(
            "%s/server/files/gcodes/%s/%s" % (
                self.origin, QUEUE_DIR, urllib.parse.quote(staged_name)),
            method="DELETE")
        try:
            self._open(req, HTTP_TIMEOUT).close()
        except PrinterError:
            pass

    def options(self):
        """Current printer settings for the window. {} when the printer's
        multiACE does not offer them yet."""
        try:
            got = self.get_json("/api/orca/options")
        except PrinterError:
            return {}
        return got if isinstance(got, dict) else {}

    def set_options(self, options):
        return self.post_json("/api/orca/options", options)

    def start(self, body):
        return self.post_json("/api/preflight/print", body)

    def status(self, job_id):
        return self.get_json("/api/preflight/print/status?job_id="
                             + urllib.parse.quote(job_id))

    def fetch(self, job_id, dest):
        self.download("/api/preflight/result?job_id="
                      + urllib.parse.quote(job_id), dest)


# --- the table -------------------------------------------------------------

def _mat(value):
    return str(value or "").strip().lower()


def build_view(report):
    """What the window shows, from the printer's preflight report.

    rows: one per slicer colour, each with its selectable targets (same
    material only) and the printer's proposal as the default."""
    head = bool(report.get("head_mode"))
    if head:
        targets = [dict(t) for t in report.get("targets") or []]
        plan = (report.get("plans") or {}).get("loadout") or {}
        default = {m.get("t"): m.get("target_id")
                   for m in plan.get("mapping") or []}
    else:
        targets = []
        for s in report.get("live_slots") or []:
            targets.append({
                "id": "slot-%d-%d" % (s["ace"], s["slot"]), "kind": "ace",
                "ace": s["ace"], "slot": s["slot"], "head": s["slot"],
                "material": s.get("material") or "",
                "color": s.get("color") or "", "name": s.get("name") or ""})
        plan = (report.get("plans") or {}).get("slicer") or {}
        default = {}
        for m in plan.get("mapping") or []:
            slot = m.get("slot")
            if slot:
                default[m.get("t")] = "slot-%d-%d" % (slot["ace"],
                                                      slot["slot"])
    rows = []
    for c in report.get("slicer_colors") or []:
        t = c.get("t")
        mat = _mat(c.get("material"))
        opts = [x["id"] for x in targets
                if not mat or not _mat(x.get("material"))
                or _mat(x.get("material")) == mat]
        pick = default.get(t)
        rows.append({"t": t, "hex": c.get("hex") or "",
                     "name": c.get("name") or "",
                     "material": c.get("material") or "",
                     "options": opts,
                     "default": pick if pick in opts else ""})
    est = plan.get("estimate") if isinstance(plan.get("estimate"),
                                              dict) else {}
    need = {}
    for c in est.get("per_color") or []:
        try:
            need[c.get("t")] = round(float(c.get("print_g") or 0)
                                     + float(c.get("purge_g") or 0), 1)
        except (TypeError, ValueError):
            pass
    for row in rows:
        row["need_g"] = need.get(row["t"])
    bg = plan.get("bg") if isinstance(plan.get("bg"), dict) else {}
    plans = [_plan_card("loaded", plan,
                        {r["t"]: r["default"] for r in rows if r["default"]})]
    if head:
        by_t = {r["t"]: r for r in rows}
        for key in PLAN_KEYS:
            p = (report.get("plans_reorder") or {}).get(key)
            pick = _plan_choice(p, by_t)
            if pick is not None:
                card = _plan_card(key, p, pick)
                card["moves"] = _plan_moves(p, pick, by_t)
                card["todo"] = [{"tid": pick[int(t)], "t": int(t),
                                 "why": "swap"} for t in card["moves"]]
                plans.append(card)
        # scope 'all': the load proposal over every slot of the connected
        # MMUs, empty ones included; it prints once the spools lie so
        planned = {x["id"]: x for x in targets}
        for key in PLAN_KEYS:
            card = _all_scope_card(key, (report.get("plans") or {}).get(key),
                                   by_t, planned, targets)
            if card is not None:
                plans.append(card)
    return {"head_mode": head, "rows": rows, "targets": targets,
            "plans": plans if len(plans) > 1 else [],
            "summary": {"swaps": plan.get("swaps"),
                        "bg_ok": bg.get("bg_ok"),
                        "bg_of": bg.get("unloads"),
                        "total_s": est.get("total_s"),
                        "grams": (round(sum(need.values()), 1)
                                  if need else None)},
            "plan": {"estimate": plan.get("estimate"),
                     "swaps": plan.get("swaps")},
            "filename": report.get("filename") or "",
            "missing_materials": report.get("missing_materials") or [],
            "unconnected_heads": report.get("unconnected_heads") or []}


PLAN_KEYS = ("optimize", "layer", "color")


def _plan_choice(plan, by_t):
    """{t: target_id} of a printer plan on the loaded spools, or None when
    the plan cannot be shown as a choice in the window."""
    if not isinstance(plan, dict) or not plan.get("feasible"):
        return None
    pick = {}
    for m in plan.get("mapping") or []:
        if m.get("tier") == "copy":
            continue
        try:
            if m.get("kind") == "pin":
                tid = "feeder-%d" % int(m["head"])
            elif m.get("kind") == "ace":
                tid = "slot-%d-%d" % (int(m["ace"]), int(m["slot"]))
            else:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        row = by_t.get(m.get("t"))
        if row is None or tid not in row["options"]:
            return None
        pick[m.get("t")] = tid
    return pick if len(pick) == len(by_t) else None


def _plan_moves(plan, pick, by_t):
    """The slicer colours whose spool has to be moved into the plan's slot
    first: the slot holds a spool of another colour (the printer's tier
    'fallback') and it is not where the colour prints as loaded."""
    out = []
    for m in plan.get("mapping") or []:
        t = m.get("t")
        if m.get("tier") != "fallback" or t not in pick:
            continue
        if pick[t] != (by_t[t].get("default") or ""):
            out.append(str(t))
    return sorted(set(out), key=int)


def _rgb_hex(value):
    h = str(value or "").strip().lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    if len(h) < 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _near(a, b, limit=60.0):
    ra, rb = _rgb_hex(a), _rgb_hex(b)
    if ra is None or rb is None:
        return False
    return sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5 <= limit


def _all_scope_card(key, plan, by_t, planned, targets):
    """A load proposal ('use all slots') as a plan card, or None. Slots the
    window does not know yet (empty ones) are added to `targets` as
    placeholders. `todo` names every slot that needs a spool first: 'load'
    for an empty slot, 'swap' for one holding another material or
    colour."""
    if not isinstance(plan, dict) or not plan.get("feasible"):
        return None
    pick, extra = {}, []
    for m in plan.get("mapping") or []:
        t = m.get("t")
        if t not in by_t:
            continue
        try:
            if m.get("kind") == "pin":
                tid = "feeder-%d" % int(m["head"])
            elif m.get("kind") == "ace":
                tid = "slot-%d-%d" % (int(m["ace"]), int(m["slot"]))
            else:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        if tid not in planned:
            if m.get("kind") != "ace":
                return None
            ghost = {"id": tid, "kind": "ace", "ace": int(m["ace"]),
                     "slot": int(m["slot"]), "head": m.get("head"),
                     "material": "", "color": "", "name": "", "ghost": True}
            planned[tid] = ghost
            targets.append(ghost)
        if m.get("tier") == "copy":
            extra.append({"t": t, "tid": tid})
        elif t not in pick:
            pick[t] = tid
    if len(pick) != len(by_t):
        return None
    todo = []
    for t, tid in list(pick.items()) + [(e["t"], e["tid"]) for e in extra]:
        x, row = planned[tid], by_t[t]
        if x.get("ghost"):
            todo.append({"tid": tid, "t": t, "why": "load"})
        elif (_mat(x.get("material")) != _mat(row.get("material"))
              or not _near(x.get("color"), row.get("hex"))):
            todo.append({"tid": tid, "t": t, "why": "swap"})
    card = _plan_card(key, plan, pick)
    card.update({"id": "all-" + key, "scope": "all", "todo": todo,
                 "extra": [{"t": e["t"], "tid": e["tid"]} for e in extra],
                 "moves": sorted({str(e["t"]) for e in todo}, key=int)})
    return card


def _plan_card(key, plan, pick):
    plan = plan if isinstance(plan, dict) else {}
    est = plan.get("estimate") if isinstance(plan.get("estimate"),
                                              dict) else {}
    bg = plan.get("bg") if isinstance(plan.get("bg"), dict) else {}
    need = {}
    for c in est.get("per_color") or []:
        try:
            need[str(c.get("t"))] = round(float(c.get("print_g") or 0)
                                          + float(c.get("purge_g") or 0), 1)
        except (TypeError, ValueError):
            pass
    return {"key": key, "id": key, "scope": "reorder", "todo": [],
            "extra": [],
            "choice": {str(t): v for t, v in pick.items()},
            "swaps": plan.get("swaps"), "bg_ok": bg.get("bg_ok"),
            "bg_of": bg.get("unloads"), "total_s": est.get("total_s"),
            "need": need, "moves": [],
            "grams": round(sum(need.values()), 1) if need else None,
            "estimate": plan.get("estimate")}


def plan_for_choice(view, choice, plan=None):
    """The plan card the choice equals (the loaded one first; with `plan`
    that card when it matches), or None."""
    want = {str(r["t"]): str((choice or {}).get(str(r["t"])) or "")
            for r in view["rows"]}
    cards = view.get("plans") or []
    if plan:
        cards = [p for p in cards if p.get("id") == plan] + cards
    for p in cards:
        if {k: str(p["choice"].get(k) or "") for k in want} == want:
            return p
    return None


PREF_KEYS = ("bed_mesh", "camera", "flow_cal")
OPTION_KEYS = ("airprint", "quad", "replenish", "noodle", "clean_bed",
               "led_off")


def print_body(report, view, choice, prefs=None, plan=None):
    """The /api/preflight/print body for the chosen {t: target_id}.
    prefs: the per-print switches (bed mesh, timelapse, flow calibration).
    plan: the id of the plan card the window showed; a load proposal
    ('use all slots') that was not edited prints as that plan, so the
    printer places its copies and checks the spools itself."""
    body = {"token": report["token"], "deliver": "return"}
    for key in PREF_KEYS:
        if (prefs or {}).get(key):
            body[key] = True
    card = plan_for_choice(view, choice, plan) if plan else None
    if card is not None and card.get("id") == plan \
            and card.get("scope") == "all" and view["head_mode"]:
        body.update({"mode": "head", "head_plan": card["key"],
                     "head_scope": "all",
                     "head_assignment": dict(card["choice"])})
        return body
    by_id = {x["id"]: x for x in view["targets"]}
    picked = {}
    for row in view["rows"]:
        tid = (choice or {}).get(str(row["t"]))
        if tid is None:
            tid = (choice or {}).get(row["t"])
        if tid and (by_id.get(tid) or {}).get("ghost"):
            raise PrinterError("colour %d still points at an empty slot: "
                               "load the spool or pick a loaded one"
                               % (int(row["t"]) + 1))
        if not tid or tid not in row["options"]:
            raise PrinterError("no spool chosen for colour %d"
                               % (int(row["t"]) + 1))
        picked[row["t"]] = tid
    if view["head_mode"]:
        body["mode"] = "head"
        body["head_plan"] = "loadout"
        body["head_assignment"] = {str(t): tid for t, tid in picked.items()}
    else:
        body["mode"] = "slicer"
        remap = {}
        for t, tid in picked.items():
            x = by_id[tid]
            synth = int(x["ace"]) * 4 + int(x["slot"])
            if synth != int(t):
                remap[str(t)] = synth
        body["remap"] = remap
    return body


def rewrite_into(printer, report, view, choice, gcode_path,
                 progress=None, cancelled=None, sleep=time.sleep,
                 prefs=None, options=None, plan=None):
    """Have the printer rewrite the file and put the result at gcode_path.
    options: printer settings from the window, applied first."""
    body = print_body(report, view, choice, prefs, plan)
    wanted = {k: bool(v) for k, v in (options or {}).items()
              if k in OPTION_KEYS}
    if wanted:
        printer.set_options(wanted)
    job = printer.start(body)["job_id"]
    deadline = time.time() + JOB_TIMEOUT
    while True:
        st = printer.status(job)
        if progress:
            progress(st.get("percent") or 0.0, st.get("stage") or "")
        if st.get("done"):
            break
        if cancelled and cancelled():
            raise PrinterError("cancelled")
        if time.time() > deadline:
            raise PrinterError("the printer did not finish the file")
        sleep(POLL_S)
    if st.get("error"):
        raise PrinterError(st["error"])
    tmp = gcode_path + ".multiace"
    try:
        printer.fetch(job, tmp)
        with open(tmp, "rb") as fh:
            if PROCESSED_MARKER not in fh.read(1 << 20):
                raise PrinterError("the printer returned an unprocessed "
                                   "file")
        if cancelled and cancelled():
            raise PrinterError("cancelled")
        os.replace(tmp, gcode_path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --- local engine ------------------------------------------------------------

ENGINE_MODULES = (("swap_cost", "swap_cost"),
                  ("post_process_virtual_toolheads", "postprocess"),
                  ("preflight_core", "core"))


def load_engine_modules(sources):
    """The printer's preflight code as modules: {name: module}. They are
    private to this plugin: nothing is registered with Python's import
    system. preflight_core finds its estimate model through its own
    module variable."""
    mods = {}
    for name, key in ENGINE_MODULES:
        src = sources.get(key)
        if not src:
            if key == "swap_cost":           # optional on the printer too
                continue
            raise PrinterError("the printer sent no %s source" % key)
        mod = types.ModuleType(name)
        mod.__file__ = "<printer>/%s.py" % name
        exec(compile(src, mod.__file__, "exec"), mod.__dict__)
        mods[name] = mod
    core = mods["preflight_core"]
    if "swap_cost" in mods and getattr(core, "_swap_cost", None) is None:
        core._swap_cost = mods["swap_cost"]
    return mods


class LocalEngine:
    """The printer's preflight, run here: the code and the loaded spools
    come from the printer, the file never leaves this machine until
    OrcaSlicer uploads the finished print."""

    def __init__(self, printer):
        mods = load_engine_modules(printer.get_json("/api/preflight/pysrc"))
        self.pp = mods["post_process_virtual_toolheads"]
        self.core = mods["preflight_core"]
        self.sig = self._take(printer.get_json("/api/preflight/livedata"))
        self.parsed = None
        self.capture = {}

    def _take(self, live):
        self.live_slots = live.get("live_slots") or []
        self.head_ctx = live.get("head_ctx") or {"mode": "multi"}
        self.cost_params = live.get("cost_params") or {}
        return json.dumps([self.live_slots, self.head_ctx], sort_keys=True)

    def changed(self, printer):
        """True when the loaded spools or the heads changed on the printer
        since they were last read; the new state is then taken over."""
        live = printer.get_json("/api/preflight/livedata", timeout=3.0)
        sig = json.dumps([live.get("live_slots") or [],
                          live.get("head_ctx") or {"mode": "multi"}],
                         sort_keys=True)
        if sig == self.sig:
            return False
        self.sig = self._take(live)
        self.capture = {}
        return True

    def _parse(self, gcode_path):
        with open(gcode_path, "r", encoding="utf-8",
                  errors="replace") as fh:
            return self.core.parse_meta(self.pp, fh, with_header=True)

    def report(self, gcode_path, name):
        colors, types_, naces, _used, plan, meta, header = \
            self._parse(gcode_path)
        self.parsed = (colors, types_, naces, meta)
        return self.core.build_report(
            self.pp, slicer_colors=colors, slicer_types=types_,
            num_aces=naces, plan_proxy=plan, live_slots=self.live_slots,
            head_ctx=self.head_ctx, token="", filename=name,
            size=os.path.getsize(gcode_path), meta=meta,
            header_text=header, cost_params=self.cost_params,
            estimate_capture=self.capture)

    def can_count(self):
        return bool(self.capture.get("ctx"))

    def numbers(self, view, choice):
        """The plan card (swaps, background swaps, time, filament) of a
        hand-made choice, or None when it cannot be worked out here."""
        ctx = self.capture.get("ctx")
        if not ctx or not view.get("head_mode"):
            return None
        try:
            pick = {str(r["t"]): str((choice or {})[str(r["t"])])
                    for r in view["rows"]}
            core, pp = self.core, self.pp
            assignment = core.assignment_from_target_ids(
                pick, self.capture.get("targets") or [])
            if any(e.get("kind") == "none" for e in assignment.values()):
                return None
            events = self.capture.get("events") or []
            plan = {"swaps": pp.head_mode_swap_count(events, assignment)}
            bg = core._bg_stats_for(
                pp, events, assignment, ctx.get("event_times"),
                ctx.get("bg_heads"),
                bg_units=core.multi_bg_units(self.head_ctx))
            if bg is not None:
                plan["bg"] = bg
            est = core.recompute_estimate(self.capture, target_ids=pick)
            plan["estimate"] = (est or {}).get("estimate")
            return _plan_card("custom", plan, pick)
        except Exception:
            return None

    def rewrite(self, body, gcode_path, progress=None, cancelled=None):
        """Rewrite gcode_path in place for the /api/preflight/print body.
        cancelled() is asked at every stage and before the file is put in
        place; a cancel leaves gcode_path untouched."""
        colors, types_, naces, meta = self.parsed

        def stage(st, pc):
            if cancelled and cancelled():
                raise PrinterError("cancelled")
            if progress:
                progress(pc, st)
        tmp_a, tmp_b = gcode_path + ".ma_a", gcode_path + ".ma_b"
        try:
            final = self.core.rewrite_pipeline(
                self.pp, src_path=gcode_path, tmp_a=tmp_a, tmp_b=tmp_b,
                slicer_colors=colors, slicer_types=types_, num_aces=naces,
                live_slots=self.live_slots, head_ctx=self.head_ctx,
                mode=body["mode"], remap_override=body.get("remap"),
                head_assignment=body.get("head_assignment"),
                head_plan=body.get("head_plan", "loadout"), meta=meta,
                set_stage=stage, head_scope=body.get("head_scope", "all"))
            prefs = [bool(body.get(k)) for k in PREF_KEYS]
            if any(prefs):
                other = tmp_b if final == tmp_a else tmp_a
                self.core.prepend_print_prefs(final, other, *prefs)
                final = other
            with open(final, "rb") as fh:
                if PROCESSED_MARKER not in fh.read(1 << 20):
                    raise PrinterError("the file came out unprocessed")
            if cancelled and cancelled():
                raise PrinterError("cancelled")
            os.replace(final, gcode_path)
        finally:
            for tmp in (tmp_a, tmp_b):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def make_engine(printer):
    """The local engine, or None when it cannot be set up (a printer whose
    multiACE does not hand out its code, code this Python cannot run):
    the printer then does the work itself."""
    try:
        return LocalEngine(printer)
    except Exception:
        return None


def rewrite_local(engine, printer, report, view, choice, gcode_path,
                  progress=None, prefs=None, options=None, cancelled=None,
                  plan=None):
    """rewrite_into, computed here. The settings and the per-print
    switches still go to the printer."""
    body = print_body(report, view, choice, prefs, plan)
    wanted = {k: bool(v) for k, v in (options or {}).items()
              if k in OPTION_KEYS}
    if wanted:
        printer.set_options(wanted)
    try:
        engine.rewrite(body, gcode_path, progress, cancelled)
    except PrinterError:
        raise
    except Exception as exc:
        raise PrinterError(str(exc) or type(exc).__name__)
    sent = {k: True for k in PREF_KEYS if body.get(k)}
    if sent:
        try:
            printer.post_json("/api/preflight/prefs", sent)
        except PrinterError:
            pass             # refused while a print runs, as on the printer


def is_default_choice(view, choice):
    return all(str((choice or {}).get(str(r["t"])) or "") == (r["default"]
               or "") for r in view["rows"])


def queue_local(engine, printer, report, view, choice, gcode_path, name,
                progress=None, prefs=None, options=None, cancelled=None,
                plan=None):
    """Rewrite the file here and stage it in the printer's print queue.
    The per-print switches travel in the file's marker; the queue applies
    them when the entry is started."""
    body = print_body(report, view, choice, prefs, plan)
    wanted = {k: bool(v) for k, v in (options or {}).items()
              if k in OPTION_KEYS}
    if wanted:
        printer.set_options(wanted)
    try:
        engine.rewrite(body, gcode_path, progress, cancelled)
        used = engine.core.used_ace_slots_file(gcode_path)
    except PrinterError:
        raise
    except Exception as exc:
        raise PrinterError(str(exc) or type(exc).__name__)
    base_name = os.path.basename(name).replace('"', "") or "print.gcode"
    staged = uuid.uuid4().hex[:12] + "_" + base_name
    if cancelled and cancelled():
        raise PrinterError("cancelled")
    printer.stage_in_queue(gcode_path, staged)
    entry = {"filename": base_name, "path": QUEUE_DIR + "/" + staged,
             "mode": body["mode"], "used_slots": used}
    shown = plan_for_choice(view, choice, plan)   # the numbers it showed
    if shown is None:
        shown = engine.numbers(view, choice)
    if shown is None and is_default_choice(view, choice):
        shown = view.get("plan") or {}
    if shown:
        entry.update({k: shown.get(k) for k in ("estimate", "swaps")
                      if shown.get(k) is not None})
    try:
        printer.post_json("/api/queue", entry)
    except PrinterError:
        printer.unstage(staged)
        raise


THUMB_RE = re.compile(
    r"^; thumbnail(?:_PNG)? begin (\d+)x(\d+) \d+\s*$(.*?)^; thumbnail"
    r"(?:_PNG)? end", re.S | re.M)


def extract_thumbnail(gcode_path, limit=4 << 20):
    """The largest PNG preview the slicer embedded in the file head, as a
    data URL, or ''."""
    try:
        with open(gcode_path, "r", encoding="utf-8",
                  errors="replace") as fh:
            head = fh.read(limit)
    except OSError:
        return ""
    best, area = "", 0
    for m in THUMB_RE.finditer(head):
        size = int(m.group(1)) * int(m.group(2))
        if size <= area or size > 1024 * 1024:
            continue
        b64 = "".join(line.lstrip(";").strip()
                      for line in m.group(3).splitlines())
        if not b64.startswith("iVBOR"):          # PNG only
            continue
        best, area = b64, size
    return "data:image/png;base64," + best if best else ""


def already_processed(path):
    try:
        with open(path, "rb") as fh:
            return PROCESSED_MARKER in fh.read(1 << 20)
    except OSError:
        return False


# --- window ----------------------------------------------------------------

WINDOW_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
:root{--bg:#0b0d0e;--card:#0f1213;--fg:#e4e8e9;--mut:#8a9497;--line:#22282b;--line2:#1b2023;--ctl:#2c3437;--acc:#15bdc6;--accfg:#001619;--err:#ff6b6b;--warn:#ffbb66;--sel:#12181a;--core:#131719;--edge:#1e2427;--fa:#56626a;--fb:#2a3236;--label:#6f797c}
@media (prefers-color-scheme: light){:root{--bg:#eef1f2;--card:#fff;--fg:#161b1d;--mut:#5d676a;--line:#d2d9db;--line2:#e1e6e8;--ctl:#c2cacd;--acc:#08767d;--accfg:#fff;--err:#c62828;--warn:#b9770e;--sel:#e6f3f4;--core:#e9edee;--edge:#d2d9db;--fa:#aeb8bc;--fb:#8a9599;--label:#626c6f}}
*{box-sizing:border-box}
html,body{height:100%}
html,body{background:var(--bg) !important;color:var(--fg) !important}
body{margin:0;font:13px/1.35 "Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column}
header,main,footer{background:var(--bg)}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;border-bottom:1px solid var(--line)}
.logo{font-size:20px;font-weight:600;flex:none}
.logo span{color:var(--acc)}
#file{color:var(--mut);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.sum{display:flex;gap:18px;margin-left:auto;color:var(--mut);font-size:12px;flex:none}
.sum b{color:var(--fg);font-weight:600;font-size:14px;display:block}
main{flex:1;overflow:auto;padding:12px 18px}
.sec{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}
.sec.gap{margin-top:14px}
.cols{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(168px,1fr))}
.col{display:flex;align-items:center;gap:8px;padding:5px 12px 5px 5px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border:1px solid var(--line);border-radius:18px;background:var(--card);cursor:pointer;color:var(--fg);font:inherit;font-weight:400}
.plans{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.pl{display:flex;align-items:baseline;gap:8px;padding:6px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;color:var(--fg);font:inherit;font-weight:600}
.pl small{color:var(--mut);font-weight:400}
.pl.on{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc);color:var(--acc)}
.col.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.col.todo{border-style:dashed;border-color:var(--warn)}
.col small{color:var(--mut);margin-left:auto}
.dot{width:22px;height:22px;border-radius:50%;border:1px solid var(--ctl);display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex:none}
.top{display:flex;gap:14px;align-items:flex-start}
.tl{flex:1;min-width:0}
.tb{flex:none;width:150px;height:150px;position:relative}
#thumb{width:150px;height:150px;display:block;background:var(--card);border:1px solid var(--line);cursor:zoom-in}
.tb.big #thumb{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:min(82vw,82vh);height:min(82vw,82vh);z-index:50;cursor:zoom-out;box-shadow:0 0 0 100vmax rgba(0,0,0,.72)}
#tnote{position:absolute;top:4px;right:4px;width:18px;height:18px;border-radius:50%;background:var(--warn);color:#000;font-size:12px;font-weight:700;line-height:18px;text-align:center;cursor:pointer}
.mmus{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.mmu{border:1px solid var(--line);background:var(--card)}
.mh{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid var(--line);font-weight:600}
.mh i{width:8px;height:8px;background:var(--acc);display:block}
.slots{display:grid;grid-template-columns:repeat(4,1fr)}
.slot{position:relative;border:0;border-radius:0;border-right:1px solid var(--line);padding:0 0 6px;background:var(--card);color:var(--fg);font:inherit;text-align:left;cursor:pointer;min-width:0}
.slot:last-child{border-right:0}
.slot.dim{opacity:.32;cursor:default}
.slot.none{cursor:default}
.slot.on{box-shadow:inset 0 -2px 0 var(--acc)}
.slot:not(.dim):not(.none):hover{outline:1px solid var(--acc);outline-offset:-1px}
.img{position:relative;height:74px;display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line2)}
.n{position:absolute;top:5px;left:7px;font-size:11px;color:var(--label)}
.pct{position:absolute;top:5px;right:7px;font-size:11px;color:var(--mut)}
.spool{display:flex;align-items:stretch;height:56px;margin-top:8px}
.spool>i{display:block;width:3px;border-radius:2px;background:linear-gradient(180deg,var(--fa),var(--fb))}
.core{display:flex;flex-direction:column;justify-content:center;width:20px;margin:5px 2px;background:var(--core);border-radius:3px;overflow:hidden;box-shadow:inset 0 0 0 1px var(--edge)}
.fill{display:block;width:100%;border-radius:2px;background-color:var(--c);box-shadow:0 0 12px -2px var(--c);background-image:repeating-linear-gradient(90deg,rgba(0,0,0,.30) 0 1px,transparent 1px 3px),linear-gradient(90deg,rgba(0,0,0,.22),rgba(255,255,255,.14) 45%,rgba(0,0,0,.18))}
.tags{position:absolute;left:6px;bottom:4px;display:flex;gap:3px}
.tags .dot{width:20px;height:20px;box-shadow:0 0 0 2px var(--card)}
.mv{position:absolute;right:6px;top:22px;color:var(--acc);font-size:17px;font-weight:700}
.pl small.m{color:var(--warn)}
.scope{display:inline-flex;margin-right:8px}
.sc{padding:6px 12px;border:1px solid var(--line);background:var(--card);color:var(--mut);font:inherit;cursor:pointer}
.sc+.sc{border-left:0}
.sc.on{color:var(--fg);background:var(--sel);border-color:var(--acc)}
.tags .cp{color:var(--acc);font-weight:700;font-size:12px;margin-left:2px}
.wn{position:absolute;right:6px;top:22px;color:var(--warn);font-size:15px}
.mat{padding:5px 8px 0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.w{padding:0 8px;color:var(--mut);font-size:12px;display:flex;justify-content:space-between;gap:4px}
.w .hd{color:var(--label);white-space:nowrap}
.need{padding:1px 8px 0;font-size:11px;color:var(--mut);min-height:16px}
.need.bad{color:var(--warn)}
.opts{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
label.opt{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);padding:6px 8px;cursor:pointer;user-select:none;min-width:0}
label.opt input{position:absolute;opacity:0;pointer-events:none}
label.opt span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tg{width:30px;height:18px;border-radius:9px;background:var(--ctl);position:relative;flex:none;transition:background .15s}
.tg::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--fg);transition:left .15s}
label.opt input:checked + .tg{background:var(--acc)}
label.opt input:checked + .tg::after{left:14px;background:var(--accfg)}
label.opt input:focus-visible + .tg{outline:2px solid var(--acc);outline-offset:2px}
#live{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.78);display:flex;align-items:center;justify-content:center}
#live[hidden]{display:none}
.lv{width:min(94vw,860px);background:var(--bg);border:1px solid var(--line)}
.lh{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--line)}
.lh .b{padding:5px 12px}
.lh .b.on{border-color:var(--acc);color:var(--acc)}
.lh .sp{flex:1}
.lb{position:relative;height:min(74vh,540px);display:flex;align-items:center;justify-content:center;background:#000}
#limg{width:100%;height:100%;object-fit:contain;display:block;user-select:none;touch-action:none}
#limg.touch{cursor:pointer}
#lfr{border:0;width:100%;height:100%;background:#000}
#lfr[hidden],#limg[hidden],#llight[hidden]{display:none}
#llight{padding:4px 8px}
#lmsg{position:absolute;left:0;right:0;bottom:8px;text-align:center;color:var(--mut);font-size:12px;pointer-events:none}
footer{border-top:1px solid var(--line);padding:10px 18px 12px}
#msg{min-height:19px;font-size:12.5px;color:var(--mut);white-space:pre-wrap;margin-bottom:6px}
#msg.err{color:var(--err)}
#msg.warn{color:var(--warn)}
.prog{height:3px;background:var(--line);overflow:hidden;margin-bottom:8px;visibility:hidden}
.prog i{display:block;height:100%;width:0;background:var(--acc);transition:width .3s}
.bar{display:flex;gap:8px;align-items:center}
.bar .sp{flex:1}
button.b{padding:8px 14px;border:1px solid var(--ctl);border-radius:0;background:var(--bg);color:var(--fg);font:inherit;cursor:pointer}
button.b:hover:not(:disabled){border-color:var(--acc)}
button.b.go{background:var(--acc);border-color:var(--acc);color:var(--accfg);font-weight:600}
button.b:disabled{opacity:.45;cursor:default}
</style></head><body>
<header><div class="logo">m<span>U</span>lt<span>1</span>ACE<span>+</span></div><div id="file"></div><div class="sum" id="sum"></div></header>
<main>
 <div class="top"><div class="tl">
 <div class="plans" id="plans" hidden></div>
 <div class="sec" id="h0"></div>
 <div class="cols" id="cols"></div>
 </div><div class="tb"><canvas id="thumb" width="300" height="300" hidden></canvas><div id="tnote" hidden></div></div></div>
 <div class="sec gap" id="h1"></div>
 <div class="mmus" id="mmus"></div>
 <div id="optbox" hidden>
  <div class="sec gap" id="h2"></div><div class="opts" id="prefs"></div>
  <div class="sec gap" id="h3"></div><div class="opts" id="sets"></div>
 </div>
</main>
<footer>
 <div id="msg"></div><div class="prog" id="prog"><i></i></div>
 <div class="bar">
  <button class="b" id="pre"></button><button class="b" id="look" hidden></button><span class="sp"></span>
  <button class="b" id="cancel"></button><button class="b" id="queue"></button><button class="b go" id="go"></button>
 </div>
</footer>
<div id="live" hidden><div class="lv">
 <div class="lh"><span id="ltabs"></span><span class="sp"></span><label class="opt" id="llight" hidden><input type="checkbox" id="llin"><i class="tg"></i><span id="lltx"></span></label><button class="b" id="lx">&times;</button></div>
 <div class="lb"><img id="limg" alt="" draggable="false"><iframe id="lfr" hidden></iframe><div id="lmsg"></div></div>
</div></div>
<script>
const DE = (navigator.language || "").toLowerCase().startsWith("de");
const T = DE ? {
  pl_loaded:"Wie geladen", pl_optimize:"Optimize", pl_layer:"Layer", pl_color:"Color",
  h0:"Slicer-Farben  ·  Farbe wählen, dann Spule anklicken", h1:"Geladene Spulen",
  h2:"Für diesen Druck", h3:"Druckereinstellungen (bleiben gespeichert)",
  go:"Übernehmen und senden", cancel:"Abbrechen", pre:"Im Preflight öffnen", queue:"In die Warteschlange",
  load:"Datei wird gelesen ...", work:"Datei wird vorbereitet", stop:"Wird abgebrochen ...",
  inbox:"Datei wird an den Preflight übergeben ...", head:"Kopf", feeder:"Feeder", empty:"leer",
  llight:"Licht", look:"Drucker ansehen", lscreen:"Display", lcam:"Kamera", lpanel:"multiACE mini", lwait:"Warte auf Bild ...", lerr:"Kein Bild",
  mv:"umlegen", mvhere:"Farbe {n} hierher umlegen", ld:"einlegen", ldhere:"Spule für Farbe {n} hier einlegen", copy:"zweite Spule",
  sc_reorder:"Nur umsortieren", sc_all:"Alle Slots nutzen",
  ldask:"Dieser Plan braucht noch {n} Spule(n) an anderer Stelle, siehe ⇄. Erst einlegen, das Fenster sieht es von selbst. Nochmal klicken sendet trotzdem, der Drucker lehnt dann ab, solange Spulen fehlen.",
  mvask:"Dieser Plan braucht noch {n} umgelegte Spule(n), siehe ⇄. Erst umlegen und neu senden. Nochmal klicken druckt mit den Spulen, wie sie jetzt liegen.",
  needs:"braucht", has:"Spule hat noch", todo:"Noch nicht zugeordnet: Farbe", nofit:"keine passende Spule geladen",
  colors:"Farben", swaps:"Wechsel", bg:"BG-Swaps", time:"mit Wechseln", fil:"Filament",
  bed_mesh:"Bed-Mesh", camera:"Timelapse", flow_cal:"Flow-Kalibrierung",
  airprint:"Air-Print", quad:"Quad Replenish", replenish:"Stock-Replenish",
  noodle:"Spaghetti-Erkennung", clean_bed:"Fremdkörper auf dem Bett", led_off:"Licht aus am Ende",
  twin:"Filament {n}: gleiche Farbe, im Bild nicht zu trennen"
} : {
  pl_loaded:"As loaded", pl_optimize:"Optimize", pl_layer:"Layer", pl_color:"Color",
  h0:"Slicer colours  ·  pick a colour, then click a spool", h1:"Loaded spools",
  h2:"For this print", h3:"Printer settings (stay saved)",
  go:"Apply and send", cancel:"Cancel", pre:"Open in preflight", queue:"Add to queue",
  load:"Reading the file ...", work:"Preparing the file", stop:"Cancelling ...",
  inbox:"Handing the file to the preflight ...", head:"Head", feeder:"Feeder", empty:"empty",
  llight:"Light", look:"Look at the printer", lscreen:"Display", lcam:"Camera", lpanel:"multiACE mini", lwait:"Waiting for a picture ...", lerr:"No picture",
  mv:"to move", mvhere:"move colour {n} here", ld:"to load", ldhere:"load a spool for colour {n} here", copy:"second spool",
  sc_reorder:"Only reorder", sc_all:"Use all slots",
  ldask:"This plan still needs {n} spool(s) elsewhere, see ⇄. Load them first, the window picks it up. Click once more to send anyway; the printer refuses while spools are missing.",
  mvask:"This plan still needs {n} spool(s) moved, see ⇄. Move them and send again. Click once more to print with the spools as they lie now.",
  needs:"needs", has:"spool has", todo:"Not assigned yet: colour", nofit:"no matching spool loaded",
  colors:"colours", swaps:"swaps", bg:"BG swaps", time:"with swaps", fil:"filament",
  bed_mesh:"Bed mesh", camera:"Timelapse", flow_cal:"Flow calibration",
  airprint:"Air print", quad:"Quad Replenish", replenish:"Stock replenish",
  noodle:"Spaghetti detection", clean_bed:"Objects on the bed", led_off:"Light off when done",
  twin:"Filament {n}: same colour, the image cannot tell them apart"
};
const PREFS = ["bed_mesh", "camera", "flow_cal"];
const SETS = ["airprint", "quad", "replenish", "noodle", "clean_bed", "led_off"];
const KEY = "multiace.orca.prefs";
const $ = id => document.getElementById(id);
["h0","h1","h2","h3"].forEach(k => $(k).textContent = T[k]);
$("go").textContent = T.go; $("cancel").textContent = T.cancel; $("pre").textContent = T.pre; $("queue").textContent = T.queue;
let view = null, busy = false, choice = {}, sel = null, by = {};
function esc(s){return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function hex(c){ c = String(c || ""); return c ? (c[0] === "#" ? c : "#" + c) : "#444"; }
function ink(c){ const h = hex(c).slice(1); if (h.length < 6) return "#fff";
  const l = (parseInt(h.slice(0,2),16)*299 + parseInt(h.slice(2,4),16)*587 + parseInt(h.slice(4,6),16)*114) / 1000;
  return l > 150 ? "#000" : "#fff"; }
function dot(r){ return '<span class="dot" style="background:' + esc(hex(r.hex)) + ";color:" + ink(r.hex) + '">' + (r.t + 1) + "</span>"; }
function say(text, cls){ const m = $("msg"); m.textContent = text || ""; m.className = cls || ""; }
function prog(p){ const b = $("prog"); b.style.visibility = p == null ? "hidden" : "visible"; b.firstChild.style.width = (p || 0) + "%"; }
function g(v){ return (Math.round(v * 10) / 10) + " g"; }
function dur(s){ s = Math.round(s / 60); const h = Math.floor(s / 60), m = s % 60; return (h ? h + " h " : "") + m + " min"; }
function row(t){ return view.rows.find(r => r.t === t); }
let custom = null, asked = "";
function ckey(){ return view.rows.map(r => choice[r.t] || "").join("|"); }
function fits(p){ return view.rows.every(r => (choice[r.t] || "") === (p.choice[r.t] || "")); }
let scope = (() => { try { return localStorage.getItem("multiace.orca.scope") === "all" ? "all" : "reorder"; } catch (e) { return "reorder"; } })();
function hasAll(){ return (view.plans || []).some(p => p.scope === "all"); }
function shown(){ return (view.plans || []).filter(p => p.key === "loaded" || p.scope === (hasAll() ? scope : "reorder")); }
function cur(){ return shown().find(fits) || (custom && fits(custom) ? custom : null); }
function count(){            // numbers of a hand-made choice: ask the plugin
  if (!view.can_calc || cur() || !view.rows.every(r => choice[r.t])) return;
  const k = ckey(); if (k === asked) return; asked = k;
  const c = {}; view.rows.forEach(r => c[String(r.t)] = choice[r.t]);
  window.orca.postMessage({action:"calc", key:k, choice:c});
}
function need(r){ const p = cur(); return p && p.need && p.need[r.t] != null ? p.need[r.t] : r.need_g; }
function needOn(id){ let n = 0, any = false;
  view.rows.forEach(r => { const v = need(r); if (choice[r.t] === id && v != null) { n += v; any = true; } });
  return any ? n : null; }
function todo(){ const p = cur(); return p && p.todo || []; }
function moved(t){ return todo().some(e => e.t === t && e.tid === choice[t]); }
function todoAt(id){ return todo().filter(e => e.tid === id); }
function movedTo(id){ return todoAt(id).map(e => row(e.t)).filter(Boolean); }
function extraAt(id){ const p = cur(); return (p && p.extra || []).filter(e => e.tid === id).map(e => row(e.t)).filter(Boolean); }
function short(id){ const x = by[id], n = needOn(id);
  if (movedTo(id).length) return false;      // another spool goes in here
  return x && n != null && x.weight_g != null && n > x.weight_g; }
function place(x){ return x.kind === "pin" ? T.feeder + " " + T.head + " " + (x.head + 1)
  : "MMU " + (x.ace + 1) + " / Slot " + (x.slot + 1); }
function mvText(id){ const l = todoAt(id).some(e => e.why === "load");
  return place(by[id]) + ": " + (l ? T.ldhere : T.mvhere).replace("{n}", todoAt(id).map(e => e.t + 1).join(", ")); }
function isDefault(){ return view.rows.every(r => (choice[r.t] || "") === (r.default || "")); }
function refresh(){
  const all = view && view.rows.length && view.rows.every(r => choice[r.t]);
  $("go").disabled = busy || !all;
  $("queue").disabled = busy || !all;
  $("pre").disabled = busy || !view;
  document.querySelectorAll("label.opt input").forEach(x => x.disabled = busy);
}
function tile(x, n){
  if (x && x.ghost && !todoAt(x.id).length) x = null;     // empty, not planned
  if (!x) return '<div class="slot none"><div class="img"><span class="n">S' + n + '</span></div><div class="mat" style="color:var(--label);font-weight:400">' + esc(T.empty) + '</div><div class="w">&nbsp;</div><div class="need"></div></div>';
  const r = sel == null ? null : row(sel);
  const fits = !r || r.options.includes(x.id);
  const mine = view.rows.filter(q => choice[q.t] === x.id), more = extraAt(x.id);
  const n1 = needOn(x.id), bad = short(x.id), mv = movedTo(x.id);
  const pct = x.pct == null ? 100 : x.pct;
  return '<button type="button" class="slot' + (fits || x.ghost ? "" : " dim") + (mine.length ? " on" : "") + '" data-id="' + esc(x.id) + '"' + (fits && !busy ? "" : " disabled") + '>' +
    '<div class="img"><span class="n">' + (x.kind === "pin" ? "T" + (x.head + 1) : "S" + n) + '</span>' +
    (x.pct == null ? "" : '<span class="pct">' + x.pct + " %</span>") +
    '<span class="spool"><i></i><span class="core"><span class="fill" style="--c:' + esc(hex(x.color)) + ";height:" + Math.max(pct, 14) + '%"></span></span><i></i></span>' +
    ((mine.length || more.length) ? '<span class="tags">' + mine.map(dot).join("") + more.map(r => '<span class="cp" title="' + esc(T.copy) + '">+</span>' + dot(r)).join("") + "</span>" : "") +
    (bad ? '<span class="wn">&#9888;</span>' : "") +
    (mv.length ? '<span class="mv" title="' + esc(mvText(x.id)) + '">&#8644;</span>' : "") + "</div>" +
    (x.ghost ? '<div class="mat" style="color:var(--label);font-weight:400">' + esc(T.empty) + "</div>" :
    '<div class="mat" title="' + esc(x.name || "") + '">' + esc(x.material || "?") + ((x.sub || x.name) ? " " + esc(x.sub || x.name) : "") + "</div>") +
    '<div class="w"><span>' + (x.weight_g == null ? "&nbsp;" : Math.round(x.weight_g) + " g") + '</span><span class="hd">' + (x.head == null ? "" : esc(T.head) + " " + (x.head + 1)) + "</span></div>" +
    '<div class="need' + (bad ? " bad" : "") + '">' + (n1 == null ? "" : esc(T.needs) + " " + g(n1)) + "</div></button>";
}
function render(){
  $("file").textContent = view.filename || "";
  count();
  const p0 = cur(), same = !!p0 || isDefault(), s = p0 || view.summary || {};
  const parts = [[view.rows.length, T.colors]];
  if (same && s.swaps != null) parts.push([s.swaps, T.swaps]);
  if (same && s.bg_ok != null && s.bg_of) parts.push([s.bg_ok + "/" + s.bg_of, T.bg]);
  if (same && s.total_s) parts.push([dur(s.total_s), T.time]);
  const gr = s.grams != null ? s.grams : (view.summary || {}).grams;
  if (gr != null) parts.push([g(gr), T.fil]);
  const pls = shown();
  $("plans").hidden = pls.length < 2 && !hasAll();
  const sw = hasAll() ? '<span class="scope">' + ["reorder", "all"].map(k => '<button type="button" class="sc' + (scope === k ? " on" : "") + '" data-s="' + k + '"' + (busy ? " disabled" : "") + ">" + esc(T["sc_" + k]) + "</button>").join("") + "</span>" : "";
  $("plans").innerHTML = sw + pls.map(p => '<button type="button" class="pl' + (p === p0 ? " on" : "") + '" data-p="' + esc(p.id) + '"' + (busy ? " disabled" : "") + ">" +
    esc(T["pl_" + p.key] || p.key) + (p.swaps == null ? "" : " <small>" + p.swaps + " " + esc(T.swaps) + "</small>") +
    ((p.todo || []).length ? ' <small class="m">&#8644; ' + p.todo.length + " " + esc(p.scope === "all" ? T.ld : T.mv) + "</small>" : "") + "</button>").join("");
  document.querySelectorAll("button.pl").forEach(b => b.onclick = () => {
    const p = pls.find(q => q.id === b.dataset.p); if (!p) return;
    choice = {}; view.rows.forEach(r => { if (p.choice[r.t]) choice[r.t] = p.choice[r.t]; });
    render(); });
  document.querySelectorAll("button.sc").forEach(b => b.onclick = () => {
    if (scope === b.dataset.s) return;
    const was = cur();
    scope = b.dataset.s; try { localStorage.setItem("multiace.orca.scope", scope); } catch (e) {}
    if (was && was.key !== "loaded") {           // the plan of the other scope
      const p = shown().find(q => q.key === was.key) || shown()[0];
      choice = {}; view.rows.forEach(r => { if (p.choice[r.t]) choice[r.t] = p.choice[r.t]; });
    }
    render(); });
  $("sum").innerHTML = parts.map(p => "<div><b>" + esc(p[0]) + "</b>" + esc(p[1]) + "</div>").join("");
  $("cols").innerHTML = view.rows.map(r =>
    '<button type="button" class="col' + (sel === r.t ? " sel" : "") + (choice[r.t] ? "" : " todo") + '" data-t="' + r.t + '"' + (busy ? " disabled" : "") + ">" + dot(r) +
    esc(r.material) + " " + esc(r.name || r.hex) + (need(r) == null ? "" : " <small>" + g(need(r)) + "</small>") + "</button>").join("");
  const aces = [...new Set(view.targets.filter(x => x.kind !== "pin").map(x => x.ace))].sort((a, b) => a - b);
  let html = aces.map(a => { const n = Math.max(4, ...view.targets.filter(x => x.kind !== "pin" && x.ace === a).map(x => x.slot + 1));
    let cells = ""; for (let i = 0; i < n; i++) cells += tile(view.targets.find(x => x.kind !== "pin" && x.ace === a && x.slot === i), i + 1);
    return '<div class="mmu"><div class="mh"><i></i>MMU ' + (a + 1) + '</div><div class="slots">' + cells + "</div></div>"; }).join("");
  const pins = view.targets.filter(x => x.kind === "pin").sort((a, b) => a.head - b.head);
  if (pins.length) { let cells = pins.map(x => tile(x, 0)).join(""); for (let i = pins.length; i < 4; i++) cells += '<div class="slot none"></div>';
    html += '<div class="mmu"><div class="mh"><i></i>' + esc(T.feeder) + '</div><div class="slots">' + cells + "</div></div>"; }
  $("mmus").innerHTML = html;
  document.querySelectorAll("button.col").forEach(b => b.onclick = () => { sel = Number(b.dataset.t); render(); });
  document.querySelectorAll("button.slot").forEach(b => b.onclick = () => {
    if (sel == null) return;
    choice[sel] = b.dataset.id;
    const next = view.rows.find(r => !choice[r.t]);
    if (next) sel = next.t;
    render(); });
  if (!busy) {
    const open = view.rows.filter(r => !choice[r.t]);
    const bad = Object.keys(by).filter(short);
    const mvs = Object.keys(by).filter(id => movedTo(id).length);
    if (open.length) say(open.map(r => T.todo + " " + (r.t + 1) + (r.options.length ? "" : " (" + T.nofit + ")")).join("\n"), "warn");
    else if (mvs.length || bad.length) say(mvs.map(id => "⇄ " + mvText(id)).concat(bad.map(id => "⚠ " + place(by[id]) + ": " + T.needs + " " + g(needOn(id)) + ", " + T.has + " " + Math.round(by[id].weight_g) + " g")).join("\n"), "warn");
    else say("");
  }
  paintThumb();
  refresh();
}
// --- preview: the slicer's own thumbnail, recoloured to the chosen spools
let thumb = null;
function rgb(c){ const h = hex(c).slice(1); return h.length < 6 ? [68,68,68]
  : [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)]; }
function lift(c){ return c.map(v => Math.max(v, 28)); }
function loadThumb(){
  const cv = $("thumb");
  if (!view.thumb) { cv.hidden = true; return; }
  const img = new Image();
  img.onload = () => {
    try {
      // cut the empty margin away so the model fills the square
      const tw0 = img.naturalWidth, th0 = img.naturalHeight;
      const tc = document.createElement("canvas"); tc.width = tw0; tc.height = th0;
      const tx = tc.getContext("2d"); tx.drawImage(img, 0, 0);
      const a = tx.getImageData(0, 0, tw0, th0).data;
      let x0 = tw0, y0 = th0, x1 = -1, y1 = -1;
      for (let y = 0; y < th0; y++) for (let x = 0; x < tw0; x++)
        if (a[(y * tw0 + x) * 4 + 3] >= 16) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
      if (x1 < 0) { x0 = 0; y0 = 0; x1 = tw0 - 1; y1 = th0 - 1; }
      const side = Math.max(x1 - x0 + 1, y1 - y0 + 1), pad = Math.round(side * 0.06), sq = side + 2 * pad;
      cv.width = sq; cv.height = sq;
      const cx = cv.getContext("2d");
      cx.clearRect(0, 0, sq, sq);
      cx.drawImage(img, pad + Math.round((side - (x1 - x0 + 1)) / 2) - x0, pad + Math.round((side - (y1 - y0 + 1)) / 2) - y0);
      const d = cx.getImageData(0, 0, cv.width, cv.height);
      const n = cv.width * cv.height, src = d.data;
      const base = view.rows.map(r => lift(rgb(r.hex)));
      const idx = new Int8Array(n), k = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const o = i * 4; idx[i] = -1;
        if (src[o + 3] < 16) continue;
        let best = 8100 * 3, bi = -1, bk = 1;       // further off: leave as is
        for (let j = 0; j < base.length; j++) {
          const c = base[j];
          let f = (src[o]*c[0] + src[o+1]*c[1] + src[o+2]*c[2]) / (c[0]*c[0] + c[1]*c[1] + c[2]*c[2]);
          f = Math.max(0.3, Math.min(1.25, f));
          const e = (src[o]-f*c[0])**2 + (src[o+1]-f*c[1])**2 + (src[o+2]-f*c[2])**2;
          if (e < best) { best = e; bi = j; bk = f; }
        }
        idx[i] = bi; k[i] = bk;
      }
      thumb = {cx, w: cv.width, h: cv.height, src: new Uint8ClampedArray(src), idx, k};
      cv.hidden = false; paintThumb();
    } catch (e) { cv.hidden = true; }
  };
  img.onerror = () => { cv.hidden = true; };
  img.src = view.thumb;
}
function twins(){            // slicer filaments that share one colour
  const seen = {}, out = [];
  view.rows.forEach(r => { const h = hex(r.hex).toLowerCase(); (seen[h] = seen[h] || []).push(r.t + 1); });
  Object.keys(seen).forEach(h => { if (seen[h].length > 1) out.push(seen[h].join(" + ")); });
  return out;
}
function paintThumb(){
  const tw = thumb ? twins() : [];
  $("tnote").hidden = !tw.length;
  $("tnote").textContent = "!";
  $("tnote").title = tw.length ? T.twin.replace("{n}", tw.join(", ")) : "";
  if (!thumb) return;
  const out = new Uint8ClampedArray(thumb.src);
  const to = view.rows.map(r => { const x = by[choice[r.t]];
    if (!x || !x.color || moved(r.t) || hex(x.color).toLowerCase() === hex(r.hex).toLowerCase()) return null;
    return lift(rgb(x.color)); });
  for (let i = 0; i < thumb.idx.length; i++) {
    const j = thumb.idx[i]; if (j < 0 || !to[j]) continue;
    const o = i * 4, f = thumb.k[i], c = to[j];
    out[o] = f * c[0]; out[o+1] = f * c[1]; out[o+2] = f * c[2];
  }
  thumb.cx.putImageData(new ImageData(out, thumb.w, thumb.h), 0, 0);
}
function toggle(key, on){
  return '<label class="opt"><input type="checkbox" data-k="' + key + '"' + (on ? " checked" : "") + '><i class="tg"></i><span>' + esc(T[key]) + "</span></label>";
}
function resync(was){       // the printer's spools changed: keep what still fits
  by = {}; view.targets.forEach(x => by[x.id] = x);
  const old = choice; choice = {};
  const same = was && was !== custom && (view.plans || []).find(p => p.id === was.id);
  if (same && was.key !== "loaded")            // a chosen plan stays chosen
    view.rows.forEach(r => { if (same.choice[r.t]) choice[r.t] = same.choice[r.t]; });
  else view.rows.forEach(r => { const c = old[r.t];
    if (c && r.options.includes(c)) choice[r.t] = c; else if (r.default) choice[r.t] = r.default; });
  if (sel == null || !row(sel)) { const f = view.rows.find(r => !choice[r.t]) || view.rows[0]; sel = f ? f.t : null; }
  custom = null; asked = ""; armed = "";
  $("queue").hidden = !view.can_queue;
  if (busy) refresh(); else render();
}
function setup(){
  by = {}; view.targets.forEach(x => by[x.id] = x);
  choice = {}; view.rows.forEach(r => { if (r.default) choice[r.t] = r.default; });
  const first = view.rows.find(r => !choice[r.t]) || view.rows[0];
  sel = first ? first.t : null;
  const o = view.options || {};
  let last = {}; try { last = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) {}
  $("prefs").innerHTML = PREFS.map(k => toggle(k, k in last ? !!last[k] : !!o[k])).join("");
  const have = SETS.filter(k => k in o);
  $("sets").innerHTML = have.map(k => toggle(k, !!o[k])).join("");
  $("h3").hidden = !have.length;
  $("optbox").hidden = false;
  $("queue").hidden = !view.can_queue;
  $("look").hidden = !liveSources().length; $("look").textContent = T.look;
  thumb = null; loadThumb();
  render();
}
function picked(keys){
  const out = {};
  document.querySelectorAll("label.opt input").forEach(x => { if (keys.includes(x.dataset.k)) out[x.dataset.k] = x.checked; });
  return out;
}
function lock(on){ busy = on; if (view) render(); else refresh(); }
let armed = "";
function fire(action){
  const p = cur(), n = todo().length;
  if (n && action !== "preflight" && armed !== ckey()) {   // spools still to move
    armed = ckey(); say((p.scope === "all" ? T.ldask : T.mvask).replace("{n}", n), "warn"); return; }
  const c = {}; view.rows.forEach(r => c[String(r.t)] = choice[r.t]);
  const prefs = picked(PREFS), options = picked(SETS);
  try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e) {}
  lock(true); say(T.work); prog(0);
  window.orca.postMessage({action, choice: c, prefs, options, plan: p ? p.id : ""});
}
window.orca.onMessage(m => {
  if (!m || typeof m !== "object") return;
  if (m.type === "frame"){ liveFrame(m); return; }
  if (m.type === "light"){ if (view && view.live) liveLight(m); return; }
  if (m.type === "calc"){ if (view && m.card && m.key === ckey()) { custom = m.card; render(); } return; }
  if (m.type === "view" && m.keep && view){ const lv = view.live, was = cur(); view = m.view; if (live) view.live = lv; resync(was); return; }
  if (m.type === "view"){ view = m.view; custom = null; asked = ""; busy = false; prog(null); say(""); setup(); }
  else if (m.type === "progress"){ say(T.work + " (" + Math.round(m.percent || 0) + " %)"); prog(m.percent || 0); }
  else if (m.type === "error"){ prog(null); busy = false; if (view) render(); else refresh(); say(m.text, "err"); }
  else if (m.type === "info"){ say(m.text); }
});
// --- look at the printer: its touchscreen, a camera, the compact multiACE view
let live = null;             // {kind, cam}
function liveSources(){
  const l = view && view.live || {}, out = [];
  if (l.screen) out.push({kind:"screen", cam:"", label:T.lscreen});
  (l.cams || []).forEach((c, i) => out.push({kind:"cam", cam:c, label:(l.cams.length > 1 && c) ? c : T.lcam}));
  if (l.panel) out.push({kind:"panel", cam:"", label:T.lpanel});
  return out;
}
function liveShow(src){
  live = src;
  $("live").hidden = !src;
  if (!src) { $("lfr").src = "about:blank"; window.orca.postMessage({action:"live", kind:"", open:false}); return; }
  $("ltabs").innerHTML = liveSources().map((s, i) => '<button type="button" class="b' + (s.kind === src.kind && s.cam === src.cam ? " on" : "") + '" data-i="' + i + '">' + esc(s.label) + "</button>").join(" ");
  document.querySelectorAll("#ltabs button").forEach(b => b.onclick = () => liveShow(liveSources()[Number(b.dataset.i)]));
  const panel = src.kind === "panel";
  $("lfr").hidden = !panel; $("limg").hidden = panel;
  $("limg").removeAttribute("src"); $("limg").className = src.kind === "screen" ? "touch" : "";
  $("lmsg").textContent = panel ? "" : T.lwait;
  // the shade this window really shows, read off its own background
  const bgc = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(getComputedStyle(document.body).backgroundColor);
  const dark = bgc ? (bgc[1] * 299 + bgc[2] * 587 + bgc[3] * 114) / 1000 < 128
    : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (panel) $("lfr").src = view.live.panel + (view.live.panel.includes("?") ? "&" : "?") + "theme=" + (dark ? "dark" : "light");
  else $("lfr").src = "about:blank";
  const lv = (view.live || {}).light;
  $("llight").hidden = !(src.kind === "cam" && lv != null);
  $("llin").checked = lv > 0; $("lltx").textContent = T.llight;
  window.orca.postMessage({action:"live", kind: panel ? "" : src.kind, cam: src.cam, open:true});
}
function liveFrame(m){
  if (!live || m.kind !== live.kind || (m.cam || "") !== live.cam) return;
  if (m.error) { $("lmsg").textContent = T.lerr + ": " + m.error; return; }
  $("limg").src = m.data; $("lmsg").textContent = "";
}
function livePoint(a, ev){
  if (!live || live.kind !== "screen") return;
  const im = $("limg"), r = im.getBoundingClientRect();
  if (!im.naturalWidth || !r.width) return;
  ev.preventDefault();
  // the picture is fitted into the box: map through its drawn rectangle
  const k = Math.min(r.width / im.naturalWidth, r.height / im.naturalHeight);
  const x = (ev.clientX - r.left - (r.width - im.naturalWidth * k) / 2) / k;
  const y = (ev.clientY - r.top - (r.height - im.naturalHeight * k) / 2) / k;
  if (x < 0 || y < 0 || x > im.naturalWidth || y > im.naturalHeight) return;
  window.orca.postMessage({action:"touch", a, x: Math.round(x), y: Math.round(y)});
}
let lvDown = false;
$("limg").addEventListener("pointerdown", e => { lvDown = true; try { $("limg").setPointerCapture(e.pointerId); } catch (x) {} livePoint("down", e); });
$("limg").addEventListener("pointermove", e => { if (lvDown) livePoint("move", e); });
["pointerup", "pointercancel"].forEach(n => $("limg").addEventListener(n, e => { if (lvDown) { lvDown = false; livePoint("up", e); } }));
$("look").onclick = () => { const s = liveSources(); if (s.length) liveShow(s[0]); };
$("lx").onclick = () => liveShow(null);
$("llin").onchange = () => { $("llin").disabled = true; window.orca.postMessage({action:"light", on: $("llin").checked}); };
function liveLight(m){
  $("llin").disabled = false;
  if (m.error) { $("llin").checked = (view.live.light || 0) > 0; $("lmsg").textContent = m.error; return; }
  view.live.light = m.value; $("llin").checked = m.value > 0;
}
$("live").addEventListener("pointerdown", e => { if (e.target === $("live")) liveShow(null); });
$("thumb").onclick = () => $("thumb").parentNode.classList.toggle("big");
document.addEventListener("keydown", e => { if (e.key === "Escape") { $("thumb").parentNode.classList.remove("big"); if (live) liveShow(null); } });
$("tnote").onclick = () => { if (!busy && $("tnote").title) say($("tnote").title, "warn"); };
$("go").onclick = () => fire("send");
$("queue").onclick = () => fire("queue");
$("pre").onclick = () => { lock(true); say(T.inbox); window.orca.postMessage({action:"preflight"}); };
$("cancel").onclick = () => { if (busy) say(T.stop); $("cancel").disabled = true; window.orca.postMessage({action:"cancel"}); };
say(T.load); busy = true; refresh(); $("queue").hidden = true;
window.orca.postMessage({action:"ready"});
</script></body></html>
"""

PAGE_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
html,body{margin:0;height:100%;background:#1e2227;color:#e6e8eb;font:14px system-ui,sans-serif}
#bar{display:flex;gap:8px;padding:6px 8px;align-items:center;border-bottom:1px solid #343a42}
#bar input{flex:1;padding:5px 8px;background:#15181c;color:inherit;border:1px solid #343a42;border-radius:6px;font:inherit}
#bar button{padding:5px 12px;border-radius:6px;border:1px solid #343a42;background:transparent;color:inherit;font:inherit;cursor:pointer}
iframe{border:0;width:100%;height:calc(100% - 42px);background:#fff}
#hint{padding:24px;color:#9aa3ad}
</style></head><body>
<div id="bar"><input id="url" placeholder="http://printer/multiace/"><button id="go">Open</button></div>
<div id="hint"></div><iframe id="f" style="display:none"></iframe>
<script>
const KEY = "multiace.url";
const DE = (navigator.language || "").toLowerCase().startsWith("de");
const $ = id => document.getElementById(id);
$("hint").textContent = DE
  ? "Adresse des Druckers eintragen oder ein Snapmaker-U1-Profil mit Druckeradresse wählen."
  : "Enter the printer address or select a Snapmaker U1 profile with a printer address.";
function norm(u){
  u = (u || "").trim(); if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "http://" + u;
  if (!/\/multiace\/?$/.test(u.split("?")[0])) u = u.replace(/\/+$/, "") + "/multiace";
  return u.replace(/\/+$/, "") + "/";
}
function show(u, keep){
  u = norm(u); if (!u) return;
  $("url").value = u; $("f").src = u; $("f").style.display = ""; $("hint").style.display = "none";
  if (keep) try { localStorage.setItem(KEY, u); } catch (e) {}
}
$("go").onclick = () => show($("url").value, true);
$("url").onkeydown = e => { if (e.key === "Enter") show($("url").value, true); };
let stored = ""; try { stored = localStorage.getItem(KEY) || ""; } catch (e) {}
if (stored) show(stored, false);
window.orca.onMessage(m => {
  if (!m || typeof m !== "object") return;
  if (m.type === "url" && m.url && (!stored || m.force)) show(m.url, false);
});
window.orca.postMessage({action:"ready"});
</script></body></html>
"""

# The address the last send used; the page falls back to it.
_last_base = {"url": ""}




def with_send_step(current):
    """The process option 'slicing_pipeline_plugin' with our send step in
    it. `current` is OrcaSlicer's serialized list (entries separated by
    ';', quoted when they hold spaces). An entry under the name the step
    had before 1.95.60 is renamed. Returns None when nothing changes."""
    current = str(current or "").strip()
    quoted = '"%s"' % SEND_NAME
    renamed = current.replace('"%s"' % OLD_SEND_NAME, quoted)
    if quoted in renamed or SEND_NAME in renamed.split(";"):
        return None if renamed == current else renamed
    return quoted if not renamed else renamed + ";" + quoted



def _profile_base():
    """multiACE address of the selected printer profile, '' if unknown."""
    try:
        bundle = orca.host.preset_bundle()
        return printer_base(bundle.full_config_value("print_host"))
    except Exception:
        return ""


def window_view(printer, engine, report, gcode_path, thumb=None):
    """The view message for a report: the table plus what the window needs
    around it (printer settings, weights, preview, live sources)."""
    view = build_view(report)
    view["options"] = printer.options()
    view["can_queue"] = engine is not None
    view["can_calc"] = bool(engine is not None and view["head_mode"]
                            and engine.can_count())
    view["thumb"] = (extract_thumbnail(gcode_path) if thumb is None
                     else thumb)
    try:
        view["live"] = printer.live_sources()
    except Exception:
        view["live"] = {}
    weights = printer.spools()
    for x in view["targets"]:
        x.update(weights.get(x["id"]) or {})
    return view


SPOOL_WATCH_S = 3.0          # how often the open window asks for changes


class LiveFeed:
    """Frames of the printer's touchscreen or a camera for the window,
    fetched beside the window loop while the window asks for them."""
    PERIOD = {"screen": 0.25, "cam": 0.6}

    def __init__(self, printer, post):
        self.printer, self.post = printer, post
        self.want = None
        self.stop = threading.Event()
        self.thread = None

    def show(self, kind, cam=""):
        self.want = (kind, cam or "") if kind in self.PERIOD else None
        if self.want and (self.thread is None or not self.thread.is_alive()):
            self.stop.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def close(self):
        self.want = None
        self.stop.set()

    def frame(self, want, etag):
        """One message for the window, and the etag to send next."""
        kind, cam = want
        if kind == "screen":
            data, ctype, etag = self.printer.screen_frame(etag)
        else:
            data, ctype, _ = self.printer.cam_frame(cam)
        if data is None:
            return None, etag
        return {"type": "frame", "kind": kind, "cam": cam,
                "data": "data:%s;base64,%s" % (
                    ctype, base64.b64encode(data).decode("ascii"))}, etag

    def _run(self):
        last, etag, fails = None, None, 0
        while not self.stop.is_set():
            want = self.want
            if not want:
                return
            if want != last:
                last, etag, fails = want, None, 0
            try:
                msg, etag = self.frame(want, etag)
                fails = 0
                if msg and self.want == want:
                    self.post(msg)
            except PrinterError as e:
                fails += 1
                if fails == 3 and self.want == want:
                    self.post({"type": "frame", "kind": want[0],
                               "cam": want[1], "error": str(e)})
            except Exception:
                return
            self.stop.wait(self.PERIOD[want[0]])


def run_window(ctx, printer, gcode_path, name, known=None, ui=None):
    """Show the assignment window and act on it.

    Returns ('ok'|'stop'|'skip', text). known: True when the printer has
    already answered as multiACE, None when that could not be asked yet.

    Printer calls run here, on the thread OrcaSlicer called us on; the
    window's callbacks only queue what the user clicked. The one exception
    is the permission touch (Printer.grant), which has to run on the UI
    thread and does so in the callback of the page's first message."""
    ui = ui or orca.host.ui
    inbox = queue.Queue()

    def on_message(msg):
        if isinstance(msg, dict) and msg.get("action") == "ready":
            printer.grant()
        inbox.put(msg)

    win = ui.create_window(
        html=WINDOW_HTML, title="multiACE+", width=900, height=760,
        on_message=on_message,
        on_close=lambda: inbox.put({"action": "closed"}),
        style=ui.WINDOW_MODAL)
    outcome = ("stop", "Not an error. Sending stopped on purpose: "
               "cancelled in the multiACE window.")
    report = view = engine = None
    feed = LiveFeed(printer, lambda m: win.post(m))
    looking, next_watch = False, 0.0
    try:
        while True:
            if ctx.cancelled():
                break
            try:
                msg = inbox.get(timeout=0.15)
            except queue.Empty:
                if not win.is_open():
                    break
                # spools changed on the printer while the window is open
                # (in the compact view, at the machine): show them
                if (view is not None and engine is not None and not looking
                        and time.time() >= next_watch):
                    next_watch = time.time() + SPOOL_WATCH_S
                    try:
                        if engine.changed(printer):
                            report = engine.report(gcode_path, name)
                            fresh = window_view(printer, engine, report,
                                                gcode_path, view["thumb"])
                            if fresh["rows"]:
                                view = fresh
                                win.post({"type": "view", "view": view,
                                          "keep": True})
                    except Exception:
                        pass
                continue
            action = msg.get("action") if isinstance(msg, dict) else None
            if action == "ready":
                if view is not None:
                    win.post({"type": "view", "view": view})
                    continue
                try:
                    if not known and not printer.has_multiace():
                        outcome = ("skip", "no multiACE at " + printer.base)
                        break
                    known = True
                    _last_base["url"] = printer.base
                    engine = make_engine(printer)
                    report = None
                    if engine is not None:
                        try:
                            report = engine.report(gcode_path, name)
                        except ValueError as e:      # a refusal with a reason
                            raise PrinterError(str(e))
                        except Exception:
                            engine = None
                    if report is None:
                        report = printer.preflight(gcode_path, name)
                    view = window_view(printer, engine, report, gcode_path)
                    if not view["rows"]:
                        view = None
                        raise PrinterError("the printer found no slicer "
                                           "colours in this file")
                except PrinterError as e:
                    win.post({"type": "error", "text": str(e)})
                    continue
                win.post({"type": "view", "view": view})
            elif action in ("cancel", "closed"):
                break
            elif action == "live":
                feed.show(msg.get("kind"), msg.get("cam"))
                looking = bool(msg.get("open"))
                if not looking:
                    next_watch = 0.0         # look for changes right away
            elif action == "light":
                try:
                    win.post({"type": "light",
                              "value": printer.set_light(bool(msg.get("on")))})
                except PrinterError as e:
                    win.post({"type": "light", "error": str(e)})
            elif action == "touch":
                try:
                    printer.touch(msg.get("a"), msg.get("x") or 0,
                                  msg.get("y") or 0)
                except Exception:
                    pass
            elif action == "calc":
                card = None
                if engine is not None and view is not None:
                    card = engine.numbers(view, msg.get("choice") or {})
                win.post({"type": "calc", "key": msg.get("key"),
                          "card": card})
            elif action == "preflight":
                try:
                    printer.to_inbox(gcode_path, name)
                except PrinterError as e:
                    win.post({"type": "error", "text": str(e)})
                    continue
                outcome = ("stop", "Not an error. Sending stopped on "
                           "purpose: the file is waiting in the multiACE "
                           "preflight. Open the multiACE page to continue.")
                break
            elif action in ("send", "queue") and view is not None:
                to_queue = action == "queue" and engine is not None
                # The rewrite runs beside this loop, so Cancel and closing
                # the window still work while it is busy.
                stop = threading.Event()
                done = {}
                args = dict(
                    progress=lambda p, st: win.post(
                        {"type": "progress", "percent": p, "stage": st}),
                    prefs=msg.get("prefs") or {},
                    options=msg.get("options") or {},
                    plan=str(msg.get("plan") or ""),
                    cancelled=lambda: stop.is_set() or ctx.cancelled())
                choice = msg.get("choice") or {}

                def work():
                    try:
                        if to_queue:
                            queue_local(engine, printer, report, view,
                                        choice, gcode_path, name, **args)
                        elif engine is not None:
                            rewrite_local(engine, printer, report, view,
                                          choice, gcode_path, **args)
                        else:
                            rewrite_into(printer, report, view, choice,
                                         gcode_path, **args)
                    except PrinterError as e:
                        done["error"] = str(e)
                    except Exception as e:
                        done["error"] = str(e) or type(e).__name__
                    finally:
                        done["end"] = True

                worker = threading.Thread(target=work, daemon=True)
                worker.start()
                while not done.get("end"):
                    try:
                        m = inbox.get(timeout=0.1)
                    except queue.Empty:
                        if ctx.cancelled() or not win.is_open():
                            stop.set()
                        if stop.is_set():
                            break
                        continue
                    act = m.get("action") if isinstance(m, dict) else None
                    if act in ("cancel", "closed"):
                        stop.set()
                        break
                if stop.is_set():
                    worker.join(10.0)
                    if not (done.get("end") and "error" not in done):
                        break            # cancelled: the file is untouched
                if done.get("error"):
                    win.post({"type": "error", "text": done["error"]})
                    continue
                if to_queue:
                    outcome = ("stop", "Not an error. Sending stopped on "
                               "purpose: the file is in the multiACE print "
                               "queue.")
                else:
                    outcome = ("ok", "multiACE: file prepared.")
                break
    finally:
        feed.close()
        try:
            win.close()
        except Exception:
            pass
        for _ in range(20):
            try:
                if not win.is_open():
                    break
            except Exception:
                break
            time.sleep(0.05)
    return outcome


def send_step(ctx, make_printer=Printer, ui=None):
    """The psGCodePostProcess step. Returns ('skip'|'ok'|'stop', text)."""
    if str(ctx.host or "File").strip().lower() == "file":
        return ("skip", "file export")
    path = os.fspath(ctx.gcode_path) if ctx.gcode_path else ""
    if not path or not os.path.isfile(path):
        return ("skip", "no g-code file")
    if already_processed(path):
        return ("skip", "already processed by multiACE")
    base = printer_base(ctx.config_value("print_host"))
    if not base:
        return ("skip", "no printer address in the profile")
    printer = make_printer(base)
    try:
        known = printer.has_multiace()
    except PrinterError:
        known = None      # not reachable, or not allowed yet: the window asks
    if known is False:
        return ("skip", "no multiACE at " + base)
    name = os.path.basename(str(ctx.output_name or "")) or "print.gcode"
    if not name.lower().endswith((".gcode", ".gco", ".g")):
        name += ".gcode"
    return run_window(ctx, printer, path, name, known=known, ui=ui)


if orca is not None:
    class MultiAceSend(orca.slicing.SlicingPipelineCapabilityBase):
        def get_name(self):
            return SEND_NAME

        def execute(self, ctx):
            if ctx.step != orca.slicing.Step.psGCodePostProcess:
                return orca.ExecutionResult.skipped()
            try:
                kind, text = send_step(ctx)
            except Exception as exc:          # never send a half-done file
                kind, text = "stop", "multiACE: %s" % exc
            if kind == "skip":
                return orca.ExecutionResult.skipped(text)
            if kind == "ok":
                return orca.ExecutionResult.success(text)
            return orca.ExecutionResult.failure(
                orca.PluginResult.FatalError, text)

    _capabilities = [MultiAceSend]

    if hasattr(getattr(orca, "pages", None), "PagesPluginCapabilityBase"):
        class MultiAcePage(orca.pages.PagesPluginCapabilityBase):
            def get_name(self):
                return PAGE_NAME

            def get_icon(self):
                return ""

            def get_ui(self):
                return PAGE_HTML

            def on_message(self, message):
                if isinstance(message, dict) and \
                        message.get("action") == "ready":
                    url = _profile_base() or _last_base["url"]
                    if url:
                        self.post_message({"type": "url", "url": url})

        _capabilities.append(MultiAcePage)

    if hasattr(getattr(orca, "script", None), "ScriptPluginCapabilityBase"):
        class MultiAceSwitchOn(orca.script.ScriptPluginCapabilityBase):
            """Adds the send step to the process profile in use, the same
            as picking it under Others > Slicing Pipeline Plugin."""

            def get_name(self):
                return "multiACE+ - switch on for this process"

            def execute(self):
                key = "slicing_pipeline_plugin"
                try:
                    value = with_send_step(
                        orca.host.preset_bundle().full_config_value(key))
                    if value is None:
                        text = "multiACE+ is already on for this process."
                    else:
                        orca.host.preset.process.update(key, value)
                        text = ("multiACE+ is on for this process. Save the "
                                "process profile to keep it.")
                except Exception as exc:
                    text = "multiACE+ could not be switched on: %s" % exc
                    try:
                        orca.host.ui.message(text, "multiACE+")
                    except Exception:
                        pass
                    return orca.ExecutionResult.failure(
                        orca.PluginResult.FatalError, text)
                try:
                    orca.host.ui.message(text, "multiACE+")
                except Exception:
                    pass
                return orca.ExecutionResult.success(text)

        _capabilities.append(MultiAceSwitchOn)

    @orca.plugin
    class MultiAcePlugin(orca.base):
        def register_capabilities(self):
            for cap in _capabilities:
                orca.register_capability(cap)

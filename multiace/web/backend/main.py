"""
multiACE Web - FastAPI backend.

Serves the REST + WebSocket API consumed by both the bundled Vue/CDN
frontend and any future mobile app. Auth is delegated to nginx
(auth_request /auth_check → Moonraker /access/user), so this service
trusts every request that reaches it.

Environment variables:
  MOONRAKER_URL          default http://127.0.0.1:7125
  MULTIACE_CFG_PATH      default /home/lava/printer_data/config/extended/ace.cfg
  MULTIACE_FRONTEND_DIR  default ../frontend (relative to this file)
  MULTIACE_WEB_VERSION   default "0.1.0"
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import websockets

_trace = logging.getLogger("multiace")
_trace.setLevel(logging.INFO)
if not _trace.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[multiace] %(message)s"))
    _trace.addHandler(_h)
    _trace.propagate = False

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import preflight_core

MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")


def _user_paths(rel: str) -> list[str]:
    """Ordered candidates for a path under the Klipper user's home.

    The U1 location stays FIRST, so nothing about a Snapmaker changes; the
    home of whoever runs the service is appended for a generic Klipper host,
    where there is no 'lava' user. Every caller keeps
    its own further candidates after these."""
    out = ["/home/lava/" + rel]
    home = os.path.expanduser("~")
    if home and home not in ("/", "/home/lava"):
        out.append(os.path.join(home, rel))
    return out


def _first_existing(candidates: list[str]) -> str:
    """First candidate that exists, else the first one - so an install that
    is simply missing still reports the path it was looked for under."""
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return candidates[0]


# Anchor on printer_data/config, which exists wherever Klipper runs. Probing
# for 'extended' or 'persistent' instead would fall back to the U1 path on
# any host that does not have those multiACE subfolders yet, which is every
# fresh generic install.
_CFG_DIR = _first_existing(_user_paths("printer_data/config"))
_CFG_EXT_DIR = os.path.join(_CFG_DIR, "extended")

def _resolve_cfg_path() -> str:
    env_path = os.environ.get("MULTIACE_CFG_PATH")
    if env_path:
        return env_path
    
    candidates = [
        os.path.join(_CFG_EXT_DIR, "ace.cfg"),
        os.path.join(_CFG_DIR, "ace.cfg"),
    ]
    return _first_existing(candidates)

MULTIACE_CFG_PATH = _resolve_cfg_path()
SNAPSHOT_DIR = os.environ.get(
    "MULTIACE_SNAPSHOT_DIR",
    os.path.join(_CFG_EXT_DIR, "multiace", "filament_snapshots"),
)
OVERRIDE_FILE = os.environ.get(
    "MULTIACE_OVERRIDE_FILE",
    os.path.join(_CFG_EXT_DIR, "multiace", "slot_overrides.json"),
)
# Firmware filament database (Snapmaker's filament_parameters.py). This is the
# single source of truth for selectable materials + their subtypes; the web
# dropdown is built from it. We parse the module's FILAMENT_PARA_CFG_DEFAULT
# literal with ast (no import - the module needs a Klipper printer object) and
# only read the dict KEYS, never the values (which reference module constants).
FILAMENT_PARAMS_PATHS = tuple(
    os.environ.get(
        "MULTIACE_FILAMENT_PARAMS",
        ":".join(
            _user_paths("klipper/klippy/extras/filament_parameters.py")
            + ["/home/printer_data/klipper/klippy/extras/"
               "filament_parameters.py",
               "/usr/share/klipper/klippy/extras/filament_parameters.py"]
        ),
    ).split(":")
)
# Keys in FILAMENT_PARA_CFG_DEFAULT that are NOT materials.
_FIL_DB_META_KEYS = {
    "version", "hard_filaments_max_flow_k", "soft_filaments_max_flow_k",
}
# Fallback list if the firmware DB can't be read (e.g. unusual install path).
# Kept so the UI is never empty; the firmware DB supersedes it when present.
DEFAULT_MATERIALS = [
    "PLA", "PLA-CF",
    "PETG", "PETG-CF", "PETG-HF",
    "ABS", "ASA",
    "TPU",
    "PA", "PA-CF", "PA-GF", "PA6-CF", "PA6-GF",
    "PC", "PC-ABS",
    "PVA",
]
I18N_DIR = os.environ.get(
    "MULTIACE_I18N_DIR",
    str((Path(__file__).resolve().parent.parent / "i18n")),
)
SCREEN_PROBE_URL = os.environ.get("SCREEN_PROBE_URL", "http://127.0.0.1:8092/snapshot")

# 0003 mitigation: ace.py (the Klipper module) touches this tmpfs flag on
# every homing/probe move. While the flag is fresh we pause our periodic
# Moonraker polling so the web's I/O load doesn't evict klippy code pages
# during the ~50ms homing-probe window (which made toolhead e3 miss the
# trsync window -> "Communication timeout during homing" on eMMC-overlay
# SSH installs). TTL covers the gap between consecutive bed-mesh probe
# points; a stale flag (klippy crashed mid-home) is ignored after it.
HOMING_FLAG_PATH = os.environ.get(
    "MULTIACE_HOMING_FLAG", "/tmp/multiace_homing_active")
HOMING_GATE_TTL = float(os.environ.get("MULTIACE_HOMING_GATE_TTL", "2.0"))

def _homing_active() -> bool:
    """True if ace.py signalled an in-progress homing/probe move recently
    (flag mtime within TTL). Best-effort; any error -> not gating."""
    try:
        age = time.time() - os.path.getmtime(HOMING_FLAG_PATH)
    except OSError:
        return False
    return 0.0 <= age < HOMING_GATE_TTL

# Last-good status cache for the homing gate. _query_state() hits Moonraker,
# which is the I/O path that competes with klippy's homing-probe window. When
# the flag is set we serve the most recent cached result instead of hitting
# Moonraker; if there is no cache yet we briefly wait for the window to close
# (capped) and only then fall through to a real query.
_LAST_STATUS: dict = {}
_LAST_STATUS_TS: float = 0.0
_STATUS_CACHE_TTL = float(os.environ.get("MULTIACE_STATUS_CACHE_TTL", "5.0"))
_GATE_WAIT_MAX = float(os.environ.get("MULTIACE_GATE_WAIT_MAX", "0.5"))

async def _query_state_gated() -> dict:
    """Homing-gated wrapper around _query_state. Serves the last cached
    status during a homing window so on-demand HTTP routes don't add
    Moonraker poll load while the multi-MCU homing-probe is running."""
    global _LAST_STATUS, _LAST_STATUS_TS
    now = time.time()
    if _homing_active():
        if _LAST_STATUS and (now - _LAST_STATUS_TS) <= _STATUS_CACHE_TTL:
            return _LAST_STATUS
        # No usable cache - wait briefly for the homing window to close.
        deadline = now + _GATE_WAIT_MAX
        while _homing_active() and time.time() < deadline:
            await asyncio.sleep(0.05)
    status = await _query_state()
    _LAST_STATUS = status
    _LAST_STATUS_TS = time.time()
    return status

PLUGIN_PORT_RANGE = os.environ.get("MULTIACE_PLUGIN_PORTS", "8089-8098")
PLUGIN_DISCOVERY_TTL = float(os.environ.get("MULTIACE_PLUGIN_TTL", "30"))
DEFAULT_FRONTEND = str((Path(__file__).resolve().parent.parent / "frontend"))
FRONTEND_DIR = os.environ.get("MULTIACE_FRONTEND_DIR", DEFAULT_FRONTEND)
def _resolve_version() -> str:
    v = os.environ.get("MULTIACE_WEB_VERSION", "")
    if v:
        return v
    for path in (_user_paths("klipper/klippy/extras/ace.py")
                 + ["/home/printer_data/klipper/klippy/extras/ace.py",
                    "/usr/share/klipper/klippy/extras/ace.py"]):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        m_ver = re.search(r'^MULTIACE_VERSION\s*=\s*["\']([^"\']+)["\']',
                          head, re.MULTILINE)
        m_tag = re.search(r'^MULTIACE_BUILD_TAG\s*=\s*["\']([^"\']+)["\']',
                          head, re.MULTILINE)
        if m_ver:
            return ('%s+%s' % (m_ver.group(1), m_tag.group(1))
                    if m_tag else m_ver.group(1))
    return "0.2.0"


VERSION = _resolve_version()

ACE_OBJECTS = [
    "ace",
    "filament_feed left",
    "filament_feed right",
    "save_variables",
    "print_task_config",
    "print_stats",
    "idle_timeout",
    # optional background-swap module; an unregistered object queries as {}
    "ace_bg_swap",
    # optional per-material tip-forming module (same {} semantics)
    "ace_tipform",
]

def _slot_state_name(v: Any) -> str:
    if v is None:
        return "unknown"
    return {
        0: "empty", 1: "ready", 2: "loading", 3: "unloading",
        4: "error", 5: "feeding", 6: "assist",
    }.get(v, str(v))

def _resolve_head_source(src: Any) -> tuple[int | None, int | None]:
    """head_source[toolhead] can be null, an int (slot, device implied),
    a list [device, slot] or a dict with 'ace_index'+'slot' keys (the
    shape ace.py emits at LOAD_HEAD time)."""
    if src is None:
        return (None, None)
    if isinstance(src, int):
        return (None, src)
    if isinstance(src, (list, tuple)) and len(src) >= 2:
        return (src[0], src[1])
    if isinstance(src, dict):

        d = src["ace_index"] if "ace_index" in src else src.get("device")
        return (d, src.get("slot"))
    return (None, None)

def _color_to_hex(c: Any, declared: bool = False) -> str | None:
    """[r,g,b] (0-255) -> '#rrggbb', or None for missing.

    All-zero is ambiguous: an EMPTY slot reports (0,0,0), a tag may
    declare black. Stock's discriminator (ace._device_color_hex) decides
    by the TYPE: with a declared type the zeros are the tag's black and are
    kept, without one they mean "nothing known". Callers pass
    declared=bool(type); dropping every (0,0,0) showed a black tag grey."""
    if not isinstance(c, (list, tuple)) or len(c) < 3:
        return None
    r, g, b = int(c[0]), int(c[1]), int(c[2])
    if r == 0 and g == 0 and b == 0 and not declared:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"

def _int_or(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _community_fw_kind(fw):
    """Same rule as ace._community_fw_kind, for status from an older ace.py."""
    fw = str(fw or "").strip().upper()
    if not fw:
        return ""
    if fw.endswith("O"):
        return "ace2_open"
    if fw.startswith("CV"):
        return "gen1_cfw"
    return ""


def _parse_state(status: dict) -> dict:
    """
    Translate the raw multi-object status block into the dashboard schema.

    With ace.py's extended get_status() we now have aces[] with per-ACE
    per-slot detail (RFID, material, brand, colour). The toolheads table
    is enriched from filament_feed left/right + head_source, and we add
    a wiring[] list that shows only loaded source→toolhead links for the
    SVG diagram.
    """

    _reload_overrides_if_changed()

    ace = status.get("ace", {}) or {}
    fl = status.get("filament_feed left",  {}) or {}
    fr = status.get("filament_feed right", {}) or {}
    bg = status.get("ace_bg_swap", {}) or {}
    tf = status.get("ace_tipform", {}) or {}

    device_count = int(ace.get("device_count", 1))
    active_device = int(ace.get("active_device", 0))
    head_source = ace.get("head_source", {}) or {}
    head_manual = ace.get("head_manual", {}) or {}
    head_feeder = ace.get("head_feeder", {}) or {}
    # Reader-resolved spool id per non-ACE head (SpoolLink stamped it from
    # the feeder reader's card UID; Moonraker counts that spool). Rendered
    # as the SL badge on the feeder tile - per-head counting authority.
    head_reader = ace.get("head_reader_spool", {}) or {}
    raw_aces = ace.get("aces", []) or []

    ptc = status.get("print_task_config", {}) or {}
    ptc_types  = ptc.get("filament_type", []) or []
    ptc_subs   = ptc.get("filament_sub_type", []) or []
    ptc_vendors = ptc.get("filament_vendor", []) or []
    ptc_rgbas  = ptc.get("filament_color_rgba", []) or []

    def _ptc_at(n: int) -> dict | None:
        if not (n < len(ptc_types) and n < len(ptc_rgbas)):
            return None
        mat = (ptc_types[n] or "").strip()
        rgba = (ptc_rgbas[n] or "").strip()
        if not mat and not rgba:
            return None

        if mat in ("", "NONE") and rgba in ("", "00000000", "000000FF"):
            return None
        color_hex = None
        if rgba and len(rgba) >= 6 and rgba.upper() != "00000000":
            color_hex = "#" + rgba[:6].lower()
        sub = (ptc_subs[n] or "").strip() if n < len(ptc_subs) else ""
        vendor = (ptc_vendors[n] or "").strip() if n < len(ptc_vendors) else ""
        return {
            "material": mat if mat != "NONE" else "",
            # Same stock placeholder as material/vendor: print_task_config
            # initialises every field with the string "NONE", so the subtype
            # needs the same filter as material and vendor.
            "sku":      sub if sub != "NONE" else "",
            "brand":    vendor if vendor != "NONE" else "",
            "color":    color_hex,
        }

    SLOT_COUNT = 4
    by_idx = {a.get("idx", n): a for n, a in enumerate(raw_aces) if isinstance(a, dict)}

    def _head_in_op(t: int) -> bool:

        feed = (fl if t < 2 else fr).get(
            f"extruder{t}" if t > 0 else "extruder0", {}) or {}
        cs = (feed.get("channel_state") or "")
        if cs and not (cs.endswith("_finish") or cs.endswith("_fail")
                       or cs in ("wait_insert", "inited", "test")):
            if (cs.startswith("load_") or cs.startswith("unload_")
                    or cs.startswith("preload_") or cs.startswith("manual_sta_")):
                return True
        src = head_source.get(str(t)) or head_source.get(t)
        if isinstance(src, dict):
            # A half-written capture still means "operation running". The
            # test is on TYPE only: the colour used to count 000000 as
            # incomplete, which pinned a head whose spool is genuinely
            # black as permanently in-op (buttons disabled). '' is the
            # only "unknown" colour since ace._device_color_hex.
            if not (src.get("type") or "").strip():
                return True
        return False

    def _head_has_filament(t: int):
        feed = (fl if t < 2 else fr).get(
            f"extruder{t}" if t > 0 else "extruder0", {}) or {}
        return feed.get("filament_at_extruder")

    loaded_by_source: dict[tuple[int, int], int] = {}
    for t_key, src in (head_source or {}).items():
        d_l, sl_l = _resolve_head_source(src)
        if d_l is None or sl_l is None:
            continue
        try:
            t_idx = int(t_key)
        except (TypeError, ValueError):
            continue
        if _head_in_op(t_idx):
            continue
        # head_source survives a RUNOUT on purpose (the reload needs the
        # slot, and FA re-arms from it on resume) - it is cleared only by a
        # VERIFIED unload. So it alone does not prove the head still holds
        # filament, and using it alone made an emptied slot claim it still
        # feeds a head: after a runout the slot's label kept coming from
        # that head's print_task_config ("Job"), which then described the
        # spool that had just run out - and kept describing it after a
        # fresh spool went in. Sensor truth decides, same rule the load
        # buttons already use; None (sensor unknown/offline) stays
        # permissive so this can only ever remove a wrong claim.
        if _head_has_filament(t_idx) is False:
            continue
        loaded_by_source[(int(d_l), int(sl_l))] = t_idx

    aces_out: list[dict] = []
    overrides_dirty = False
    for i in range(device_count):
        a = by_idx.get(i, {})
        gate_status = a.get("gate_status") or (
            ace.get("gate_status", []) if i == active_device else []
        )
        ace_slots = a.get("slots", []) or []
        slots_by_idx = {s.get("index", n): s for n, s in enumerate(ace_slots)}
        slots_out = []
        for s in range(SLOT_COUNT):
            sd = slots_by_idx.get(s, {}) or {}
            gate = gate_status[s] if s < len(gate_status) else None
            raw_status = sd.get("status", "") or ""

            is_empty = (
                gate == 0
                or raw_status.startswith("empty")
                or (raw_status == "" and gate is None)
            )

            # An override dies with its spool: a CONFIRMED gate==0 (never the
            # 'empty1' default status) drops the label, else a hand-pulled
            # spool keeps showing the old material/colour. A new RFID spool
            # brings its own identity and an undeclared one gets declared by
            # hand. Quad replenish gets its want-side from the head_source
            # capture inheritance (_inherit_prev_capture), not from a label
            # outliving the spool.
            if gate == 0:
                _now = time.time()
                _pending = _eject_pending_since.get((i, s))
                if _pending is None:
                    _eject_pending_since[(i, s)] = _now
                elif _now - _pending >= EJECT_DEBOUNCE_S:
                    if _drop_override_if_present(i, s):
                        overrides_dirty = True
                    _eject_pending_since.pop((i, s), None)
            else:
                _eject_pending_since.pop((i, s), None)
            rfid_status = sd.get("rfid", 0)
            rfid_data = None
            if rfid_status == 2:
                rfid_data = {
                    "material": sd.get("material", "") or sd.get("type", ""),
                    "brand":    sd.get("brand", ""),
                    "sku":      sd.get("sku", ""),
                    "subtype":  sd.get("subtype", ""),
                    "color":    _color_to_hex(
                        sd.get("color"),
                        declared=bool((sd.get("material", "")
                                       or sd.get("type", "")).strip())),
                }

            override = _override_for(i, s)
            loaded_t = loaded_by_source.get((i, s))
            # Identity precedence: Override > RFID > (only if loaded into a
            # head) job config > none. `source` exposes provenance to the UI
            # badge.
            if override is not None:
                ptc_overlay = {
                    "material": override.get("material", ""),
                    # subtype rides in the dedicated 'subtype' field (below);
                    # don't also stuff it into sku or the card shows it twice.
                    "sku":      "",
                    "brand":    override.get("brand", ""),
                    "color":    override.get("color") or None,
                }
                source = "override"
            elif rfid_data is not None and not is_empty:
                ptc_overlay = {
                    "material": rfid_data["material"],
                    "sku":      rfid_data["sku"],
                    "brand":    rfid_data["brand"],
                    "color":    rfid_data["color"],
                }
                source = "rfid"
            elif loaded_t is not None:
                ptc_overlay = _ptc_at(loaded_t)
                source = "derived" if ptc_overlay is not None else None
            else:
                ptc_overlay = None
                source = None

            # Sub-type to display (e.g. RFID 'PLA Glow' -> material 'PLA',
            # subtype 'Glow'). Same precedence as the identity above.
            if override is not None:
                disp_subtype = (override.get("subtype") or "").strip()
            elif rfid_data is not None and not is_empty:
                disp_subtype = (sd.get("subtype") or "").strip()
            elif loaded_t is not None and loaded_t < len(ptc_subs):
                disp_subtype = (ptc_subs[loaded_t] or "").strip()
            else:
                disp_subtype = ""

            if is_empty and ptc_overlay is None:
                slots_out.append({
                    "idx":       s,
                    "state":     "empty",
                    "raw":       gate,
                    "status":    raw_status,
                    "rfid":      0,
                    "material":  "",
                    "brand":     "",
                    "sku":       "",
                    "subtype":   "",
                    "color":     None,
                    "color_rgb": None,
                    "uid":       "",
                    "tag_format": "",
                    "rfid_data": rfid_data,
                    "source":    "empty",
                })
            else:

                if ptc_overlay is not None:
                    slots_out.append({
                        "idx":       s,
                        "state":     "ready" if not is_empty else "empty",
                        "raw":       gate,
                        "status":    raw_status,
                        "rfid":      rfid_status,
                        "material":  ptc_overlay["material"],
                        "brand":     ptc_overlay["brand"],
                        "sku":       ptc_overlay["sku"],
                        "subtype":   disp_subtype,
                        "color":     ptc_overlay["color"],
                        "color_rgb": None,
                        # Same two fields as the no-identity branch below:
                        # a slot WITH an identity (RFID/override/spool)
                        # took this branch and lost uid + tag_format -
                        # MIFARE (no identity) showed its format, an
                        # OpenSpool/Anycubic tag showed none.
                        "uid":       (sd.get("uid") or ""),
                        "tag_format": (sd.get("tag_format") or ""),
                        "rfid_data": rfid_data,
                        "source":    source,
                    })
                else:
                    slots_out.append({
                        "idx":       s,
                        "state":     _slot_state_name(gate),
                        "raw":       gate,
                        "status":    raw_status,
                        "rfid":      rfid_status,
                        "material":  sd.get("material", "") or sd.get("type", ""),
                        "brand":     sd.get("brand", ""),
                        "sku":       sd.get("sku", ""),
                        "subtype":   disp_subtype,
                        "color":     _color_to_hex(
                            sd.get("color"),
                            declared=bool((sd.get("material", "")
                                           or sd.get("type", "")).strip())),
                        "color_rgb": sd.get("color"),
                        # Card UID of the last host read (also for a
                        # UID-only read without identity) - seeds a new
                        # spool's sku from the picker.
                        "uid":       (sd.get("uid") or ""),
                        # anycubic / openspool / mifare / unknown
                        "tag_format": (sd.get("tag_format") or ""),
                        "rfid_data": rfid_data,
                        "source":    source,
                    })
        aces_out.append({
            "idx":          i,
            "connected":    a.get("connected"),
            "protocol":     a.get("protocol", ""),
            "model":        a.get("model", ""),
            "firmware":     a.get("firmware", ""),
            # ACE2-Open firmware (Simon-CR): version string ends in the
            # letter O (V1.1.3O) - same rule as ace._is_open_fw_idx. The
            # RC522 tag read/write live only there.
            "open_fw":      str(a.get("firmware", "") or "").strip().upper().endswith("O"),
            # Display-only badge: 'ace2_open' | 'gen1_cfw' | ''. Klipper sends
            # it since 1.20b; derived here from the firmware string for an
            # older ace.py (same rules).
            "community_fw": (a.get("community_fw")
                             if a.get("community_fw") is not None
                             else _community_fw_kind(a.get("firmware", ""))),
            "status":       a.get("status"),
            "temp":         a.get("temp"),

            "humidity":     a.get("humidity"),
            "auto_dry":     a.get("auto_dry"),
            "auto_dry_running": bool(a.get("auto_dry_running")),
            "dryer":        a.get("dryer_status") or {},
            "feed_assist":  a.get("feed_assist", -1),
            # For the ACE 2 OTA updater: which device node to open once the
            # port is released, and whether the release hold is active.
            # _parse_state REBUILDS this dict field by field, so a field
            # added to Klipper's get_status does not exist here until it is
            # passed through explicitly.
            "serial_path":  a.get("serial_path", ""),
            "fw_hold":      bool(a.get("fw_hold")),
            "slots":        slots_out,
        })

    if overrides_dirty:
        _save_overrides_to_disk()

    toolheads = []
    wiring = []
    for t in range(4):
        ext_key = f"extruder{t}" if t > 0 else "extruder0"
        feed = (fl if t < 2 else fr).get(ext_key, {}) or {}

        _src_raw = head_source.get(str(t)) or head_source.get(t)
        d_explicit, sl_explicit = _resolve_head_source(_src_raw)
        # ace.py keeps head_source on a FAILED load (the retry-unload needs the
        # slot) but stamps load_failed=True. Surface it so the UI can render
        # "load failed" instead of "loaded" and allow a direct retry.
        load_failed = bool(isinstance(_src_raw, dict)
                           and _src_raw.get("load_failed"))
        loaded = bool(feed.get("filament_detected"))
        color = None
        material = ""
        subtype = ""
        sku = ""
        brand = ""
        source = None
        ace_field = None
        slot_field = None
        if d_explicit is not None and sl_explicit is not None:
            ace_field = d_explicit
            slot_field = sl_explicit
            if 0 <= d_explicit < len(aces_out):
                slots_arr = aces_out[d_explicit]["slots"]
                if 0 <= sl_explicit < len(slots_arr):
                    slot_obj = slots_arr[sl_explicit]
                    color = slot_obj.get("color")
                    material = slot_obj.get("material", "")
                    # Display the resolved SUBTYPE (override>rfid>derived), not
                    # the raw spool SKU - the toolhead card was showing e.g.
                    # 'AHPLBK-101' as if it were a subtype. sku stays available
                    # for matching but is no longer the displayed sub-line.
                    subtype = slot_obj.get("subtype", "")
                    sku = slot_obj.get("sku", "")
                    source = slot_obj.get("source")
        is_manual = bool(head_manual.get(str(t), head_manual.get(t, False)))
        # head mode: each head is individually ACE or a stock feeder
        # (head_feeder flag). A feeder head has no ACE source - render it like a
        # manual head (identity from print_task_config, no ACE/slot wiring),
        # just flagged "feeder".
        op_mode = ace.get("mode", "multi")
        is_feeder = (op_mode == "head"
                     and bool(head_feeder.get(str(t), head_feeder.get(t, False)))
                     and not is_manual)
        if is_manual or is_feeder:
            # Manual/TPU or feeder head: never show an ACE/slot mapping (even a
            # stale head_source a pre-fix display load may have stamped on).
            # Show the filament the user set at the display (print_task_config).
            # Also drop d_explicit/sl_explicit so the wiring SVG (built from
            # them below) draws no ACE->head line.
            d_explicit = sl_explicit = None
            ace_field = slot_field = None
            color = None
            material = subtype = sku = brand = ""
            source = None
            ptc_id = _ptc_at(t)
            if ptc_id:
                material = ptc_id.get("material", "") or ""
                color = ptc_id.get("color")
                subtype = ptc_id.get("sku", "") or ""
                brand = ptc_id.get("brand", "") or ""
        toolheads.append({
            "idx":                t,
            "name":               f"T{t}",
            "ace":                ace_field,
            "slot":               slot_field,
            "filament_detected":  feed.get("filament_detected"),
            "filament_in_ace":      feed.get("filament_in_ace"),
            "filament_in_toolhead": feed.get("filament_in_toolhead"),
            "filament_at_extruder": feed.get("filament_at_extruder"),
            "channel_state":      feed.get("channel_state"),
            "channel_error":      feed.get("channel_error"),
            "module_exist":       feed.get("module_exist"),
            "color":              color,
            "material":           material,
            "subtype":            subtype,
            "sku":                sku,
            "brand":              brand,
            "head_source_known":  (d_explicit is not None) and not is_manual and not is_feeder,
            "load_failed":        load_failed and not is_manual and not is_feeder,
            "manual":             is_manual,
            "feeder":             is_feeder,
            "reader_spool_id":    (int(head_reader.get(str(t),
                                                       head_reader.get(t, 0))
                                       or 0)
                                   if (is_manual or is_feeder) else 0),
            "source":             source,
        })

        if d_explicit is not None and sl_explicit is not None:
            wiring.append({
                "ace": d_explicit, "slot": sl_explicit, "toolhead": t,
                "color": color, "material": material,
            })

    sv = status.get("save_variables", {})
    sv_vars = sv.get("variables", {}) if isinstance(sv, dict) else {}
    mode = sv_vars.get("ace__mode", "normal")

    ps = status.get("print_stats", {}) or {}
    it = status.get("idle_timeout", {}) or {}
    ps_state = (ps.get("state") or "").lower()
    if ps_state in ("printing", "paused", "complete", "error"):

        printer_state = ps_state
    else:

        raw_it = (it.get("state") or "Idle").lower()
        printer_state = "busy" if raw_it == "printing" else raw_it
    language = sv_vars.get("ace__language", os.environ.get("MULTIACE_LANGUAGE", "en"))
    idx_base = _read_display_index_base()
    return {
        'calibration':        ace.get('calibration') or {'state': 'idle'},
        "unload_all_active":  bool(ace.get("unload_all_active", False)),
        "ace_status":         ace.get("status"),
        "ace_temp":           ace.get("temp"),
        "printer_state":      printer_state,
        "active_device":      active_device,
        "device_count":       device_count,
        "mode":               mode,
        "pickup_cleaning":    bool(ace.get("pickup_cleaning", False)),
        "confirm_commands":   bool(ace.get("confirm_commands", False)),
        "airprint_detection": bool(ace.get("airprint_detection", False)),
        "quad_replenish": bool(ace.get("quad_replenish", False)),
        "purge_matrix": bool(ace.get("purge_matrix", True)),
        # Multifilament-Optimierung: targets per slicer colour (1 = off).
        # An older ace.py has no field -> 1, today's behaviour.
        "preflight_max_copies": _int_or(ace.get("preflight_max_copies", 1), 1),
        "preflight_copies_strict": bool(ace.get("preflight_copies_strict", False)),
        # Default TRUE like the engine; an older ace.py has no field.
        "pa_sync": bool(ace.get("pa_sync", True)),
        # RC522 tag read/write enabled - gates the picker "write to tag" button.
        "rc522": bool(ace.get("rc522", False)),
        # [ace] tag_write_format - the picker's preselected write format.
        "tag_write_format": str(ace.get("tag_write_format") or "openspool"),
        "tag_write_uid_sku": bool(ace.get("tag_write_uid_sku", True)),
        # Running tag op {busy, kind} - drives the "writing..." bar.
        "tag_op": dict(ace.get("tag_op") or {}),
        # '{dia}_{volume_type}' per head (list, unset heads dropped) - the
        # PA dialog's selectable keys. Cheap engine-side lookups.
        "nozzle_keys": ace.get("nozzle_keys", []) or [],
        "quad_first": bool(ace.get("quad_first", False)),
        # ACE 2 indices a follower may point at (only they report humidity).
        "auto_dry_masters":   ace.get("auto_dry_masters", []) or [],
        # Spool table (Klipper-owned; the UI edits via gcode only).
        "spools": ace.get("spools", {}) or {},
        "spool_binding": ace.get("spool_binding", {}) or {},
        # Last feeder-reader code per non-ACE head (card UID hex / M1 SKU
        # int as string) - source of the tag sweep's head loop.
        "head_tag_seen": ace.get("head_tag_seen", {}) or {},
        "spoolman_url": ace.get("spoolman_url", "") or "",
        "spoolman_auto": bool(ace.get("spoolman_auto", False)),
        # The spool world switch (local|spoolman|spoollink). An older
        # ace.py has no field - derive its world the pre-switch way (URL
        # set = spoolman) so a mixed deploy keeps behaving.
        "spool_mode": (ace.get("spool_mode")
                       or ("spoolman" if (ace.get("spoolman_url") or "")
                           else "local")),
        # SpoolLink world live (drives the red no-uuid badge) + whether
        # the resolver agent is registered (gates offering the mode).
        "spoollink": bool(ace.get("spoollink", False)),
        "spoollink_agent": bool(ace.get("spoollink_agent", False)),
        "ace_head":           int(ace.get("ace_head", 3) or 3),
        "ace_heads":          ace.get("ace_heads", []) or [],
        "head_feeder":        head_feeder,
        "head_ace":           ace.get("head_ace", {}) or {},
        "language":           language,
        "display_index_base": idx_base,
        "dryer":              ace.get("dryer_status"),
        "swap_in_progress":   bool(ace.get("swap_in_progress", False)),
        "aces":               aces_out,
        "toolheads":          toolheads,
        "wiring":             wiring,
        "save_variables":     sv_vars,
        # Background-swap module ([ace_bg_swap]). Klipper answers {} for an
        # unregistered object (webhooks QueryStatusHelper), so a printer
        # without the section reports available=False here - no error path.
        "bg_swap": {
            "available":     bool(bg.get("version")),
            "version":       bg.get("version"),
            "enabled_heads": bg.get("enabled_heads", []) or [],
            "busy":          bg.get("busy", []) or [],
        },
        # Per-material tip forming ([ace_tipform]) - the LIVE state (what
        # Klipper currently runs). The cfg may differ until a restart; the
        # tipform editor compares both to show "restart pending".
        "tipform": {
            "available": bool(tf.get("mode")),
            "mode":      tf.get("mode"),
            "tables":    tf.get("tables", []) or [],
        },
        # Send-to-multiACE inbox: a slicer-pushed file waiting for pickup.
        # Local disk stat only - piggybacked here so the UI needs no extra
        # poll to notice a delivery.
        "preflight_inbox": _inbox_status(),
    }

async def _query_state() -> dict:
    qs = "&".join(o.replace(" ", "%20") for o in ACE_OBJECTS)
    data = await _mr_get(f"/printer/objects/query?{qs}")
    return data.get("result", {}).get("status", {})


async def _machine_nozzles() -> dict:
    """{head: nozzle diameter mm} straight from Klipper's extruder objects.

    WHY this is not derived from the gcode: the file's `nozzle_diameter` list
    says which diameter each FILAMENT was sliced for (a FOrcaSlicer file with
    10 filaments carries 10 entries, every used tool matching its measured
    line width). It does NOT say which HEAD carries which nozzle.
    Reading the first four entries as "head 0..3" happens to work when the
    slicer profile lists them in machine order and breaks silently otherwise -
    a mixed file declared 0.2,0.8,0.4,0.6 and nothing in it states whether that
    is the physical order. So the file supplies demand, the printer supplies
    supply, and the gate matches the two.

    Deliberately NOT in ACE_OBJECTS: that list is pulled on every status poll,
    while nozzle diameters only matter per preflight. Empty dict on any failure
    -> callers fall back to the file-derived reading rather than blocking."""
    try:
        objs = ["extruder"] + ["extruder%d" % i for i in range(1, 4)]
        qs = "&".join(objs)
        data = await _mr_get(f"/printer/objects/query?{qs}")
        st = data.get("result", {}).get("status", {})
    except Exception as e:
        logging.info("[multiace] nozzle query failed (ignored): %s", e)
        return {}
    out = {}
    for i, name in enumerate(objs):
        d = (st.get(name) or {}).get("nozzle_diameter")
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if d > 0:
            out[i] = d
    return out

async def _machine_nozzle_types() -> dict:
    """{head: 'standard'|'high_flow'} from the extruder status objects.

    Firmware 1.6.0 introduced the nozzle VOLUME TYPE next to the diameter
    (per-extruder JSON, SET_NOZZLE_PROPERTIES, sts['nozzle_volume_type']) -
    the supply side of the HF/standard axis that previously had no source
    at all. Empty dict on pre-1.6.0 firmware (the field is simply absent) -
    callers must treat missing as unknown, never as 'standard'."""
    try:
        objs = ["extruder"] + ["extruder%d" % i for i in range(1, 4)]
        data = await _mr_get("/printer/objects/query?" + "&".join(objs))
        st = data.get("result", {}).get("status", {})
    except Exception as e:
        logging.info("[multiace] nozzle type query failed (ignored): %s", e)
        return {}
    out = {}
    for i, name in enumerate(objs):
        vt = (st.get(name) or {}).get("nozzle_volume_type")
        if isinstance(vt, str) and vt:
            out[i] = vt
    return out

app = FastAPI(title="multiACE Web", version=VERSION)

class MacroRequest(BaseModel):
    name: str
    args: dict[str, Any] | None = None

class MacroBatchRequest(BaseModel):
    commands: list[MacroRequest]

class ConfigUpdate(BaseModel):
    content: str
    restart_klipper: bool = False
    # Optimistic-concurrency token: the sha1 the browser got from GET
    # /api/config. The config FORM patches its cached copy of the file
    # (formToCfgContent), so a stale tab silently writes back an old
    # revision and can revert a cfg repaired via SSH. None = no check (raw
    # editor / old
    # frontend), so this stays backward compatible.
    base_sha1: str | None = None

class TipformUpdate(BaseModel):
    mode: str
    tables: dict[str, str]
    restart_klipper: bool = False


class CalibrationStart(BaseModel):
    ace: int
    slot: int
    head: int
    scope: str = 'ace'

class CalibrationAction(BaseModel):
    action: str
    length: int | None = None
    session_id: int | None = None
    ace: int | None = None
    slot: int | None = None
    head: int | None = None
    scope: str | None = None

class SnapshotSave(BaseModel):
    name: str
    description: str | None = None
    mode: str | None = None    # "head" -> stored separately from multi

class HeadManual(BaseModel):
    head: int
    enable: bool

class HeadFeeder(BaseModel):
    head: int
    enable: bool

class HeadAce(BaseModel):
    head: int
    ace: int

class SlotOverride(BaseModel):
    ace: int
    slot: int
    material: str | None = ""
    brand: str | None = ""
    subtype: str | None = ""
    color: str | None = ""

async def _mr_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{MOONRAKER_URL}{path}")
        r.raise_for_status()
        return r.json()

async def _mr_post(path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{MOONRAKER_URL}{path}", json=body or {})
        r.raise_for_status()
        return r.json()

@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": VERSION, "ts": time.time()}

@app.get("/api/version")
async def version() -> dict:

    printer = {}
    try:
        sysinfo = await _mr_get("/machine/system_info")
        pi = (sysinfo.get("result", {})
                     .get("system_info", {})
                     .get("product_info", {})) or {}
        printer = {
            "device_name":      pi.get("device_name"),
            "machine_type":     pi.get("machine_type"),
            "firmware_version": pi.get("firmware_version"),
        }
    except Exception:
        pass
    return {
        "web": VERSION,
        "moonraker_url": MOONRAKER_URL,
        "config_path": _resolve_cfg_path(),
        "frontend_dir": FRONTEND_DIR,
        "printer": printer,
    }

_PREFLIGHT_DIR = Path("/tmp/multiace-preflight")
_PREFLIGHT_TTL = 86400.0
_PREFLIGHT_FUZZY = 30

_PREFLIGHT_MAX_SIZE = int(os.environ.get(
    "MULTIACE_PREFLIGHT_MAX_MB", "110")) * 1024 * 1024

# --- Send-to-multiACE inbox -------------------------------------------------
# A store-only drop point for an external tool (slicer "Send to multiACE").
# Deliberately NOT /api/preflight: that endpoint analyses on the printer's
# slow CPU. The inbox just keeps the raw file; the browser picks it up via
# the normal Pyodide preflight flow, so the analysis runs on the user's PC
# and against the slot state at pickup time, not at send time. One slot,
# newest wins, one-shot delivery (the UI deletes on pickup).
# The size cap is larger than _PREFLIGHT_MAX_SIZE on purpose - storage is
# cheap, only server-side ANALYSIS is slow. Streamed to disk in chunks so a
# big upload never sits in RAM on the U1.
_INBOX_DIR = _PREFLIGHT_DIR / "inbox"

def _inbox_max_size() -> int:
    """Upload cap in bytes. [ace] inbox_max_mb from ace.cfg wins (the
    display_index_base pattern - ace.py reads the option too, purely so
    Klipper's option check passes), env var as fallback, default 256 MB.
    Read per upload, so a cfg edit applies without a web restart."""
    raw = _read_cfg_scalars().get("inbox_max_mb")
    if raw is None:
        raw = os.environ.get("MULTIACE_INBOX_MAX_MB", "256")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        v = 256
    return max(1, min(v, 4096)) * 1024 * 1024
# Already-processed files are refused at the door (re-processing
# scrambles the swaps). Two markers: format-stamped output (>= PP_FORMAT_VERSION builds)
# and the plain auto-load comment older builds emitted.
_INBOX_PROCESSED_MARKERS = (b"; multiACE processed:", b"; multiACE auto-load:")

def _inbox_paths():
    return (_INBOX_DIR / "pending.gcode", _INBOX_DIR / "pending.name")

def _inbox_status() -> dict:
    gpath, npath = _inbox_paths()
    try:
        st = gpath.stat()
    except OSError:
        return {"pending": False, "name": None, "size": 0, "ts": 0}
    name = ""
    try:
        name = npath.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return {"pending": True, "name": name or "upload.gcode",
            "size": st.st_size, "ts": st.st_mtime}

def _inbox_clear():
    for p in _inbox_paths():
        try:
            p.unlink()
        except OSError:
            pass

_pp_module = None
_pp_src_sig = None

def _load_post_processor():
    """Lazy-load the post-processor as a Python module so its parsing
    and remap helpers can be reused server-side without a subprocess.
    mtime-aware: a multiACE update only restarts Klipper, NOT this uvicorn
    process - a process-lifetime cache made every preflight after an update
    silently run the OLD post-processor until the next reboot. Reload
    whenever
    the source file changed (path/mtime/size signature)."""
    global _pp_module, _pp_src_sig
    candidates = [
        Path(p) for p in _user_paths(
            "printer_data/config/tools/post_process_virtual_toolheads.py")
    ] + [
        Path(__file__).resolve().parent.parent.parent / "tools" / "post_process_virtual_toolheads.py",
    ]
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        raise HTTPException(status_code=503,
                            detail="post-processor script not installed")
    try:
        st = src.stat()
        sig = (str(src), st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (str(src), 0, 0)
    if _pp_module is not None and sig == _pp_src_sig:
        return _pp_module
    import importlib.util
    spec = importlib.util.spec_from_file_location("multiace_postprocess", src)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"post-processor failed to load: {exc}")
    _pp_module = mod
    _pp_src_sig = sig
    return mod

def _cleanup_preflight_dir() -> None:
    if not _PREFLIGHT_DIR.is_dir():
        return
    now = time.time()
    for p in _PREFLIGHT_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > _PREFLIGHT_TTL:
                p.unlink()
        except Exception:
            pass

async def _live_slots_async() -> list[dict]:
    status = await _query_state_gated()
    out = []
    parsed = _parse_state(status)
    for ace in parsed.get("aces", []) or []:
        for slot in ace.get("slots", []) or []:
            if slot.get("state") == "empty":
                continue
            # Preflight trusts only physically-known identity: an RFID tag
            # or a user override. A 'derived' (job print_task_config) label
            # is not a real slot identity and must not become a selectable,
            # colored preflight target.
            if slot.get("source") not in ("rfid", "override"):
                continue
            out.append({
                "ace":      ace.get("idx"),
                "slot":     slot.get("idx"),
                "material": (slot.get("material") or "").strip(),
                "color":    (slot.get("color") or "").strip().lower(),
            })
    return out


def _remap_mapping(base_mapping: list[dict], remap_t_to_t: dict[int, int]) -> list[dict]:
    """Apply a T-index → T-index remap on top of an existing slicer-T →
    physical-slot mapping. The remap is the format that
    compute_optimal_remap()/apply_layer_remap() emit: keys are
    post-live-lookup T-indices (= ace*4+slot), values are the
    optimized T-indices the rewritten gcode will use. We translate
    each base entry's slot back through that to land on the
    physical ACE/slot the new gcode will actually target."""
    out = []
    for m in base_mapping:
        if m["slot"] is None:
            out.append(m)
            continue
        live_t = m["slot"]["ace"] * 4 + m["slot"]["slot"]
        new_t = remap_t_to_t.get(live_t, live_t)
        new_slot = dict(m["slot"])
        new_slot["ace"]  = new_t // 4
        new_slot["slot"] = new_t % 4
        new_m = dict(m)
        new_m["slot"] = new_slot
        out.append(new_m)
    return out


async def _head_mode_context() -> dict:
    """Head-mode preflight context: mode, the ACE head list + each head's ACE,
    and the loaded feeders (pin candidates). ace_head/ace_heads/head_ace let the
    matcher build one swap bin per ACE head from its own ACE's slots."""
    status = await _query_state_gated()
    parsed = _parse_state(status)
    mode = parsed.get("mode") or "normal"
    ace_head = int(parsed.get("ace_head", 3) or 3)
    ace_heads = [int(h) for h in (parsed.get("ace_heads") or [])]
    raw_head_ace = parsed.get("head_ace", {}) or {}
    head_ace = {}
    for h in range(4):
        try:
            head_ace[h] = int(raw_head_ace.get(str(h), raw_head_ace.get(h, h)))
        except (TypeError, ValueError):
            head_ace[h] = h
    feeders = []
    for th in parsed.get("toolheads", []) or []:
        if not th.get("feeder"):
            continue
        if not th.get("filament_detected"):
            continue                       # empty feeder (no spool fed) can't pin
        mat = (th.get("material") or "").strip()
        col = (th.get("color") or "").strip()
        if not mat and not col:
            continue                       # present but no identity -> can't pin
        feeders.append({"head": int(th["idx"]), "material": mat, "color": col})
    # Hand-fed (manual) heads, multi mode: pin candidates like the feeders
    # in head mode, identity from print_task_config (what the user set at
    # the display), presence = the toolhead sensor. Listed even WITHOUT an
    # identity: the head cannot pin then, but its slot index is dead for
    # ACE colours and the planner must know.
    manual_heads = []
    for th in parsed.get("toolheads", []) or []:
        if not th.get("manual"):
            continue
        has_fil = th.get("filament_at_extruder") is not False
        manual_heads.append({
            "head": int(th["idx"]),
            "material": (th.get("material") or "").strip() if has_fil else "",
            "color": (th.get("color") or "").strip() if has_fil else ""})
    bgs = parsed.get("bg_swap") or {}
    # Colour copies (Multifilament-Optimierung): the EMPTY slots the planner
    # may suggest a second spool for, and the max targets per colour. Empty
    # = the slot's own presence gate reads empty (not merely "no identity").
    empty_slots = []
    for a in parsed.get("aces", []) or []:
        for sl in a.get("slots", []) or []:
            if sl.get("state") == "empty":
                empty_slots.append({"ace": a.get("idx"), "slot": sl.get("idx")})
    # Per-head nozzle diameters for the mixed-nozzle gate. Carried in head_ctx
    # rather than fetched separately because head_ctx is the ONE object both
    # preflight paths already receive - the backend passes it straight in, and
    # /api/preflight/context hands the identical dict to the browser worker.
    # A separate channel would have had to be plumbed twice and could drift.
    # Not mode-specific despite the function name: multi needs it just as much.
    return {"mode": mode, "ace_head": ace_head, "ace_heads": ace_heads,
            "head_ace": head_ace, "feeders": feeders,
            "manual_heads": manual_heads,
            "head_nozzles": {str(h): d
                             for h, d in (await _machine_nozzles()).items()},
            # 1.6.0+: per-head nozzle volume type (standard/high_flow).
            # Empty on older firmware - consumers must read absence as
            # UNKNOWN, not as standard: do not invent a value the machine
            # never declared).
            "head_nozzle_types": {str(h): v
                                  for h, v in
                                  (await _machine_nozzle_types()).items()},
            "pickup_cleaning": bool(parsed.get("pickup_cleaning")),
            "max_copies": _int_or(parsed.get("preflight_max_copies", 1), 1),
            "copies_strict": bool(parsed.get("preflight_copies_strict")),
            "empty_slots": empty_slots,
            "bg_available": bool(bgs.get("available")),
            "bg_heads": [int(h) for h in (bgs.get("enabled_heads") or [])]}


@app.post("/api/preflight")
async def preflight(file: UploadFile = File(...)) -> dict:
    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not safe_name.lower().endswith((".gcode", ".gco", ".g")):
        raise HTTPException(status_code=400, detail="not a g-code file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _PREFLIGHT_MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(f"This g-code is too large for in-printer preflight "
                    f"({len(data)//1024//1024} MB > "
                    f"{_PREFLIGHT_MAX_SIZE//1024//1024} MB limit). The "
                    f"Snapmaker U1 is too slow to analyse files this large. "
                    f"Run the multiACE post-processing script in your slicer "
                    f"instead - it does the same analysis on your PC in "
                    f"seconds - then upload the result directly via Moonraker. "
                    f"Advanced: raise the limit via the "
                    f"MULTIACE_PREFLIGHT_MAX_MB env var."))

    _cleanup_preflight_dir()
    _PREFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    import uuid as _uuid
    token = _uuid.uuid4().hex
    upload_size = len(data)
    src_path = _PREFLIGHT_DIR / (token + ".gcode")
    src_path.write_bytes(data)
    (_PREFLIGHT_DIR / (token + ".name")).write_text(safe_name, encoding="utf-8")
    del data

    pp = _load_post_processor()

    with open(src_path, "r", encoding="utf-8", errors="replace") as f:
        slicer_colors, slicer_types, num_aces, _used, plan_proxy, meta = \
            preflight_core.parse_meta(pp, f)

    live_slots = await _live_slots_async()
    if not live_slots:
        raise HTTPException(status_code=409,
                            detail="no slots are loaded on the printer")
    head_ctx = await _head_mode_context()

    # The report build (multi plans OR the head-mode preview) lives in
    # preflight_core, shared verbatim with the in-browser Pyodide path - one
    # source of truth, no JS re-port / drift.
    try:
        return preflight_core.build_report(
            pp, slicer_colors=slicer_colors, slicer_types=slicer_types,
            num_aces=num_aces, plan_proxy=plan_proxy, live_slots=live_slots,
            head_ctx=head_ctx, token=token, filename=safe_name, size=upload_size,
            fuzzy=_PREFLIGHT_FUZZY, meta=meta)
    except ValueError as e:
        # e.g. an already-processed file (re-processing scrambles the swaps):
        # a user error with a clear fix, not a server fault.
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        # e.g. a stale post-processor without head-mode support (PAXX-bin
        # upgrade): surface a clear, actionable message instead of a bare 500.
        raise HTTPException(status_code=503, detail=str(e))

_PREFLIGHT_JOBS: dict[str, dict] = {}
_PREFLIGHT_JOBS_LOCK = asyncio.Lock()
_PREFLIGHT_JOB_TTL = 600.0

def _set_stage(state: dict, stage: str, percent: float) -> None:
    state["stage"]   = stage
    state["percent"] = max(0.0, min(100.0, percent))
    state["ts"]      = time.time()

def _stage_progress(state: dict, base: float, span: float):
    """Return a (bytes_done, bytes_total) callable that maps the
    streaming-fn's progress into the job's overall percent track."""
    def cb(done: int, total: int) -> None:
        if total <= 0:
            return
        state["percent"] = max(state.get("percent", 0.0),
                                base + span * (done / total))
        state["ts"] = time.time()
    return cb

_FLOW_CAL_LINE_RE = re.compile(r"^\s*SM_PRINT_FLOW_CALIBRATE\b", re.IGNORECASE)
_AUTO_LOAD_END_RE = re.compile(r"^\s*;\s*multiACE auto-load: end\b")
_BARE_T_RE = re.compile(r"^\s*T(\d+)\s*(?:;.*)?$")

def _print_prefs_line(bed_mesh: bool, camera: bool,
                      flow_cal: bool = False) -> str:
    """Build the SET_PRINT_PREFERENCES line for the chosen preflight toggles.
    FORCE=1 is required: the line is the first line of the uploaded file, which
    already runs with print_stats.state == 'printing', and stock rejects a
    non-forced preference change there (error 531). FORCE=1 bypasses that gate
    (print_task_config.cmd_SET_PRINT_PREFERENCES); it still runs before the
    start/bed-leveling steps so the flags are set in time.

    FLOW_CALIBRATE: stock's "flow calibration" is the per-head pressure-
    advance measurement (flow_calibrator.FLOW_CALIBRATE). With the flag on,
    stock also IGNORES every SET_PRESSURE_ADVANCE from the slicer while
    printing (kinematics/extruder.py); with it off, the slicer's own line
    (Snapmaker Orca ships ADVANCE=0.02 as a placeholder) is what prints.
    The touchscreen start dialog is the only stock place that turns it on,
    and a web-started print never sees that dialog - so a preflight print
    ran at PA 0.02 whatever the user had calibrated (issue #115: rough
    top surfaces, "overextrusion"). The start gcode's own
    SM_PRINT_FLOW_CALIBRATE lines sit BEFORE our auto-load block (heads
    still empty -> runout pause 523), which is why the flag used to be
    forced off; _prepend_print_prefs moves the calibration behind the
    block instead when the toggle is on."""
    return ("SET_PRINT_PREFERENCES BED_LEVEL=%d FLOW_CALIBRATE=%d "
            "TIME_LAPSE_CAMERA=%d FORCE=1"
            % (1 if bed_mesh else 0, 1 if flow_cal else 0,
               1 if camera else 0))

def _flow_cal_block(lines: list) -> tuple:
    """(insert_index, block_lines) for the relocated flow calibration, or
    (None, []) when the file has no auto-load block to anchor on (then the
    slicer's lines stay where they are = stock placement).

    One T<h> A0 + FLOW_CALIBRATE pair per PHYSICAL head the processed file
    actually prints with (bare T lines), NOT the stock SM_PRINT_FLOW_
    CALIBRATE wrapper: that wrapper skips heads stock believes unused, and
    stock derives 'used' from the slicer header's per-LOGICAL-T grams via
    its map table - which no longer matches the physical heads after our
    remap (issue #115 file: head 0 printed but read as unused). The pair
    is exactly what the wrapper runs internally (print_stats.py). Ends by
    re-selecting the tool the start gcode had selected before the block,
    mirroring stock's own re-select after its calibration, because the
    prime line follows right after the block with the active tool."""
    end_idx = None
    initial = None
    heads = set()
    for i, line in enumerate(lines):
        m = _BARE_T_RE.match(line)
        if m:
            h = int(m.group(1))
            if 0 <= h <= 3:
                heads.add(h)
                if end_idx is None:
                    initial = h
            continue
        if end_idx is None and _AUTO_LOAD_END_RE.match(line):
            end_idx = i
    if end_idx is None or not heads:
        return None, []
    if initial is None:
        initial = min(heads)
    block = ["; multiACE preflight: flow calibration (moved behind the "
             "auto-load, heads loaded)\n"]
    # Start tool LAST: the block ends with the head the start line needs
    # already in the gripper, so the closing re-select is a no-op instead
    # of one more tool change.
    order = sorted(h for h in heads if h != initial) + \
        ([initial] if initial in heads else [])
    for h in order:
        block.append("T%d A0\n" % h)
        block.append("FLOW_CALIBRATE EXTRUDER=%d\n" % h)
    block.append("T%d\n" % initial)
    block.append("; multiACE preflight: flow calibration end\n")
    return end_idx + 1, block

def _prepend_print_prefs(in_path: str, out_path: str,
                         bed_mesh: bool = False, camera: bool = False,
                         flow_cal: bool = False) -> None:
    """Stream-copy in_path to out_path with the print-preference line
    prepended at the very top (before the start gcode's calibration).
    Any SET_PRINT_PREFERENCES the slicer already emits is commented out
    so it can't override ours from further down the file. With flow_cal
    the slicer's SM_PRINT_FLOW_CALIBRATE lines are commented out and the
    calibration is re-emitted behind the auto-load block (see
    _flow_cal_block); the file is read twice for that (anchor + heads),
    still streamed on the write side."""
    ins_idx, block = None, []
    if flow_cal:
        with open(in_path, "r", encoding="utf-8", errors="replace") as src:
            ins_idx, block = _flow_cal_block(src.readlines())
    with open(out_path, "w", encoding="utf-8", errors="replace") as out:
        out.write("; multiACE preflight: print preferences\n")
        out.write(_print_prefs_line(bed_mesh, camera, flow_cal) + "\n")
        with open(in_path, "r", encoding="utf-8", errors="replace") as src:
            for i, line in enumerate(src):
                if ins_idx is not None and i == ins_idx:
                    out.writelines(block)
                if line.lstrip().upper().startswith("SET_PRINT_PREFERENCES"):
                    out.write("; multiACE disabled: " + line.lstrip())
                    continue
                if ins_idx is not None and _FLOW_CAL_LINE_RE.match(line):
                    out.write("; multiACE moved: " + line.lstrip())
                    continue
                out.write(line)
            if ins_idx is not None and ins_idx >= i + 1:
                out.writelines(block)

def _prune_old_jobs() -> None:
    now = time.time()
    dead = [j for j, s in _PREFLIGHT_JOBS.items()
            if s.get("done") and now - s.get("ts", 0) > _PREFLIGHT_JOB_TTL]
    for j in dead:

        for k in ("tmp_in", "tmp_a", "tmp_b", "tmp_out"):
            p = _PREFLIGHT_JOBS[j].get(k)
            if p:
                try: Path(p).unlink()
                except Exception: pass
        del _PREFLIGHT_JOBS[j]

async def _run_preflight_pipeline(job_id: str, token: str, mode: str,
                                  safe_name: str,
                                  bed_mesh: bool = False,
                                  camera: bool = False,
                                  remap_override: dict | None = None,
                                  head_assignment: dict | None = None,
                                  head_plan: str = "loadout",
                                  flow_cal: bool = False,
                                  head_copies: dict | None = None) -> None:
    state = _PREFLIGHT_JOBS[job_id]
    pp = _load_post_processor()
    src = _PREFLIGHT_DIR / (token + ".gcode")

    tmp_a = _PREFLIGHT_DIR / (job_id + ".a.gcode")
    tmp_b = _PREFLIGHT_DIR / (job_id + ".b.gcode")
    state["tmp_a"] = str(tmp_a)
    state["tmp_b"] = str(tmp_b)

    try:

        _set_stage(state, "analyze", 0.0)

        with open(src, "r", encoding="utf-8", errors="replace") as f:
            slicer_colors, slicer_types, num_aces, _used, _plan, meta = \
                preflight_core.parse_meta(pp, f)

        live_slots = await _live_slots_async()
        if mode == "head":
            head_ctx = await _head_mode_context()
            head_ctx["mode"] = "head"
        else:
            head_ctx = {"mode": "multi"}
        # Pickup-Cleaning is a global setting (both modes): the rewrite stamps
        # ACE_PICKUP_CLEAN after bare-T picks with no cleaning move only when it
        # is on (the runtime command also no-ops unless enabled). Read from the
        # live state that _parse_state surfaces.
        if "pickup_cleaning" not in head_ctx:
            try:
                head_ctx["pickup_cleaning"] = bool(_parse_state(
                    await _query_state_gated()).get("pickup_cleaning"))
            except Exception:
                head_ctx["pickup_cleaning"] = False

        # The rewrite pipeline (apply_remap -> rewrite -> inject_auto_load, the
        # head-mode variants and the slicer/optimize/layer dispatch) lives in
        # preflight_core, shared verbatim with the in-browser Pyodide path. Run
        # it off the event loop; it ping-pongs tmp_a/tmp_b and returns the final
        # path (the material-availability check + infeasible-plan raises happen
        # inside, surfacing as RuntimeError just like before).
        final = await asyncio.to_thread(
            preflight_core.rewrite_pipeline, pp,
            src_path=str(src), tmp_a=str(tmp_a), tmp_b=str(tmp_b),
            slicer_colors=slicer_colors, slicer_types=slicer_types,
            num_aces=num_aces, live_slots=live_slots, head_ctx=head_ctx,
            mode=mode, remap_override=remap_override,
            head_assignment=head_assignment, head_plan=head_plan,
            fuzzy=_PREFLIGHT_FUZZY, meta=meta, head_copies=head_copies,
            set_stage=lambda s, p: _set_stage(state, s, p),
            stage_cb=lambda base, span: _stage_progress(state, base, span))
        cur = Path(final)
        nxt = tmp_b if cur == tmp_a else tmp_a

        if bed_mesh or camera or flow_cal:
            _set_stage(state, "print_prefs", 84.0)
            await asyncio.to_thread(
                _prepend_print_prefs, str(cur), str(nxt), bed_mesh, camera,
                flow_cal)
            cur, nxt = nxt, cur

        _set_stage(state, "upload", 85.0)
        with open(cur, "rb") as fh:
            files = {"file": (safe_name, fh, "application/octet-stream")}
            payload = {"root": "gcodes", "print": "true"}
            try:
                async with httpx.AsyncClient(timeout=600.0) as client:
                    r = await client.post(
                        f"{MOONRAKER_URL}/server/files/upload",
                        data=payload, files=files)
                    r.raise_for_status()
                    state["moonraker"] = r.json()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"moonraker {e.response.status_code}: "
                                   f"{e.response.text}")
            except httpx.HTTPError as e:
                raise RuntimeError(f"moonraker: {e}")

        _set_stage(state, "done", 100.0)
        state["filename"] = safe_name
        state["mode"]     = mode
        state["done"]     = True
    except Exception as exc:
        state["error"] = str(exc)
        state["done"]  = True
        state["ts"]    = time.time()
    finally:

        for p in (tmp_a, tmp_b):
            try: p.unlink()
            except Exception: pass

class _PreflightPrint(BaseModel):
    token: str
    mode:  str
    # Preflight print-preference toggles (default off). Inject a
    # SET_PRINT_PREFERENCES line before PRINT_START so an upload/SD start can
    # run bed mesh / timelapse camera (which stock only does on the official
    # start). Flow-calibrate/PA are intentionally not exposed here.
    bed_mesh: bool = False
    camera:   bool = False
    flow_cal: bool = False
    # Optional explicit slicer-T -> synthetic-T remap (ace*4+slot), set by the
    # web when the user manually reassigns slots in the slicer plan. When given
    # for mode=="slicer" the pipeline uses it verbatim instead of re-running the
    # auto colour matcher, so the print matches exactly what the preview showed.
    # Ignored for optimize/layer (those stay auto-optimized).
    remap: dict[str, int] | None = None
    # Head-mode only: explicit slicer-T -> target-id assignment ("feeder-N" /
    # "slot-A-S"), set by the head-mode table when the user reassigns a colour.
    # When given (mode=="head") the pipeline uses it verbatim instead of the
    # auto head-mode layout, so the print matches the preview.
    head_assignment: dict[str, str] | None = None
    # Head-mode plan selector (mode=="head"): which of the three head plans to
    # print - "loadout" (as loaded, uses head_assignment), "optimize" or "layer"
    # (proposed loadout, recomputed server-side; head_assignment ignored).
    head_plan: str = "loadout"
    # Head-mode loadout only: the colour copies the preview used, as
    # {slicer-T: [target_id, ...]} - sent verbatim so the print's per-event
    # head choice matches the previewed swap count. None (older frontend)
    # -> the pipeline detects them from the loaded slots itself.
    head_copies: dict[str, list[str]] | None = None

@app.post("/api/preflight/print")
async def preflight_print(req: _PreflightPrint) -> dict:
    if req.mode not in ("slicer", "optimize", "layer", "head"):
        raise HTTPException(status_code=400, detail="invalid mode")
    if not re.fullmatch(r"[0-9a-f]{32}", req.token or ""):
        raise HTTPException(status_code=400, detail="invalid token")
    gpath = _PREFLIGHT_DIR / (req.token + ".gcode")
    npath = _PREFLIGHT_DIR / (req.token + ".name")
    if not gpath.is_file():
        raise HTTPException(status_code=404,
                            detail="preflight token expired or unknown")
    safe_name = (npath.read_text(encoding="utf-8").strip()
                 if npath.is_file() else (req.token + ".gcode"))

    _prune_old_jobs()
    import uuid as _uuid
    job_id = _uuid.uuid4().hex
    _PREFLIGHT_JOBS[job_id] = {
        "stage":    "queued",
        "percent":  0.0,
        "done":     False,
        "error":    None,
        "filename": safe_name,
        "mode":     req.mode,
        "ts":       time.time(),
    }
    head_plan = req.head_plan if req.head_plan in (
        "loadout", "optimize", "layer") else "loadout"
    asyncio.create_task(_run_preflight_pipeline(
        job_id, req.token, req.mode, safe_name, req.bed_mesh, req.camera,
        req.remap, req.head_assignment, head_plan, req.flow_cal,
        req.head_copies))
    return {"job_id": job_id, "filename": safe_name, "mode": req.mode}

@app.get("/api/preflight/print/status")
async def preflight_print_status(job_id: str) -> dict:
    state = _PREFLIGHT_JOBS.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")

    return {
        "job_id":  job_id,
        "stage":   state.get("stage"),
        "percent": round(state.get("percent", 0.0), 1),
        "done":    bool(state.get("done")),
        "error":   state.get("error"),
        "filename": state.get("filename"),
        "mode":    state.get("mode"),
    }

@app.get("/api/preflight/pysrc")
async def preflight_pysrc() -> dict:
    """The two Python sources the in-browser Pyodide worker runs: the
    unmodified post-processor + preflight_core. Served so the browser executes
    the SAME code as the backend (one source of truth, no JS re-port/drift)."""
    candidates = [
        Path(p) for p in _user_paths(
            "printer_data/config/tools/post_process_virtual_toolheads.py")
    ] + [
        Path(__file__).resolve().parent.parent.parent / "tools" / "post_process_virtual_toolheads.py",
    ]
    pp_src = next((p for p in candidates if p.is_file()), None)
    if pp_src is None:
        raise HTTPException(status_code=503,
                            detail="post-processor script not installed")
    core_src = Path(__file__).resolve().parent / "preflight_core.py"
    if not core_src.is_file():
        raise HTTPException(status_code=503,
                            detail="preflight_core not installed")
    try:
        return {
            "postprocess": pp_src.read_text(encoding="utf-8"),
            "core":        core_src.read_text(encoding="utf-8"),
        }
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"cannot read sources: {exc}")

@app.get("/api/preflight/livedata")
async def preflight_livedata() -> dict:
    """Live ACE/slot identities + head-mode context for the in-browser
    preflight, in the exact shape preflight_core.build_report expects. Keeps the
    slot filtering (rfid/override only) and head-mode resolution single-source on
    the backend - the browser never re-derives it."""
    live_slots = await _live_slots_async()
    head_ctx = await _head_mode_context()
    return {
        "live_slots": live_slots,
        "head_ctx":   head_ctx,
    }

_cfg_scalar_cache: dict = {"mtime": 0.0, "values": {}}

def _read_cfg_scalars() -> dict:
    try:
        st = Path(_resolve_cfg_path()).stat()
    except OSError:
        return _cfg_scalar_cache["values"]
    if st.st_mtime == _cfg_scalar_cache["mtime"]:
        return _cfg_scalar_cache["values"]
    try:
        text = Path(_resolve_cfg_path()).read_text(encoding="utf-8")
        main, _per_ace = _extract_params(text)
    except Exception:
        return _cfg_scalar_cache["values"]
    _cfg_scalar_cache["mtime"] = st.st_mtime
    _cfg_scalar_cache["values"] = main
    return main

def _read_display_index_base() -> int:
    """ace.cfg is the source of truth, with the env-var (passed by the
    Klipper-side spawn) as a fallback for setups where multiace-web
    was started by /etc/init.d/S98multiace-web (which doesn't forward
    the cfg value) instead of by ace.py's _spawn_multiace_web."""
    scalars = _read_cfg_scalars()
    raw = scalars.get("display_index_base")
    if raw is None:
        raw = os.environ.get("MULTIACE_DISPLAY_INDEX_BASE", "0")
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return 0 if v < 0 else (1 if v > 1 else v)

def _read_update_cfg() -> dict[str, str]:
    """Pull update_repo, update_prerelease and update_url_base from
    ace.cfg so the Web backend uses the same source as the gcode
    ACE_UPDATE_* commands. Falls back to defaults if the cfg isn't
    parseable or keys are missing."""
    repo = "decay71/multiACE"
    prerelease = "0"
    url_base = ""
    try:
        text = Path(_resolve_cfg_path()).read_text(encoding="utf-8")
        main, _per_ace = _extract_params(text)
        if "update_repo" in main and main["update_repo"]:
            repo = main["update_repo"]
        v = main.get("update_prerelease", "").strip().lower()
        if v in ("true", "1", "yes", "on"):
            prerelease = "1"
        if "update_url_base" in main and main["update_url_base"]:
            url_base = main["update_url_base"].strip()
    except Exception:
        pass
    return {
        "MULTIACE_UPDATE_REPO":      repo,
        "MULTIACE_UPDATE_PRERELEASE": prerelease,
        "MULTIACE_UPDATE_URL_BASE":  url_base,
    }

async def _run_update_script(args: list[str], timeout: float) -> dict:
    """Exec the bundled multiace_update.sh and capture stdout+rc."""

    # Canonical install location first. The PAXX-baked
    # /home/lava/multiace/tools/multiace_update.sh comes from the
    # squashfs and never gets refreshed by online updates, so it
    # serves only as a last-resort fallback.
    # The two U1 entries keep their exact order; the home-relative
    # pair is appended for a generic Klipper host and can never reorder them.
    update_script = None
    for candidate in (
        ["/home/lava/multiace_update.sh",
         "/home/lava/multiace/tools/multiace_update.sh"]
        + _user_paths("multiace_update.sh")[1:]
        + _user_paths("multiace/tools/multiace_update.sh")[1:]
    ):
        if Path(candidate).is_file():
            update_script = candidate
            break
    if update_script is None:
        raise HTTPException(
            status_code=503,
            detail=("Updater script not found at "
                    "/home/lava/multiace/tools/multiace_update.sh "
                    "or /home/lava/multiace_update.sh. "
                    "Re-run install_multiace.sh from the repo to ship it."))
    env = os.environ.copy()
    env.update(_read_update_cfg())
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", update_script, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(),
                                               timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504,
                                detail=f"Updater timed out after {timeout}s")
    except FileNotFoundError:
        raise HTTPException(status_code=500,
                            detail="bash not on PATH on this host")
    out = (stdout or b"").decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stdout": out,

        "status_lines": [
            line.split("STATUS:", 1)[1].strip()
            for line in out.splitlines() if "STATUS:" in line
        ],
    }

@app.post("/api/preflight/inbox")
async def preflight_inbox_put(file: UploadFile = File(...)) -> dict:
    """Store-only drop point for "Send to multiACE" (see _INBOX_DIR notes).
    Validates like /api/preflight but never analyses - the browser runs the
    normal Pyodide preflight on pickup."""
    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not safe_name.lower().endswith((".gcode", ".gco", ".g")):
        raise HTTPException(status_code=400, detail="not a g-code file")
    _INBOX_DIR.mkdir(parents=True, exist_ok=True)
    gpath, npath = _inbox_paths()
    tmp = _INBOX_DIR / "incoming.tmp"
    limit = _inbox_max_size()
    size = 0
    first = b""
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                if not first:
                    first = chunk
                    for marker in _INBOX_PROCESSED_MARKERS:
                        if marker in first:
                            raise HTTPException(
                                status_code=409,
                                detail=("this file is already multiACE-"
                                        "processed - send the ORIGINAL "
                                        "slicer export, never a processed "
                                        "one (double-processing corrupts "
                                        "the swaps)"))
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"file too large for the inbox "
                                f"(> {limit//1024//1024} MB; raise via "
                                f"[ace] inbox_max_mb in ace.cfg)"))
                fh.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="empty file")
        os.replace(tmp, gpath)
        npath.write_text(safe_name, encoding="utf-8")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    logging.info("[inbox] stored %s (%d bytes)", safe_name, size)
    return {"ok": True, "name": safe_name, "size": size}

@app.get("/api/preflight/inbox")
async def preflight_inbox_status() -> dict:
    return _inbox_status()

@app.get("/api/preflight/inbox/file")
async def preflight_inbox_file() -> Response:
    # FileResponse streams from disk - a 100+ MB gcode must never sit in the
    # U1's RAM (memavail ~600 MB) just to hand it to the browser.
    gpath, _ = _inbox_paths()
    st = _inbox_status()
    if not st["pending"]:
        raise HTTPException(status_code=404, detail="inbox empty")
    return FileResponse(gpath, media_type="text/plain; charset=utf-8",
                        filename=st["name"])

@app.delete("/api/preflight/inbox")
async def preflight_inbox_clear() -> dict:
    _inbox_clear()
    return {"ok": True}

@app.get("/api/update/check")
async def update_check() -> dict:
    return await _run_update_script(["check"], timeout=30.0)

@app.post("/api/update/apply")
async def update_apply(force: bool = False) -> dict:

    if not _DEBUG_FLAG_PATH.exists():
        raise HTTPException(
            status_code=409,
            detail=("Persistent updates disabled. Enable debug mode "
                    "(touch /oem/.debug) and reboot before applying "
                    "updates, otherwise the install is wiped on next "
                    "boot."))
    args = ["apply"]
    if force:
        args.append("--force")
    return await _run_update_script(args, timeout=600.0)

_DEBUG_FLAG_PATH = Path("/oem/.debug")

async def _sudo_run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timeout"
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "sudo not on PATH"

@app.get("/api/debug-mode")
async def debug_mode_get() -> dict:
    return {"enabled": _DEBUG_FLAG_PATH.exists()}

async def _set_debug_flag(enable: bool) -> str:
    """Create or remove /oem/.debug. The service normally runs as root
    (S98), so the direct file operation is the primary path; sudo with
    the multiace-debug sudoers rule is the fallback for a service that
    runs as lava."""
    try:
        if enable:
            _DEBUG_FLAG_PATH.touch()
        else:
            _DEBUG_FLAG_PATH.unlink()
        return "direct"
    except FileNotFoundError:
        return "already removed"
    except PermissionError:
        pass
    argv = (["/usr/bin/touch", str(_DEBUG_FLAG_PATH)] if enable
            else ["/bin/rm", "-f", str(_DEBUG_FLAG_PATH)])
    rc, out = await _sudo_run(argv)
    if rc != 0:
        raise HTTPException(
            status_code=500,
            detail=(f"{'touch' if enable else 'rm'} /oem/.debug failed: no "
                    f"write access as this user and sudo failed (rc={rc}): "
                    f"{out.strip()}. Either run the web service as root "
                    "(S98 default) or install the sudoers drop-in "
                    "/etc/sudoers.d/multiace-debug via install_multiace.sh."))
    return out

@app.post("/api/debug-mode/enable")
async def debug_mode_enable() -> dict:
    out = await _set_debug_flag(True)
    return {"enabled": _DEBUG_FLAG_PATH.exists(), "stdout": out}

@app.post("/api/debug-mode/disable")
async def debug_mode_disable() -> dict:
    if not _DEBUG_FLAG_PATH.exists():
        return {"enabled": False, "stdout": "already disabled"}
    out = await _set_debug_flag(False)
    return {"enabled": _DEBUG_FLAG_PATH.exists(), "stdout": out}

@app.post("/api/reboot")
async def reboot() -> dict:

    try:
        result = await _mr_post("/machine/reboot", timeout=10.0)
        return {"ok": True, "moonraker": result}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail=f"moonraker reboot failed: {e}")

FLUIDD_CAMERA_NAME = "multiACE"
# Mirrors what Fluidd itself writes for an "HTTP Page" camera (read back
# from a hand-made entry): service 'iframe', location 'printer'. 'uid' and
# 'source' are server-managed and must NOT be sent. The URL stays RELATIVE
# so it is the same origin as Fluidd and goes through our nginx block,
# inheriting the Moonraker-managed auth - an absolute http:// one breaks
# as soon as the user reaches Fluidd over https. 16:9 is Fluidd's own
# default and what the other cameras in the row use, so the card lines up
# with them; the panel handles the reduced height by itself (the loadout
# row steps aside below 200px, the cards below that). Only ever applied to
# a NEW entry - an existing camera keeps whatever the user set.
FLUIDD_CAMERA = {
    "name": FLUIDD_CAMERA_NAME,
    "location": "printer",
    "service": "iframe",
    "stream_url": "/multiace/?panel=1",
    "snapshot_url": "",
    "aspect_ratio": "16:9",
    "target_fps": 15,
    "target_fps_idle": 5,
    "enabled": True,
    "icon": "mdiWebcam",
}

@app.post("/api/fluidd-camera")
async def fluidd_camera() -> dict:
    """Register the panel as a camera in Fluidd, via Moonraker.

    Fluidd keeps cameras in Moonraker's database, so this is a plain
    POST - no config file to edit and nothing to restart. Deliberately a
    button rather than something the installer does: it changes the
    user's own Fluidd dashboard, and nobody should find a camera there
    they did not ask for.

    Idempotent: an existing entry of the same name is reported and left
    alone, so a second click cannot overwrite a URL or aspect ratio the
    user has adjusted by hand.
    """
    try:
        listing = await _mr_get("/server/webcams/list")
        cams = (listing.get("result") or {}).get("webcams") or []
        for cam in cams:
            if str(cam.get("name", "")).strip().lower() \
                    == FLUIDD_CAMERA_NAME.lower():
                return {"ok": True, "existed": True,
                        "stream_url": cam.get("stream_url", "")}
        result = await _mr_post("/server/webcams/item", FLUIDD_CAMERA,
                                timeout=10.0)
        return {"ok": True, "existed": False, "moonraker": result}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                            detail=f"moonraker: {e}")

@app.post("/api/upload-and-print")
async def upload_and_print(file: UploadFile = File(...)) -> dict:

    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or safe_name in (".", "..") or "/" in safe_name or "\\" in safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not safe_name.lower().endswith((".gcode", ".gco", ".g")):
        raise HTTPException(status_code=400, detail="not a g-code file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    files = {"file": (safe_name, data, file.content_type or "application/octet-stream")}
    payload = {"root": "gcodes", "print": "true"}
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(f"{MOONRAKER_URL}/server/files/upload",
                                  data=payload, files=files)
            r.raise_for_status()
            return {"ok": True, "filename": safe_name, "moonraker": r.json()}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")

@app.get("/api/state")
async def get_state() -> dict:
    """Aggregated dashboard state (ACEs + toolheads + dryer + status)."""
    try:
        status = await _query_state_gated()
    except httpx.HTTPStatusError as e:
        # 503 "Klippy Host not connected" = Klipper is down (firmware_restart
        # or a full reboot in progress, or a Klipper error). Moonraker itself
        # is still up. Surface a clean transient marker instead of the raw
        # httpx error so the UI shows a "reconnecting" banner and keeps the
        # last dashboard; it recovers on the next poll once Klipper is back.
        if e.response is not None and e.response.status_code == 503:
            return {"klippy": "disconnected"}
        return {"error": f"moonraker: {e}"}
    except httpx.HTTPError as e:
        return {"error": f"moonraker: {e}"}
    return _parse_state(status)

@app.get("/api/aces")
async def list_aces() -> dict:
    """Backwards-compatible subset of /api/state - only the per-ACE list."""
    try:
        status = await _query_state_gated()
    except httpx.HTTPError as e:
        return {"aces": [], "error": f"moonraker: {e}"}
    parsed = _parse_state(status)
    return {"aces": parsed["aces"], "active_device": parsed["active_device"]}

@app.get("/api/debug")
async def get_debug() -> dict:
    """Raw moonraker dump - useful for inspecting unknown fields."""
    try:
        return await _query_state_gated()
    except httpx.HTTPError as e:
        return {"error": f"moonraker: {e}"}

_MACRO_PREFIX = "gcode_macro "
_MACRO_BUCKETS = (
    ("switch", lambda m: m.startswith("ACEA__Switch")),
    ("load",   lambda m: m.startswith("ACEB__Load") or m.startswith("ACEC__Load")),
    ("unload", lambda m: m.startswith("ACEC__Unload")),
    ("dry",    lambda m: m.startswith("ACED__Dry")),
    ("mode",   lambda m: m.startswith("ACEF__Mode") or m == "SET_ACE_MODE"),
    ("status", lambda m: m.startswith("ACEG__")),
)

@app.get("/api/macros")
async def list_macros() -> dict:
    """
    Auto-discover ACE-related gcode_macro objects from Moonraker and
    bucket them into categories that the frontend can render as button
    groups. Source of truth = whatever ace.cfg / printer.cfg defines.
    """
    try:
        data = await _mr_get("/printer/objects/list")
    except httpx.HTTPError as e:
        return {"all": [], "categorized": {}, "error": f"moonraker: {e}"}
    objs = data.get("result", {}).get("objects", []) or []
    macros = sorted(
        o[len(_MACRO_PREFIX):]
        for o in objs
        if isinstance(o, str) and o.startswith(_MACRO_PREFIX)
        and ("ACE" in o or o.endswith(" SET_ACE_MODE"))
    )
    cats: dict[str, list[str]] = {name: [] for name, _ in _MACRO_BUCKETS}
    cats["other"] = []
    for m in macros:
        for name, pred in _MACRO_BUCKETS:
            if pred(m):
                cats[name].append(m)
                break
        else:
            cats["other"].append(m)
    return {"all": macros, "categorized": cats}

def _gcode_kv(key: str, value) -> str:
    """One KEY=VALUE token for a gcode command line. A value with spaces
    ('Matte Black') breaks Klipper's parser as bare KEY=Matte Black -> the
    Snapmaker fork's _get_extended_params supports KEY="value with spaces"
    on every firmware tree (1.4.1..1.5.2, verified), so quote when needed.
    First strip the chars that break BEFORE quoting can help: #*; abort the
    extended_r arg match entirely (they never reach the quote handler), and
    a "/' inside would close the quote early. None of these belong in a
    filament label/vendor; # never survives to the table anyway."""
    s = str(value)
    for bad in ('#', '*', ';', '"', "'"):
        s = s.replace(bad, '')
    if ' ' in s or '\t' in s:
        return f'{key}="{s}"'
    return f'{key}={s}'


@app.post("/api/macro-batch", status_code=202)
async def run_macro_batch(req: MacroBatchRequest) -> dict:

    if not req.commands:
        raise HTTPException(status_code=400, detail="no commands")
    lines = []
    for c in req.commands:
        parts = [c.name]
        if c.args:
            for k, v in c.args.items():
                parts.append(_gcode_kv(k, v))
        lines.append(" ".join(parts))
    script = "\n".join(lines)

    async def _dispatch():
        try:
            await _mr_post("/printer/gcode/script", {"script": script},
                           timeout=None)
        except Exception as e:
            _trace.warning("macro-batch dispatch failed: %s", e)

    asyncio.create_task(_dispatch())
    _trace.info("macro-batch: dispatched %d commands to Moonraker", len(lines))
    return {"ok": True, "count": len(lines), "script_lines": lines}

SPOOL_DB_PATH = os.environ.get(
    "MULTIACE_SPOOL_DB",
    os.path.join(_CFG_DIR, "persistent", "multiace_spools.json"))

@app.get("/api/spools/export")
async def export_spools() -> Response:
    """Download the spool table as JSON (the off-printer backup). Read-only
    - Klipper stays the only writer of this file."""
    p = Path(SPOOL_DB_PATH)
    if not p.exists():
        raise HTTPException(404, "no spool table yet")
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="application/json",
        headers={"Content-Disposition":
                 'attachment; filename="multiace_spools.json"'})

@app.post("/api/spools/import")
async def import_spools(file: UploadFile = File(...),
                        mode: str = "merge") -> dict:
    """Restore/merge a table from an uploaded JSON. We do NOT write the
    table file: the upload lands in a temp file and Klipper imports it via
    ACE_SPOOL_IMPORT, so there is exactly one writer (the config
    lost-update lesson)."""
    if mode not in ("merge", "replace"):
        raise HTTPException(400, "mode must be merge or replace")
    data = await file.read()
    if len(data) > 4 * 1024 * 1024:
        raise HTTPException(413, "spool table too large")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"not valid JSON: {e}")
    if not isinstance(parsed.get("spools"), dict):
        raise HTTPException(400, 'no "spools" object in the file')
    tmp = Path("/tmp/multiace_spools_import.json")
    tmp.write_text(json.dumps(parsed), encoding="utf-8")
    return await _mr_post(
        "/printer/gcode/script",
        {"script": f"ACE_SPOOL_IMPORT PATH={tmp} MODE={mode}"})

# ---------------------------------------------------------------- Spoolman
# The printer owns the setting (ace.spoolman_url / _auto), this side owns the
# HTTP: Klipper must never block on a network call. Only spools carrying a
# spoolman_id take part - anything added locally stays local, because creating
# a spool in Spoolman would mean guessing a filament (and vendor) record in a
# collection the user curates.
_spoolman_lock = asyncio.Lock()
_spoolman_last: dict = {"ts": 0.0, "ok": None, "msg": "", "pulled": 0, "pushed": 0}
# 2.405 mm2 (1.75mm filament) - only used to turn our mm into a gram estimate
# for the log line; the push itself sends LENGTH, so Spoolman applies its own
# density and diameter and our estimate never reaches the database.
_SPOOLMAN_TIMEOUT = 15.0
# Moonraker's /printer/gcode/script BLOCKS until the script has RUN, so our
# ACE_SPOOL_* writes queue behind whatever the printer is doing - a load is
# ~90 s, a full swap ~150 s. The 30 s _mr_post default therefore expires on a
# busy printer and the write never happens (a tag adopt stays unassigned; on
# the SYNCED_MM writeback the timeout would push a delta to Spoolman and then
# fail to record it, so the next push would send it a second time).
# These are queued writes, not interactive calls - waiting out a swap is the
# correct behaviour.
_MR_SPOOL_GCODE_TIMEOUT = 180.0

def _known_subtypes_for(material: str) -> list:
    """Sub-types the firmware DB knows for this material, longest first (so
    TPU's '95A HF' wins over a shorter overlap). Case-insensitive material
    lookup - Spoolman spells it however the user typed it."""
    db = _load_filament_db()
    if not db or not material:
        return []
    want = material.strip().lower()
    entry = None
    for k, v in db.items():
        if str(k).strip().lower() == want:
            entry = v
            break
    if not isinstance(entry, dict):
        return []
    subs = set()
    for vendor_subs in entry.values():
        if isinstance(vendor_subs, (list, tuple)):
            subs.update(str(s).strip() for s in vendor_subs if str(s).strip())
    return sorted(subs, key=len, reverse=True)

def _spoolman_subtype_guess(name: str, material: str) -> str:
    """Spoolman has no sub-type field, so it has to be read out of the
    filament NAME. A wrong guess is corrected in one click; an empty
    sub-type silently means 'Basic', which is the WRONG tip-form table and
    unload temperature for e.g. Matte - so guessing matters.

    Known sub-types are matched ANYWHERE in the name, as whole words. The
    original rule (take whatever follows the material token) assumed names
    like 'PLA Matte', but material is a SEPARATE FIELD in Spoolman, so the
    name usually does not repeat it: 'Matte Black' returned nothing at all,
    and
    'PLA Matte Schwarz' dragged the colour into the sub-type. The DB list
    is short and specific (PLA: Matte/Silk/SnapSpeed/Wood, PETG: HF,
    TPU: '95A HF'), which is what makes a free scan safe here.

    The old rule survives as the fallback so a sub-type the firmware does
    not know ('PLA Glow') is still picked up - known list first, because it
    returns the DB's canonical spelling and stops at the sub-type instead of
    swallowing the colour behind it."""
    n = (name or "").strip()
    m = (material or "").strip()
    if not n:
        return ""
    for sub in _known_subtypes_for(m):
        # Whole word/phrase only: a substring hit would read 'Silk' out of
        # a name like 'Silkyway' and 'HF' out of any word containing it.
        if re.search(r"(?<!\w)%s(?!\w)" % re.escape(sub), n, re.IGNORECASE):
            return sub
    if not m:
        return ""
    low, mlow = n.lower(), m.lower()
    if mlow not in low:
        return ""
    rest = n[low.index(mlow) + len(m):].strip(" -_/")
    return rest[:32]

def _spool_card_uids(sp: dict) -> list:
    """Entries of the spool's `card_uids` extra field, uppercased - the
    field SpoolLink maintains (comma-separated UID hex, JSON-string-
    encoded). Mirrors paxx's own _parse_card_uids so both sides read the
    field identically."""
    raw = str((sp.get("extra") or {}).get("card_uids") or "").strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return [_card_canon(u) for u in raw.split(",") if u.strip()]


def _card_canon(s: str) -> str:
    """Canonical form for card_uids comparison: uppercase, ':' and spaces
    stripped. SpoolLink itself writes bare uppercase hex (no colons), but a
    hand-entered '04:A3:...' must still match the same chip."""
    return str(s or "").replace(":", "").replace(" ", "").upper()


def _spool_local_uids(row: dict) -> list:
    """Card UIDs a LOCAL row carries in its code list (sku = 'SM123,04BF..'
    since the hand assignment learns the slot's read UID): every comma-separated code that is plain hex of UID
    length (4- or 7-byte chips) and not the SM<id>/bare-id link. These
    are what Spoolman's card_uids must know as well."""
    out = []
    for code in str((row or {}).get("sku") or "").split(","):
        c = _card_canon(code.strip().lstrip("#"))
        if not c or c.startswith("SM") or c.isdigit():
            continue
        if len(c) not in (8, 14, 20):
            continue
        if any(ch not in "0123456789ABCDEF" for ch in c):
            continue
        if c not in out:
            out.append(c)
    return out


_SM_CARD_FIELD = "card_uids"
_sm_card_field_state = {"base": "", "ok": False}


async def _spoolman_ensure_card_field(client, base: str) -> bool:
    """card_uids is SpoolLink's field; on an instance without the mod the
    extra key does not exist and a PATCH is refused - create it once
    (text), like the PA field."""
    if _sm_card_field_state["ok"] and _sm_card_field_state["base"] == base:
        return True
    try:
        r = await client.get(f"{base}/api/v1/field/spool")
        r.raise_for_status()
        keys = {str(f.get("key")) for f in (r.json() or [])
                if isinstance(f, dict)}
        if _SM_CARD_FIELD not in keys:
            r2 = await client.post(
                f"{base}/api/v1/field/spool/{_SM_CARD_FIELD}",
                json={"name": "Card UIDs", "field_type": "text"})
            r2.raise_for_status()
            _trace.info("spoolman: created extra field %s", _SM_CARD_FIELD)
        _sm_card_field_state.update(base=base, ok=True)
        return True
    except httpx.HTTPError as e:
        _trace.info("spoolman: card_uids field ensure failed: %s",
                    str(e) or type(e).__name__)
        return False


async def _spoolman_card_uids_push_one(client, base: str, smid: str,
                                       sp: dict, uids: list) -> bool:
    """Add the given card UIDs to one SM spool's card_uids extra (merge,
    never drop what is there - SpoolLink's own entries stay). Sends the
    full extra dict, same reasoning as the PA push. The other direction
    (adopt by card_uids) is the tag sweep."""
    have = _spool_card_uids(sp)
    missing = [u for u in uids if u not in have]
    if not missing:
        return True
    if not await _spoolman_ensure_card_field(client, base):
        return False
    extra = dict(sp.get("extra") or {})
    extra[_SM_CARD_FIELD] = json.dumps(",".join(have + missing))
    try:
        r = await client.patch(f"{base}/api/v1/spool/{smid}",
                               json={"extra": extra})
        r.raise_for_status()
        sp["extra"] = extra
        _trace.info("spoolman: card_uids += %s on SM%s",
                    ",".join(missing), smid)
        return True
    except httpx.HTTPError as e:
        _trace.info("spoolman: card_uids push SM%s failed: %s", smid,
                    str(e) or type(e).__name__)
        return False


# --- Spoolman PA sync ------------------------------------------------------
# The spool's pressure-advance matrix travels in Spoolman's
# extra.pressure_advance_matrix text field - the SAME field and keys the
# pechex/SpoolLink mod uses, so a spool moving between the two worlds keeps
# its calibration (mod contract, see ace.py's pa_matrix block; do not rename
# either). Direction rules: LOCAL LEADS (this printer is the measuring
# instrument) - a differing local matrix is PATCHed to Spoolman on every
# sync moment (_spoolman_pa_maybe_push, piggybacked on the consumption
# push: periodic tick, pause, idle, print-end, manual); Spoolman only ever
# seeds a row that has NO local matrix yet (adopt / first refresh, in
# _spoolman_to_local). Local deletions propagate (an emptied matrix clears
# the field). In SpoolLink mode the push never runs - the SL branch returns
# before pushing, and the mod owns the field there.
_SM_PA_FIELD = "pressure_advance_matrix"
_sm_pa_field_state: dict = {"base": "", "ok": False}


def _pa_norm(m) -> dict:
    """{str key: round(float, 6)} with unparseable entries dropped - the
    comparable form of a PA matrix (matches ace.py's round(...,6) store)."""
    out = {}
    if isinstance(m, dict):
        for k, v in m.items():
            try:
                out[str(k)] = round(float(v), 6)
            except (TypeError, ValueError):
                continue
    return out


def _spoolman_pa_extra(sp: dict) -> dict | None:
    """The SM spool's PA matrix, tolerantly decoded. Spoolman text extra
    fields hold JSON-string-encoded values (see _spool_card_uids), so the
    raw value is usually '"{\\"0.4_standard\\": 0.222}"' - one loads yields
    the inner JSON string, a second the dict. Accepts a bare dict / bare
    JSON object too. None = no usable data."""
    raw = (sp.get("extra") or {}).get(_SM_PA_FIELD)
    if raw is None:
        return None
    if isinstance(raw, dict):
        return _pa_norm(raw) or None
    try:
        v = json.loads(str(raw))
        if isinstance(v, str):
            v = json.loads(v) if v.strip() else None
        return _pa_norm(v) or None
    except (ValueError, TypeError):
        return None


async def _spoolman_ensure_pa_field(client, base: str) -> bool:
    """Create the extra-field definition once if the instance lacks it
    (a PATCH with an undefined extra key is refused). Cached per base for
    the process lifetime; the mod may have created it already."""
    if _sm_pa_field_state["ok"] and _sm_pa_field_state["base"] == base:
        return True
    try:
        r = await client.get(f"{base}/api/v1/field/spool")
        r.raise_for_status()
        keys = {str(f.get("key")) for f in (r.json() or [])
                if isinstance(f, dict)}
        if _SM_PA_FIELD not in keys:
            r2 = await client.post(
                f"{base}/api/v1/field/spool/{_SM_PA_FIELD}",
                json={"name": "Pressure Advance Matrix",
                      "field_type": "text"})
            r2.raise_for_status()
            _trace.info("spoolman: created extra field %s", _SM_PA_FIELD)
        _sm_pa_field_state.update(base=base, ok=True)
        return True
    except httpx.HTTPError as e:
        _trace.info("spoolman: pa field ensure failed: %s",
                    str(e) or type(e).__name__)
        return False


async def _spoolman_pa_push_one(client, base: str, smid: str, sp: dict,
                                matrix: dict) -> bool:
    """PATCH one SM spool's PA field to the local matrix. Sends the FULL
    existing extra dict with only our key changed - whether Spoolman's
    PATCH replaces or merges the extra object, card_uids etc. survive.
    An empty matrix writes an empty text value (clears under either
    semantics)."""
    if not await _spoolman_ensure_pa_field(client, base):
        return False
    extra = dict(sp.get("extra") or {})
    norm = _pa_norm(matrix)
    extra[_SM_PA_FIELD] = json.dumps(
        json.dumps(norm, sort_keys=True) if norm else "")
    try:
        r = await client.patch(f"{base}/api/v1/spool/{smid}",
                               json={"extra": extra})
        r.raise_for_status()
        _trace.info("spoolman: pa pushed to SM%s (%d entr%s)", smid,
                    len(norm), "y" if len(norm) == 1 else "ies")
        return True
    except httpx.HTTPError as e:
        _trace.info("spoolman: pa push SM%s failed: %s", smid,
                    str(e) or type(e).__name__)
        return False


def _spoolman_to_local(sp: dict, existing: dict | None,
                       tag_sku: str | None = None) -> dict:
    """One Spoolman spool -> our record. Spoolman leads for everything it
    knows (the user chose it as the source of truth); we keep what only we
    have: sku, sub-type, and the local id/binding (handled by the merge
    import, which matches on spoolman_id)."""
    fil = sp.get("filament") or {}
    ven = fil.get("vendor") or {}
    color = (fil.get("color_hex") or "")
    if not color:
        multi = fil.get("multi_color_hexes") or ""
        color = str(multi).split(",")[0] if multi else ""
    out = {
        "spoolman_id": str(sp.get("id", "")),
        "material": (fil.get("material") or "").strip(),
        "vendor": (ven.get("name") or "").strip(),
        "color": color.lstrip("#").upper()[:6],
        "label": (fil.get("name") or "").strip(),
        # Spoolman leads: its remaining weight is usually a scale reading,
        # ours an estimate from extruded length. Taking it means the local
        # consumption counters must restart from here, or the next push
        # would report filament that is already deducted in that number.
        "used_mm": 0.0,
        "spoolman_synced_mm": 0.0,
    }
    rw = sp.get("remaining_weight")
    if rw is not None:
        try:
            out["weight_g"] = round(float(rw), 1)
        except (TypeError, ValueError):
            pass
    try:
        d = float(fil.get("density") or 0)
        if d > 0:
            out["density"] = d
    except (TypeError, ValueError):
        pass
    ex = existing or {}
    # Our own fields win when already set - they cannot come from Spoolman.
    # A card_uid adopt passes the TAG's verbatim value (e.g. 'U04A1B2C3')
    # as the row's sku when we would otherwise GENERATE one: the row sku
    # must equal what the tag physically reads, or the next boot's tag
    # read finds no match, the stale-release rule unbinds, and the sweep
    # has to re-adopt every single boot. A user-set sku stays untouched.
    _ex_sku = (ex.get("sku") or "").strip()
    _gen = "SM%s" % sp.get("id", "")
    if tag_sku and (not _ex_sku or _ex_sku == _gen):
        out["sku"] = tag_sku
    else:
        out["sku"] = _ex_sku or _gen
    out["subtype"] = ((ex.get("subtype") or "").strip()
                      or _spoolman_subtype_guess(out["label"], out["material"]))
    # PA seed: only a row with NO local matrix takes Spoolman's (adopt /
    # first refresh - the between-printers calibration handover). A local
    # matrix - even a deliberately emptied {} - wins and is pushed the
    # other way by the refresh loop (local leads; key-absent check, so a
    # delete-all does not resurrect from SM).
    if ex.get("pa_matrix") is None:
        _pa = _spoolman_pa_extra(sp)
        if _pa:
            out["pa_matrix"] = _pa
    return out

async def _spoolman_refresh_known(base: str, spools: dict,
                                  force: bool = False) -> tuple[int, str]:
    """Refresh the spools THIS printer knows - one GET per linked spool,
    merged in a single import. Replaces the old bulk pull (GET the whole
    collection): with a 10k-spool Spoolman the collection landed in the
    local table, in every /api/state payload and in every picker list
    - the table must
    only ever hold what this printer touches; the collection stays in
    Spoolman and is reached via /api/spoolman/search. A spool Spoolman no
    longer answers for (deleted, archived, unreachable) is left alone
    locally: refresh updates, it never removes."""
    known = [(k, str(v.get("spoolman_id") or "").strip())
             for k, v in (spools or {}).items()
             if str(v.get("spoolman_id") or "").strip()]
    if not known:
        return 0, "no linked spools"

    def _row_changed(new_row, ex):
        # Import gate: only rows that actually moved reach the printer.
        # Without it every periodic pull fired a merge import + eMMC
        # write even when Spoolman had nothing new - at the 3-min
        # cadence that is the difference between "a write when weight
        # moved >=1g" and "a write every tick". Compare only the fields
        # the pull OWNS (sku/subtype are ours-win and equal by
        # construction; used_mm/synced_mm are reset commands, not data).
        if ex is None:
            return True
        for k in ("material", "vendor", "color", "label"):
            if str(new_row.get(k) or "") != str(ex.get(k) or ""):
                return True
        try:
            nw, xw = new_row.get("weight_g"), ex.get("weight_g")
            if (nw is None) != (xw is None):
                return True
            if nw is not None and abs(float(nw) - float(xw)) >= 1.0:
                return True
        except (TypeError, ValueError):
            return True
        try:
            if abs(float(new_row.get("density") or 0)
                   - float(ex.get("density") or 0)) > 1e-6:
                return True
        except (TypeError, ValueError):
            return True
        # A PA seed (SM matrix onto a row without one) must reach the
        # printer even when nothing else moved.
        if (new_row.get("pa_matrix") is not None
                and ex.get("pa_matrix") is None):
            return True
        return False

    out, n = {}, 0
    async with httpx.AsyncClient(timeout=_SPOOLMAN_TIMEOUT) as client:
        for key, smid in known:
            try:
                r = await client.get(f"{base}/api/v1/spool/{smid}")
                r.raise_for_status()
                sp = r.json()
            except httpx.HTTPError:
                continue
            if not isinstance(sp, dict) or sp.get("archived"):
                continue
            ex = (spools or {}).get(key)
            # Learned card UIDs (hand assignment, ace.py) go UP to
            # Spoolman's card_uids so SpoolLink recognises the roll at
            # its own readers too; merge, never drop.
            try:
                _luids = _spool_local_uids(ex or {})
                if _luids:
                    await _spoolman_card_uids_push_one(client, base, smid,
                                                       sp, _luids)
            except Exception as _e:
                _trace.info("spoolman: card_uids push SM%s skipped: %s",
                            smid, _e)
            # PA push lives in _spoolman_push (piggyback on every sync
            # moment incl. the periodic tick), NOT here - refresh only
            # SEEDS a matrix-less row via _spoolman_to_local.
            entry = _spoolman_to_local(sp, ex)
            # force = user-triggered / print-end moments: the >=1g weight
            # deadband below exists for the 3-min tick's eMMC budget, but
            # it also freezes a sub-gram residue FOREVER once bookings
            # stop. A sync the user asked for must land exactly.
            if not force and not _row_changed(entry, ex):
                continue
            out[str(n)] = entry
            n += 1
    if not n:
        return 0, "no changes"
    tmp = Path("/tmp/multiace_spoolman_pull.json")
    tmp.write_text(json.dumps({"spools": out}), encoding="utf-8")
    await _mr_post("/printer/gcode/script",
                   {"script": f"ACE_SPOOL_IMPORT PATH={tmp} MODE=merge"},
                   timeout=_MR_SPOOL_GCODE_TIMEOUT)
    return n, ""

# The search's working set: the full collection, briefly cached in RAM so
# typing refines against one fetch instead of hammering Spoolman per
# keystroke. Deliberately NOT persisted and NOT imported anywhere - the
# rows go to the browser as a filtered top-50 and are forgotten.
_spoolman_cache: dict = {"ts": 0.0, "base": "", "rows": []}

async def _spoolman_collection(base: str) -> list:
    now = time.time()
    if _spoolman_cache["base"] == base \
            and now - _spoolman_cache["ts"] < 10.0:
        return _spoolman_cache["rows"]
    async with httpx.AsyncClient(timeout=_SPOOLMAN_TIMEOUT) as client:
        r = await client.get(f"{base}/api/v1/spool")
        r.raise_for_status()
        rows = r.json()
    if not isinstance(rows, list):
        raise HTTPException(502, "Spoolman returned no spool list")
    _spoolman_cache.update({"ts": now, "base": base, "rows": rows})
    return rows

@app.get("/api/spoolman/search")
async def spoolman_search(q: str = "") -> dict:
    """Search over EVERYTHING Spoolman has -
    id, name, material, vendor, location, lot number; every whitespace-
    separated term must match somewhere. Filtered here in the backend, not
    via Spoolman query params: their matching semantics are not something
    to build on unverified, and the RAM cache makes the fetch per search
    session, not per keystroke."""
    state = _parse_state(await _query_state_gated())
    base = (state.get("spoolman_url") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "no Spoolman URL configured")
    terms = [t for t in (q or "").lower().split() if t]
    local_by_sm = {str(v.get("spoolman_id") or "").strip(): str(v.get("id") or k)
                   for k, v in (state.get("spools") or {}).items()
                   if str(v.get("spoolman_id") or "").strip()}
    out = []
    for sp in await _spoolman_collection(base):
        if not isinstance(sp, dict) or sp.get("archived"):
            continue
        fil = sp.get("filament") or {}
        ven = fil.get("vendor") or {}
        hay = " ".join([str(sp.get("id", "")),
                        fil.get("name") or "", fil.get("material") or "",
                        ven.get("name") or "", sp.get("location") or "",
                        sp.get("lot_nr") or ""]).lower()
        if any(t not in hay for t in terms):
            continue
        color = (fil.get("color_hex") or "")
        if not color:
            multi = fil.get("multi_color_hexes") or ""
            color = str(multi).split(",")[0] if multi else ""
        try:
            weight = round(float(sp.get("remaining_weight")), 1)
        except (TypeError, ValueError):
            weight = None
        smid = str(sp.get("id", ""))
        out.append({"spoolman_id": smid,
                    "name": (fil.get("name") or "").strip(),
                    "vendor": (ven.get("name") or "").strip(),
                    "material": (fil.get("material") or "").strip(),
                    "color": color.lstrip("#")[:6],
                    "weight_g": weight,
                    "local_id": local_by_sm.get(smid)})
        if len(out) >= 50:
            break
    return {"rows": out}

async def _spoolman_adopt_one(smid: str, tag_sku: str | None = None) -> str:
    """Fetch spool <smid> from Spoolman and merge-import it into the local
    table (a re-adopt updates in place and keeps local id/bindings/sku).
    Returns the local table id. Shared by the single-adopt endpoint and
    the tag sweep; `tag_sku` is the card_uid path's verbatim tag value
    (see _spoolman_to_local for why it becomes the row sku)."""
    state = _parse_state(await _query_state_gated())
    base = (state.get("spoolman_url") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "no Spoolman URL configured")
    async with httpx.AsyncClient(timeout=_SPOOLMAN_TIMEOUT) as client:
        r = await client.get(f"{base}/api/v1/spool/{smid}")
        if r.status_code == 404:
            raise HTTPException(404, f"Spoolman has no spool {smid}")
        r.raise_for_status()
        sp = r.json()
    existing = next((v for v in (state.get("spools") or {}).values()
                     if str(v.get("spoolman_id") or "").strip() == smid),
                    None)
    entry = _spoolman_to_local(sp, existing, tag_sku=tag_sku)
    tmp = Path("/tmp/multiace_spoolman_adopt.json")
    tmp.write_text(json.dumps({"spools": {"0": entry}}), encoding="utf-8")
    await _mr_post("/printer/gcode/script",
                   {"script": f"ACE_SPOOL_IMPORT PATH={tmp} MODE=merge"},
                   timeout=_MR_SPOOL_GCODE_TIMEOUT)
    state2 = _parse_state(await _query_state_gated())
    lid = next((str(v.get("id") or k)
                for k, v in (state2.get("spools") or {}).items()
                if str(v.get("spoolman_id") or "").strip() == smid), None)
    if lid is None:
        raise HTTPException(502, "import did not surface the spool")
    return lid

@app.post("/api/spoolman/adopt")
async def spoolman_adopt(payload: dict | None = None) -> dict:
    """Adopt ONE Spoolman spool into the local table (single-spool fetch +
    the existing merge import, so a re-adopt updates instead of
    duplicating and keeps local id/bindings/sku). This is the only road
    from Spoolman into the table now - with a URL configured, local
    creation is off, so the table
    stays a cache of what this printer actually touches."""
    smid = str((payload or {}).get("spoolman_id") or "").strip()
    if not smid.isdigit():
        raise HTTPException(400, "spoolman_id required")
    _st = _parse_state(await _query_state_gated())
    if _st.get("spool_mode") == "local":
        raise HTTPException(400, "spool mode is local - adopt disabled")
    return {"ok": True, "id": await _spoolman_adopt_one(smid)}

# Tags the unattended sweep tried and Spoolman ANSWERED FOR NEGATIVELY,
# {slot_key: sku}. Keyed by the TAG, so a different roll in the same slot is
# tried again while a spool Spoolman does not know is not re-fetched every
# minute. Only definitive refusals belong here (id branch: 404; card_uid
# branch: UID on multiple spools = a Spoolman data error the user must
# fix; a card_uid NO-match is never remembered - the user may add the UID
# any time and the compare is cache-local anyway) - an entry is dropped
# again both on success and on a transient failure:
#   success   - the local row is a CACHE and leaves the table when its
#               binding goes (gate-empty release + _spool_drop_if_unbound_sm),
#               so the very same roll in the very same slot legitimately
#               needs adopting again. Remembering the success made that slot
#               unassignable until the web service restarted.
#   transient - a timeout/connection error says nothing about whether
#               Spoolman knows the spool, and keeping it poisoned the slot
#               for good.
_sweep_tried: dict[str, str] = {}


@app.post("/api/spoolman/adopt_by_tags")
async def spoolman_adopt_by_tags() -> dict:
    """Endpoint form of the tag sweep (world switch INTO Spoolman)."""
    return await _spoolman_sweep_tags(strict=True)


async def _spoolman_sweep_tags(strict: bool = False) -> dict:
    """The world switch INTO Spoolman, other direction of the Klipper-side
    rebind: every
    occupied, still-unbound slot whose LAST tag read carries our own
    SM<id> scheme is adopted and bound in one sweep - the rolls stay in
    their slots, nothing needs re-inserting or hand-adopting.
    Accepted tag forms (each with or without a leading '#'):
      SM<digits> / bare digits - lookup by Spoolman id (the tag-writer
        community's convention; the residual risk that a purely numeric
        FACTORY code collides with a Spoolman id is accepted - a real
        vendor code like 'SM100-BLK' still never matches).
      any other code (3-19 chars) - compared directly against the
        spools' `card_uids` extra field, the SAME field SpoolLink
        maintains, so one entry serves both recognition paths (a tag
        written as U<code> needs the U in the field too). Compare is
        canonical on both sides (_card_canon: uppercase, ':'/spaces
        stripped - SpoolLink writes bare hex, a hand-entered colon form
        must still match). A FACTORY code only ever binds when the user
        deliberately entered exactly that string in a spool's card_uids
        - no hit is silent and cache-local, so this is a feature, not a
        risk.
    Entries that already exist were re-bound by Klipper itself
    during the switch; this creates the ones that do not."""
    state = _parse_state(await _query_state_gated())
    base = (state.get("spoolman_url") or "").strip().rstrip("/")
    if not base or state.get("spool_mode") == "local":
        # Local world: the table is the truth, nothing is adopted from
        # Spoolman - even with a URL still configured (the mode decides
        # the world, not the URL).
        if strict:
            raise HTTPException(400, "no Spoolman URL configured"
                                if not base else "spool mode is local")
        return {"ok": True, "adopted": 0, "errors": []}
    binding = state.get("spool_binding") or {}
    adopted, errs = 0, []
    coll = None   # collection, fetched lazily on the first non-SM code
    # Work items: (key, raw code, assign script template, bare_digits_are_id).
    # Slots and heads resolve through ONE loop so the two paths cannot
    # drift; only the code SOURCE and the assign target differ.
    items: list = []
    for ace in state.get("aces") or []:
        for sl in ace.get("slots") or []:
            if (sl.get("state") or "") in ("", "empty", "unknown"):
                continue
            items.append((f"{ace.get('idx')}_{sl.get('idx')}",
                          str(sl.get("sku") or "").strip(),
                          f"ACE_SPOOL_ASSIGN ACE={ace.get('idx')} "
                          f"SLOT={sl.get('idx')} ID={{lid}}",
                          True))
    # Head loop (feeder/manual): Klipper fills head_tag_seen only for
    # non-ACE heads, from the STOCK feeder reader's card UID (hex) or M1
    # SKU int. Bare digits are NOT treated as a Spoolman id here - feeder
    # card UIDs are hex and often digit-only, so 'sm<id>' must be explicit
    # (unlike ACE tags, where the tag-writer convention owns bare digits).
    for hk, code in (state.get("head_tag_seen") or {}).items():
        try:
            h = int(hk)
        except (TypeError, ValueError):
            continue
        code = str(code or "").strip()
        if code:
            items.append((f"h{h}", code,
                          f"ACE_SPOOL_ASSIGN HEAD={h} ID={{lid}}",
                          False))
    for key, sku_raw, script_tpl, bare_id_ok in items:
        if key in binding:
            continue
        sku = sku_raw.lstrip("#").lower()
        m = re.fullmatch(r"(?:sm)?(\d+)" if bare_id_ok else r"sm(\d+)", sku)
        # Everything else (3-19 chars, tag SKU hard cap) goes to the
        # card_uids compare; shorter reads are junk, not codes.
        if not m and not (3 <= len(sku) <= 19):
            continue
        if not strict and _sweep_tried.get(key) == sku:
            # Spoolman already answered definitively for THIS tag (id
            # branch: 404; card branch: UID on multiple spools).
            # Retrying every minute would hammer the instance / spam
            # the log for nothing; a different tag in the slot, a
            # restart or the manual sweep tries again.
            continue
        tag_sku = None
        if m:
            smid_s = m.group(1)
        else:
            uid = _card_canon(sku_raw.lstrip("#"))
            try:
                if coll is None:
                    coll = await _spoolman_collection(base)
            except Exception as e:
                # Transient - says nothing about the spool.
                errs.append(f"{key}: {str(e) or type(e).__name__}")
                continue
            hits = [sp for sp in coll
                    if isinstance(sp, dict) and not sp.get("archived")
                    and uid in _spool_card_uids(sp)]
            if not hits:
                # NOT definitive: the user may add the UID to a spool's
                # card_uids any time. The compare runs against the
                # 10s-cached collection, so retrying every tick costs
                # no per-slot HTTP - and no log line either (the
                # Klipper-side 'matches no table entry' already said
                # what there is to say).
                continue
            if len(hits) > 1:
                ids = ", ".join(f"#{sp.get('id')}" for sp in hits)
                _sweep_tried[key] = sku
                errs.append(f"{key}: card UID {uid} on multiple "
                            f"spools: {ids} - fix in Spoolman")
                continue
            smid_s = str(hits[0].get("id", ""))
            tag_sku = sku_raw
        _sweep_tried[key] = sku
        try:
            lid = await _spoolman_adopt_one(smid_s, tag_sku=tag_sku)
            await _mr_post("/printer/gcode/script", {
                "script": script_tpl.format(lid=lid)},
                timeout=_MR_SPOOL_GCODE_TIMEOUT)
            adopted += 1
            _sweep_tried.pop(key, None)
            _trace.info("spoolman tag adopt: %s -> SM%s (local #%s%s)",
                        key, smid_s, lid,
                        " via card_uid" if tag_sku else "")
        except HTTPException as e:
            # Spoolman answered definitively (404 = no such spool) - the
            # one case worth remembering.
            errs.append(f"{key}: {e.detail}")
        except Exception as e:
            # Timeout / connection / anything else: says nothing about
            # the spool, so let the next tick try again.
            _sweep_tried.pop(key, None)
            # str(httpx.ReadTimeout()) is EMPTY - the bare "0_1: " it
            # logged cost a round of guessing. Name the class instead.
            # str(e), not `e or ...`: an exception object is TRUTHY even
            # with no args, so the or-form would still format to "".
            errs.append(f"{key}: {str(e) or type(e).__name__}")
    return {"ok": not errs, "adopted": adopted, "errors": errs}

# Event-driven sweep trigger: on insert, not on the next poll tick. Klipper announces an unmatched tag read on the
# response pipe with a fixed-format line (ace.py _spool_bind_by_tag - the
# wording is load-bearing on BOTH sides); the gcode_response listener calls
# _sweep_kick on it. The 2 s debounce collapses the boot rescan's burst
# (one line per unmatched slot) into a single sweep. The 60 s poll stays as
# the fallback for lines missed while the WS reconnects, and for a tag that
# arrives while a kick-sweep is already running (deliberately not re-armed:
# rare, and the poll picks it up).
_SPOOL_UNMATCHED_RE = re.compile(
    r"\[spool\] tag .+ matches no table entry")
# ace.py's hand-assignment learn line (log_always) - push the UID up now.
_SPOOL_LEARNED_RE = re.compile(r"spool #\d+: learned card UID")
_sweep_kick_task: "asyncio.Task | None" = None
_card_kick_task: "asyncio.Task | None" = None


def _card_kick() -> None:
    global _card_kick_task
    if _card_kick_task is not None and not _card_kick_task.done():
        return
    _card_kick_task = asyncio.create_task(_card_kick_run())


async def _card_kick_run() -> None:
    await asyncio.sleep(2.0)
    try:
        state = _parse_state(await _query_state_gated())
        base = (state.get("spoolman_url") or "").strip().rstrip("/")
        if not base or state.get("spool_mode") == "local":
            return
        if _spoolman_lock.locked():
            return                       # the running sync covers it
        async with _spoolman_lock:
            pulled, perr = await _spoolman_refresh_known(
                base, state.get("spools") or {})
        _trace.info("spoolman card_uids kick: pulled=%d%s", pulled,
                    (" " + perr) if perr else "")
    except Exception as e:
        _trace.warning("spoolman card_uids kick failed: %s", e)


def _sweep_kick() -> None:
    global _sweep_kick_task
    if _sweep_kick_task is not None and not _sweep_kick_task.done():
        return
    _sweep_kick_task = asyncio.create_task(_sweep_kick_run())


async def _sweep_kick_run() -> None:
    await asyncio.sleep(2.0)
    if _spoolman_lock.locked():
        # A running sync covers the same ground; the fallback poll retries.
        return
    try:
        res = await _spoolman_sweep_tags()
        if res.get("adopted"):
            _trace.info("spoolman tag sweep (kick): %d spool(s) adopted",
                        res["adopted"])
        for e in res.get("errors") or []:
            _trace.warning("spoolman tag sweep (kick): %s", e)
    except Exception as e:
        _trace.warning("spoolman tag sweep (kick) failed: %s", e)

# --- ACE 2 firmware update (OTA) ---------------------------------------
# Flash engine: ace2_ota.py (based on hakimio's updater, see its header;
# DEV-ONLY until his license OK). The PORT comes from Klipper:
# ACE_FW_RELEASE disconnects the unit and holds every reconnect path,
# ACE_FW_RESUME hands it back - so the flasher never fights the running
# heartbeat for the serial port, and the other three units keep working.

_ACEFW_DIR = Path("/tmp/multiace-acefw")
_acefw = {"state": "idle", "pct": None, "msg": "", "ace": None,
          "result": None, "error": "", "file": "", "size": 0}


def _acefw_running() -> bool:
    return _acefw["state"] in ("releasing", "flashing", "resuming")


@app.post("/api/acefw/upload")
async def acefw_upload(file: UploadFile = File(...)) -> dict:
    """Stage the firmware file (.bin or .swu). One staging slot - a second
    upload replaces the first; extraction/validation happens at flash
    time so the .swu password does not need to travel twice."""
    if _acefw_running():
        raise HTTPException(409, "a firmware update is running")
    _ACEFW_DIR.mkdir(parents=True, exist_ok=True)
    dest = _ACEFW_DIR / "upload.bin"
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    dest.write_bytes(data)
    _acefw.update({"file": file.filename or "upload",
                   "size": len(data), "state": "idle",
                   "msg": "", "error": "", "result": None, "pct": None})
    # Guess the version from the upload NAME (no password needed - the
    # inner name is only reachable once decrypted, that guess is refined
    # at flash time). The UI pre-fills its version field with this, so the
    # common case ('ACE2_V1.1.31.swu') needs no typing.
    guess = ""
    try:
        import ace2_ota
        guess = ace2_ota.guess_version(file.filename or "")
    except Exception:
        pass
    return {"ok": True, "name": _acefw["file"], "size": len(data),
            "version_guess": guess}


async def _acefw_run(ace: int, port: str, version: str,
                     password, md5, dry_run: bool, force: bool,
                     patch_to_open: bool = False,
                     patch_target: str = "") -> None:
    def _prog(pct, msg):
        _acefw["pct"] = pct
        _acefw["msg"] = str(msg)
    try:
        _acefw["state"] = "flashing"
        # Local import: a missing/broken flasher module must
        # break THIS request, never the uvicorn start.
        import ace2_ota
        upload = str(_ACEFW_DIR / "upload.bin")
        fw, image_error = None, ""
        try:
            # patch_to_open: the user uploaded the STOCK V1.1.31 package;
            # extract it, then turn it into ACE2-Open in-process (Simon-CR
            # patch, deterministic + base-verified). The result carries the
            # patch target's version, so check_known gates the PATCHED image
            # byte-exact - a bad patch can never reach the wire. Load with a
            # neutral version label; apply_open_patch sets the real one.
            _load_ver = "" if patch_to_open else version
            fw = await asyncio.to_thread(
                ace2_ota.load_image, upload, _load_ver, md5, password)
            if patch_to_open:
                # The target is whatever the caller picked; the endpoint has
                # already validated it against PATCH_SPECS and fell back to
                # PATCH_TARGET when nothing was sent (an older frontend).
                _tgt = patch_target or ace2_ota.PATCH_TARGET
                _prog(None, "patching V1.1.31 -> ACE2-Open %s" % _tgt)
                fw = await asyncio.to_thread(ace2_ota.apply_open_patch,
                                             fw.data, _tgt)
        except Exception as e:
            image_error = str(e)
            # A real flash cannot proceed without the image; a dry run can
            # still test the port + version (the file half is optional
            # there, see flash()).
            if not dry_run:
                raise
        res = await asyncio.to_thread(
            ace2_ota.flash, port, fw, _prog, dry_run, force, image_error)
        _acefw["result"] = res
    except Exception as e:
        _acefw["error"] = str(e)
        _trace.warning("acefw: flash failed: %s", e)
    finally:
        _acefw["state"] = "resuming"
        try:
            await _mr_post("/printer/gcode/script",
                           {"script": f"ACE_FW_RESUME ACE={ace}"})
            _acefw["state"] = "error" if _acefw["error"] else "done"
        except Exception as e:
            # The port hand-back failed - that is now the loudest problem:
            # the unit stays disconnected until ACE_FW_RESUME succeeds.
            _acefw["error"] = ((_acefw["error"] + "; ") if _acefw["error"]
                               else "") + f"resume failed: {e}"
            _acefw["state"] = "error"
        _trace.info("acefw: finished state=%s result=%s error=%s",
                    _acefw["state"], _acefw["result"], _acefw["error"])


@app.post("/api/acefw/flash")
async def acefw_flash(payload: dict | None = None) -> dict:
    """Release the port via Klipper, then flash in the background.
    dry_run runs the identical chain (release, open, version query,
    firmware parse) without writing anything - the 'Testlauf'."""
    p = payload or {}
    if _acefw_running():
        raise HTTPException(409, "a firmware update is already running")
    try:
        ace = int(p.get("ace"))
    except (TypeError, ValueError):
        raise HTTPException(400, "ace index required")
    dry_run = bool(p.get("dry_run"))
    version = str(p.get("version") or "").strip()
    # Patch-to-Open: the upload is stock V1.1.31 and the backend patches it
    # to ACE2-Open before flashing. The effective TARGET (gate key + announce
    # base) then becomes ace2_ota.PATCH_TARGET regardless of the selected
    # version; guarded to the 1.1.31 source so it cannot be ticked for
    # another base.
    patch_to_open = bool(p.get("patch_to_open"))
    # Which ACE2-Open build to patch to. The frontend offers every entry of
    # PATCH_SPECS and sends the picked one; no value means the preselection,
    # so an older frontend keeps working unchanged. Validated against the
    # registry here, before the port release cycle, so an unknown string
    # cannot reach the patcher.
    _target = str(p.get("patch_target") or "").strip()
    if patch_to_open:
        try:
            import ace2_ota
            _known = ace2_ota.KNOWN_FIRMWARE
            _specs = getattr(ace2_ota, "PATCH_SPECS", {})
            if _target and _target not in _specs:
                raise HTTPException(
                    400, f"{_target} is not an ACE2-Open patch target")
            if not _target:
                _target = getattr(ace2_ota, "PATCH_TARGET", "")
            if not (_target and _known.get(_target)
                    and hasattr(ace2_ota, "apply_open_patch")):
                _target = ""
        except HTTPException:
            raise
        except Exception:
            _target = ""
        if not _target:
            raise HTTPException(400, "the ACE2-Open patch target is not "
                                     "available in this build")
        version = _target
    # The version is the string ANNOUNCED to the ACE + the skip/verify key
    # - a real flash needs it, a dry run does not (it only reads the
    # current version). So it is required only for the actual flash.
    if not version and not dry_run:
        raise HTTPException(400, "target version required (e.g. 1.1.31)")
    # Fast reject for an unlisted version BEFORE the port release cycle -
    # the byte-exact gate (check_known) sits in the flash path itself.
    if version and not dry_run:
        try:
            import ace2_ota
            _known = ace2_ota.KNOWN_FIRMWARE.get(version.lstrip("Vv"))
        except Exception:
            _known = True    # module trouble -> the in-flash gate decides
        if _known is None:
            raise HTTPException(
                400, f"version {version} is not on the tested-versions list")
    if not (_ACEFW_DIR / "upload.bin").exists():
        raise HTTPException(400, "no firmware file uploaded")
    state = _parse_state(await _query_state_gated())
    entry = next((a for a in (state.get("aces") or [])
                  if int(a.get("idx", -1)) == ace), None)
    if entry is None:
        raise HTTPException(404, f"no ACE {ace}")
    port = str(entry.get("serial_path") or "").strip()
    if not port:
        raise HTTPException(400, "ACE reports no serial path")
    _acefw.update({"state": "releasing", "ace": ace, "pct": None,
                   "msg": "releasing serial port", "error": "",
                   "result": None})
    try:
        await _mr_post("/printer/gcode/script",
                       {"script": f"ACE_FW_RELEASE ACE={ace}"})
    except Exception as e:
        _acefw.update({"state": "error", "error": f"release failed: {e}"})
        raise HTTPException(500, f"ACE_FW_RELEASE failed: {e}")
    # Paranoia gate: flash only against a CONFIRMED hold - if the state
    # does not show it, hand the port back and stop.
    state2 = _parse_state(await _query_state_gated())
    entry2 = next((a for a in (state2.get("aces") or [])
                   if int(a.get("idx", -1)) == ace), None)
    if not (entry2 and entry2.get("fw_hold")):
        try:
            await _mr_post("/printer/gcode/script",
                           {"script": f"ACE_FW_RESUME ACE={ace}"})
        except Exception:
            pass
        _acefw.update({"state": "error",
                       "error": "release not confirmed by the printer"})
        raise HTTPException(500, "release not confirmed by the printer")
    asyncio.create_task(_acefw_run(
        ace, port, version, p.get("password") or None,
        p.get("md5") or None, dry_run, bool(p.get("force")),
        patch_to_open, _target))
    return {"ok": True}


@app.get("/api/acefw/status")
async def acefw_status() -> dict:
    return dict(_acefw)


@app.get("/api/acefw/versions")
async def acefw_versions() -> dict:
    """The tested-versions allowlist -
    the UI's version dropdown offers exactly these; the byte gate sits in
    ace2_ota.flash via check_known. The ACE2-Open build is NOT offered as
    a direct target: it is reached by uploading stock 1.1.31 and ticking
    'patch to ACE2-Open'. Its KNOWN_FIRMWARE entry stays - it is the
    byte-exact gate for the patched image."""
    try:
        import ace2_ota
        return {"versions": [
            {"version": v, "size": e.get("size"),
             "crc": "0x%04X" % e["crc"], "source": e.get("source", ""),
             # The googleable package name - shown in brackets behind the
             # version in the dropdown.
             "swu": e.get("swu", "")}
            for v, e in sorted(ace2_ota.KNOWN_FIRMWARE.items())
            if v not in getattr(ace2_ota, "PATCH_SPECS", {"1.1.3O": 1})],
            # The patch targets are offered as a checkbox plus a picker
            # on the 1.1.31 upload instead - the UI shows them when this is
            # set. They are whatever ace2_ota can build, so a newer
            # ACE2-Open arrives here by adding its spec, not by editing the
            # UI. patch_target is the PRESELECTION; patch_targets is the
            # whole ladder, newest first, each with the size that
            # distinguishes them.
            "patch_target": (getattr(ace2_ota, "PATCH_TARGET", "1.1.3O")
                             if hasattr(ace2_ota, "apply_open_patch")
                             else ""),
            "patch_targets": ([
                {"version": v,
                 "size": (ace2_ota.KNOWN_FIRMWARE.get(v) or {}).get("size"),
                 "tested": (ace2_ota.KNOWN_FIRMWARE.get(v) or {}).get(
                     "tested", "")}
                for v in sorted(getattr(ace2_ota, "PATCH_SPECS", {}),
                                reverse=True)
                if ace2_ota.KNOWN_FIRMWARE.get(v)]
                if hasattr(ace2_ota, "apply_open_patch") else [])}
    except Exception as e:
        return {"versions": [], "error": str(e)}

# What this process last knew Spoolman's PA field to hold, per smid - the
# piggyback push's change gate. RAM only: after a backend restart the first
# differing check costs one GET per linked spool to re-learn, then it is
# quiet again. Keyed on the NORMALIZED matrix (_pa_norm).
_sm_pa_pushed: dict = {}


async def _spoolman_pa_maybe_push(client, base: str, smid: str,
                                  local_pa) -> bool:
    """Piggyback PA push for the consumption pusher: acts only when the
    local matrix differs from what this process last saw/wrote in Spoolman
    (RAM gate -> the periodic tick normally costs NOTHING). On a change:
    one GET (also yields the extra dict the PATCH must carry), skip if
    Spoolman already matches, else PATCH. Local leads."""
    if not isinstance(local_pa, dict):
        return False
    want = _pa_norm(local_pa)
    if _sm_pa_pushed.get(smid) == want:
        return False
    r = await client.get(f"{base}/api/v1/spool/{smid}")
    r.raise_for_status()
    sp = r.json()
    if not isinstance(sp, dict):
        return False
    if (_spoolman_pa_extra(sp) or {}) == want:
        _sm_pa_pushed[smid] = want
        return False
    ok = await _spoolman_pa_push_one(client, base, smid, sp, local_pa)
    if ok:
        _sm_pa_pushed[smid] = want
    return ok


async def _spoolman_push(base: str, spools: dict) -> tuple[int, list[str]]:
    """Report consumption per spool as LENGTH, so Spoolman applies its own
    density/diameter and our estimate never enters its database. The synced
    counter advances only after a 2xx, so a failure repeats the same amount
    next time instead of losing or double-counting it.
    Also the ONE home of the PA push, so every sync moment updates it: this
    runs on the periodic tick, pause, idle, print-end
    AND the manual button - and never in SpoolLink mode (that branch
    returns before pushing), which is exactly the mod-owns-the-field gate."""
    pushed, errs = 0, []
    async with httpx.AsyncClient(timeout=_SPOOLMAN_TIMEOUT) as client:
        for sp in (spools or {}).values():
            smid = str(sp.get("spoolman_id") or "").strip()
            if not smid:
                continue
            try:
                await _spoolman_pa_maybe_push(client, base, smid,
                                              sp.get("pa_matrix"))
            except (httpx.HTTPError, ValueError) as e:
                _trace.info("spoolman: pa push SM%s failed: %s", smid,
                            str(e) or type(e).__name__)
            try:
                used = float(sp.get("used_mm") or 0.0)
                done = float(sp.get("spoolman_synced_mm") or 0.0)
            except (TypeError, ValueError):
                continue
            delta = used - done
            if delta <= 0.5:      # sub-millimetre noise is not a report
                continue
            try:
                r = await client.put(f"{base}/api/v1/spool/{smid}/use",
                                     json={"use_length": round(delta, 2)})
                r.raise_for_status()
            except httpx.HTTPError as e:
                errs.append(f"#{sp.get('id')}: {e}")
                continue
            await _mr_post("/printer/gcode/script", {
                "script": f"ACE_SPOOL_SET ID={sp.get('id')} "
                          f"SYNCED_MM={round(used, 1)}"},
                timeout=_MR_SPOOL_GCODE_TIMEOUT)
            pushed += 1
    return pushed, errs

async def _spoolman_sync(pull: bool = True, push: bool = True) -> dict:
    state = _parse_state(await _query_state_gated())
    base = (state.get("spoolman_url") or "").strip().rstrip("/")
    if not base:
        raise HTTPException(400, "no Spoolman URL configured")
    if state.get("spool_mode") == "local":
        raise HTTPException(400, "spool mode is local - nothing to sync")
    if _spoolman_lock.locked():
        raise HTTPException(409, "a Spoolman sync is already running")
    async with _spoolman_lock:
        pushed, pulled, errs = 0, 0, []
        # Push FIRST: the pull adopts Spoolman's remaining weight and resets
        # the local counters, so pulling first would discard exactly the
        # consumption we still owe it.
        if push:
            pushed, errs = await _spoolman_push(base, state.get("spools") or {})
        if pull:
            state2 = _parse_state(await _query_state_gated()) if push else state
            # force: this is the manual button / the print-end sync - a
            # once-per-ask moment where the exact value must land (the
            # change deadband is periodic-tick economy only).
            pulled, _ = await _spoolman_refresh_known(
                base, state2.get("spools") or {}, force=True)
        _spoolman_last.update({"ts": time.time(), "ok": not errs,
                               "msg": "; ".join(errs)[:300],
                               "pulled": pulled, "pushed": pushed})
        return dict(_spoolman_last)

@app.get("/api/spoolman/status")
async def spoolman_status() -> dict:
    return dict(_spoolman_last)

@app.get("/api/spoolman/ping")
async def spoolman_ping() -> dict:
    """Is the configured instance actually answering? Drives the REAL
    connection checkmark in the config tab - the old always-visible one
    was the save button, which next to a URL field read as "connected".
    Probes Spoolman's own /api/v1/info; never raises,
    the caller only wants true/false plus a reason for the tooltip."""
    state = _parse_state(await _query_state_gated())
    base = (state.get("spoolman_url") or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "reason": "no_url"}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{base}/api/v1/info")
            r.raise_for_status()
            info = r.json() if r.content else {}
        return {"ok": True,
                "version": str((info or {}).get("version", ""))}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "reason": str(e)[:200]}

@app.post("/api/spoolman/sync")
async def spoolman_sync(payload: dict | None = None) -> dict:
    p = payload or {}
    return await _spoolman_sync(pull=bool(p.get("pull", True)),
                                push=bool(p.get("push", True)))

@app.post("/api/macro")
async def run_macro(req: MacroRequest) -> dict:
    parts = [req.name]
    if req.args:
        for k, v in req.args.items():
            parts.append(_gcode_kv(k, v))
    script = " ".join(parts)
    try:
        # Moonraker's /printer/gcode/script blocks until Klipper finishes
        # the gcode. Returns immediately on completion regardless of
        # timeout value; the timeout only fires if Klipper truly hangs
        # (e.g. MCU disconnect, deadlock). ACE_SWITCH TARGET=N AUTOLOAD=1
        # = full unload + load cycle (~14 min observed); worst-case
        # retry storm on a stuck slot can push toward 25 min. 1800s
        # gives margin for the worst case while still catching real
        # hangs.
        result = await _mr_post("/printer/gcode/script",
                                {"script": script}, timeout=1800.0)
    except httpx.HTTPStatusError as e:
        print('[/api/macro] HTTPStatusError on %r: %d %s'
              % (script, e.response.status_code,
                 (e.response.text or '').strip()[:300]),
              file=sys.stderr, flush=True)
        raise HTTPException(
            status_code=e.response.status_code,
            detail=e.response.text,
        )
    except httpx.HTTPError as e:
        print('[/api/macro] HTTPError on %r: %s: %s'
              % (script, type(e).__name__, str(e) or '(no message)'),
              file=sys.stderr, flush=True)
        raise HTTPException(status_code=502,
            detail='moonraker: %s' % (str(e) or type(e).__name__))
    return {"script": script, "result": result}

def _raw_calibration(status: dict) -> dict:
    ace = status.get('ace', {}) or {}
    return ace.get('calibration') or {'state': 'idle', 'session_id': 0}

async def _dispatch_calibration(script: str) -> dict:
    try:
        return await _mr_post('/printer/gcode/script', {'script': script},
                              timeout=30.0)
    except httpx.HTTPStatusError as e:
        detail = e.response.text if e.response is not None else str(e)
        raise HTTPException(
            status_code=e.response.status_code if e.response is not None else 502,
            detail=detail)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail='moonraker: %s' % e)

@app.get('/api/calibration')
async def get_calibration() -> dict:
    try:
        status = await _query_state_gated()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail='moonraker: %s' % e)
    return _raw_calibration(status)

@app.post('/api/calibration/start')
async def start_calibration(payload: CalibrationStart) -> dict:
    if not (0 <= payload.ace <= 3 and 0 <= payload.slot <= 3
            and 0 <= payload.head <= 3):
        raise HTTPException(400, 'ace, slot and head must be 0-3')
    scope = (payload.scope or 'ace').strip().lower()
    if scope not in ('ace', 'slot'):
        raise HTTPException(400, 'scope must be ace or slot')
    script = ('ACE_CALIBRATION_START ACE=%d SLOT=%d HEAD=%d SCOPE=%s'
              % (payload.ace, payload.slot, payload.head, scope))
    result = await _dispatch_calibration(script)
    return {'ok': True, 'script': script, 'result': result}

@app.post('/api/calibration/action')
async def calibration_action(payload: CalibrationAction) -> dict:
    action = (payload.action or '').strip().lower()
    try:
        status = await _query_state_gated()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail='moonraker: %s' % e)
    current = _raw_calibration(status)
    if (payload.session_id is not None
            and int(current.get('session_id', 0)) != payload.session_id):
        raise HTTPException(409, 'calibration session changed; reload state')
    if action == 'feed':
        script = 'ACE_CALIBRATION_FEED'
    elif action == 'retract':
        length = int(payload.length or 0)
        if length < 5 or length > 500:
            raise HTTPException(400, 'retract length must be 5-500 mm')
        script = 'ACE_CALIBRATION_RETRACT LENGTH=%d' % length
    elif action == 'feed_jog':
        length = int(payload.length or 0)
        if length < 5 or length > 500:
            raise HTTPException(400, 'forward jog length must be 5-500 mm')
        script = 'ACE_CALIBRATION_FEED_JOG LENGTH=%d' % length
    elif action == 'mark_swap':
        script = 'ACE_CALIBRATION_MARK MARK=swap'
    elif action == 'return_park':
        script = 'ACE_CALIBRATION_RETURN'
    elif action == 'verify_start':
        script = 'ACE_CALIBRATION_VERIFY ACTION=start'
        route = (payload.ace, payload.slot, payload.head)
        if any(value is not None for value in route):
            if not all(value is not None for value in route):
                raise HTTPException(400, 'verify route requires ace, slot and head')
            if not all(0 <= int(value) <= 3 for value in route):
                raise HTTPException(400, 'ace, slot and head must be 0-3')
            scope = (payload.scope or 'ace').strip().lower()
            if scope not in ('ace', 'slot'):
                raise HTTPException(400, 'scope must be ace or slot')
            script += ' ACE=%d SLOT=%d HEAD=%d SCOPE=%s' % (
                payload.ace, payload.slot, payload.head, scope)
    elif action == 'verify_continue':
        script = 'ACE_CALIBRATION_VERIFY ACTION=continue'
    elif action == 'verify_pause':
        script = 'ACE_CALIBRATION_VERIFY ACTION=pause'
    elif action == 'verify_resume':
        script = 'ACE_CALIBRATION_VERIFY ACTION=resume'
    elif action == 'verify_jog':
        length = int(payload.length or 0)
        if abs(length) not in (1, 2, 5, 10):
            raise HTTPException(
                400, 'verification jog must be +/-1, 2, 5, or 10 mm')
        script = 'ACE_CALIBRATION_VERIFY ACTION=jog LENGTH=%d' % length
    elif action == 'cancel':
        script = 'ACE_CALIBRATION_CANCEL'
    elif action == 'reset':
        script = 'ACE_CALIBRATION_RESET'
    else:
        raise HTTPException(400, 'unknown calibration action')
    result = await _dispatch_calibration(script)
    return {'ok': True, 'script': script, 'result': result}

@app.post('/api/calibration/unload-cancel')
async def cancel_calibration_unload() -> dict:
    """Cancel preparation out-of-band through a Klipper webhook.

    This deliberately does not dispatch G-code: a cancellation submitted to
    the G-code queue cannot run until the blocking unload has already ended.
    """
    try:
        result = await _mr_post(
            '/printer/multiace/calibration_unload_cancel', {}, timeout=5.0)
    except httpx.HTTPStatusError as e:
        detail = e.response.text if e.response is not None else str(e)
        raise HTTPException(
            status_code=e.response.status_code if e.response is not None else 502,
            detail=detail)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail='moonraker: %s' % e)
    return {'ok': True, 'result': result}

def _extract_params(text: str) -> tuple[dict[str, str], dict[int, dict[str, str]]]:
    """Pull `key: value` pairs out of [ace] and per-ACE [ace N] sections.
    Returns (main_params, per_ace_params) where per_ace_params is a dict
    keyed by ACE index (int). Comments are skipped."""
    main: dict[str, str] = {}
    per_ace: dict[int, dict[str, str]] = {}
    section: object = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            head = s[1:-1].strip()
            if head == "ace":
                section = "ace"
            elif head.startswith("ace ") or head.startswith("ace\t"):
                try:
                    section = int(head.split(None, 1)[1])
                except (IndexError, ValueError):
                    section = None
            else:
                section = None
            continue
        if section is None or ":" not in s:
            continue
        k, v = s.split(":", 1)
        key, val = k.strip(), v.strip()
        if section == "ace":
            main[key] = val
        else:
            per_ace.setdefault(section, {})[key] = val
    return main, per_ace

_TIPFORM_SECTION_RE = re.compile(r"^\[\s*ace_tipform\s*\]\s*$")
_TIPFORM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*(.*)$")
_TIPFORM_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")
_tipform_mod_cache: dict = {"sig": None, "mod": None}

def _load_tipform_module():
    """Import the INSTALLED ace_tipform.py so the web validates tables with
    the exact parser Klipper will run them through (a bad table written
    unvalidated would HALT Klipper at the next restart). mtime-aware like
    the post-processor loader (updates replace the file, uvicorn lives
    on). None = module not on this build -> editor disabled."""
    import importlib.util
    candidates = [
        Path(p) for p in _user_paths("klipper/klippy/extras/ace_tipform.py")
    ] + [
        Path(__file__).resolve().parents[2] / "klipper" / "extras"
        / "ace_tipform.py",
    ]
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            st = cand.stat()
            sig = (str(cand), st.st_mtime, st.st_size)
            if _tipform_mod_cache["sig"] == sig \
                    and _tipform_mod_cache["mod"] is not None:
                return _tipform_mod_cache["mod"]
            spec = importlib.util.spec_from_file_location(
                "ace_tipform_webvalidate", str(cand))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _tipform_mod_cache["sig"] = sig
            _tipform_mod_cache["mod"] = mod
            return mod
        except Exception as e:
            print("[/api/tipform] validator load failed from %s: %s"
                  % (cand, e), file=sys.stderr, flush=True)
    return None

def _extract_tipform(text: str) -> tuple[str, dict[str, str]]:
    """(mode, {table_name: raw_table_string}) from the cfg's [ace_tipform]
    section. Missing section -> ('stock', {})."""
    mode, tables = "stock", {}
    in_section = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = bool(_TIPFORM_SECTION_RE.match(stripped))
            continue
        if not in_section or not stripped or stripped.startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            continue
        m = _TIPFORM_KEY_RE.match(stripped)
        if not m:
            continue
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key == "mode":
            mode = val.lower()
        else:
            tables[key] = val
    return mode, tables

def _rewrite_tipform_section(text: str, mode: str,
                             tables: dict[str, str]) -> str:
    """Replace (or append) the [ace_tipform] section body. Everything
    outside the section - including the shipped comment block ABOVE the
    header - is preserved byte-identically."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i, n = 0, len(lines)
    placed = False
    while i < n:
        raw = lines[i]
        if _TIPFORM_SECTION_RE.match(raw.strip()):
            out.append(raw if raw.endswith("\n") else raw + "\n")
            out.append("mode: %s\n" % mode)
            for key in sorted(tables.keys()):
                out.append("%s: %s\n" % (key, tables[key]))
            i += 1
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            placed = True
            continue
        out.append(raw)
        i += 1
    if not placed:
        if out and out[-1].strip():
            out.append("\n")
        out.append("[ace_tipform]\n")
        out.append("mode: %s\n" % mode)
        for key in sorted(tables.keys()):
            out.append("%s: %s\n" % (key, tables[key]))
    return "".join(out)

@app.get("/api/tipform")
async def get_tipform() -> dict:
    """The tip-forming editor state: cfg truth (mode + raw table strings)
    plus whether this build supports the feature at all."""
    mod = _load_tipform_module()
    p = Path(_resolve_cfg_path())
    mode, tables = ("stock", {})
    if p.exists():
        mode, tables = _extract_tipform(p.read_text(encoding="utf-8"))
    return {
        "supported": mod is not None,
        "mode": mode,
        "tables": tables,
    }

@app.post("/api/tipform")
async def set_tipform(payload: TipformUpdate) -> dict:
    """Validate + write the [ace_tipform] section. Validation runs the
    installed module's parse_table - the same code Klipper runs at
    startup - so a table the web accepts can never halt the printer."""
    mod = _load_tipform_module()
    if mod is None:
        raise HTTPException(409, "this build has no ace_tipform module - "
                            "update multiACE first")
    mode = (payload.mode or "stock").strip().lower()
    if mode not in ("stock", "custom"):
        raise HTTPException(400, "mode must be 'stock' or 'custom'")
    tables: dict[str, str] = {}
    for name, raw in (payload.tables or {}).items():
        key = (name or "").strip().lower()
        raw = (raw or "").strip()
        if not raw:
            continue
        if key == "mode" or not _TIPFORM_NAME_RE.match(key):
            raise HTTPException(
                400, "invalid table name %r (a-z, 0-9, _ and -, max 32)"
                % name)
        try:
            mod.parse_table(raw)
        except ValueError as e:
            raise HTTPException(400, "table %r: %s" % (key, e))
        tables[key] = raw
    p = Path(_resolve_cfg_path())
    if not p.exists():
        raise HTTPException(404, f"config file not found: {_resolve_cfg_path()}")
    text = p.read_text(encoding="utf-8")
    backup = p.with_suffix(p.suffix + ".bak")
    _write_cfg_atomic(backup, text)
    _write_cfg_atomic(p, _rewrite_tipform_section(text, mode, tables))
    restart: dict | None = None
    if payload.restart_klipper:
        try:
            # FIRMWARE_restart, not the bare host RESTART: on the U1's
            # multi-MCU setup a host-only restart reconnects into an
            # "abnormal mcu connection" error; firmware_restart resets the
            # MCUs and is the proven path (the mode switch and updater
            # restart the same way).
            restart = await _mr_post("/printer/firmware_restart", {})
        except httpx.HTTPError as e:
            restart = {"error": str(e)}
    # Live apply, like the write-through [ace] parameters. Best-effort: an older ace_tipform
    # without the command answers "Unknown command" -> reloaded stays
    # False and the frontend shows the old restart hint instead.
    reloaded = False
    if not payload.restart_klipper:
        try:
            await _mr_post("/printer/gcode/script",
                           {"script": "ACE_TIPFORM_RELOAD"})
            reloaded = True
        except Exception as e:
            _trace.info("tipform live reload not available "
                        "(restart applies): %s", str(e)[:200])
    return {"mode": mode, "tables": tables, "path": str(p),
            "backup": str(backup), "restart": restart, "reloaded": reloaded}

def _cfg_sha1(text: str) -> str:
    """Revision token of the config file, used for the lost-update guard
    (ConfigUpdate.base_sha1). Content-based, not mtime: a boot hook or an
    SSH install may rewrite the file byte-identically, which is not a
    conflict."""
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _write_cfg_atomic(path, text: str) -> None:
    """Write `text` to `path` atomically: a sibling .tmp then os.replace.

    os.replace needs write permission on the DIRECTORY, not on the target
    file, so this succeeds even when ace.cfg is root-owned - the normal
    state right after an SSH-as-root install (install_multiace.sh runs as
    root). An in-place p.write_text() opens the existing file for
    truncation and needs write permission on the FILE, so it raises
    PermissionError [Errno 13] -> HTTP 500 there (tipform apply / config
    save). The replace also flips ownership to the writer (lava),
    self-healing the file the same way the Klipper-side write-through
    (_cfg_write_ace_option) already does - which is why the error was
    transient (any Klipper setter in between fixed it)."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(p))

@app.get("/api/config")
async def get_config() -> dict:
    p = Path(_resolve_cfg_path())
    if not p.exists():
        raise HTTPException(404, f"config file not found: {_resolve_cfg_path()}")
    text = p.read_text(encoding="utf-8")
    main, per_ace = _extract_params(text)
    return {"path": str(p), "content": text, "params": main,
            "per_ace_params": per_ace, "sha1": _cfg_sha1(text)}

@app.put("/api/config")
async def update_config(payload: ConfigUpdate) -> dict:
    p = Path(_resolve_cfg_path())
    if not p.exists():
        raise HTTPException(404, f"config file not found: {_resolve_cfg_path()}")
    if payload.base_sha1:
        # Lost-update guard (see ConfigUpdate.base_sha1): the file changed
        # since the browser loaded it, so the payload is built on a stale
        # base. Refuse with the CURRENT content so the client can re-apply
        # its form values on top and retry - never silently overwrite.
        cur = p.read_text(encoding="utf-8")
        cur_sha1 = _cfg_sha1(cur)
        if cur_sha1 != payload.base_sha1:
            raise HTTPException(409, json.dumps({
                "error": "config changed on disk since it was loaded",
                "sha1": cur_sha1,
                "content": cur,
            }))
    backup = p.with_suffix(p.suffix + ".bak")
    _write_cfg_atomic(backup, p.read_text(encoding="utf-8"))
    _write_cfg_atomic(p, payload.content)
    new_sha1 = _cfg_sha1(payload.content)
    restart: dict | None = None
    if payload.restart_klipper:
        try:
            # FIRMWARE_restart, not the bare host RESTART: on the U1's
            # multi-MCU setup a host-only restart reconnects into an
            # "abnormal mcu connection" error; firmware_restart resets the
            # MCUs and is the proven path (the mode switch and updater
            # restart the same way).
            restart = await _mr_post("/printer/firmware_restart", {})
        except httpx.HTTPError as e:
            restart = {"error": str(e)}
    return {"path": str(p), "backup": str(backup), "restart": restart,
            "sha1": new_sha1}

_LANG_NAME_RE = re.compile(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$")

def _load_catalog(lang: str) -> dict:
    if not _LANG_NAME_RE.match(lang):
        raise HTTPException(400, "invalid language code")
    p = Path(I18N_DIR) / f"{lang}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _merge_dicts(base: dict, overlay: dict) -> dict:
    """Recursive overlay-merge: keys in `overlay` override `base`,
    nested dicts are merged the same way."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dicts(out[k], v)
        else:
            out[k] = v
    return out

@app.get("/api/i18n/{lang}")
async def get_i18n(lang: str) -> dict:
    """
    Return the catalog for `lang`, merged on top of the en.json fallback
    so missing keys still resolve to English.
    """
    en = _load_catalog("en")
    if lang == "en":
        return en
    catalog = _load_catalog(lang)
    if not catalog:
        raise HTTPException(404, f"language not found: {lang}")
    return _merge_dicts(en, catalog)

@app.get("/api/i18n")
async def list_i18n() -> dict:
    """List available catalog languages."""
    d = Path(I18N_DIR)
    if not d.is_dir():
        return {"languages": []}
    langs = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            meta = data.get("_meta", {}) or {}
            langs.append({
                "code": p.stem,
                "name": meta.get("name", p.stem),
                "fallback": meta.get("fallback"),
            })
        except Exception:
            continue
    return {"languages": langs}

@app.get("/api/screen-available")
async def screen_available() -> dict:
    """
    Probe paxx fb-http (port 8092). Returns {available: true} if reachable,
    {available: false, error: ...} otherwise. Frontend uses this to show
    or hide the Display tab.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.head(SCREEN_PROBE_URL)
            return {"available": r.status_code < 500}
    except httpx.HTTPError as e:
        return {"available": False, "error": str(e)}

_SNAP_NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{1,64}$")

def _snap_dir(mode: str | None) -> Path:
    # Head-mode snapshots live in a separate subfolder so they never mix with
    # the multi/normal snapshots (a head loadout - feeder colours + ACE-head
    # slots - is a different shape from a multi loadout).
    base = Path(SNAPSHOT_DIR)
    return base / "head" if (mode or "") == "head" else base

def _snap_path(name: str, mode: str | None = None) -> Path:
    if not _SNAP_NAME_RE.match(name):
        raise HTTPException(400, "name must match [A-Za-z0-9_- ]{1,64}")
    return _snap_dir(mode) / f"{name}.json"

def _capture_snapshot(now_status: dict, mode: str | None = None) -> dict:
    """Build a snapshot from the current parsed state - what's loaded and
    where. Used for both saving (after parse_state) and as preview data.

    Multi/normal: only ACE-loaded toolheads (known head_source ace/slot) are
    captured; a head with filament but no source can't be reproduced, so it is
    dropped (apply would otherwise emit a 'slot is empty' error).

    Head mode: ACE heads are captured the same way (ace/slot), AND feeder heads
    are captured by their filament IDENTITY (material/colour/brand/subtype from
    print_task_config) with kind='feeder' and ace/slot=None - apply restores the
    identity via SET_PRINT_FILAMENT_CONFIG (the user reloads the feeder by hand).
    """
    parsed = _parse_state(now_status)
    head_mode = (mode or "") == "head"
    toolheads = []
    for t in parsed["toolheads"]:
        ace = t.get("ace")
        slot = t.get("slot")
        if ace is not None and slot is not None:
            slot_obj = None
            if 0 <= ace < len(parsed["aces"]):
                slots = parsed["aces"][ace]["slots"]
                if 0 <= slot < len(slots):
                    slot_obj = slots[slot]
            if not t.get("filament_detected"):
                continue
            toolheads.append({
                "idx":      t["idx"],
                "kind":     "ace",
                "ace":      ace,
                "slot":     slot,
                "material": (slot_obj or {}).get("material", ""),
                "brand":    (slot_obj or {}).get("brand", ""),
                "color":    (slot_obj or {}).get("color"),
                "color_rgb": (slot_obj or {}).get("color_rgb"),
                "sku":      (slot_obj or {}).get("sku", ""),
            })
        elif head_mode and t.get("feeder"):
            # Feeder head: capture the user-set filament identity (no ACE slot).
            mat = (t.get("material") or "").strip()
            col = (t.get("color") or "")
            if not mat and not col:
                continue                 # no identity set -> nothing to restore
            toolheads.append({
                "idx":      t["idx"],
                "kind":     "feeder",
                "ace":      None,
                "slot":     None,
                "material": mat,
                "brand":    (t.get("brand") or "").strip(),
                "color":    col,
                "sku":      (t.get("subtype") or "").strip(),
            })
    return {"toolheads": toolheads}

@app.get("/api/snapshots")
async def list_snapshots(mode: str | None = None) -> dict:
    d = _snap_dir(mode)
    d.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "name":        p.stem,
                "saved":       data.get("saved"),
                "description": data.get("description"),
                "toolheads":   data.get("toolheads", []),
            })
        except Exception as e:
            items.append({"name": p.stem, "error": str(e)})
    return {"snapshots": items}

@app.post("/api/snapshots")
async def save_snapshot(req: SnapshotSave) -> dict:
    p = _snap_path(req.name, req.mode)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = await _query_state_gated()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"moonraker: {e}")
    snap = _capture_snapshot(status, req.mode)
    snap["name"] = req.name
    snap["description"] = req.description
    snap["saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(p), "snapshot": snap}

@app.get("/api/snapshots/{name}")
async def get_snapshot(name: str, mode: str | None = None) -> dict:
    p = _snap_path(name, mode)
    if not p.exists():
        raise HTTPException(404, "snapshot not found")
    return json.loads(p.read_text(encoding="utf-8"))

@app.delete("/api/snapshots/{name}")
async def delete_snapshot(name: str, mode: str | None = None) -> dict:
    p = _snap_path(name, mode)
    if not p.exists():
        raise HTTPException(404, "snapshot not found")
    p.unlink()
    return {"ok": True}

@app.post("/api/snapshots/{name}/apply")
async def apply_snapshot(name: str, mode: str | None = None) -> dict:
    """
    Plan a snapshot apply. Computes the ordered command list to bring
    the printer from the current state to the snapshot, but does NOT
    execute. The caller (web frontend) enqueues each step into its
    command queue, so the user sees the full plan as queue chips and
    long-running commands don't time out our HTTP call.
    """
    p = _snap_path(name, mode)
    if not p.exists():
        raise HTTPException(404, "snapshot not found")
    snap = json.loads(p.read_text(encoding="utf-8"))
    try:
        status = await _query_state_gated()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"moonraker: {e}")
    cur = _parse_state(status)
    cur_th = {t["idx"]: t for t in cur["toolheads"]}
    desired = {t["idx"]: t for t in snap.get("toolheads", [])}
    cur_aces = cur.get("aces", []) or []

    def _slot_view(ace_i, slot_i):
        if ace_i is None or slot_i is None:
            return None
        if not (0 <= ace_i < len(cur_aces)):
            return None
        slots = cur_aces[ace_i].get("slots") or []
        if not (0 <= slot_i < len(slots)):
            return None
        return slots[slot_i]

    errors: list[dict] = []
    warnings: list[dict] = []

    for idx, dt in desired.items():
        if dt.get("kind") == "feeder" or dt.get("ace") is None:
            continue                   # feeder head: identity-only, checked below
        ace_i  = dt.get("ace")
        slot_i = dt.get("slot")
        sv = _slot_view(ace_i, slot_i)
        if sv is None or sv.get("raw") == 0 or (sv.get("state") or "").startswith("empty"):
            errors.append({
                "head": idx, "ace": ace_i, "slot": slot_i,
                "kind": "empty",
                "message": (f"T{idx}: ACE {ace_i} / Slot {slot_i} ist leer "
                            f"({(dt.get('material') or '?')} erwartet)"),
            })
            continue

        want_mat = (dt.get("material") or "").strip()
        have_mat = (sv.get("material") or "").strip()
        want_col = (dt.get("color") or "")
        have_col = (sv.get("color") or "")
        want_brand = (dt.get("brand") or "").strip()
        have_brand = (sv.get("brand") or "").strip()
        if want_mat and have_mat and want_mat != have_mat:
            warnings.append({
                "head": idx, "ace": ace_i, "slot": slot_i, "kind": "material",
                "want": want_mat, "have": have_mat,
                "message": (f"T{idx}: Snapshot will {want_mat}, "
                            f"ACE {ace_i} / Slot {slot_i} hat {have_mat or '?'}"),
            })
        elif want_col and have_col and want_col.lower() != have_col.lower():
            warnings.append({
                "head": idx, "ace": ace_i, "slot": slot_i, "kind": "color",
                "want": want_col, "have": have_col,
                "message": (f"T{idx}: Farbabweichung - Snapshot {want_col}, "
                            f"Slot {have_col}"),
            })
        elif want_brand and have_brand and want_brand != have_brand:
            warnings.append({
                "head": idx, "ace": ace_i, "slot": slot_i, "kind": "brand",
                "want": want_brand, "have": have_brand,
                "message": (f"T{idx}: Hersteller-Abweichung - Snapshot {want_brand}, "
                            f"Slot {have_brand}"),
            })

    actions: list[dict] = []

    for idx, ct in cur_th.items():
        if not ct.get("head_source_known"):
            continue
        d = desired.get(idx)
        if (d is None
            or d.get("ace") != ct.get("ace")
            or d.get("slot") != ct.get("slot")):
            actions.append({"name": "ACE_UNLOAD_HEAD", "args": {"HEAD": idx}})

    by_ace: dict[int, list[int]] = {}
    for idx, dt in desired.items():
        ace_idx = dt.get("ace")
        if ace_idx is None:
            continue
        ct = cur_th.get(idx, {})
        if (ct.get("head_source_known")
            and ct.get("ace") == ace_idx
            and ct.get("slot") == dt.get("slot")):
            continue
        by_ace.setdefault(ace_idx, []).append(idx)

    for ace_idx in sorted(by_ace):
        for head in sorted(by_ace[ace_idx]):
            actions.append({"name": "ACE_LOAD_HEAD", "args": {"HEAD": head, "ACE": ace_idx}})

    # Feeder heads (head mode): restore the saved filament identity straight to
    # the head's print_task_config (no ACE load - the user reloads by hand).
    for idx, dt in sorted(desired.items()):
        if dt.get("kind") != "feeder":
            continue
        mat = (dt.get("material") or "").strip()
        col = (dt.get("color") or "").strip()
        if not mat and not col:
            continue
        hexc = col.lstrip("#") or "ffffff"
        dq = lambda s: '"%s"' % str(s or "").replace('"', "")
        actions.append({"name": "SET_PRINT_FILAMENT_CONFIG", "args": {
            "CONFIG_EXTRUDER":     idx,
            "FILAMENT_TYPE":       dq(mat or "PLA"),
            "FILAMENT_COLOR_RGBA": hexc.upper() + "FF",
            "VENDOR":              dq((dt.get("brand") or "Generic")),
            "FILAMENT_SUBTYPE":    dq((dt.get("sku") or "")),
        }})

    override_proposals: list[dict] = []
    for idx, dt in desired.items():
        ace_i = dt.get("ace")
        slot_i = dt.get("slot")
        if ace_i is None or slot_i is None:
            continue
        material = (dt.get("material") or "").strip()
        color = (dt.get("color") or "").strip()
        if not material and not color:

            continue
        override_proposals.append({
            "ace":      ace_i,
            "slot":     slot_i,
            "material": material,
            "brand":    (dt.get("brand") or "").strip(),
            "subtype":  (dt.get("sku") or "").strip(),
            "color":    color,
        })

    return {
        "snapshot": name,
        "actions": actions,
        "errors":   errors,
        "warnings": warnings,
        "override_proposals": override_proposals,
    }

_slot_overrides: dict[str, dict] = {}
_last_head_source: dict[int, tuple[int, int] | None] = {}

_overrides_mtime: float = 0.0

def _override_key(ace: int, slot: int) -> str:
    return f"{int(ace)}_{int(slot)}"

def _reload_overrides_if_changed() -> None:
    """Cheap mtime check; reloads only when the file has been touched
    since we last read it (e.g. by ace.py picking up a display edit)."""
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    if not p.exists():
        if _slot_overrides:
            _slot_overrides.clear()
        _overrides_mtime = 0.0
        return
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _overrides_mtime:
        return
    _load_overrides_from_disk()
    _overrides_mtime = m

def _load_overrides_from_disk() -> None:
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _slot_overrides.clear()
            _slot_overrides.update(data)
        try:
            _overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _save_overrides_to_disk() -> None:
    """Atomic write: render to a sibling .tmp file then os.replace,
    so concurrent readers (= ace.py reverse-sync, mtime poller) never
    see a half-written file."""
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(_slot_overrides, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(p))
        try:
            _overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _drop_override_if_present(ace: int, slot: int) -> bool:
    """Remove any manual slot override for (ace, slot). Returns True
    when an entry was popped so the caller can batch the file write
    across multiple drops in the same poll. Used both on
    toolhead-unload bookkeeping and on physical eject from the ACE
    slot (gate_status == 0)."""
    key = _override_key(ace, slot)
    if key in _slot_overrides:
        old = _slot_overrides.pop(key, None)
        _trace.info("override DROP gate==0 ACE %d / slot %d (was %s)", ace, slot, old)
        return True
    return False

EJECT_DEBOUNCE_S = 0.5
_eject_pending_since: dict[tuple[int, int], float] = {}

def _override_for(ace: int, slot: int) -> dict | None:
    """Return the override dict for this (ace, slot) if any meaningful
    fields are set, else None."""
    o = _slot_overrides.get(_override_key(ace, slot))
    if not o:
        return None
    mat = (o.get("material") or "").strip()
    col = (o.get("color") or "").strip()
    if not mat and not col:
        return None
    return o

def _track_unload_clears(head_source: dict) -> None:
    """Compare current head_source against last seen state. When a
    toolhead transitions from "loaded from (a,s)" to None, clear that
    (a,s)'s override."""
    changed = False
    for t in range(4):
        cur = head_source.get(str(t)) or head_source.get(t)
        d, sl = _resolve_head_source(cur)
        prev = _last_head_source.get(t)
        if prev is not None and (d, sl) != prev and d is None and sl is None:

            key = _override_key(prev[0], prev[1])
            if key in _slot_overrides:
                old = _slot_overrides.pop(key, None)
                _trace.info("override DROP unload T%d (was loaded from ACE %d / slot %d): %s",
                            t, prev[0], prev[1], old)
                changed = True
        _last_head_source[t] = (d, sl) if (d is not None and sl is not None) else None
    if changed:
        _save_overrides_to_disk()

@app.get("/api/slot-override")
async def list_slot_overrides() -> dict:
    return {"overrides": _slot_overrides}

@app.post("/api/slot-override")
async def set_slot_override(req: SlotOverride) -> dict:
    key = _override_key(req.ace, req.slot)
    new = {
        "ace":      req.ace,
        "slot":     req.slot,
        "material": req.material or "",
        "brand":    req.brand or "",
        "subtype":  req.subtype or "",
        "color":    req.color or "",
    }
    old = _slot_overrides.get(key)
    _slot_overrides[key] = new
    _trace.info("override SET via picker POST ACE %d / slot %d: %s -> %s",
                req.ace, req.slot, old, new)
    _save_overrides_to_disk()
    return {"ok": True, "key": key, "override": _slot_overrides[key]}

@app.delete("/api/slot-override/{ace}/{slot}")
async def delete_slot_override(ace: int, slot: int) -> dict:
    key = _override_key(ace, slot)
    if key in _slot_overrides:
        old = _slot_overrides.pop(key, None)
        _trace.info("override DROP via picker DELETE ACE %d / slot %d (was %s)",
                    ace, slot, old)
        _save_overrides_to_disk()
    return {"ok": True}

_load_overrides_from_disk()

_notifications: deque = deque(maxlen=50)
_next_notification_id = int(time.time() * 1000)
_notifications_lock = asyncio.Lock()

_NOTIF_ONLY_MULTIACE = os.environ.get(
    "MULTIACE_NOTIF_ONLY_MULTIACE", "1") in ("1", "true", "yes")

def _is_error_gcode_response(text: str) -> bool:
    """Filter for gcode_response strings that should surface as a
    notification. The ace.py module pumps a lot of plain status
    messages through respond_raw (= log_always); only log_error
    prepends '!!' so we can tell them apart by the prefix.

    Default mode (MULTIACE_NOTIF_ONLY_MULTIACE=1): require BOTH a
    '[multiACE]' tag AND an error marker (!!, Error:, aborting).
    Off (=0): catch any error-shaped Klipper response."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    body = s[3:].strip() if s.startswith("// ") else s
    # '[warn]' marker = ace.log_warn: intermediate/recoverable events
    # (reconnect attempts, retry-ladder hiccups). Captured as a WARN-level
    # notification (amber), never red - only final failures use '!!'.
    if body.startswith("[warn]") and "[multiACE]" in s:
        return True
    # '[info]' marker = ace.log_notice: progress/success of a user-started
    # operation (tag write). Info-level (green) notification.
    if ((body.startswith("[info]") or body.startswith("[done]"))
            and "[multiACE]" in s):
        return True
    is_error = (
        body.startswith("!!")
        or "Error:" in body
        or body.lower().startswith("aborting")
    )
    if _NOTIF_ONLY_MULTIACE:
        return is_error and "[multiACE]" in s
    if is_error:
        return True
    if body.lower().startswith("unknown command"):
        return True
    return False

def _record_notification(text: str) -> dict | None:
    global _next_notification_id
    if not _is_error_gcode_response(text):
        return None
    _next_notification_id += 1
    msg = text.strip()

    for prefix in ("// !! ", "// Error:", "// ", "!! ", "!!", "Error:"):
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
            break

    # ace.log_warn marker -> warn-level (amber) notification; strip it
    # from the displayed text.
    level = "error"
    done = False
    if msg.startswith("[warn]"):
        level = "warn"
        msg = msg[len("[warn]"):].strip()
    elif msg.startswith("[info]") or msg.startswith("[done]"):
        level = "info"
        done = msg.startswith("[done]")
        msg = msg[len("[info]"):].strip()

    if msg.startswith("[multiACE] "):
        msg = msg[len("[multiACE] "):].strip()
    elif msg.startswith("[multiACE]"):
        msg = msg[len("[multiACE]"):].strip()
    note = {
        "id":    _next_notification_id,
        "ts":    time.time(),
        "msg":   msg,
        "raw":   text.strip(),
        "level": level,
    }
    if level == "info":
        # Progress of ONE running op: only the latest line stays (the
        # frontend mirrors this via replaces_info); a [done] line also
        # dismisses itself after ttl seconds.
        keep = [n for n in _notifications if n.get("level") != "info"]
        _notifications.clear()
        _notifications.extend(keep)
        note["replaces_info"] = True
        if done:
            note["ttl"] = 8
    _notifications.append(note)
    _trace.info("notification %d captured: %s", note["id"], note["msg"])
    return note

_print_state_last = ""

async def _on_status_update(params: list) -> None:
    """print_stats transitions -> the optional Spoolman auto-sync when a
    print ENDS, on 'complete' AND on 'cancelled'/'error'. The old complete-only
    choice reasoned "nothing is lost, only delayed" - that held while
    entries lived forever, and died with delete-on-unbind: a spool taken
    out after a CANCELLED print would take its unsynced consumption with
    it. The consumption of an aborted print is just as real; the push is
    a background POST to Spoolman and touches neither printer nor user
    intervention."""
    global _print_state_last
    if not params or not isinstance(params[0], dict):
        return
    st = ((params[0].get("print_stats") or {}).get("state") or "").strip()
    if not st or st == _print_state_last:
        return
    prev, _print_state_last = _print_state_last, st
    if st == "paused" and prev == "printing":
        # A spool is only ever swapped with the print paused, and pausing is
        # also when Klipper releases the binding of a slot whose gate ran
        # empty (ace.py gate transition, printing-gated).
        # A released Spoolman row with unsynced debt survives until the next
        # successful push, so without this it lingers up to a full periodic
        # interval - i.e. the user is at the machine before the table is
        # tidy. Push-only, no pull: this is a hands-on moment. Guarded like
        # the print-end sync below - this runs inside the websocket status
        # handler and must never take the listener down with it.
        try:
            await _spoolman_push_now("pause")
        except Exception as e:
            _trace.warning("spoolman pause sync failed: %s", e)
        return
    if st not in ("complete", "cancelled", "error") \
            or prev not in ("printing", "paused"):
        return
    try:
        state = _parse_state(await _query_state_gated())
    except Exception as e:
        _trace.warning("spoolman auto-sync: state query failed: %s", e)
        return
    if not (state.get("spoolman_auto") and (state.get("spoolman_url") or "")):
        return
    if state.get("spool_mode") == "local":
        return
    _trace.info("spoolman auto-sync after print end (%s)", st)
    try:
        res = await _spoolman_sync()
        _trace.info("spoolman auto-sync done: %s", res)
    except HTTPException as e:
        _trace.warning("spoolman auto-sync skipped: %s", e.detail)
    except Exception as e:
        _trace.warning("spoolman auto-sync failed: %s", e)

# 3 min: both directions are change-gated -
# the push skips spools whose delta is sub-millimetre, the spoollink pull
# skips the import when nothing moved (see _spoolman_refresh_known) - so
# a quiet tick costs one state query plus backend HTTP, nothing
# printer-side. eMMC writes stay in the minutes class.
_SPOOLMAN_PRINT_SYNC_S = 180.0


async def _spoolman_push_now(why: str, sl_pull: bool = True) -> None:
    """One push-only sync: consumption out, nothing pulled back in. Shared by
    the periodic timer, the pause transition and the idle triggers. Quiet by
    design - no URL, the auto switch off, or another sync already running is
    a no-op, never an error, because every caller fires unattended.

    `sl_pull=False` disables the spoollink-mode PULL branch below: that
    branch costs one GET per linked spool plus a merge import, which is
    right for a timed tick but not for a click-driven trigger (tab
    switch). Those callers pass False; the periodic tick keeps it."""
    if _spoolman_lock.locked():
        # A manual or print-end sync is running - that one covers this debt.
        return
    state = _parse_state(await _query_state_gated())
    base = (state.get("spoolman_url") or "").strip().rstrip("/")
    if not (state.get("spoolman_auto") and base):
        return
    if state.get("spool_mode") == "local":
        return
    if state.get("spool_mode") == "spoollink":
        if not sl_pull:
            return
        # SpoolLink counts, our used_mm is frozen - there is nothing to
        # push. What this moment needs is the OTHER direction: SpoolLink's
        # bookings land in Spoolman, and the local rows (= the webui's
        # weight display) only ever move on a PULL. Same cadence, same auto switch, pull instead
        # of push. Cost per firing: one GET per linked spool + ONE merge
        # import that queues behind whatever the printer runs (180s
        # timeout covers a swap).
        async with _spoolman_lock:
            pulled, perr = await _spoolman_refresh_known(
                base, state.get("spools") or {})
        if pulled or perr:
            _spoolman_last.update({
                "ts": time.time(), "ok": not perr,
                "msg": str(perr or "")[:300],
                "pulled": pulled, "pushed": 0})
            _trace.info("spoolman %s refresh (spoollink): pulled=%d%s",
                        why, pulled, (" " + perr) if perr else "")
        return
    async with _spoolman_lock:
        pushed, errs = await _spoolman_push(base, state.get("spools") or {})
    if pushed or errs:
        _spoolman_last.update({
            "ts": time.time(), "ok": not errs,
            "msg": "; ".join(errs)[:300],
            "pulled": 0, "pushed": pushed})
        _trace.info("spoolman %s sync: pushed=%d%s", why, pushed,
                    (" errs=" + "; ".join(errs)[:200]) if errs else "")


# Idle push triggers (e.g. the Spoolman host was off overnight):
# a failed print-end sync leaves the debt on disk (used_mm vs
# spoolman_synced_mm, atomically persisted; the unbound row is kept), but
# nothing retries it - the periodic tick runs ONLY while printing/paused
# and there was no boot push at all, so an idle printer sat on the debt
# until the next print. Two idle moments now pay it: the web service
# starting (printer rebooted, NAS reachable) and opening the spools tab
# (the user is looking at exactly these numbers). Deliberately NOT during
# a print - the 3 min tick owns that window. Idle means no booking runs, so the debt is
# static: the first trigger pays it, every later one finds delta <= 0.5mm
# and costs one state query, no HTTP and no gcode.
_SPOOLMAN_IDLE_PUSH_COOLDOWN_S = 60.0
_spoolman_idle_push_last = 0.0


async def _spoolman_push_if_idle(why: str) -> dict:
    """Push-only, idle-only, rate-limited. Never pulls (see sl_pull)."""
    global _spoolman_idle_push_last
    if _print_state_last in ("printing", "paused"):
        return {"ok": True, "pushed": False, "skipped": "printing"}
    now = time.monotonic()
    if now - _spoolman_idle_push_last < _SPOOLMAN_IDLE_PUSH_COOLDOWN_S:
        return {"ok": True, "pushed": False, "skipped": "cooldown"}
    _spoolman_idle_push_last = now
    try:
        await _spoolman_push_now(why, sl_pull=False)
    except Exception as e:
        # Unattended trigger: a push failure is the normal offline case
        # (that is what the debt survives for), never an error to the UI.
        _trace.info("spoolman %s push failed: %s", why, e)
        return {"ok": False, "pushed": False, "error": str(e)[:200]}
    return {"ok": True, "pushed": True}


@app.post("/api/spoolman/push")
async def spoolman_push() -> dict:
    """Push-only sync for the spools tab. Quiet no-op while a print runs,
    inside the cooldown, in local/spoollink mode or without a URL."""
    return await _spoolman_push_if_idle("tab")


async def _spoolman_startup_push() -> None:
    """One idle push shortly after the web service came up."""
    try:
        await asyncio.sleep(20.0)
        await _spoolman_push_if_idle("startup")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _trace.info("spoolman startup push failed: %s", e)


async def _spoolman_periodic_push() -> None:
    """Spoolman sync every _SPOOLMAN_PRINT_SYNC_S (3 min) WHILE a print
    runs, with as little printer load as possible.
    Direction follows the world: spoolman mode pushes our deltas,
    spoollink mode PULLS the linked spools instead so the webui's
    weights track SpoolLink's bookings (see _spoolman_push_now). Bounds two lags at once: Spoolman's remaining-weight
    display on multi-day prints, and the deferred-drop wait of a
    mid-print runout (ace.py keeps the unbound row until the next
    successful push - this IS that push, at most one interval late).
    Printer cost per firing: one gated state query plus one quiet
    SYNCED_MM write per spool that actually moved; the HTTP runs
    entirely here. Push-only on purpose - the pull half does per-spool
    GETs plus a merge import and belongs to idle moments. Gates on the
    same auto switch as the print-end sync; the interval is a constant,
    nobody should have to tune it. Swap-triggered and heartbeat-coupled
    were considered and rejected: the swap is the densest response-pipe
    moment there is, and the heartbeat is Klipper-side, which never
    does HTTP."""
    last = 0.0
    while True:
        try:
            await asyncio.sleep(60.0)
            if _print_state_last not in ("printing", "paused"):
                continue
            now = time.monotonic()
            if now - last < _SPOOLMAN_PRINT_SYNC_S:
                continue
            await _spoolman_push_now("print")
            last = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _trace.info("spoolman print sync failed: %s", e)

async def _moonraker_log_listener() -> None:
    """Background task that follows Moonraker's gcode_response stream
    via websocket and records error-level lines as notifications.
    Reconnects with backoff on any failure."""
    global _print_state_last
    url = MOONRAKER_URL.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/websocket"
    backoff = 1.0
    debug_recv = os.environ.get("MULTIACE_WS_DEBUG", "0") in ("1", "true", "yes")
    while True:
        try:
            _trace.info("moonraker WS connecting to %s ...", url)

            async with websockets.connect(url, ping_interval=None, close_timeout=5) as ws:
                _trace.info("moonraker WS connected")

                try:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "server.connection.identify",
                        "params": {
                            "client_name": "multiace_web",
                            "version": VERSION,
                            "type": "agent",
                            "url": "https://github.com/decay71/multiACE",
                        },
                        "id": 1,
                    }))
                    _trace.info("moonraker WS identify sent")
                    # Print state, for the after-print Spoolman sync. A
                    # subscription costs one message per transition; polling
                    # would add periodic queries to a printer we deliberately
                    # keep idle (the 0003 code-page pressure lesson).
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "printer.objects.subscribe",
                        "params": {"objects": {"print_stats": ["state"]}},
                        "id": 2,
                    }))
                except Exception as ie:
                    _trace.warning("moonraker WS identify failed: %s", ie)
                backoff = 1.0
                msg_count = 0
                async for raw in ws:
                    msg_count += 1

                    if debug_recv:
                        _trace.warning("moonraker WS recv #%d: %s", msg_count, str(raw)[:240])
                    # During a homing/probe window, drop the message without
                    # parsing - skip JSON decode, regex, and any handler work
                    # to keep the backend off the CPU and out of klippy's
                    # code-page pressure (0003). The frame is still consumed
                    # by `async for`, so the WS doesn't backpressure Moonraker.
                    # Errors raised in this brief window are accepted as lost;
                    # they would have surfaced on the next event anyway.
                    if _homing_active():
                        continue
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if (msg.get("id") == 2
                            and isinstance(msg.get("result"), dict)):
                        # The subscribe RESPONSE carries the CURRENT
                        # values - seed the print state so a backend
                        # (re)started MID-print knows a print is running
                        # without waiting for the next transition (the
                        # periodic Spoolman push gates on this). Seed
                        # only, no transition handling: an end-of-print
                        # sync must come from a real transition.
                        _st = (((msg["result"].get("status") or {})
                                .get("print_stats") or {})
                               .get("state") or "")
                        if _st:
                            _print_state_last = _st
                        continue
                    method = msg.get("method")
                    if method == "notify_status_update":
                        await _on_status_update(msg.get("params") or [])
                        continue
                    if method != "notify_gcode_response":
                        continue
                    params = msg.get("params") or []
                    if not params:
                        continue
                    text = params[0]
                    if (isinstance(text, str)
                            and _SPOOL_UNMATCHED_RE.search(text)):
                        # Klipper just read a tag with no table entry -
                        # adopt it NOW instead of on the next poll tick.
                        _sweep_kick()
                    if (isinstance(text, str)
                            and _SPOOL_LEARNED_RE.search(text)):
                        # A hand assignment just learned a card UID -
                        # write it into Spoolman's card_uids now.
                        _card_kick()
                    rec = _record_notification(text)
                    if rec is not None:
                        _trace.warning("Klipper error captured: %s", rec["msg"])
                _trace.info("moonraker WS loop ended after %d messages", msg_count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _trace.warning("moonraker WS error: %s; reconnect in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
        else:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

_SPOOLMAN_TAG_SWEEP_S = 60.0

async def _spoolman_tag_sweep_loop() -> None:
    """Adopt-by-tag, unattended: in Spoolman mode a spool whose tag carries a
    Spoolman id should just appear - on boot, and when one is inserted while
    the printer runs. Klipper cannot do this itself (no HTTP), and it must
    not invent entries either, so the tag sweep lives here and only ever
    creates what Spoolman actually answers for.

    Why a poll and not an event: the backend has no channel from the
    Klipper-side tag read; it reads the same state everyone else does. The
    sweep is cheap - it costs one gated state query, and it reaches the
    Spoolman instance only for a slot that is occupied, unbound, carries a
    numeric/SM tag AND was not tried with that same tag before."""
    await asyncio.sleep(15.0)   # let Klipper connect and read its tags first
    while True:
        try:
            await asyncio.sleep(_SPOOLMAN_TAG_SWEEP_S)
            if _spoolman_lock.locked():
                continue
            res = await _spoolman_sweep_tags()
            if res.get("adopted"):
                _trace.info("spoolman tag sweep: %d spool(s) adopted",
                            res["adopted"])
            for e in res.get("errors") or []:
                _trace.warning("spoolman tag sweep: %s", e)
        except Exception as e:
            _trace.warning("spoolman tag sweep failed: %s", e)

@app.on_event("startup")
async def _start_log_listener() -> None:
    asyncio.create_task(_moonraker_log_listener())
    asyncio.create_task(_spoolman_periodic_push())
    asyncio.create_task(_spoolman_startup_push())
    asyncio.create_task(_spoolman_tag_sweep_loop())

@app.get("/api/notifications")
async def list_notifications() -> dict:
    return {"notifications": list(_notifications)}

@app.post("/api/notifications/test")
async def test_notification(payload: dict | None = None) -> dict:
    """Inject a fake Klipper-error notification - useful for verifying
    the WS bridge from the printer command line:
        curl -X POST http://127.0.0.1:7126/api/notifications/test
    """
    msg = (payload or {}).get("msg") if payload else None
    text = "!! [multiACE] " + (msg or "Test notification from /api/notifications/test")
    rec = _record_notification(text)
    return {"ok": rec is not None, "notification": rec}

@app.delete("/api/notifications/{nid}")
async def dismiss_notification(nid: int) -> dict:
    async with _notifications_lock:
        before = len(_notifications)
        keep = [n for n in _notifications if n["id"] != nid]
        _notifications.clear()
        _notifications.extend(keep)
    return {"ok": True, "dismissed": before - len(_notifications)}

@app.delete("/api/notifications")
async def clear_notifications() -> dict:
    async with _notifications_lock:
        n = len(_notifications)
        _notifications.clear()
    return {"ok": True, "cleared": n}

def _parse_port_range(spec: str) -> list[int]:
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            if lo <= hi:
                out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return out

_PLUGIN_PORTS = _parse_port_range(PLUGIN_PORT_RANGE)
_plugin_cache: dict = {"ts": 0.0, "items": []}
_plugin_lock = asyncio.Lock()

async def _probe_plugin(client: httpx.AsyncClient, port: int) -> dict | None:
    base = f"http://127.0.0.1:{port}"
    try:
        r = await client.get(f"{base}/integration-manifest", timeout=0.4)
        if r.status_code != 200:
            return None
        m = r.json()
    except Exception:
        return None
    name = str(m.get("name") or "").strip()
    if not name or not re.match(r"^[A-Za-z0-9_.-]+$", name):
        return None
    return {
        "name":     name,
        "label":    str(m.get("label") or name),
        "version":  str(m.get("version") or ""),
        "tabs":     list(m.get("tabs") or []),
        "ui_url":   str(m.get("ui_url") or "/"),
        "port":     port,
        "base_url": f"/plugin/{name}",
    }

async def _discover_plugins(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and (now - _plugin_cache["ts"]) < PLUGIN_DISCOVERY_TTL:
        return _plugin_cache["items"]
    async with _plugin_lock:
        if not force and (time.time() - _plugin_cache["ts"]) < PLUGIN_DISCOVERY_TTL:
            return _plugin_cache["items"]
        items: list[dict] = []
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(_probe_plugin(client, p) for p in _PLUGIN_PORTS),
                return_exceptions=True,
            )
        seen: set[str] = set()
        for res in results:
            if isinstance(res, dict) and res["name"] not in seen:
                seen.add(res["name"])
                items.append(res)
        _plugin_cache["ts"] = time.time()
        _plugin_cache["items"] = items
        return items

@app.get("/api/integrations")
async def list_integrations(refresh: bool = False) -> dict:
    items = await _discover_plugins(force=refresh)
    return {"plugins": items, "ports": _PLUGIN_PORTS}

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

async def _plugin_proxy_target(name: str) -> str:
    for p in await _discover_plugins():
        if p["name"] == name:
            return f"http://127.0.0.1:{p['port']}"
    raise HTTPException(status_code=404, detail=f"plugin '{name}' not registered")

@app.api_route(
    "/plugin/{name}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def plugin_proxy(name: str, path: str, request: Request) -> Response:
    target_base = await _plugin_proxy_target(name)
    url = f"{target_base}/{path}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_BY_HOP}
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.request(
                request.method, url,
                params=request.query_params,
                headers=headers,
                content=body,
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"plugin proxy: {e}")
    out_headers = {k: v for k, v in r.headers.items()
                   if k.lower() not in _HOP_BY_HOP}
    return Response(content=r.content, status_code=r.status_code,
                    headers=out_headers, media_type=r.headers.get("content-type"))

class _PluginGcode(BaseModel):
    script: str

@app.get("/api/plugin-api/state")
async def plugin_api_state() -> dict:
    """Aggregated host state - same shape as /api/state."""
    return await get_state()

@app.get("/api/plugin-api/aces")
async def plugin_api_aces() -> dict:
    """ACE list - same shape as /api/aces."""
    return await list_aces()

@app.post("/api/plugin-api/gcode")
async def plugin_api_gcode(req: _PluginGcode) -> dict:
    """Run a gcode script on the printer. Pass-through to Moonraker
    /printer/gcode/script - Moonraker enforces the print-state rules
    (busy / paused / printing) on its end."""
    script = (req.script or "").strip()
    if not script:
        raise HTTPException(status_code=400, detail="empty script")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{MOONRAKER_URL}/printer/gcode/script",
                json={"script": script},
            )
            r.raise_for_status()
            return {"ok": True, "moonraker": r.json()}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")

@app.post("/api/head-manual")
async def head_manual_set(req: HeadManual) -> dict:
    """Toggle manual/TPU bypass for a head (no ACE feed/retract/FA/RFID;
    the head sensor stays active). Persisted by the Klipper module."""
    if req.head < 0 or req.head > 3:
        raise HTTPException(status_code=400, detail="head must be 0..3")
    script = "ACE_SET_HEAD_MANUAL HEAD=%d ENABLE=%d" % (
        req.head, 1 if req.enable else 0)
    try:
        await _mr_post("/printer/gcode/script", {"script": script})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    return {"ok": True, "head": req.head, "manual": req.enable}

@app.post("/api/head-feeder")
async def head_feeder_set(req: HeadFeeder) -> dict:
    """Toggle stock-feeder mode for a head (head mode only): the head
    loads/unloads via its stock side feeder and the ACE never touches it.
    Persisted by the Klipper module."""
    if req.head < 0 or req.head > 3:
        raise HTTPException(status_code=400, detail="head must be 0..3")
    script = "ACE_SET_HEAD_FEEDER HEAD=%d ENABLE=%d" % (
        req.head, 1 if req.enable else 0)
    try:
        await _mr_post("/printer/gcode/script", {"script": script})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    return {"ok": True, "head": req.head, "feeder": req.enable}

@app.post("/api/head-ace")
async def head_ace_set(req: HeadAce) -> dict:
    """Set which ACE feeds an ACE head (head mode): the head can only
    load/swap that ACE's slots. Persisted by the Klipper module."""
    if req.head < 0 or req.head > 3:
        raise HTTPException(status_code=400, detail="head must be 0..3")
    if req.ace < 0 or req.ace > 3:
        raise HTTPException(status_code=400, detail="ace must be 0..3")
    script = "ACE_SET_HEAD_ACE HEAD=%d ACE=%d" % (req.head, req.ace)
    try:
        await _mr_post("/printer/gcode/script", {"script": script})
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code,
                            detail=f"moonraker: {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"moonraker: {e}")
    return {"ok": True, "head": req.head, "ace": req.ace}

_FIL_DB_CACHE: dict = {}

def _load_filament_db() -> dict:
    """Parse the Snapmaker firmware filament DB and return the full
    {type: {vendor: [subtype, ...]}} hierarchy (subtypes exclude the implicit
    'generic'; the 'generic' vendor is normalised to 'Generic' to match the
    display/PTC vocabulary). Reads only dict KEYS from the
    FILAMENT_PARA_CFG_DEFAULT literal via ast, so module-constant values never
    need to resolve. Cached per file mtime. Returns {} if no readable file."""
    for raw in FILAMENT_PARAMS_PATHS:
        path = raw.strip()
        if not path:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        cached = _FIL_DB_CACHE.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            continue
        cfg = None
        flat = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                if t.id == "FILAMENT_PARA_CFG_DEFAULT":
                    cfg = node.value
                elif t.id == "FILAMENT_PARA_CFG_STANDARD_04_DEFAULT":
                    flat = node.value
        if not isinstance(cfg, ast.Dict):
            # 1.6.0 flattened the DB into five per-nozzle literals with
            # '{vendor}_{material}_{sub}_{param}' keys. Rebuild the
            # type -> vendor -> subtypes hierarchy from the 04 literal
            # (the broadest one). Materials come from the 'generic_' rows
            # only, which also drops the meta keys ('hard filaments max',
            # 'process print slow'); vendor rows (Snapmaker, Polymaker)
            # carry print_temp/flow keys but no load_temp, so every key is
            # taken, not only '_load_temp'.
            if isinstance(flat, ast.Dict):
                keys = [k.value for k in flat.keys
                        if isinstance(k, ast.Constant)
                        and isinstance(k.value, str)]
                mats: list = []
                for kv in keys:
                    tok = kv.split("_")
                    if len(tok) >= 4 and tok[0] == "generic" and tok[1] \
                            and tok[1] not in mats:
                        mats.append(tok[1])
                db2: dict = {}
                for kv in keys:
                    tok = kv.split("_")
                    if len(tok) < 4 or tok[1] not in mats:
                        continue
                    vendor, mat, sub = tok[0], tok[1], tok[2]
                    if not vendor or not sub:
                        continue
                    vendor = "Generic" if vendor == "generic" else vendor
                    vendors = db2.setdefault(mat, {"Generic": []})
                    subs = vendors.setdefault(vendor, [])
                    if sub != "generic" and sub not in subs:
                        subs.append(sub)
                if db2:
                    _FIL_DB_CACHE[path] = (mtime, db2)
                    return db2
            continue
        db: dict = {}
        for k, v in zip(cfg.keys, cfg.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            name = k.value
            if name in _FIL_DB_META_KEYS or not isinstance(v, ast.Dict):
                continue
            vendors: dict = {}
            for vk, vv in zip(v.keys, v.values):
                if not (isinstance(vk, ast.Constant)
                        and isinstance(vk.value, str)
                        and vk.value.startswith("vendor_")
                        and isinstance(vv, ast.Dict)):
                    continue
                vendor = vk.value[7:]
                vendor = "Generic" if vendor == "generic" else vendor
                subs: list = []
                for sk in vv.keys:
                    if (isinstance(sk, ast.Constant)
                            and isinstance(sk.value, str)
                            and sk.value.startswith("sub_")):
                        s = sk.value[4:]
                        if s and s != "generic" and s not in subs:
                            subs.append(s)
                vendors[vendor] = subs
            db[name] = vendors or {"Generic": []}
        _FIL_DB_CACHE[path] = (mtime, db)
        return db
    return {}

@app.get("/api/materials")
async def get_materials() -> dict:
    """Return the selectable filament materials and the full
    type -> vendor -> subtypes hierarchy, sourced from the firmware filament
    DB (filament_parameters.py). Falls back to DEFAULT_MATERIALS if the
    firmware file can't be read."""
    db = _load_filament_db()
    if db:
        return {"materials": list(db.keys()), "db": db}
    return {"materials": DEFAULT_MATERIALS,
            "db": {m: {"Generic": []} for m in DEFAULT_MATERIALS}}

@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """
    Push channel for live updates. v1: simple ping every 5s plus a
    periodic ACE snapshot every 1s. Clients can rely on this for
    dashboard liveness without polling REST themselves.
    """
    await websocket.accept()
    last_seen_notif_id = 0
    try:
        last_ts = 0.0
        while True:
            now = time.time()

            for n in list(_notifications):
                if n["id"] > last_seen_notif_id:
                    try:
                        await websocket.send_json({
                            "type":       "gcode_error",
                            "ts":         n["ts"],
                            "id":         n["id"],
                            "msg":        n["msg"],
                            "raw":        n["raw"],
                            "level":      n["level"],
                            # info-level progress/done flags (tag write)
                            "replaces_info": bool(n.get("replaces_info")),
                            "ttl":        n.get("ttl") or 0,
                        })
                    except Exception:
                        return
                    last_seen_notif_id = n["id"]
            if now - last_ts >= 1.0 and not _homing_active():
                # Skip the Moonraker query while a homing/probe move is in
                # progress (0003 gate). The dashboard just pauses updates
                # for the brief homing window; it resumes on the next tick.
                try:
                    status = await _query_state()
                    payload = _parse_state(status)
                    payload["type"] = "state"
                    payload["ts"] = now
                    await websocket.send_json(payload)
                except httpx.HTTPStatusError as e:
                    # 503 = Klippy down (restart/reboot in progress). Send a
                    # clean transient state so the UI shows the "please restart"
                    # hint and keeps the last dashboard, not a raw error every
                    # second. Other HTTP errors still surface as errors.
                    if e.response is not None and e.response.status_code == 503:
                        await websocket.send_json(
                            {"type": "state", "klippy": "disconnected", "ts": now})
                    else:
                        await websocket.send_json({"type": "error", "ts": now, "error": str(e)})
                except Exception as e:
                    await websocket.send_json({"type": "error", "ts": now, "error": str(e)})
                last_ts = now
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return
    except Exception:
        return

# The app shell carries no Cache-Control of its own - StaticFiles sends only
# ETag/Last-Modified - so browsers fall back to HEURISTIC freshness and may
# reuse a stored copy for hours without ever asking. That bit the Fluidd
# camera tile hardest, because the QUERY STRING is part of the cache key:
# /multiace/?panel=1 kept being served a weeks-old index.html from disk
# while /multiace/ was long since fresh. The tile then rendered the full UI
# squeezed into a camera card - with no error anywhere, and the obvious hard
# reload never touched that entry (only DevTools' "disable cache" helped,
# and only while it was open).
# no-cache is NOT no-store: the copy stays in the cache, the browser just
# asks before using it, and with the ETag StaticFiles already sends that is
# a 304 with no body - unnoticeable on a LAN. Deliberately only the shell:
# the vendored Vue bundle and the icons change with a release, and it was
# the stale shell that hid them.
_SHELL_PATHS = {"/", "/index.html", "/app.js", "/style.css"}

@app.middleware("http")
async def _shell_revalidate(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in _SHELL_PATHS:
        response.headers["Cache-Control"] = "no-cache"
    return response

if Path(FRONTEND_DIR).is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

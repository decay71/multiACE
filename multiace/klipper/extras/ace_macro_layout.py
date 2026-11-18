#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/decay71/multiACE
# SPDX-FileCopyrightText: Copyright (c) 2026 @decay71 and contributors
"""Apply multiACE's Fluidd and Mainsail macro layout.

This module is loaded by Klipper from the [ace_macro_layout] section in
ace.cfg. Its startup worker waits for Klipper's macro list and each frontend's
Moonraker database namespace before making small, repeatable updates. It can
also be run directly with the `apply` command for a manual retry.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


DEFAULT_MOONRAKER_URL = "http://127.0.0.1:7125"
REQUEST_TIMEOUT = 5.0
WATCH_WINDOW_SECONDS = 1800.0
POLL_INTERVAL_SECONDS = 30.0
FRONTEND_NOT_READY_LIMIT = 5
# @MULTIACE_FLEXI_SHARED distributable Fluidd/Mainsail macro layout manifest, all modes
LAYOUT_FILENAME = "fluidd_macro_layout.json"
LAYOUT_ENV = "MULTIACE_FLUIDD_LAYOUT"
LAYOUT_SCHEMA = 1
MAINSAIL_LAYOUTS = (
    "mobileLayout",
    "tabletLayout1",
    "tabletLayout2",
    "desktopLayout1",
    "desktopLayout2",
    "widescreenLayout1",
    "widescreenLayout2",
    "widescreenLayout3",
)


def _group(key, name, color, macros, show_in_printing=False, show_in_pause=False):
    return {
        "key": key,
        "name": name,
        "fluidd_id": "multiace-" + key,
        "mainsail_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "https://github.com/decay71/multiACE/macro-group/" + key,
            )
        ),
        "color": color,
        "show_in_standby": True,
        "show_in_printing": show_in_printing,
        "show_in_pause": show_in_pause,
        "macros": tuple(macros),
    }


# Keep this list aligned with the public [gcode_macro] controls in ace.cfg.
# Filtering against Klipper's live object list avoids adding controls omitted
# by managed packages or removed from a user's active config.
GROUPS = (
    _group("status", "multiACE | Status", "primary",
           ("ACEG__Status", "ACEG__List"), True, True),
    _group(
        "mode-tools",
        "multiACE | Mode & Tool Selection",
        "secondary",
        (
            "SET_ACE_MODE",
            "ACEF__Mode_Normal",
            "ACEF__Mode_Multi",
            "ACEF__Mode_Head",
            "ACEA__Switch_0",
            "ACEA__Switch_1",
            "ACEA__Switch_2",
            "ACEA__Switch_3",
        ),
    ),
    _group(
        "loading",
        "multiACE | Loading",
        "success",
        (
            "ACEB__Load_0",
            "ACEB__Load_1",
            "ACEB__Load_2",
            "ACEB__Load_3",
            "ACEC__Load_T0",
            "ACEC__Load_T1",
            "ACEC__Load_T2",
            "ACEC__Load_T3",
        ),
        show_in_pause=True,
    ),
    _group(
        "unloading",
        "multiACE | Unloading",
        "warning",
        (
            "ACEC__Unload_All",
            "ACEC__Unload_T0",
            "ACEC__Unload_T1",
            "ACEC__Unload_T2",
            "ACEC__Unload_T3",
        ),
        show_in_pause=True,
    ),
    _group(
        "drying",
        "multiACE | Drying",
        "primary",
        (
            "ACED__Dry_Start_0",
            "ACED__Dry_Start_1",
            "ACED__Dry_Start_2",
            "ACED__Dry_Start_3",
            "ACED__Dry_Stop",
        ),
        show_in_printing=True,
    ),
    _group(
        "maintenance",
        "multiACE | Update & Maintenance",
        "secondary",
        ("ACEH__Update_Check", "ACEH__Update_Apply"),
    ),
)

ACE_MACRO_KEYS = {name.lower() for group in GROUPS for name in group["macros"]}
INTERNAL_MACRO_NAMES = ("INNER_RESUME",)
INTERNAL_MACRO_KEYS = {name.lower() for name in INTERNAL_MACRO_NAMES}
OWNED_FLUIDD_IDS = {group["fluidd_id"] for group in GROUPS}
OWNED_MAINSAIL_IDS = {group["mainsail_id"] for group in GROUPS}


class LayoutError(RuntimeError):
    """Moonraker or frontend data could not be read or written."""


class FrontendNotReady(LayoutError):
    """The frontend has not initialized a Moonraker database namespace yet."""


class MoonrakerClient:
    def __init__(self, base_url=DEFAULT_MOONRAKER_URL, timeout=REQUEST_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def _request(self, path, method="GET", payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 404:
                raise FrontendNotReady(detail or "Moonraker returned 404") from error
            raise LayoutError(
                "Moonraker HTTP %s: %s" % (error.code, detail or error.reason)
            ) from error
        except urllib.error.URLError as error:
            raise LayoutError("Moonraker is unavailable: %s" % error.reason) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LayoutError("Moonraker returned invalid JSON: %s" % error) from error

        if isinstance(data, dict) and data.get("error"):
            raise LayoutError("Moonraker error: " + json.dumps(data["error"], sort_keys=True))
        result = data.get("result") if isinstance(data, dict) else None
        return result

    def list_macro_names(self):
        result = self._request("/printer/objects/list")
        objects = result.get("objects") if isinstance(result, dict) else None
        if not isinstance(objects, list):
            raise LayoutError("Moonraker did not return the Klipper object list")
        names = set()
        for item in objects:
            if not isinstance(item, str) or not item.lower().startswith("gcode_macro "):
                continue
            names.add(item[len("gcode_macro "):].strip().lower())
        return names

    def get_namespace(self, namespace):
        path = "/server/database/item?" + urllib.parse.urlencode(
            {"namespace": namespace}
        )
        try:
            result = self._request(path)
        except FrontendNotReady as error:
            raise FrontendNotReady(
                "%s database namespace is not initialized" % namespace
            ) from error
        if isinstance(result, dict) and "value" in result:
            result = result["value"]
        if not isinstance(result, dict):
            raise FrontendNotReady(
                "%s database namespace has no settings yet" % namespace
            )
        return result

    def post_item(self, namespace, key, value):
        self._request(
            "/server/database/item",
            method="POST",
            payload={"namespace": namespace, "key": key, "value": value},
        )


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _case_index(items):
    index = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            index.setdefault(item["name"].lower(), item)
    return index


def active_groups(macro_names):
    active = {name.lower() for name in macro_names}
    return tuple(
        dict(group, macros=tuple(name for name in group["macros"] if name.lower() in active))
        for group in GROUPS
        if any(name.lower() in active for name in group["macros"])
    )


def _normalise_categories(value):
    """Return valid category records once, preserving their first order."""
    categories = []
    seen = set()
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        category_id = item.get("id")
        if not isinstance(category_id, str) or not category_id:
            continue
        if category_id in seen:
            continue
        seen.add(category_id)
        categories.append(copy.deepcopy(item))
    return categories


def _normalise_stored(value):
    """Return macro records once by name, preserving their first order."""
    stored = []
    seen = set()
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
        stored.append(copy.deepcopy(item))
    return stored


def is_fresh_layout(current):
    """True only when no category or macro organization exists yet."""
    state = _dict(current)
    return not _normalise_categories(state.get("categories")) and not _normalise_stored(
        state.get("stored")
    )


def merge_fluidd_macros(current, groups, macro_names=()):
    """Merge provider categories into Fluidd without moving existing categories.

    The previous implementation rebuilt the list as ``custom + owned``, which
    moved the six multiACE categories below every user category on each startup.
    Keeping original records in place and only appending genuinely missing
    provider records makes this operation idempotent.
    """
    state = copy.deepcopy(_dict(current))
    categories = _normalise_categories(state.get("categories"))
    stored = _normalise_stored(state.get("stored"))
    category_ids = {item["id"] for item in categories}
    custom_ids = category_ids - OWNED_FLUIDD_IDS

    stored_by_name = _case_index(stored)
    active_macro_names = {name.lower() for name in macro_names}
    for name in INTERNAL_MACRO_NAMES:
        if name.lower() not in active_macro_names:
            continue
        item = stored_by_name.get(name.lower())
        if item is None:
            item = {"name": name, "visible": False}
            stored.append(item)
            stored_by_name[name.lower()] = item
        item["visible"] = False

    owned_categories = {}
    for group in groups:
        assigned = False
        for name in group["macros"]:
            item = stored_by_name.get(name.lower())
            if item is None:
                item = {"name": name, "visible": True}
                stored.append(item)
                stored_by_name[name.lower()] = item
            category_id = item.get("categoryId")
            if category_id in custom_ids:
                # A user's explicit category for an ACE macro takes priority.
                continue
            item["categoryId"] = group["fluidd_id"]
            assigned = True

        existing = next(
            (item for item in categories if item["id"] == group["fluidd_id"]),
            None,
        )
        if not assigned and existing is None:
            continue
        category = copy.deepcopy(existing) if existing is not None else {}
        category["id"] = group["fluidd_id"]
        category.setdefault("name", group["name"])
        owned_categories[group["fluidd_id"]] = category

    # Keep every original category in its original position. Replace an owned
    # record in place when active; append only a genuinely missing one.
    ordered = []
    seen = set()
    for item in categories:
        category_id = item["id"]
        replacement = owned_categories.get(category_id)
        if category_id not in seen:
            ordered.append(copy.deepcopy(replacement) if replacement else item)
            seen.add(category_id)
    for group in groups:
        category_id = group["fluidd_id"]
        category = owned_categories.get(category_id)
        if category is not None and category_id not in seen:
            ordered.append(copy.deepcopy(category))
            seen.add(category_id)

    state["categories"] = ordered
    state["stored"] = stored
    state.setdefault("expanded", [])
    return state


def load_layout(path=None):
    """Load and validate the versioned Fluidd layout manifest."""
    chosen = path or os.environ.get(LAYOUT_ENV)
    manifest_path = Path(chosen) if chosen else Path(__file__).with_name(LAYOUT_FILENAME)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LayoutError("Fluidd layout manifest is unavailable: %s" % error) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LayoutError("Fluidd layout manifest is invalid JSON: %s" % error) from error
    if not isinstance(data, dict):
        raise LayoutError("Fluidd layout manifest must be an object")
    if data.get("schema") != LAYOUT_SCHEMA:
        raise LayoutError(
            "Unsupported Fluidd layout schema: %r" % (data.get("schema"),)
        )
    if not isinstance(data.get("layout_version"), str) or not data["layout_version"]:
        raise LayoutError("Fluidd layout manifest needs a layout_version")
    categories = data.get("categories")
    assignments = data.get("assignments")
    if not isinstance(categories, list) or not categories:
        raise LayoutError("Fluidd layout manifest needs categories")
    if not isinstance(assignments, list):
        raise LayoutError("Fluidd layout manifest needs assignments")

    category_ids = set()
    category_names = set()
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise LayoutError("Layout category %d is not an object" % index)
        category_id = category.get("id")
        name = category.get("name")
        if not isinstance(category_id, str) or not category_id:
            raise LayoutError("Layout category %d has no id" % index)
        if not isinstance(name, str) or not name:
            raise LayoutError("Layout category %s has no name" % category_id)
        if category_id in category_ids:
            raise LayoutError("Duplicate layout category id: %s" % category_id)
        if name.casefold() in category_names:
            raise LayoutError("Duplicate layout category name: %s" % name)
        if not isinstance(category.get("order"), int) or category["order"] < 0:
            raise LayoutError("Layout category %s has invalid order" % category_id)
        category_ids.add(category_id)
        category_names.add(name.casefold())

    macro_names = set()
    default_visibility = data.get("default_visibility", True)
    if not isinstance(default_visibility, bool):
        raise LayoutError("default_visibility must be boolean")
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            raise LayoutError("Layout assignment %d is not an object" % index)
        name = assignment.get("name")
        category = assignment.get("category")
        if not isinstance(name, str) or not name:
            raise LayoutError("Layout assignment %d has no macro name" % index)
        if name.casefold() in macro_names:
            raise LayoutError("Duplicate layout macro assignment: %s" % name)
        if category not in category_ids:
            raise LayoutError("Layout macro %s references unknown category %r" % (name, category))
        if not isinstance(assignment.get("order"), int) or assignment["order"] < 0:
            raise LayoutError("Layout macro %s has invalid order" % name)
        if not isinstance(assignment.get("visible", default_visibility), bool):
            raise LayoutError("Layout macro %s has invalid visibility" % name)
        macro_names.add(name.casefold())
    return data


def layout_supports_platform(layout, platform=None):
    """Apply explicit platform selection when the caller supplies one.

    Automatic startup has no platform detector; the manifest's platform list is
    advisory metadata and unknown macros are filtered separately. An explicit
    CLI platform is checked to prevent accidentally applying a Snapmaker layout
    to a different printer.
    """
    if not platform:
        return True
    platforms = layout.get("platforms")
    if not platforms:
        return True
    return str(platform).casefold() in {str(item).casefold() for item in platforms}


def _recommended_state(current, layout, macro_names, mode):
    if mode not in ("merge", "restore"):
        raise LayoutError("Unknown recommended-layout mode: %s" % mode)
    state = copy.deepcopy(_dict(current))
    categories = _normalise_categories(state.get("categories"))
    stored = _normalise_stored(state.get("stored"))
    active = {str(name).casefold() for name in macro_names}
    layout_categories = layout["categories"]
    layout_by_id = {item["id"]: item for item in layout_categories}
    active_assignments = [
        item for item in layout["assignments"]
        if str(item["name"]).casefold() in active
    ]
    active_ids = {item["category"] for item in active_assignments}
    active_categories = [item for item in layout_categories if item["id"] in active_ids]
    existing_by_id = {item["id"]: item for item in categories}

    if mode == "merge":
        ordered = [copy.deepcopy(item) for item in categories]
        seen = {item["id"] for item in ordered}
        for definition in active_categories:
            if definition["id"] in seen:
                continue
            ordered.append({"id": definition["id"], "name": definition["name"]})
            seen.add(definition["id"])
    else:
        # Restore the recommended categories first, but retain every custom or
        # otherwise existing category after them rather than deleting it.
        ordered = []
        seen = set()
        for definition in active_categories:
            existing = existing_by_id.get(definition["id"])
            category = copy.deepcopy(existing) if existing is not None else {}
            category["id"] = definition["id"]
            category.setdefault("name", definition["name"])
            ordered.append(category)
            seen.add(definition["id"])
        for item in categories:
            if item["id"] not in seen:
                ordered.append(copy.deepcopy(item))
                seen.add(item["id"])

    category_ids = {item["id"] for item in ordered}
    recommended_ids = set(layout_by_id)
    custom_ids = category_ids - recommended_ids
    stored_by_name = _case_index(stored)
    default_visibility = layout.get("default_visibility", True)

    for assignment in active_assignments:
        name = assignment["name"]
        item = stored_by_name.get(name.casefold())
        if item is None:
            item = {
                "name": name,
                "visible": assignment.get("visible", default_visibility),
                "categoryId": assignment["category"],
                "order": assignment["order"],
            }
            stored.append(item)
            stored_by_name[name.casefold()] = item
            continue

        current_category = item.get("categoryId")
        missing_category = current_category not in category_ids
        if mode == "restore":
            # Explicit restore resets managed assignments but never steals a
            # macro that the user placed in a custom category.
            if current_category in custom_ids:
                continue
            item["categoryId"] = assignment["category"]
            item["order"] = assignment["order"]
        elif missing_category or current_category is None:
            item["categoryId"] = assignment["category"]
            item.setdefault("order", assignment["order"])

    state["categories"] = ordered
    state["stored"] = stored
    state.setdefault("expanded", [])
    return state


def merge_recommended_layout(current, layout, macro_names=(), mode="merge"):
    """Merge or restore the distributed layout without deleting custom data."""
    if not isinstance(layout, dict):
        raise LayoutError("A loaded layout manifest is required")
    return _recommended_state(current, layout, macro_names, mode)


def backup_layout(current, path):
    """Write an exact, re-postable backup of the Fluidd macro item."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "namespace": "fluidd",
        "key": "macros",
        "value": copy.deepcopy(_dict(current)),
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(target)


def read_layout_backup(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise LayoutError("Layout backup is unavailable: %s" % error) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LayoutError("Layout backup is invalid JSON: %s" % error) from error
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict) or not isinstance(data.get("value"), dict):
        raise LayoutError("Layout backup does not contain a Fluidd macros value")
    return data["value"]


def apply_recommended(client, layout, macro_names=None, mode="merge", backup_path=None):
    """Apply the full recommended layout through the supported Moonraker item."""
    if mode not in ("merge", "restore"):
        raise LayoutError("Unknown recommended-layout mode: %s" % mode)
    if macro_names is None:
        macro_names = client.list_macro_names()
    namespace = client.get_namespace("fluidd")
    current = namespace.get("macros")
    updated = merge_recommended_layout(current, layout, macro_names, mode)
    if updated == _dict(current):
        return "current", "Fluidd recommended layout is already current"
    if mode == "restore" and not backup_path:
        raise LayoutError("Restoring the recommended layout requires --backup")
    if backup_path:
        backup_layout(current, backup_path)
    client.post_item("fluidd", "macros", updated)
    return "updated", "Fluidd recommended layout " + (
        "restored" if mode == "restore" else "merged"
    )


def restore_layout_backup(client, path):
    """Post an exact saved macro item back to Fluidd."""
    value = read_layout_backup(path)
    client.post_item("fluidd", "macros", value)
    return "updated", "Fluidd macro layout restored from backup"


def _mainsail_macro_defaults(group, position, name):
    return {
        "pos": position,
        "name": name,
        "color": "group",
        "showInStandby": group["show_in_standby"],
        "showInPrinting": group["show_in_printing"],
        "showInPause": group["show_in_pause"],
    }


def merge_mainsail_group(current, group, user_group_names):
    """Merge one multiACE group without editing user-created groups."""
    existing = copy.deepcopy(_dict(current))
    existing.setdefault("name", group["name"])
    existing.setdefault("color", group["color"])
    existing.setdefault("showInStandby", group["show_in_standby"])
    existing.setdefault("showInPrinting", group["show_in_printing"])
    existing.setdefault("showInPause", group["show_in_pause"])

    active_names = {name.lower() for name in group["macros"]}
    desired_names = active_names - user_group_names
    existing_macros = _list(existing.get("macros"))
    macros_by_name = {}
    extras = []
    for macro in existing_macros:
        if not isinstance(macro, dict) or not isinstance(macro.get("name"), str):
            extras.append(macro)
            continue
        name = macro["name"].lower()
        if name in desired_names:
            macros_by_name.setdefault(name, macro)
        elif name in ACE_MACRO_KEYS or name in INTERNAL_MACRO_KEYS:
            # Provider macros belong in one canonical group; internal controls
            # stay out of clickable provider groups.
            continue
        else:
            extras.append(macro)

    merged = []
    for position, name in enumerate(group["macros"], start=1):
        if name.lower() not in desired_names:
            continue
        macro = copy.deepcopy(macros_by_name.get(name.lower(), {}))
        macro.update({"pos": position, "name": name})
        for key, value in _mainsail_macro_defaults(group, position, name).items():
            macro.setdefault(key, value)
        merged.append(macro)

    for offset, macro in enumerate(extras, start=len(merged) + 1):
        if isinstance(macro, dict):
            macro["pos"] = offset
        merged.append(macro)
    existing["macros"] = merged
    return existing


def merge_mainsail_namespace(current, groups):
    """Return the updated Mainsail namespace and minimal database writes."""
    original = copy.deepcopy(_dict(current))
    state = copy.deepcopy(original)
    macros_state = _dict(state.get("macros"))
    groups_state = copy.deepcopy(_dict(macros_state.get("macrogroups")))
    hidden = _list(macros_state.get("hiddenMacros"))
    hidden = list(dict.fromkeys(hidden + list(INTERNAL_MACRO_NAMES)))
    macros_state["hiddenMacros"] = hidden

    user_group_names = set()
    for group_id, group_state in groups_state.items():
        if group_id in OWNED_MAINSAIL_IDS or not isinstance(group_state, dict):
            continue
        for macro in _list(group_state.get("macros")):
            if isinstance(macro, dict) and isinstance(macro.get("name"), str):
                user_group_names.add(macro["name"].lower())

    original_groups = _dict(_dict(original.get("macros")).get("macrogroups"))
    active_ids = set()
    for group in groups:
        group_id = group["mainsail_id"]
        target_names = {name.lower() for name in group["macros"]}
        if target_names - user_group_names or group_id in groups_state:
            active_ids.add(group_id)
            groups_state[group_id] = merge_mainsail_group(
                groups_state.get(group_id), group, user_group_names
            )

    # A prior multiACE layout may include a macro removed from this package.
    # Clear only stale records inside multiACE-owned groups.
    for group in GROUPS:
        group_id = group["mainsail_id"]
        if group_id in groups_state and group_id not in active_ids:
            prior = _dict(groups_state[group_id])
            prior_macros = _list(prior.get("macros"))
            prior["macros"] = [
                macro for macro in prior_macros
                if not (
                    isinstance(macro, dict)
                    and isinstance(macro.get("name"), str)
                    and macro["name"].lower() in ACE_MACRO_KEYS | INTERNAL_MACRO_KEYS
                )
            ]
            groups_state[group_id] = prior

    macros_state["macrogroups"] = groups_state
    state["macros"] = macros_state
    changed_group_ids = {
        group_id for group_id in OWNED_MAINSAIL_IDS
        if groups_state.get(group_id) != original_groups.get(group_id)
    }
    visible_group_ids = {
        group_id for group_id in OWNED_MAINSAIL_IDS
        if _list(_dict(groups_state.get(group_id)).get("macros"))
    }

    dashboard = _dict(state.get("dashboard"))
    updated_layouts = {}
    for layout_name in MAINSAIL_LAYOUTS:
        layout = dashboard.get(layout_name)
        if not isinstance(layout, list):
            continue
        updated = copy.deepcopy(layout)
        for group_id in sorted(OWNED_MAINSAIL_IDS):
            panel_name = "macrogroup_" + group_id
            has_panel = any(
                isinstance(entry, dict) and entry.get("name") == panel_name
                for entry in updated
            )
            if group_id in visible_group_ids and not has_panel:
                updated.append({"name": panel_name, "visible": True})
            elif group_id not in visible_group_ids and has_panel:
                updated = [
                    entry for entry in updated
                    if not (isinstance(entry, dict) and entry.get("name") == panel_name)
                ]
        if updated != layout:
            updated_layouts[layout_name] = updated
            dashboard[layout_name] = updated
    if dashboard:
        state["dashboard"] = dashboard

    posts = []
    for group_id in sorted(changed_group_ids):
        posts.append(("mainsail", "macros.macrogroups." + group_id, groups_state[group_id]))
    if hidden != _list(_dict(original.get("macros")).get("hiddenMacros")):
        posts.append(("mainsail", "macros.hiddenMacros", hidden))
    for layout_name, layout in updated_layouts.items():
        posts.append(("mainsail", "dashboard." + layout_name, layout))
    return state, posts


def apply_frontend(client, frontend, macro_names, layout=None):
    groups = active_groups(macro_names)
    if not groups:
        return "pending", "no active multiACE macros are visible in Klipper yet"

    try:
        namespace = client.get_namespace(frontend)
    except FrontendNotReady as error:
        return "not_ready", str(error)

    if frontend == "fluidd":
        current = namespace.get("macros")
        if layout is not None and is_fresh_layout(current):
            updated = merge_recommended_layout(current, layout, macro_names, "merge")
            detail = "Fluidd recommended macro layout applied"
        else:
            updated = merge_fluidd_macros(current, groups, macro_names)
            detail = "Fluidd macro categories applied"
        if updated != _dict(current):
            client.post_item("fluidd", "macros", updated)
            return "updated", detail
        return "current", "Fluidd layout is already current"

    _, posts = merge_mainsail_namespace(namespace, groups)
    for target_namespace, key, value in posts:
        client.post_item(target_namespace, key, value)
    mode = _dict(namespace.get("macros")).get("mode", "simple")
    if mode != "expert":
        return (
            "updated" if posts else "current",
            "Mainsail layout saved; Expert Mode must be enabled to display macro groups",
        )
    return (
        ("updated", "Mainsail macro groups applied")
        if posts
        else ("current", "Mainsail layout is already current")
    )


def apply_layout_once(client, layout=None):
    if layout is None:
        try:
            layout = load_layout()
        except LayoutError:
            # A package without the manifest still gets the provider-only
            # behavior from older releases.
            layout = None
    try:
        macro_names = client.list_macro_names()
    except Exception as error:
        detail = "Klipper macro list is unavailable: " + str(error)
        return {"fluidd": ("pending", detail), "mainsail": ("pending", detail)}

    results = {}
    for frontend in ("fluidd", "mainsail"):
        try:
            results[frontend] = apply_frontend(client, frontend, macro_names, layout)
        except Exception as error:
            results[frontend] = ("error", str(error))
    return results


def watch_layout(
    client,
    window=WATCH_WINDOW_SECONDS,
    interval=POLL_INTERVAL_SECONDS,
    logger=None,
    stop_event=None,
    layout=None,
):
    logger = logger or logging.getLogger("multiace.macro_layout")
    window = max(0.0, float(window))
    interval = max(1.0, float(interval))
    deadline = time.monotonic() + window
    pending = {"fluidd", "mainsail"}
    last_results = {}
    not_ready_counts = {frontend: 0 for frontend in pending}
    attempts = 0
    if layout is None:
        try:
            layout = load_layout()
        except LayoutError:
            layout = None

    while pending and not (stop_event is not None and stop_event.is_set()):
        attempts += 1
        results = apply_layout_once(client, layout)
        for frontend, (status, detail) in results.items():
            last_results[frontend] = (status, detail)
            if status in ("updated", "current"):
                pending.discard(frontend)
                not_ready_counts[frontend] = 0
                logger.info("%s macro layout: %s", frontend.capitalize(), detail)
            elif status == "not_ready":
                not_ready_counts[frontend] += 1
                if not_ready_counts[frontend] >= FRONTEND_NOT_READY_LIMIT:
                    pending.discard(frontend)
                    logger.info(
                        "%s database namespace did not initialize after %d checks; "
                        "stopping startup retries. Run this module with the 'apply' "
                        "command after opening the frontend.",
                        frontend.capitalize(),
                        FRONTEND_NOT_READY_LIMIT,
                    )
            else:
                not_ready_counts[frontend] = 0

        if not pending:
            return 0, last_results

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if attempts == 1 or attempts % 10 == 0:
            for frontend in sorted(pending):
                status, detail = last_results.get(
                    frontend, ("pending", "waiting for frontend data")
                )
                logger.info(
                    "Waiting for %s macro data (%s): %s",
                    frontend.capitalize(), status, detail,
                )

        delay = min(interval, remaining)
        if stop_event is not None:
            if stop_event.wait(delay):
                break
        else:
            time.sleep(delay)

    if stop_event is not None and stop_event.is_set():
        return 0, last_results

    for frontend in sorted(pending):
        status, detail = last_results.get(
            frontend, ("pending", "frontend database did not initialize")
        )
        logger.warning(
            "%s macro layout was not applied within %.0f seconds (%s): %s. "
            "Run this module with the 'apply' command to retry.",
            frontend.capitalize(), window, status, detail,
        )
    return 1, last_results


def load_config(config):
    return MacroLayoutHook(config)


class MacroLayoutHook:
    """Start the frontend readiness worker after Klipper is ready."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.url = config.get("moonraker_url", DEFAULT_MOONRAKER_URL)
        self.timeout = float(config.get("request_timeout", str(REQUEST_TIMEOUT)))
        self.window = float(config.get("watch_window", str(WATCH_WINDOW_SECONDS)))
        self.interval = float(config.get("poll_interval", str(POLL_INTERVAL_SECONDS)))
        if self.timeout <= 0 or self.window < 0 or self.interval <= 0:
            raise config.error("multiACE macro layout timing values must be positive")
        self.logger = logging.getLogger("multiace.macro_layout")
        self.enabled = config.getsection("ace").getboolean(
            "macro_layout_enabled", True
        )
        self._lock = threading.Lock()
        self._worker = None
        self._stop_event = threading.Event()
        self._restart_requested = False
        if not self.enabled:
            self.logger.info(
                "Automatic multiACE macro layout is disabled by "
                "[ace] macro_layout_enabled"
            )
            return
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "klippy:disconnect", self._handle_disconnect
        )

    def _handle_disconnect(self):
        self._stop_event.set()

    def _start_worker_locked(self):
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._run,
            name="multiACE-macro-layout",
            daemon=True,
        )
        self._worker.start()

    def _handle_ready(self):
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                if self._stop_event.is_set():
                    self._restart_requested = True
                return
            self._restart_requested = False
            self._start_worker_locked()

    def _run(self):
        self.logger.info(
            "Starting multiACE macro layout; waiting for frontend data",
        )
        try:
            client = MoonrakerClient(self.url, self.timeout)
            watch_layout(
                client,
                self.window,
                self.interval,
                self.logger,
                self._stop_event,
            )
        finally:
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                if self._restart_requested:
                    self._restart_requested = False
                    self._start_worker_locked()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Apply multiACE macro groups in Fluidd and Mainsail"
    )
    parser.add_argument(
        "command",
        choices=(
            "apply",
            "watch",
            "apply-recommended",
            "restore-recommended",
            "restore-backup",
        ),
    )
    parser.add_argument("--url", default=DEFAULT_MOONRAKER_URL)
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT)
    parser.add_argument("--watch-window", type=float, default=WATCH_WINDOW_SECONDS)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--layout", help="path to fluidd_macro_layout.json")
    parser.add_argument("--backup", help="path for a macro-layout backup")
    parser.add_argument("--platform", help="explicit platform applicability check")
    args = parser.parse_args(argv)
    client = MoonrakerClient(args.url, args.timeout)
    logger = logging.getLogger("multiace.macro_layout.cli")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "restore-backup":
        if not args.backup:
            parser.error("restore-backup requires --backup")
        status, detail = restore_layout_backup(client, args.backup)
        print("Fluidd: %s (%s)" % (status, detail))
        return 0

    try:
        layout = load_layout(args.layout)
    except LayoutError:
        if args.command in ("apply-recommended", "restore-recommended") or args.layout:
            raise
        layout = None
    if layout is not None and not layout_supports_platform(layout, args.platform):
        raise LayoutError("Layout does not support platform: %s" % args.platform)

    if args.command in ("apply-recommended", "restore-recommended"):
        mode = "restore" if args.command == "restore-recommended" else "merge"
        if mode == "restore" and not args.backup:
            parser.error("restore-recommended requires --backup")
        status, detail = apply_recommended(
            client,
            layout,
            mode=mode,
            backup_path=args.backup,
        )
        print("Fluidd: %s (%s)" % (status, detail))
        return 0 if status in ("updated", "current") else 1

    if args.command == "watch":
        code, results = watch_layout(
            client, args.watch_window, args.poll_interval, logger, layout=layout
        )
    else:
        results = apply_layout_once(client, layout)
        code = 0 if any(status in ("updated", "current") for status, _ in results.values()) else 1

    for frontend in ("fluidd", "mainsail"):
        status, detail = results[frontend]
        print("%s: %s (%s)" % (frontend.capitalize(), status, detail))
    return code


if __name__ == "__main__":
    sys.exit(main())

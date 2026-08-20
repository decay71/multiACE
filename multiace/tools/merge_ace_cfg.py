
"""Preserve user values from an existing ace.cfg when refreshing to a new
ace.cfg.default shipped by a firmware update.

Scope: only the [ace] and [ace N] sections. For each scalar key: value
line within those sections, if the user's old cfg has the same key with
a different value (or has uncommented a documented key), the user's
value wins. Everything outside [ace]/[ace N] - macros, includes, other
sections - is copied verbatim from the new default.

Multi-line / indented values (e.g. macro bodies) are never touched.
A line is considered a candidate for substitution only when it starts
at column 0 with `key: value` shape.

Usage:
    merge_ace_cfg.py <old.cfg> <new.default> <out.cfg>

Exit codes:
    0  merged successfully (notes/orphans printed to stdout, errors to stderr)
    1  argument / file I/O error
    2  parse error
"""

from __future__ import annotations

import re
import sys

SECTION_RE = re.compile(r'^\[\s*(ace(?:\s+\d+)?|ace_bg_swap|ace_tipform)\s*\]\s*$')

PRESERVE_ALL_KEYS_SECTIONS = ('ace_tipform',)

KEY_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_\-]*)\s*:\s*(.*)$')
COMMENTED_KEY_RE = re.compile(r'^#\s*([a-zA-Z_][a-zA-Z0-9_\-]*)\s*:\s*(.*)$')

def is_section_header(stripped: str) -> bool:
    return stripped.startswith('[') and stripped.endswith(']')

def section_name(stripped: str) -> str | None:
    m = SECTION_RE.match(stripped)
    return m.group(1) if m else None

def parse_user_values(path: str) -> dict[str, dict[str, str]]:
    """Return {section: {key: value}} for [ace] and [ace N] sections.
    Indented lines (= multi-line continuations or macro bodies that
    somehow ended up under our section) are ignored."""
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    with open(path, 'r', encoding='utf-8') as f:
        for raw in f:
            stripped = raw.strip()
            if is_section_header(stripped):
                sec = section_name(stripped)
                current = sec
                if current is not None:
                    out.setdefault(current, {})
                continue
            if current is None:
                continue
            if not stripped or stripped.startswith('#'):
                continue
            if raw and raw[0] in (' ', '\t'):
                continue
            m = KEY_RE.match(stripped)
            if m:
                key, val = m.group(1), m.group(2).strip()
                out[current][key] = val
    return out

def _flush_preserved(current, user_values, written, out_lines, notes):
    """Emit user keys of a PRESERVE_ALL_KEYS_SECTIONS section that the new
    default does not carry, before the section ends."""
    if current not in PRESERVE_ALL_KEYS_SECTIONS or current not in user_values:
        return
    extra = [k for k in sorted(user_values[current].keys())
             if (current, k) not in written]
    if not extra:
        return
    insert_at = len(out_lines)
    while insert_at > 0 and out_lines[insert_at - 1].strip() == '':
        insert_at -= 1
    block = []
    for k in extra:
        block.append('%s: %s\n' % (k, user_values[current][k]))
        written.add((current, k))
        notes.append('%s.%s: preserved (user key, not in new default)'
                     % (current, k))
    out_lines[insert_at:insert_at] = block

def merge(old_path: str, new_path: str, out_path: str) -> tuple[list[str], list[str]]:
    user_values = parse_user_values(old_path)
    written: set[tuple[str, str]] = set()
    emitted_sections: set[str] = set()
    notes: list[str] = []

    current: str | None = None
    out_lines: list[str] = []
    with open(new_path, 'r', encoding='utf-8') as f:
        for raw in f:
            stripped = raw.strip()

            if is_section_header(stripped):
                _flush_preserved(current, user_values, written,
                                 out_lines, notes)
                current = section_name(stripped)
                if current is not None:
                    emitted_sections.add(current)
                out_lines.append(raw)
                continue

            if current is None or current not in user_values:
                out_lines.append(raw)
                continue

            if raw and raw[0] not in (' ', '\t'):
                m = KEY_RE.match(stripped)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if key in user_values[current]:
                        user_val = user_values[current][key]
                        if user_val != val:
                            out_lines.append('%s: %s\n' % (key, user_val))
                            notes.append('%s.%s: %s -> %s' % (current, key, val, user_val))
                        else:
                            out_lines.append(raw)
                        written.add((current, key))
                        continue

                cm = COMMENTED_KEY_RE.match(stripped)
                if cm:
                    key = cm.group(1)
                    if key in user_values[current]:
                        user_val = user_values[current][key]
                        out_lines.append('%s: %s\n' % (key, user_val))
                        notes.append('%s.%s: (was commented) -> %s' % (current, key, user_val))
                        written.add((current, key))
                        continue

            out_lines.append(raw)

    _flush_preserved(current, user_values, written, out_lines, notes)

    appended_sections: list[str] = []
    for sec in sorted(user_values.keys()):
        if sec in emitted_sections:
            continue
        if not user_values[sec]:
            continue
        if out_lines and not out_lines[-1].endswith('\n'):
            out_lines.append('\n')
        if out_lines and out_lines[-1].strip():
            out_lines.append('\n')
        out_lines.append('[%s]\n' % sec)
        for key in sorted(user_values[sec].keys()):
            val = user_values[sec][key]
            out_lines.append('%s: %s\n' % (key, val))
            written.add((sec, key))
        appended_sections.append(sec)
        notes.append('[%s]: appended (no header in new default, %d user key(s))' % (
            sec, len(user_values[sec])))

    orphans: list[str] = []
    for sec in sorted(user_values.keys()):
        for key in sorted(user_values[sec].keys()):
            if (sec, key) not in written:
                orphans.append('%s.%s = %s' % (sec, key, user_values[sec][key]))

    with open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)

    return notes, orphans

def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print('usage: merge_ace_cfg.py <old.cfg> <new.default> <out.cfg>',
              file=sys.stderr)
        return 1
    old, new, out = argv[1], argv[2], argv[3]
    try:
        notes, orphans = merge(old, new, out)
    except FileNotFoundError as e:
        print('merge_ace_cfg: %s' % e, file=sys.stderr)
        return 1
    except OSError as e:
        print('merge_ace_cfg: %s' % e, file=sys.stderr)
        return 1
    except Exception as e:
        print('merge_ace_cfg: parse error: %s' % e, file=sys.stderr)
        return 2

    for n in notes:
        print('preserved: %s' % n)
    for o in orphans:
        print('orphan (not in new default): %s' % o)
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv))

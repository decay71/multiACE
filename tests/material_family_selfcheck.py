"""Self-check: 'PLA+' / 'PLA-HF' split into base type + family sub-type, in
ace.py's tag splitter and in the backend's Spoolman adopt. Run from the repo
root: python3 tests/material_family_selfcheck.py. Both functions are lifted
from their files by source so neither Klipper nor FastAPI is needed."""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lift(path, name):
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.decorator_list = []
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {}
            exec(compile(mod, path, "exec"), ns)
            return ns[name]
    raise AssertionError("%s not found in %s" % (name, path))


KNOWN = {"PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PLA-CF", "PETG-CF"}
fam = _lift("multiace/klipper/extras/ace.py", "_split_material_family")
cases = [
    ("PLA+", ("PLA", "Plus")),
    ("pla+", ("PLA", "Plus")),
    ("PLA-HF", ("PLA", "HF")),
    ("PETG-HF", ("PETG", "HF")),
    ("PLA_Silk", ("PLA", "Silk")),
    ("PLA-CF", ("", "")),      # a DB type of its own: never split
    ("PLA", ("", "")),         # exact type: handled by the caller
    ("PLAX", ("", "")),        # no boundary char: not a family
    ("Snapmaker", ("", "")),
    ("", ("", "")),
]
for tok, want in cases:
    got = fam(tok, KNOWN)
    assert got == want, (tok, got, want)

# backend: same rule, DB spelling kept
sm = _lift("multiace/web/backend/main.py", "_spoolman_material_split")
sm.__globals__["_load_filament_db"] = lambda: {k: {} for k in KNOWN}
for mat, want in [("PLA+", ("PLA", "Plus")), ("pla", ("PLA", "")),
                  ("PLA-CF", ("PLA-CF", "")), ("PETG-HF", ("PETG", "HF")),
                  ("Nylon", ("Nylon", "")), ("", ("", ""))]:
    got = sm(mat)
    assert got == want, (mat, got, want)
sm.__globals__["_load_filament_db"] = lambda: {}
assert sm("PLA+") == ("PLA+", ""), "no DB: untouched"
print("[ok] material family split, %d cases" % (len(cases) + 7))

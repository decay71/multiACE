"""Three-way line merge of the config tab (app.js cfgMerge3), run in node."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = (Path(__file__).resolve().parents[2] / "multiace" / "web" / "frontend"
       / "app.js")


def _fn(src, name):
    start = src.index("    function %s(" % name)
    end = src.index("\n    }\n", start) + len("\n    }\n")
    return src[start:end]


def _merge(base, mine, theirs):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    src = APP.read_text(encoding="utf-8")
    cells = re.search(r"const CFG_DIFF_MAX_CELLS = [^;]+;", src).group(0)
    js = "\n".join([cells] + [_fn(src, n) for n in
                              ("cfgDiffOps", "cfgHunks", "cfgMerge3")])
    js += "\nconst a = JSON.parse(process.argv[1]);\n" \
          "process.stdout.write(JSON.stringify(cfgMerge3(a[0], a[1], a[2])));"
    out = subprocess.run([node, "-e", js, json.dumps([base, mine, theirs])],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


BASE = "[ace]\na: 1\nb: 2\n#c: 0\nd: 4\n"


def test_edit_is_carried_onto_a_file_that_gained_lines():
    mine = BASE.replace("#c: 0", "c: 9")
    theirs = BASE.replace("[ace]\n", "[ace]\nnew: 7\n") + "[macro]\nx\n"
    assert _merge(BASE, mine, theirs) == \
        "[ace]\nnew: 7\na: 1\nb: 2\nc: 9\nd: 4\n[macro]\nx\n"


def test_added_line_is_carried_over():
    mine = BASE.replace("d: 4\n", "d: 4\nlean_flush: true\n")
    theirs = BASE.replace("a: 1", "a: 5")
    assert _merge(BASE, mine, theirs) == \
        "[ace]\na: 5\nb: 2\n#c: 0\nd: 4\nlean_flush: true\n"


def test_same_line_changed_on_both_sides_is_a_conflict():
    assert _merge(BASE, BASE.replace("b: 2", "b: 3"),
                  BASE.replace("b: 2", "b: 8")) is None


def test_identical_edit_on_both_sides_counts_once():
    both = BASE.replace("b: 2", "b: 3")
    assert _merge(BASE, both, both) == both


def test_no_own_edit_gives_the_other_file():
    theirs = BASE.replace("a: 1", "a: 5")
    assert _merge(BASE, BASE, theirs) == theirs

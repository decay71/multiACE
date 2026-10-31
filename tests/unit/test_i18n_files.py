"""The i18n JSON files: a duplicate key silently drops the first group
(json.loads keeps the last one), which is how two different `ui.queue`
groups once shadowed each other and the command-queue labels went missing."""
import json
from pathlib import Path

import pytest

I18N = Path(__file__).resolve().parents[2] / "multiace" / "i18n"


def _no_duplicates(pairs):
    seen = set()
    for k, _ in pairs:
        assert k not in seen, f"duplicate key {k!r}"
        seen.add(k)
    return dict(pairs)


@pytest.mark.parametrize("lang", ["en", "de", "zh"])
def test_no_duplicate_keys_in_any_object(lang):
    json.loads((I18N / f"{lang}.json").read_text("utf-8"),
               object_pairs_hook=_no_duplicates)

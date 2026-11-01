"""The app shell: the 3D preview files take the same content-hash path as
the rest of the shell, so an update is never masked by a cached copy."""
import re

import pytest

from .test_mock_mode import _load_main  # noqa: F401


@pytest.fixture
def shell(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=True)
    with TestClient(main.app) as c:
        yield main, c


def test_the_preview_script_carries_its_hash(shell):
    main, _ = shell
    html = main._render_shell()
    h = main._asset_hash("gcode_preview.js")
    assert h and f'src="gcode_preview.js?v={h}"' in html


def test_the_worker_hash_is_injected_for_the_page(shell):
    main, _ = shell
    html = main._render_shell()
    m = re.search(r'window\.MA_ASSET_V = (\{.*?\});', html)
    assert m
    injected = __import__("json").loads(m.group(1))
    assert injected["gpreview"] == main._asset_hash("gcode_preview_worker.js")
    assert injected["gpreview"]


def test_a_current_hash_is_cached_for_good_and_anything_else_revalidates(shell):
    main, c = shell
    h = main._asset_hash("gcode_preview_worker.js")
    r = c.get(f"/gcode_preview_worker.js?v={h}")
    assert "immutable" in r.headers["cache-control"]
    assert c.get("/gcode_preview_worker.js?v=3").headers["cache-control"] == "no-cache"
    assert c.get("/gcode_preview.js").headers["cache-control"] == "no-cache"

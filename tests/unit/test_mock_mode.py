"""Mock mode: the web backend serves fixtures instead of talking to Moonraker.

Runs the real FastAPI app with the printer paths pointed at a temp dir, so
the debug endpoints' gating and the fixture contract can be pinned.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "multiace" / "web" / "backend"
FIXTURES = ROOT / "tests" / "fixtures"


def _load_main(monkeypatch, tmp_path, *, mock, state_file=None):
    """Import main.py fresh: it reads its env at import time."""
    cfg = tmp_path / "ace.cfg"
    cfg.write_text("[ace]\nace_device_count: 1\n", encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_MOCK_DIR", str(FIXTURES))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    # Keep the suite hermetic against a dev-ui shell that left these set.
    if state_file:
        monkeypatch.setenv("MULTIACE_MOCK_STATE_FILE", state_file)
    else:
        monkeypatch.delenv("MULTIACE_MOCK_STATE_FILE", raising=False)
    if mock:
        monkeypatch.setenv("MULTIACE_MOCK_MODE", "1")
    else:
        monkeypatch.delenv("MULTIACE_MOCK_MODE", raising=False)
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("main", None)
    return importlib.import_module("main")


@pytest.fixture
def mock_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=True)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def live_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)
    with TestClient(main.app) as c:
        yield c


class TestFixtures:
    @pytest.mark.parametrize("name", ["mock_state.json", "mock_state_head.json",
                                      "mock_version.json"])
    def test_fixture_is_valid_json(self, name):
        assert isinstance(json.loads((FIXTURES / name).read_text("utf-8")), dict)

    def test_sample_gcode_is_small_and_has_four_tools(self):
        p = FIXTURES / "sample_4color.gcode"
        assert p.stat().st_size < 1_000_000
        text = p.read_text("utf-8")
        for t in ("T0", "T1", "T2", "T3"):
            assert f"\n{t}\n" in text or f"\n{t} " in text


class TestMockMode:
    def test_health_reports_mock_mode(self, mock_client):
        assert mock_client.get("/api/health").json()["mock_mode"] is True

    def test_state_is_served_from_fixture(self, mock_client):
        j = mock_client.get("/api/state").json()
        assert j["mock"] is True
        assert j["mode"] == "multi"
        assert len(j["aces"]) == j["device_count"]

    def test_state_file_env_selects_head_fixture(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        main = _load_main(monkeypatch, tmp_path, mock=True,
                          state_file="mock_state_head.json")
        with TestClient(main.app) as c:
            assert c.get("/api/state").json()["mode"] == "head"

    def test_version_is_served_from_fixture(self, mock_client):
        j = mock_client.get("/api/version").json()
        assert j["mock"] is True
        assert j["web"] == "test"
        assert j["printer"]["device_name"]

    def test_query_param_forces_mock_off(self, mock_client):
        # ?mock=0 skips the fixture and goes to the (unreachable) printer.
        assert mock_client.get("/api/state?mock=0").json().get("mock") is not True

    def test_sample_gcode_is_served(self, mock_client):
        r = mock_client.get("/api/debug/sample-gcode")
        assert r.status_code == 200
        assert "total layer number" in r.text

    def test_simulate_records_and_clears_notifications(self, mock_client):
        r = mock_client.post("/api/debug/simulate",
                             json={"event": "gcode_error", "msg": "boom"})
        assert r.status_code == 200
        notes = mock_client.get("/api/notifications").json()["notifications"]
        assert any("boom" in n["msg"] for n in notes)
        mock_client.post("/api/debug/simulate", json={"event": "clear"})
        assert mock_client.get("/api/notifications").json()["notifications"] == []

    def test_simulate_rejects_unknown_event(self, mock_client):
        r = mock_client.post("/api/debug/simulate", json={"event": "nope"})
        assert r.status_code == 400


class TestDebugEndpointsAreGated:
    """A debug endpoint that leaks into production is the failure to pin."""

    def test_health_reports_mock_off(self, live_client):
        assert live_client.get("/api/health").json()["mock_mode"] is False

    def test_simulate_refused_outside_mock_mode(self, live_client):
        r = live_client.post("/api/debug/simulate",
                             json={"event": "gcode_error"})
        assert r.status_code == 403

    def test_sample_gcode_refused_outside_mock_mode(self, live_client):
        assert live_client.get("/api/debug/sample-gcode").status_code == 403

    def test_query_param_cannot_enable_mock(self, live_client):
        for path in ("/api/state?mock=1", "/api/version?mock=1"):
            assert live_client.get(path).json().get("mock") is not True

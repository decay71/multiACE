"""/api/history*: the join with Moonraker, the mock fixture and the
debug-gated edits."""
import json
from pathlib import Path

import pytest

from .test_mock_mode import FIXTURES, _load_main  # noqa: F401
from .test_mock_mode import mock_client  # noqa: F401


@pytest.fixture
def live(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(main, "MULTIACE_DATA_DIR", str(data))
    monkeypatch.setattr(main, "_DEBUG_FLAG_PATH", tmp_path / "debug")
    with TestClient(main.app) as c:
        yield main, c, data


class TestMockFixture:
    def test_the_mock_history_matches_the_join_shape(self):
        data = json.loads((FIXTURES / "mock_history.json").read_text("utf-8"))
        for row in data["jobs"]:
            assert {"filename", "start_time", "status", "multiace",
                    "meta"} <= set(row)
            assert row["multiace"]["id"] == row["id"]

    def test_mock_mode_serves_it(self, mock_client):
        j = mock_client.get("/api/history").json()
        assert j["mock"] is True and j["jobs"]

    def test_mock_limit_applies(self, mock_client):
        assert len(mock_client.get("/api/history?limit=1").json()["jobs"]) == 1

    def test_mock_detail_and_404(self, mock_client):
        assert mock_client.get("/api/history/mock-job-0").status_code == 200
        assert mock_client.get("/api/history/nope").status_code == 404


class TestLive:
    def test_list_joins_moonraker_with_our_record(self, live, monkeypatch):
        main, c, data = live
        main._require_history().append_record(str(data), {
            "id": "j1", "filename": "a.gcode", "start_time": 1000.0,
            "result": "completed", "plan": "optimize"})

        async def fake(limit):
            return [{"job_id": "0001", "filename": "a.gcode",
                     "start_time": 1010.0, "status": "completed",
                     "total_duration": 60}]
        monkeypatch.setattr(main, "_moonraker_history", fake)
        jobs = c.get("/api/history").json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["multiace"]["plan"] == "optimize"

    def test_moonraker_down_still_lists_our_records(self, live, monkeypatch):
        main, c, data = live
        main._require_history().append_record(str(data), {
            "id": "j1", "filename": "a.gcode", "start_time": 1000.0,
            "result": "completed"})

        async def boom(path):
            raise RuntimeError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        jobs = c.get("/api/history").json()["jobs"]
        assert [j["multiace"]["id"] for j in jobs] == ["j1"]

    def test_detail_returns_the_record(self, live):
        main, c, data = live
        main._require_history().append_record(str(data), {
            "id": "j1", "filename": "a.gcode", "start_time": 1.0})
        assert c.get("/api/history/j1").json()["multiace"]["id"] == "j1"
        assert c.get("/api/history/zzz").status_code == 404

    def test_edits_need_debug_mode(self, live):
        main, c, data = live
        main._require_history().append_record(str(data), {
            "id": "j1", "filename": "a.gcode", "start_time": 1.0})
        assert c.delete("/api/history/j1").status_code == 403
        assert c.post("/api/history/clear").status_code == 403

    def test_delete_and_clear_with_debug_mode(self, live):
        main, c, data = live
        main._DEBUG_FLAG_PATH.touch()
        hist = main._require_history()
        for i in ("j1", "j2"):
            hist.append_record(str(data), {
                "id": i, "filename": "a.gcode", "start_time": 1.0})
        assert c.delete("/api/history/j1").json()["deleted"] == "j1"
        assert c.delete("/api/history/j1").status_code == 404
        assert c.post("/api/history/clear").json()["ok"] is True
        assert hist.load_records(str(data)) == []

    def test_a_missing_library_is_a_503(self, live, monkeypatch):
        main, c, _ = live
        monkeypatch.setattr(main, "_job_history_mod", None)
        monkeypatch.setattr(Path, "is_file", lambda self: False)
        assert c.get("/api/history").status_code == 503


class TestReprint:
    def test_mock_mode_acknowledges(self, mock_client):
        r = mock_client.post("/api/history/reprint", json={"filename": "a.gcode"})
        assert r.json() == {"ok": True, "filename": "a.gcode", "mock": True}

    def test_a_blank_filename_is_rejected(self, mock_client):
        assert mock_client.post("/api/history/reprint",
                                json={"filename": "  "}).status_code == 400

    def test_it_starts_the_file_through_moonraker(self, live, monkeypatch):
        main, c, _ = live
        sent = []

        async def fake(path, body=None, timeout=30.0):
            sent.append((path, body))
            return {}
        monkeypatch.setattr(main, "_mr_post", fake)
        assert c.post("/api/history/reprint",
                      json={"filename": "a.gcode"}).json()["ok"] is True
        assert sent == [("/printer/print/start", {"filename": "a.gcode"})]

    def test_a_moonraker_error_is_passed_on(self, live, monkeypatch):
        import httpx
        main, c, _ = live

        async def boom(path, body=None, timeout=30.0):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_post", boom)
        r = c.post("/api/history/reprint", json={"filename": "a.gcode"})
        assert r.status_code == 502


class TestSwapStats:
    def test_the_mock_fixture_has_stats(self, mock_client):
        j = mock_client.get("/api/history/stats/swaps").json()
        assert j["stats"]["first_load"]["n"] == 3
        assert j["min_samples"] >= 1

    def test_live_stats_come_from_the_records(self, live):
        main, c, data = live
        hist = main._require_history()
        for i in range(2):
            hist.append_record(str(data), {
                "id": f"j{i}", "filename": "a.gcode", "start_time": float(i),
                "swaps": [{"kind": "same_ace", "seconds": 100 + i * 20}]})
        s = c.get("/api/history/stats/swaps").json()["stats"]["same_ace"]
        assert s["n"] == 2 and s["median_s"] == 110
        assert s["calibrated"] is False


class TestLibraryLookup:
    def test_the_copy_next_to_main_py_wins(self, live, tmp_path, monkeypatch):
        import shutil
        main, _, _ = live
        shutil.copy(Path(main.__file__).resolve().parents[2] / "tools"
                    / "job_history.py", tmp_path / "job_history.py")
        monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
        monkeypatch.setattr(main, "_job_history_mod", None)
        assert Path(main._require_history().__file__) == tmp_path / "job_history.py"

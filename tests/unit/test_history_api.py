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

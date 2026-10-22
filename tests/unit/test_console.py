"""Console ring buffer and the read-only /api/console-logs endpoint."""
import asyncio
import time

import httpx
import pytest

from .test_mock_mode import _load_main, mock_client, live_client  # noqa: F401


@pytest.fixture
def main(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path, mock=False)
    m._console_lines.clear()
    return m


class TestKind:
    @pytest.mark.parametrize("text,kind", [
        ("G28", "command"),
        ("// [multiACE] head 0 loaded", "response"),
        ("!! Move out of range", "error"),
        ("// !! Timer too close", "error"),
        ("Error: something broke", "error"),
    ])
    def test_kind(self, main, text, kind):
        assert main._console_kind(text) == kind


class TestRecord:
    def test_ids_increase_and_blank_lines_are_skipped(self, main):
        a = main._record_console_line("G28\n")
        b = main._record_console_line("   ")
        c = main._record_console_line("M105")
        assert b is None and a["msg"] == "G28" and c["id"] == a["id"] + 1

    def test_colour_markup_is_stripped(self, main):
        line = main._record_console_line(
            '<span style="color:#90EE90">ACE 1: Connected</span> &amp; ready')
        assert line["msg"] == "ACE 1: Connected & ready"
        assert main._record_console_line("<br>") is None

    def test_non_strings_are_ignored(self, main):
        assert main._record_console_line(None) is None
        assert main._record_console_line(5) is None

    def test_buffer_is_bounded(self, main):
        for i in range(main.CONSOLE_BUFFER_MAX + 20):
            main._record_console_line(f"line {i}")
        assert len(main._console_lines) == main.CONSOLE_BUFFER_MAX
        assert main._console_lines[-1]["msg"] == f"line {main.CONSOLE_BUFFER_MAX + 19}"


class TestSeed:
    def test_backfills_from_moonraker_history(self, main, monkeypatch):
        async def fake(path):
            assert path.startswith("/server/gcode_store")
            return {"result": {"gcode_store": [
                {"message": "G28", "time": 10.0, "type": "command"},
                {"message": "// ok", "time": 11.0, "type": "response"},
                {"message": "", "time": 12.0, "type": "response"}]}}
        monkeypatch.setattr(main, "_mr_get", fake)
        asyncio.run(main._seed_console_from_gcode_store())
        assert [(l["msg"], l["kind"]) for l in main._console_lines] == [
            ("G28", "command"), ("// ok", "response")]

    def test_moonraker_down_leaves_the_buffer_empty(self, main, monkeypatch):
        async def boom(path):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        asyncio.run(main._seed_console_from_gcode_store())
        assert not main._console_lines


class TestSeedWithLiveLines:
    """On a real printer lines arrive constantly, so live lines are almost
    always in the buffer before anyone opens the console."""

    def test_history_is_still_loaded_and_not_duplicated(self, main, monkeypatch):
        main._record_console_line("// early live line")   # also in the history
        time.sleep(0.05)   # a clearly earlier clock tick than the request

        async def fake(path):
            main._record_console_line("// arrived during the request")
            return {"result": {"gcode_store": [
                {"message": "G28", "time": 1.0, "type": "command"},
                {"message": "// early live line", "time": 2.0, "type": "response"}]}}
        monkeypatch.setattr(main, "_mr_get", fake)
        asyncio.run(main._seed_console_from_gcode_store())
        assert [l["msg"] for l in main._console_lines] == [
            "G28", "// early live line", "// arrived during the request"]
        ids = [l["id"] for l in main._console_lines]
        assert ids == sorted(ids) and len(set(ids)) == 3

    def test_it_is_seeded_only_once(self, main, monkeypatch):
        calls = []

        async def fake(path):
            calls.append(path)
            return {"result": {"gcode_store": [{"message": "G28", "time": 1.0}]}}
        monkeypatch.setattr(main, "_mr_get", fake)
        asyncio.run(main._seed_console_from_gcode_store())
        asyncio.run(main._seed_console_from_gcode_store())
        assert len(calls) == 1 and len(main._console_lines) == 1

    def test_a_failed_seed_is_retried_and_keeps_the_live_lines(self, main, monkeypatch):
        main._record_console_line("// live")

        async def boom(path):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        asyncio.run(main._seed_console_from_gcode_store())
        assert [l["msg"] for l in main._console_lines] == ["// live"]
        assert main._console_seeded is False


class TestEndpoint:
    def _client(self, main):
        from fastapi.testclient import TestClient
        return TestClient(main.app)

    def test_since_id_and_lines_limit(self, main, monkeypatch):
        for i in range(5):
            main._record_console_line(f"M{i}")
        with self._client(main) as c:
            allr = c.get("/api/console-logs?lines=3").json()["lines"]
            newer = c.get(f"/api/console-logs?since_id={allr[0]['id']}").json()["lines"]
        assert [l["msg"] for l in allr] == ["M2", "M3", "M4"]
        assert [l["msg"] for l in newer] == ["M3", "M4"]

    def test_empty_buffer_seeds_from_moonraker_once(self, main, monkeypatch):
        calls = []
        async def fake(path):
            calls.append(path)
            return {"result": {"gcode_store": [{"message": "G28", "time": 1.0}]}}
        monkeypatch.setattr(main, "_mr_get", fake)
        with self._client(main) as c:
            j = c.get("/api/console-logs").json()
            c.get("/api/console-logs")
        assert [l["msg"] for l in j["lines"]] == ["G28"] and len(calls) == 1

    def test_there_is_no_write_endpoint(self, main):
        with self._client(main) as c:
            assert c.post("/api/console", json={"script": "G28"}).status_code in (404, 405)


class TestMockMode:
    def test_served_from_fixture(self, mock_client):
        j = mock_client.get("/api/console-logs").json()
        assert j["mock"] is True and j["lines"]
        newest = j["lines"][-1]["id"]
        assert mock_client.get(f"/api/console-logs?since_id={newest}").json()["lines"] == []

    def test_query_param_cannot_enable_mock_on_a_real_printer(self, live_client):
        assert live_client.get("/api/console-logs?mock=1").json().get("mock") is not True

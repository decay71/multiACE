"""Restart Firmware / Restart Printer: a fixed list of behaviours, refused
while a print runs and under a managed install.

Moonraker is replaced by stubs; the calls the endpoint would make are
captured instead.
"""
import httpx
import pytest

from .test_mock_mode import _load_main, mock_client  # noqa: F401


@pytest.fixture
def main(monkeypatch, tmp_path):
    return _load_main(monkeypatch, tmp_path, mock=False)


@pytest.fixture
def printer(main, monkeypatch):
    class P:
        state = "standby"
        posts = []
        get_error = None
        post_error = None
    P.posts = []

    async def fake_get(path):
        assert path == "/printer/objects/query?print_stats"
        if P.get_error:
            raise P.get_error
        return {"result": {"status": {"print_stats": {"state": P.state}}}}

    async def fake_post(path, body=None, timeout=30.0):
        if P.post_error:
            raise P.post_error
        P.posts.append(path)
        return {"result": "ok"}

    monkeypatch.setattr(main, "_mr_get", fake_get)
    monkeypatch.setattr(main, "_mr_post", fake_post)
    return P


@pytest.fixture
def client(main):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def restart(client, behavior):
    return client.post("/api/restart", json={"behavior": behavior})


def status_error(code):
    req = httpx.Request("GET", "http://x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))


class TestBehaviours:
    @pytest.mark.parametrize("behavior,path", [
        ("firmware", "/printer/firmware_restart"),
        ("printer", "/machine/reboot"),
    ])
    def test_each_behaviour_is_one_moonraker_call(self, client, printer, behavior, path):
        r = restart(client, behavior)
        assert r.status_code == 200 and r.json() == {"ok": True, "behavior": behavior}
        assert printer.posts == [path]

    def test_behaviour_is_case_and_space_insensitive(self, client, printer):
        assert restart(client, " Firmware ").status_code == 200
        assert printer.posts == ["/printer/firmware_restart"]

    @pytest.mark.parametrize("behavior", ["", "klipper", "reboot", "poweroff",
                                          "/machine/shutdown", "firmware; x"])
    def test_anything_else_is_refused_and_sends_nothing(self, client, printer, behavior):
        assert restart(client, behavior).status_code == 400
        assert printer.posts == []


class TestPrintGuard:
    @pytest.mark.parametrize("state", ["printing", "paused"])
    @pytest.mark.parametrize("behavior", ["firmware", "printer"])
    def test_blocked_while_a_print_is_running_or_paused(
            self, client, printer, state, behavior):
        printer.state = state
        r = restart(client, behavior)
        assert r.status_code == 409 and "print" in r.json()["detail"]
        assert printer.posts == []

    @pytest.mark.parametrize("state", ["standby", "complete", "cancelled", "error"])
    def test_allowed_when_nothing_is_printing(self, client, printer, state):
        printer.state = state
        assert restart(client, "firmware").status_code == 200

    def test_a_downed_klipper_does_not_block_a_firmware_restart(self, client, printer):
        # 503: Klipper is not running, which is exactly when it is needed.
        printer.get_error = status_error(503)
        assert restart(client, "firmware").status_code == 200
        assert printer.posts == ["/printer/firmware_restart"]

    def test_an_unreachable_moonraker_blocks_it_rather_than_guessing(self, client, printer):
        printer.get_error = httpx.ConnectError("down")
        assert restart(client, "printer").status_code == 502
        assert printer.posts == []

    def test_another_error_status_blocks_it(self, client, printer):
        printer.get_error = status_error(500)
        assert restart(client, "printer").status_code == 502
        assert printer.posts == []

    def test_the_state_is_read_fresh_not_from_the_homing_cache(self, main, client, printer):
        main._LAST_PRINT_STATUS = {"print_stats": {"state": "standby"}}
        main._LAST_PRINT_STATUS_TS = 9e12
        printer.state = "printing"
        assert restart(client, "firmware").status_code == 409


class TestManaged:
    def test_refused_when_the_platform_owns_restarts(self, main, client, printer, monkeypatch):
        monkeypatch.setattr(main, "MULTIACE_MANAGED", True)
        r = restart(client, "firmware")
        assert r.status_code == 409 and "managed" in r.json()["detail"]
        assert printer.posts == []


class TestTransport:
    @pytest.mark.parametrize("err", [httpx.ReadTimeout("t"), httpx.ReadError("r"),
                                     httpx.RemoteProtocolError("p")])
    def test_a_dropped_connection_is_what_a_restart_looks_like(self, client, printer, err):
        printer.post_error = err
        assert restart(client, "printer").status_code == 200

    def test_a_connection_that_never_opened_is_a_failure(self, client, printer):
        printer.post_error = httpx.ConnectError("no route")
        assert restart(client, "firmware").status_code == 502

    def test_a_moonraker_error_status_is_passed_on(self, client, printer):
        printer.post_error = status_error(500)
        assert restart(client, "firmware").status_code == 500


class TestMockMode:
    def test_never_reaches_moonraker(self, mock_client, monkeypatch):
        import sys
        m = sys.modules["main"]

        async def boom(*a, **k):
            raise AssertionError("mock mode must not contact Moonraker")
        monkeypatch.setattr(m, "_mr_get", boom)
        monkeypatch.setattr(m, "_mr_post", boom)
        r = restart(mock_client, "printer")
        assert r.status_code == 200 and r.json()["mock"] is True

    def test_unknown_behaviours_are_refused_in_mock_mode_too(self, mock_client):
        assert restart(mock_client, "poweroff").status_code == 400

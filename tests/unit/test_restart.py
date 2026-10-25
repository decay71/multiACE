"""Restart Firmware / Restart Printer: a fixed list of behaviours, refused
while a print runs and under a managed install.

Moonraker is replaced by stubs; the calls the endpoint would make are
captured instead.
"""
import asyncio
import os

import httpx
import pytest

from .test_mock_mode import _load_main, mock_client  # noqa: F401


@pytest.fixture
def main(monkeypatch, tmp_path):
    # Never root by default, so a run as root next to a local Moonraker can not
    # reach the local reboot fallback. The rebooter fixture opts in.
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
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
        assert r.status_code == 200 and r.json() == {
            "ok": True, "behavior": behavior, "via": "moonraker"}
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


class TestLocalReboot:
    """The Snapmaker U1 has neither sudo nor systemd, so Moonraker's own reboot
    ("sudo systemctl reboot") always fails there. Where the backend runs as
    root on the printer itself it runs reboot directly, and only then."""

    SUDO_FAIL = None

    @pytest.fixture
    def rebooter(self, main, printer, monkeypatch):
        class R:
            ran = []
            rc = 0
            out = b""
            spawn_error = None
            hang = False
        R.ran = []
        printer.post_error = status_error(500)

        class Proc:
            returncode = None

            async def communicate(self_inner):
                if R.hang:
                    await asyncio.sleep(60)
                self_inner.returncode = R.rc
                return R.out, None

        async def fake_exec(*argv, **kw):
            if R.spawn_error:
                raise R.spawn_error
            R.ran.append((argv, kw.get("start_new_session")))
            return Proc()

        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(main.shutil, "which", lambda n, path=None: "/usr/sbin/reboot" if n == "reboot" else None)
        monkeypatch.setattr(main, "MOONRAKER_URL", "http://127.0.0.1:7125")
        monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(main.asyncio, "wait_for", _fast_wait_for(main.asyncio.wait_for))
        return R

    def test_a_failing_moonraker_reboot_falls_back_to_reboot(self, client, rebooter):
        r = restart(client, "printer")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "behavior": "printer", "via": "local"}
        assert rebooter.ran == [(("/usr/sbin/reboot",), True)]

    def test_a_working_moonraker_reboot_is_left_alone(self, client, printer, rebooter):
        printer.post_error = None
        r = restart(client, "printer")
        assert r.json()["via"] == "moonraker" and rebooter.ran == []
        assert printer.posts == ["/machine/reboot"]

    def test_the_firmware_restart_never_reboots_the_machine(self, client, rebooter):
        assert restart(client, "firmware").status_code == 500
        assert rebooter.ran == []

    def test_not_root_reports_the_failure_and_runs_nothing(
            self, client, rebooter, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
        assert restart(client, "printer").status_code == 500
        assert rebooter.ran == []

    def test_a_platform_without_geteuid_never_does(self, client, rebooter, monkeypatch):
        # Windows, where a dev backend runs against a real printer.
        monkeypatch.delattr(os, "geteuid", raising=False)
        assert restart(client, "printer").status_code == 500
        assert rebooter.ran == []

    @pytest.mark.parametrize("url", ["http://192.168.178.82:7125",
                                     "http://printer.local:7125",
                                     "https://example.org"])
    def test_a_remote_moonraker_never_reboots_this_machine(
            self, main, client, rebooter, monkeypatch, url):
        # A root backend on some other machine must not reboot itself.
        monkeypatch.setattr(main, "MOONRAKER_URL", url)
        assert restart(client, "printer").status_code == 500
        assert rebooter.ran == []

    @pytest.mark.parametrize("url", ["http://127.0.0.1:7125", "http://localhost:7125",
                                     "http://[::1]:7125"])
    def test_a_local_moonraker_qualifies(self, main, client, rebooter, monkeypatch, url):
        monkeypatch.setattr(main, "MOONRAKER_URL", url)
        assert restart(client, "printer").json()["via"] == "local"

    def test_no_reboot_binary_reports_the_failure(self, main, client, rebooter, monkeypatch):
        monkeypatch.setattr(main.shutil, "which", lambda n, path=None: None)
        r = restart(client, "printer")
        assert r.status_code == 500 and rebooter.ran == []

    def test_reboot_is_found_in_sbin_when_the_service_path_lacks_it(
            self, main, client, rebooter, monkeypatch):
        # The U1's init script starts the service with PATH=.local/bin:/usr/bin:/bin
        # and reboot is in /usr/sbin: a plain which() finds nothing (seen on a
        # real printer, where the first attempt did not reboot).
        def which(name, path=None):
            if name == "reboot" and path and "/usr/sbin" in path:
                return "/usr/sbin/reboot"
            return None
        monkeypatch.setattr(main.shutil, "which", which)
        r = restart(client, "printer")
        assert r.json()["via"] == "local"
        assert rebooter.ran == [(("/usr/sbin/reboot",), True)]

    def test_a_reboot_that_fails_says_so(self, client, rebooter):
        rebooter.rc, rebooter.out = 1, b"reboot: must be superuser\n"
        r = restart(client, "printer")
        assert r.status_code == 500
        assert r.json()["detail"] == "reboot failed (rc=1): reboot: must be superuser"

    def test_a_reboot_that_cannot_start_says_so(self, client, rebooter):
        rebooter.spawn_error = PermissionError("denied")
        r = restart(client, "printer")
        assert r.status_code == 500 and "reboot: denied" in r.json()["detail"]

    def test_a_reboot_that_is_still_running_is_the_machine_going_down(
            self, client, rebooter):
        rebooter.hang = True
        r = restart(client, "printer")
        assert r.status_code == 200 and r.json()["via"] == "local"

    def test_the_print_guard_comes_first(self, client, printer, rebooter):
        printer.state = "printing"
        assert restart(client, "printer").status_code == 409
        assert rebooter.ran == []

    def test_managed_installs_come_first(self, main, client, rebooter, monkeypatch):
        monkeypatch.setattr(main, "MULTIACE_MANAGED", True)
        assert restart(client, "printer").status_code == 409
        assert rebooter.ran == []

    def test_mock_mode_never_reboots(self, mock_client, monkeypatch):
        import sys
        m = sys.modules["main"]
        ran = []

        async def boom(*a, **k):
            ran.append(a)
            raise AssertionError("mock mode must not reboot anything")
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(m.asyncio, "create_subprocess_exec", boom)
        r = restart(mock_client, "printer")
        assert r.status_code == 200 and r.json()["mock"] is True and ran == []


def _fast_wait_for(real):
    """wait_for with a short timeout, so the 'still going down' case is quick."""
    async def wait_for(aw, timeout=None):
        return await real(aw, timeout=0.05 if timeout else None)
    return wait_for


class TestErrorText:
    TRACE = ("Traceback (most recent call last):\n  File \"machine.py\", line 408\n"
             "ShellCommandError: Error running shell command: 'sudo systemctl reboot'")

    def _json_error(self, code, message):
        req = httpx.Request("POST", "http://x")
        body = {"error": {"code": code, "message": message, "traceback": self.TRACE}}
        return httpx.HTTPStatusError(
            "e", request=req, response=httpx.Response(code, json=body, request=req))

    def test_only_moonrakers_message_reaches_the_panel_not_its_traceback(
            self, client, printer):
        printer.post_error = self._json_error(
            500, "Error running shell command: 'sudo systemctl reboot'")
        r = restart(client, "printer")
        assert r.status_code == 500
        assert r.json()["detail"] == (
            "moonraker: Error running shell command: 'sudo systemctl reboot'")
        assert "Traceback" not in r.text

    def test_a_body_that_is_not_moonrakers_json_is_passed_on_as_is(
            self, client, printer):
        req = httpx.Request("POST", "http://x")
        printer.post_error = httpx.HTTPStatusError(
            "e", request=req, response=httpx.Response(502, text="Bad Gateway", request=req))
        assert restart(client, "firmware").json()["detail"] == "moonraker: Bad Gateway"

    @pytest.mark.parametrize("body", [{"error": "plain string"}, {"error": {}},
                                      {"error": {"message": ""}}, ["not", "a", "dict"]])
    def test_an_unexpected_json_shape_falls_back_to_the_raw_text(
            self, main, body):
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(500, json=body, request=req)
        assert main._moonraker_error(resp) == resp.text


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

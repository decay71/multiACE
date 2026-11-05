"""Push notifications: the settings file, the level/repeat/burst filters and
the one POST per captured message."""
import asyncio

import httpx
import pytest

from .test_history_api import live  # noqa: F401


@pytest.fixture
def pn(live, monkeypatch, tmp_path):  # noqa: F811
    main, c, _data = live
    monkeypatch.setattr(main, "NOTIFY_FILE", str(tmp_path / "push_notify.json"))
    monkeypatch.setattr(main, "_notify_cfg", {"url": "", "level": "error"})
    monkeypatch.setattr(main, "_notify_recent", {})
    monkeypatch.setattr(main, "_notify_sent", main.deque())
    return main, c


def _note(level="error", msg="boom"):
    return {"level": level, "msg": msg}


class _FakeClient:
    """Stands in for httpx.AsyncClient: records the POST, answers by rule."""
    calls = []
    status = 200
    raises = None

    def __init__(self, *a, **k):
        self.kw = k

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        type(self).calls.append((url, content, headers, self.kw))
        if type(self).raises:
            raise type(self).raises
        return httpx.Response(type(self).status)


@pytest.fixture
def fake(pn, monkeypatch):
    main, _c = pn
    _FakeClient.calls, _FakeClient.status, _FakeClient.raises = [], 200, None
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


class TestSettings:
    def test_default_is_off(self, pn):
        _main, c = pn
        assert c.get("/api/notify-settings").json() == {"url": "", "level": "error"}

    def test_saved_and_restored_after_a_restart(self, pn):
        main, c = pn
        r = c.put("/api/notify-settings",
                  json={"url": "https://ntfy.example/mytopic", "level": "warn"})
        assert r.status_code == 200
        main._notify_cfg.update({"url": "", "level": "error"})   # a restart
        main._notify_load()
        assert c.get("/api/notify-settings").json() == {
            "url": "https://ntfy.example/mytopic", "level": "warn"}

    def test_empty_url_turns_it_off(self, pn):
        _main, c = pn
        c.put("/api/notify-settings", json={"url": "http://h/t", "level": "error"})
        r = c.put("/api/notify-settings", json={"url": "  ", "level": "error"})
        assert r.json()["url"] == ""

    @pytest.mark.parametrize("url", [
        "ftp://h/t", "file:///etc/passwd", "javascript:alert(1)", "http://",
        "http://h/a b", "http://h/\nX: y", "http://" + "h" * 600])
    def test_bad_urls_are_refused(self, pn, url):
        _main, c = pn
        assert c.put("/api/notify-settings",
                     json={"url": url, "level": "error"}).status_code == 422
        assert c.get("/api/notify-settings").json()["url"] == ""

    def test_bad_level_is_refused(self, pn):
        _main, c = pn
        assert c.put("/api/notify-settings",
                     json={"url": "http://h/t", "level": "info"}).status_code == 422

    def test_corrupt_file_means_off(self, pn):
        main, _c = pn
        open(main.NOTIFY_FILE, "w", encoding="utf-8").write("{nope")
        main._notify_load()
        assert main._notify_cfg == {"url": "", "level": "error"}


class TestFilters:
    def test_nothing_without_a_url(self, pn):
        main, _c = pn
        assert main._notify_wanted(_note(), 1000.) is False

    def test_errors_only_by_default(self, pn):
        main, _c = pn
        main._notify_cfg.update({"url": "http://h/t", "level": "error"})
        assert main._notify_wanted(_note("warn", "w"), 1000.) is False
        assert main._notify_wanted(_note("error", "e"), 1000.) is True

    def test_warnings_when_chosen(self, pn):
        main, _c = pn
        main._notify_cfg.update({"url": "http://h/t", "level": "warn"})
        assert main._notify_wanted(_note("warn", "w"), 1000.) is True

    def test_info_is_never_pushed(self, pn):
        main, _c = pn
        main._notify_cfg.update({"url": "http://h/t", "level": "warn"})
        assert main._notify_wanted(_note("info", "progress"), 1000.) is False

    def test_the_same_text_is_pushed_once_in_five_minutes(self, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"
        assert main._notify_wanted(_note(msg="same"), 1000.) is True
        assert main._notify_wanted(_note(msg="same"), 1100.) is False
        assert main._notify_wanted(_note(msg="other"), 1100.) is True
        assert main._notify_wanted(_note(msg="same"), 1400.) is True

    def test_a_burst_is_capped(self, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"
        ok = [main._notify_wanted(_note(msg="m%d" % i), 1000. + i)
              for i in range(12)]
        assert ok == [True] * 10 + [False] * 2
        assert main._notify_wanted(_note(msg="later"), 1100.) is True


class TestSending:
    def test_the_post_carries_text_and_headers(self, fake, pn):
        main, _c = pn
        ok, detail = asyncio.run(main._notify_post("http://h/t", "Bin full", "error"))
        assert (ok, detail) == (True, "HTTP 200")
        url, body, heads, kw = fake.calls[0]
        assert (url, body) == ("http://h/t", b"Bin full")
        assert heads["Title"] == "multiACE error" and heads["Priority"] == "high"
        assert kw["follow_redirects"] is False and kw["timeout"] == 5.0

    def test_a_warning_has_its_own_title_and_priority(self, fake, pn):
        main, _c = pn
        asyncio.run(main._notify_post("http://h/t", "w", "warn"))
        heads = fake.calls[0][2]
        assert heads["Title"] == "multiACE warning"
        assert heads["Priority"] == "default"

    def test_non_ascii_text_goes_out_as_utf8(self, fake, pn):
        main, _c = pn
        asyncio.run(main._notify_post("http://h/t", "Düse verstopft", "error"))
        assert fake.calls[0][1] == "Düse verstopft".encode("utf-8")

    def test_a_refusing_receiver_is_reported(self, fake, pn):
        main, _c = pn
        fake.status = 404
        assert asyncio.run(main._notify_post("http://h/t", "x", "error")) == (
            False, "HTTP 404")

    def test_errors_never_escape(self, fake, pn):
        main, _c = pn
        fake.raises = httpx.ConnectError("no route")
        assert asyncio.run(main._notify_post("http://h/t", "x", "error")) == (
            False, "connection failed")
        fake.raises = httpx.ReadTimeout("slow")
        assert asyncio.run(main._notify_post("http://h/t", "x", "error")) == (
            False, "timeout")


class TestPipeline:
    def test_a_captured_error_is_pushed(self, fake, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"

        async def go():
            main._record_notification("!! [multiACE] Head 2: no flow")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(go())
        assert [c[1] for c in fake.calls] == [b"Head 2: no flow"]

    def test_a_captured_warning_waits_for_the_warn_level(self, fake, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"

        async def go(text):
            main._record_notification(text)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(go("[warn] [multiACE] purge bin is probably full"))
        assert fake.calls == []
        main._notify_cfg["level"] = "warn"
        asyncio.run(go("// [warn] [multiACE] purge bin is probably full"))
        assert len(fake.calls) == 1

    def test_a_failed_send_does_not_suppress_the_same_error(self, fake, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"
        fake.status = 503

        async def go():
            main._record_notification("!! [multiACE] Head 2: no flow")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(go())
        assert len(fake.calls) == 1
        fake.status = 200                       # the receiver is back
        asyncio.run(go())
        assert len(fake.calls) == 2             # not held back for 5 min
        asyncio.run(go())
        assert len(fake.calls) == 2             # a delivered one is

    def test_a_pending_push_is_kept_alive(self, fake, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"

        async def go():
            main._record_notification("!! [multiACE] boom")
            held = len(main._notify_tasks)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return held

        assert asyncio.run(go()) == 1
        assert main._notify_tasks == set()      # released when finished

    def test_no_loop_leaves_no_repeat_reservation(self, fake, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"
        main._record_notification("!! [multiACE] boom")
        assert main._notify_recent == {}

    def test_without_a_url_nothing_is_sent(self, fake, pn):
        main, _c = pn

        async def go():
            main._record_notification("!! [multiACE] boom")
            await asyncio.sleep(0)

        asyncio.run(go())
        assert fake.calls == []

    def test_outside_a_loop_it_is_a_no_op(self, fake, pn):
        main, _c = pn
        main._notify_cfg["url"] = "http://h/t"
        main._record_notification("!! [multiACE] boom")      # no running loop
        assert fake.calls == []

    def test_the_test_button_needs_a_url(self, fake, pn):
        _main, c = pn
        assert c.post("/api/notify-settings/test").status_code == 409

    def test_the_test_button_reports_the_answer(self, fake, pn):
        main, c = pn
        main._notify_cfg["url"] = "http://h/t"
        assert c.post("/api/notify-settings/test").json() == {
            "ok": True, "detail": "HTTP 200"}
        fake.status = 401
        assert c.post("/api/notify-settings/test").json() == {
            "ok": False, "detail": "HTTP 401"}

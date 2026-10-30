"""/api/thumbnail: the preview image of a gcode file from Moonraker's
metadata scan, shared by the History, Favorites and Queue lists."""
import httpx
import pytest

from .test_history_api import live  # noqa: F401
from .test_mock_mode import mock_client  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\nfake"


def _thumb(w, rel):
    return {"width": w, "height": w, "size": 1, "relative_path": rel}


class TestPick:
    def test_smallest_that_still_fills_the_slot(self, live):
        main, _c, _d = live
        got = main._pick_thumbnail([_thumb(32, "a.png"), _thumb(300, "c.png"),
                                    _thumb(128, "b.png")])
        assert got["relative_path"] == "b.png"

    def test_falls_back_to_the_largest(self, live):
        main, _c, _d = live
        got = main._pick_thumbnail([_thumb(32, "a.png"), _thumb(64, "b.png")])
        assert got["relative_path"] == "b.png"

    def test_ignores_formats_a_browser_cannot_show(self, live):
        main, _c, _d = live
        assert main._pick_thumbnail([_thumb(300, "x.qoi")]) is None
        assert main._pick_thumbnail([]) is None
        assert main._pick_thumbnail(None) is None


class TestEndpoint:
    @pytest.fixture
    def moon(self, live, monkeypatch):
        main, c, _d = live
        seen = {}

        async def fake_get(path):
            seen["meta"] = path
            return {"result": {"thumbnails": [
                _thumb(300, ".thumbs/a-300x300.png")]}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                seen["img"] = url
                return httpx.Response(200, content=PNG,
                                      request=httpx.Request("GET", url))

        monkeypatch.setattr(main, "_mr_get", fake_get)
        monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
        return c, seen

    def test_serves_the_image_from_the_files_folder(self, moon):
        c, seen = moon
        r = c.get("/api/thumbnail", params={"path": "sub dir/a b.gcode"})
        assert r.status_code == 200
        assert r.content == PNG and r.headers["content-type"] == "image/png"
        assert seen["meta"].endswith("filename=sub%20dir/a%20b.gcode")
        assert seen["img"].endswith(
            "/server/files/gcodes/sub%20dir/.thumbs/a-300x300.png")

    def test_no_thumbnail_is_404(self, live, monkeypatch):
        main, c, _d = live

        async def fake_get(path):
            return {"result": {}}
        monkeypatch.setattr(main, "_mr_get", fake_get)
        assert c.get("/api/thumbnail", params={"path": "a.gcode"}
                     ).status_code == 404

    def test_moonraker_down_is_404(self, live, monkeypatch):
        main, c, _d = live

        async def boom(path):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        assert c.get("/api/thumbnail", params={"path": "a.gcode"}
                     ).status_code == 404

    @pytest.mark.parametrize("bad", ["", "../etc/passwd", "a/../../b.gcode",
                                     r"..\x.gcode"])
    def test_path_traversal_is_refused(self, live, bad):
        _m, c, _d = live
        assert c.get("/api/thumbnail", params={"path": bad}
                     ).status_code == 400

    def test_mock_mode_serves_a_placeholder(self, mock_client):
        r = mock_client.get("/api/thumbnail", params={"path": "a.gcode"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")

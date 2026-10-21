"""Webcam proxy: camera lookup through Moonraker, MJPEG stream and snapshot.

Moonraker is replaced by a stub for `_mr_get`; the camera itself by an
httpx MockTransport, so nothing here touches the network.
"""
import asyncio

import httpx
import pytest

from .test_mock_mode import _load_main, mock_client, live_client  # noqa: F401


def _cam(**kw):
    cam = {"name": "Printer", "service": "mjpegstreamer", "enabled": True,
           "stream_url": "/webcam/?action=stream",
           "snapshot_url": "/webcam/?action=snapshot", "aspect_ratio": "4:3"}
    cam.update(kw)
    return cam


@pytest.fixture
def main(monkeypatch, tmp_path):
    return _load_main(monkeypatch, tmp_path, mock=False)


def _listing(main, monkeypatch, cams):
    async def fake(path):
        assert path == "/server/webcams/list"
        return {"result": {"webcams": cams}}
    monkeypatch.setattr(main, "_mr_get", fake)


class TestResolve:
    def _resolve(self, main, monkeypatch, cams):
        _listing(main, monkeypatch, cams)
        return asyncio.run(main._resolve_webcam())

    def test_relative_urls_use_the_printer_host_without_the_moonraker_port(
            self, main, monkeypatch):
        monkeypatch.setattr(main, "MOONRAKER_URL", "http://10.0.0.5:7125")
        monkeypatch.setattr(main, "WEBCAM_BASE", "")
        info = self._resolve(main, monkeypatch, [_cam()])
        assert info["stream_url"] == "http://10.0.0.5/webcam/?action=stream"
        assert info["snapshot_url"] == "http://10.0.0.5/webcam/?action=snapshot"

    def test_webcam_base_override_wins(self, main, monkeypatch):
        monkeypatch.setattr(main, "WEBCAM_BASE", "http://cam.local:8080/")
        info = self._resolve(main, monkeypatch, [_cam()])
        assert info["stream_url"] == "http://cam.local:8080/webcam/?action=stream"

    def test_skips_own_panel_iframes_disabled_and_streamless(
            self, main, monkeypatch):
        cams = [_cam(name="multiACE", service="iframe"),
                _cam(name="Frame", service="iframe"),
                _cam(name="Off", enabled=False),
                _cam(name="Empty", stream_url=""),
                _cam(name="Real")]
        info = self._resolve(main, monkeypatch, cams)
        assert info["name"] == "Real"

    def test_no_camera(self, main, monkeypatch):
        info = self._resolve(main, monkeypatch, [])
        assert info["available"] is False

    def test_moonraker_down_is_reported_not_raised(self, main, monkeypatch):
        async def boom(path):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        info = asyncio.run(main._resolve_webcam())
        assert info["available"] is False and "moonraker" in info["reason"]


class TestEndpoints:
    def _client(self, main):
        from fastapi.testclient import TestClient
        return TestClient(main.app)

    def _camera_transport(self, main, monkeypatch):
        def handler(req):
            if "snapshot" in req.url.query.decode():
                return httpx.Response(200, content=b"JPEG",
                                      headers={"content-type": "image/jpeg"})
            return httpx.Response(
                200, stream=httpx.ByteStream(b"--frame\r\nJPEG\r\n"),
                headers={"content-type":
                         "multipart/x-mixed-replace; boundary=frame"})
        real = httpx.AsyncClient
        monkeypatch.setattr(
            main.httpx, "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    def test_info_hides_the_camera_address(self, main, monkeypatch):
        _listing(main, monkeypatch, [_cam()])
        with self._client(main) as c:
            j = c.get("/api/webcam/info").json()
        assert j["available"] is True
        assert j["proxy_path"] == "/api/webcam/stream"
        assert "stream_url" not in j and "snapshot_url" not in j

    def test_info_when_unavailable_has_no_proxy_path(self, main, monkeypatch):
        _listing(main, monkeypatch, [])
        with self._client(main) as c:
            j = c.get("/api/webcam/info").json()
        assert j["available"] is False and "proxy_path" not in j

    def test_stream_is_piped_with_the_cameras_content_type(
            self, main, monkeypatch):
        _listing(main, monkeypatch, [_cam()])
        self._camera_transport(main, monkeypatch)
        with self._client(main) as c:
            r = c.get("/api/webcam/stream")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
        assert r.headers["cache-control"] == "no-store"
        assert b"JPEG" in r.content

    def test_stream_without_camera_is_503(self, main, monkeypatch):
        _listing(main, monkeypatch, [])
        with self._client(main) as c:
            assert c.get("/api/webcam/stream").status_code == 503

    def test_stream_camera_error_is_502(self, main, monkeypatch):
        _listing(main, monkeypatch, [_cam()])
        real = httpx.AsyncClient
        monkeypatch.setattr(
            main.httpx, "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(
                lambda req: httpx.Response(500)), **kw))
        with self._client(main) as c:
            assert c.get("/api/webcam/stream").status_code == 502

    def test_snapshot(self, main, monkeypatch):
        _listing(main, monkeypatch, [_cam()])
        self._camera_transport(main, monkeypatch)
        with self._client(main) as c:
            r = c.get("/api/webcam/snapshot")
        assert r.status_code == 200 and r.content == b"JPEG"
        assert r.headers["content-type"] == "image/jpeg"

    def test_snapshot_without_url_is_503(self, main, monkeypatch):
        _listing(main, monkeypatch, [_cam(snapshot_url="")])
        with self._client(main) as c:
            assert c.get("/api/webcam/snapshot").status_code == 503


class TestMockMode:
    def test_info_comes_from_the_fixture(self, mock_client):
        j = mock_client.get("/api/webcam/info").json()
        assert j["mock"] is True and j["available"] is False

    def test_stream_and_snapshot_refuse(self, mock_client):
        assert mock_client.get("/api/webcam/stream").status_code == 503
        assert mock_client.get("/api/webcam/snapshot").status_code == 503

    def test_query_param_cannot_enable_mock_on_a_real_printer(self, live_client):
        assert live_client.get("/api/webcam/info?mock=1").json().get("mock") is not True

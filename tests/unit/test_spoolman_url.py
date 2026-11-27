"""A Spoolman address typed without http:// must work, and a failing
search must say why."""
import httpx
import pytest

from .test_mock_mode import _load_main


def test_address_without_scheme_gets_http(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    n = main._spoolman_norm_url
    assert n("10.0.0.230:7912") == "http://10.0.0.230:7912"
    assert n(" spoolman.local:7912/ ") == "http://spoolman.local:7912"
    assert n("http://10.0.0.230:7912/") == "http://10.0.0.230:7912"
    assert n("https://sm.example.org") == "https://sm.example.org"
    assert n("") == "" and n(None) == ""


def test_search_reports_an_unreachable_spoolman(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)

    async def state():
        return {}

    monkeypatch.setattr(main, "_query_state_gated", state)
    monkeypatch.setattr(main, "_parse_state", lambda raw: {
        "spoolman_url": main._spoolman_norm_url("10.255.255.1:1"),
        "spools": {}})

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url.startswith("http://10.255.255.1:1/")
            raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(main.httpx, "AsyncClient", Boom)
    main._spoolman_cache.update({"ts": 0.0, "base": "", "rows": []})
    with TestClient(main.app) as c:
        r = c.get("/api/spoolman/search", params={"q": "pla"})
    assert r.status_code == 502
    assert "not reachable at http://10.255.255.1:1" in r.json()["detail"]

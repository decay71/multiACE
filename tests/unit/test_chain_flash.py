"""ACE 2 daisy chain in the web: a unit on a chain bus ('<cable>@<uid>')
is marked chained with the number of units on its cable, the v2_chain
switch passes through, and the flasher refuses a chained unit up front
with the steps (decay71's review of #227)."""
import asyncio
import importlib
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "multiace" / "web" / "backend"
CABLE = "/dev/serial/by-path/platform-usb-0:1.3:1.0"


@pytest.fixture
def web(monkeypatch, tmp_path):
    cfg = tmp_path / "ace.cfg"
    cfg.write_text("[ace]\nace_device_count: 3\n", encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    monkeypatch.delenv("MULTIACE_MOCK_MODE", raising=False)
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("main", None)
    mod = importlib.import_module("main")
    monkeypatch.setattr(mod, "OVERRIDE_FILE", str(tmp_path / "ov.json"))
    mod._slot_overrides.clear()
    return mod


def _status(paths, chain_switch=None):
    aces = [{"index": i, "connected": True, "protocol": "v2",
             "serial_path": p, "slots": []} for i, p in enumerate(paths)]
    ace = {"device_count": len(paths), "active_device": 0,
           "gate_status": [1, 1, 1, 1], "aces": aces}
    if chain_switch is not None:
        ace["chain_switch"] = chain_switch
    return {"ace": ace}


def test_chained_units_are_marked_with_their_cable_count(web):
    st = web._parse_state(_status([CABLE + "@aaaa", CABLE + "@bbbb",
                                   "/dev/serial/by-path/other"]))
    a = st["aces"]
    assert [x["chained"] for x in a] == [True, True, False]
    assert [x["chain_units"] for x in a] == [2, 2, 0]


def test_a_lone_unit_on_a_chain_bus_counts_one(web):
    st = web._parse_state(_status([CABLE + "@aaaa"]))
    assert st["aces"][0]["chained"] is True
    assert st["aces"][0]["chain_units"] == 1


def test_the_chain_switch_passes_through(web):
    sw = {"enabled": True, "active": True, "available": True,
          "chained_cables": 1}
    assert web._parse_state(_status([CABLE], sw))["chain_switch"] == sw
    assert web._parse_state(_status([CABLE]))["chain_switch"] is None


def _flash(web, monkeypatch, tmp_path, paths, ace):
    monkeypatch.setattr(web, "_ACEFW_DIR", tmp_path)
    (tmp_path / "upload.bin").write_bytes(b"x")
    monkeypatch.setattr(web, "_acefw_running", lambda: False)

    async def _q():
        return _status(paths)
    monkeypatch.setattr(web, "_query_state_gated", _q)
    return asyncio.run(web.acefw_flash({"ace": ace, "dry_run": True}))


def test_flash_refuses_a_chained_unit_with_the_steps(web, monkeypatch, tmp_path):
    with pytest.raises(web.HTTPException) as e:
        _flash(web, monkeypatch, tmp_path,
               [CABLE + "@aaaa", CABLE + "@bbbb"], 1)
    assert e.value.status_code == 409
    assert "alone on its cable with v2_chain off" in e.value.detail


def test_flash_refuses_a_lone_unit_while_the_chain_is_on(web, monkeypatch, tmp_path):
    with pytest.raises(web.HTTPException) as e:
        _flash(web, monkeypatch, tmp_path, [CABLE + "@aaaa"], 0)
    assert e.value.status_code == 409
    assert "switch v2_chain off" in e.value.detail


def test_a_direct_unit_is_not_refused_by_the_chain_check(web, monkeypatch, tmp_path):
    # It gets past the chain check; whatever stops it later is no 409.
    try:
        _flash(web, monkeypatch, tmp_path, [CABLE], 0)
    except web.HTTPException as e:
        assert e.status_code != 409, e.detail
    except Exception:
        pass


def test_the_page_copies_the_chain_switch_into_its_state():
    # The page rebuilds its state field by field from the server's; a field
    # it does not copy never reaches the template (the switch row stayed
    # hidden on the U1 until this line existed).
    app = (BACKEND.parent / "frontend" / "app.js").read_text("utf-8")
    assert "state.chain_switch = " in app
    html = (BACKEND.parent / "frontend" / "index.html").read_text("utf-8")
    assert "drv.name === 'ace_v2' && state.chain_switch" in html

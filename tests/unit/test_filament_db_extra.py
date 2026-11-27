"""The picker offers the filaments the touchscreen has and the firmware
DB lacks."""
from tests.unit.test_mock_mode import _load_main


def test_extra_filaments_are_added(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    db = main._with_extra_filaments({
        "PLA": {"Generic": [], "Snapmaker": ["Basic", "Silk"]},
        "TPU": {"Generic": ["90A", "95A HF"], "Snapmaker": ["90A"]},
        "PVA": {"Generic": []},
    })
    assert list(db)[:3] == ["PLA", "TPU", "PVA"]
    assert db["PLA"]["Generic"] == ["Silk", "Support"]
    assert db["PLA"]["Snapmaker"] == ["Basic", "Silk", "Starlight", "Support"]
    assert db["TPU"]["Snapmaker"] == ["90A", "95A"]
    assert db["PVA"] == {"Generic": [], "Snapmaker": []}
    for t in ("PHA", "BVOH", "EVA", "HIPS", "PCTG", "PE", "PE-CF", "PAHT-CF"):
        assert "Generic" in db[t]


def test_db_rows_are_not_doubled(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    src = {"PLA": {"Generic": [], "Snapmaker": ["Starlight", "support"]},
           "paht-cf": {"generic": []}}
    db = main._with_extra_filaments(src)
    assert db["PLA"]["Snapmaker"] == ["Starlight", "support"]
    assert "PAHT-CF" not in db and "Snapmaker" in db["paht-cf"]
    assert list(db["paht-cf"]) == ["generic", "Snapmaker"]
    assert src["PLA"]["Snapmaker"] == ["Starlight", "support"]


def test_unreadable_db_stays_empty(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    monkeypatch.setattr(main, "_read_filament_db", lambda: {})
    assert main._load_filament_db() == {}

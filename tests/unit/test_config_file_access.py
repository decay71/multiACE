"""Files the web backend writes for Klipper stay readable by Klipper.

The backend runs as root; started from a shell with umask 0077 a plain
write + os.replace left ace.cfg root-owned 0600 and Klipper (user lava)
failed with "Unable to open config file".
"""
import os

from .test_mock_mode import _load_main  # noqa: F401


def _record_access(monkeypatch, euid):
    calls = {'chmod': [], 'chown': []}
    monkeypatch.setattr(os, 'chmod',
                        lambda p, mode: calls['chmod'].append((str(p), mode)))
    monkeypatch.setattr(os, 'chown',
                        lambda p, uid, gid: calls['chown'].append(
                            (str(p), uid, gid)), raising=False)
    monkeypatch.setattr(os, 'geteuid', lambda: euid, raising=False)
    return calls


def test_config_write_is_readable_and_owned_like_its_directory(
        monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    cfg = tmp_path / 'ace.cfg'
    cfg.write_text('[ace]\n', encoding='utf-8')
    calls = _record_access(monkeypatch, euid=0)
    main._write_cfg_atomic(cfg, '[ace]\nfoo: 1\n')
    assert cfg.read_text(encoding='utf-8') == '[ace]\nfoo: 1\n'
    tmp = str(cfg.with_suffix('.cfg.tmp'))
    assert calls['chmod'] and calls['chmod'][-1][0] == tmp
    assert calls['chmod'][-1][1] & 0o444 == 0o444
    owner = tmp_path.stat()
    assert calls['chown'] == [(tmp, owner.st_uid, owner.st_gid)]


def test_config_write_as_user_does_not_chown(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    cfg = tmp_path / 'ace.cfg'
    calls = _record_access(monkeypatch, euid=1000)
    main._write_cfg_atomic(cfg, '[ace]\n')
    assert calls['chmod'][-1][1] & 0o444 == 0o444
    assert calls['chown'] == []


def test_slot_overrides_write_is_readable(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    target = tmp_path / 'slot_overrides.json'
    monkeypatch.setattr(main, 'OVERRIDE_FILE', str(target))
    calls = _record_access(monkeypatch, euid=0)
    main._save_overrides_to_disk()
    assert target.exists()
    assert calls['chmod'] and calls['chmod'][-1][1] & 0o444 == 0o444

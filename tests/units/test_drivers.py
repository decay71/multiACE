"""Driver registry (ace_drivers.py): one switch per codec, read
unconditionally, disabled = not discovered. Design: UNIT_DRIVER_DESIGN.md
"Treiber-Registry"; policy TODO.md "Oeffentliches Treiber-Handbuch"."""
import sys

from . import harness as H


def _mod():
    H.load_ace()
    return sys.modules[H.PKG + '.ace_drivers']


class _Cfg:
    def __init__(self, values):
        self.values = values
        self.read = []

    def getboolean(self, name, default):
        self.read.append(name)
        raw = self.values.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def test_registry_is_consistent():
    D = _mod()
    names = [d.name for d in D.DRIVERS]
    options = [d.option for d in D.DRIVERS]
    assert len(set(names)) == len(names)
    assert len(set(options)) == len(options)
    assert D.KNOWN_PROTOCOLS == tuple(d.codec for d in D.DRIVERS)
    for d in D.DRIVERS:
        assert d.codec.NAME, d
        assert d.option.startswith('enable_'), d
        assert D.driver_for(d.codec) is d
    assert D.driver_for(object) is None


def test_untested_driver_defaults_off():
    D = _mod()
    for d in D.DRIVERS:
        assert d.tested or d.default is False, d


def test_every_switch_is_read_even_when_off():
    D = _mod()
    cfg = _Cfg({'enable_ace_v1': 'false', 'enable_ace_v2': 'false'})
    enabled = D.read_enabled_drivers(cfg)
    assert cfg.read == [d.option for d in D.DRIVERS]
    assert enabled == {d.name: False for d in D.DRIVERS}
    assert D.enabled_codecs(enabled) == ()


def test_defaults_and_order():
    D = _mod()
    enabled = D.read_enabled_drivers(_Cfg({}))
    assert enabled == {d.name: d.default for d in D.DRIVERS}
    on = D.read_enabled_drivers(_Cfg({'enable_ace_v2': 'true',
                                      'enable_cfs': 'true'}))
    assert D.enabled_codecs(on) == D.KNOWN_PROTOCOLS
    assert D.describe(on) == ('ace_v1 on, ace_v2 on, '
                              'cfs on (untested, opt-in)')
    assert 'ace_v2 off' in D.describe(enabled)


def test_shipped_cfg_names_every_switch():
    """Every driver's option appears in the shipped ace.cfg (set or as a
    commented template), so a user finds the switch."""
    D = _mod()
    with open(H.SHIPPED_ACE_CFG, encoding='utf-8') as f:
        text = f.read()
    for d in D.DRIVERS:
        assert ('%s:' % d.option) in text, d.option

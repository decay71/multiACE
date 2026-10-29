"""M2: the contract checker itself. The golden scenarios prove that V1/V2
honour the contract today (test_goldens asserts no 'contract' entry); this
file proves the checker bites when a codec breaks a rule."""
from . import harness as H
from .contract import ContractChecker
from .fake_v1 import FakeV1
from .scenarios import PATH_V1_A, V1_IDS, _harness


class _Stub:
    """Just enough harness for a ContractChecker without a printer."""
    inst = None

    def __init__(self):
        self.entries = []

    def trace(self, kind, *fields):
        self.entries.append([kind] + list(fields))

    def norm(self, text):
        return text


def _checker(kind='v1'):
    c = ContractChecker(_Stub())
    c.slot_states = {'ready', 'feeding', 'rollback', 'assisting'}
    return c


def _slots(**extra):
    out = []
    for i in range(4):
        s = {'index': i, 'status': 'empty1', 'sku': '', 'type': '',
             'rfid': 0, 'brand': '', 'color': [0, 0, 0]}
        s.update(extra)
        out.append(s)
    return out


def _status(**over):
    d = {'status': 'ready', 'temp': 24, 'dryer_status': {'status': 'stop'},
         'slots': _slots()}
    d.update(over)
    return d


def _rules(c):
    return sorted({r for r, _ in c.violations})


def test_status_rules_fire():
    c = _checker()
    bad = _status(); bad['slots'][0][7] = 'x'
    c._check_status(0, 'v1', bad)
    assert 'S1' in _rules(c)
    c = _checker()
    d = _status(); del d['temp']; del d['dryer_status']; d['status'] = 'idle'
    c._check_status(0, 'v1', d)
    assert _rules(c) == ['S2'], 'temp/dryer_status may be absent (M5c)'
    c = _checker()
    c._check_status(0, 'v1', _status(temp=True, dryer_status='stop'))
    assert _rules(c) == ['S4', 'S5']
    c = _checker()
    c._check_status(0, 'v1', _status(humidity=40, subtype='Basic',
                                     slots=_slots(slot_status='ready')))
    assert _rules(c) == ['S6', 'S7', 'S8']
    c = _checker()
    c._check_status(0, 'v2', _status(slots=_slots(status='unknown')))
    assert _rules(c) == ['S6', 'S7']          # V2 needs humidity + slot_status
    c = _checker()
    c._check_status(0, 'v1', _status(slots=_slots(status='unknown')))
    assert _rules(c) == ['S3']                 # 'unknown' is V2 vocabulary
    c = _checker()
    same = _status()
    c._check_status(0, 'v1', same)
    c._check_status(0, 'v1', same)
    assert _rules(c) == ['S9']


def test_response_rules_fire():
    def deliver(c, kind, resp, method='get_status', rid=1):
        c._pending[(0, rid)] = (method, kind)
        c._on_delivery(0, kind, method, (), {'self': None, 'response': resp},
                       {'sent': True})
    c = _checker()
    deliver(c, 'v1', {'id': 1, 'code': 0, 'msg': ''})
    assert _rules(c) == ['A3']
    c = _checker()
    deliver(c, 'v2', {'id': 1, 'code': 0, 'msg': 'oops'})
    assert _rules(c) == ['A2', 'A3']
    c = _checker()
    deliver(c, 'v2', {'id': 1, 'code': 2, 'msg': 'success'})
    assert _rules(c) == ['A2']
    c = _checker()
    deliver(c, 'v1', {'id': 'one', 'code': '0', 'msg': 5, 'result': []})
    assert _rules(c) == ['A1']
    c = _checker()
    c._on_delivery(0, 'v1', 'get_info', (None, {'id': 1, 'code': 0,
                   'msg': 'success'}), {}, {'sent': True})
    assert 'D2' in _rules(c) and 'D4' in _rules(c)
    c = _checker()
    c._on_delivery(0, 'v1', 'get_info', (), {'self': None, 'response': None},
                   {'sent': True})
    assert _rules(c) == ['D3']
    c = _checker()
    deliver(c, 'v1', {'id': 1, 'code': 0, 'msg': 'success'})
    c._on_delivery(0, 'v1', 'get_status', (),
                   {'self': None, 'response': {'id': 1, 'code': 0,
                                               'msg': 'success'}},
                   {'sent': True})
    assert _rules(c) == ['D4']
    c = _checker()
    c._pending[(0, 1)] = ('get_info', 'v1')
    c._on_delivery(0, 'v1', 'get_info', (),
                   {'self': None, 'response': {'id': 1, 'code': 0,
                                               'msg': 'success'}},
                   {'sent': False})
    assert _rules(c) == ['D5']


class _BadV1(FakeV1):
    """An ACE Pro model that breaks the status contract: a status word
    outside ready/busy and a humidity field it has no sensor for. (An int
    key cannot travel over the V1 wire, json.dumps stringifies it; S1 is
    covered by the unit test above.)"""

    def _status(self):
        d = super()._status()
        d['humidity'] = 41
        d['status'] = 'idle'
        return d


def test_checker_bites_end_to_end():
    def units(clock):
        return [H.Unit('bad', 'v1', PATH_V1_A, _BadV1(clock, 'bad'), V1_IDS)]
    h = _harness(units, 1)
    h.build()
    h.ready()
    h.run(3.0)
    entries = h.finish()
    rules = sorted({e[2] for e in entries if e[1] == 'contract'})
    assert rules == ['S2', 'S6'], rules
    assert h.contract.status_dicts >= 2
    assert h.contract.requests > 0 and h.contract.responses > 0


def test_v1_v2_honour_the_contract_with_traffic():
    """Belt and braces for the golden assertion: the mixed fleet exchanges
    real traffic and the checker saw it."""
    from . import scenarios as S
    h = S._harness(S._mixed, 2)
    h.build()
    h.ready()
    h.run(5.0)
    h.finish()
    c = h.contract
    assert c.violations == [], c.violations
    assert c.requests >= 10 and c.responses >= 10 and c.status_dicts >= 6, \
        c.summary()

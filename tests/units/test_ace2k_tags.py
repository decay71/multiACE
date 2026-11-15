"""ace2k tag decoding on its own (M6d): the key derivations against the
vectors of ace2k's own tests (firmware/tests/python/test_ace2k_tags.py),
HKDF against RFC 5869 case 1, and TagPlan's step order and records for
the four formats the link decodes."""
from . import fake_ace2k

UID4 = bytes(bytearray.fromhex('01020304'))


def _codec():
    return fake_ace2k.codec()


def test_hkdf_matches_rfc_5869_case_1():
    C = _codec()
    okm = C.hkdf_sha256(bytes([0x0B] * 22), bytes(range(13)),
                        bytes(range(0xF0, 0xFA)), 42)
    assert okm.hex() == ('3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c'
                         '5db02d56ecc4c5bf34007208d5b887185865')


def test_brand_keys_match_ace2k():
    C = _codec()
    assert C._Bambu.key_for(UID4, 0).hex() == '6b0d673986de'
    assert C._Bambu.key_for(UID4, 1).hex() == '9c41cdfa0a4d'
    assert C._Bambu.key_for(UID4, 15).hex() == 'be9da70ee88e'
    assert C._Snapmaker.key_for(UID4, 0).hex() == 'f658de759175'
    assert C._Snapmaker.key_for(UID4, 1).hex() == '561b70517aaf'


def _run(plan, tag):
    """Answer the plan's steps from a fake tag the way the unit does."""
    steps = []
    for _ in range(30):
        step = plan.next_step()
        if step is None:
            break
        op, arg, blocks, key = step
        steps.append((op, arg, blocks))
        frames = []
        if op == 0:
            for g in range(blocks):
                first = arg + 4 * g
                frames.append((first, 0, b''.join(
                    tag['pages'].get(first + k, b'\x00' * 4)
                    for k in range(4))))
        elif tag['keys'].get(arg) != key:
            frames.append((arg * 4, 1, b''))
        else:
            for i in range(3):
                if blocks >> i & 1:
                    b = arg * 4 + i
                    frames.append((b, 0, tag['blocks'].get(b, b'\x00' * 16)))
        assert plan.feed(step, frames) == 'ok'
    return steps, plan.result()


def test_anycubic_ntag():
    C = _codec()
    tag = fake_ace2k.ntag_anycubic('AHPLBK-101', 'PLA Matte', '#202020')
    steps, rec = _run(C.TagPlan(tag['uid'], 0), tag)
    assert steps[0] == (0, 0, 3)
    assert max(a + 4 * b - 1 for _o, a, b in steps) == 39
    assert rec == {'fmt': 'anycubic', 'type': 'PLA Matte',
                   'color': [32, 32, 32], 'brand': 'AC',
                   'sku': 'AHPLBK-101'}


def test_openspool_ntag():
    C = _codec()
    tag = fake_ace2k.ntag_openspool('PETG', '#1188FF', 'Sunlu')
    _steps, rec = _run(C.TagPlan(tag['uid'], 0), tag)
    assert rec == {'fmt': 'openspool', 'type': 'PETG',
                   'color': [0x11, 0x88, 0xFF], 'brand': 'Sunlu'}


def test_blank_ntag_reads_no_record():
    C = _codec()
    tag = {'uid': fake_ace2k.NTAG_UID, 'sak': 0,
           'pages': {3: b'\xe1\x10\x12\x00'}}
    steps, rec = _run(C.TagPlan(tag['uid'], 0), tag)
    assert steps == [(0, 0, 3)] and rec is None


def test_bambu_then_snapmaker_keys():
    C = _codec()
    tag = fake_ace2k.mifare_bambu('PLA', 'PLA Basic', (1, 2, 3, 0xFF))
    steps, rec = _run(C.TagPlan(tag['uid'], 8), tag)
    assert [(a, b) for _o, a, b in steps] == [(0, 6), (1, 7), (3, 5)]
    assert rec['fmt'] == 'bambu' and rec['type'] == 'PLA Basic'
    assert rec['color'] == [1, 2, 3] and rec['brand'] == 'Bambu Lab'
    tag = fake_ace2k.mifare_snapmaker(1, 3, (9, 8, 7))
    steps, rec = _run(C.TagPlan(tag['uid'], 8), tag)
    assert [(a, b) for _o, a, b in steps] == [(0, 6), (0, 7), (1, 7),
                                              (2, 7)]
    assert rec['fmt'] == 'snapmaker' and rec['type'] == 'PLA SnapSpeed'
    assert rec['color'] == [9, 8, 7]


def test_unknown_mifare_declines_every_brand():
    C = _codec()
    tag = {'uid': UID4, 'sak': 8, 'keys': {}, 'blocks': {}}
    steps, rec = _run(C.TagPlan(UID4, 8), tag)
    assert len(steps) == 2 and rec is None

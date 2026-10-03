import errno
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / 'multiace' / 'klipper'))

from extras.ace_protocol_v2 import AceProtocolV2, Cmd, crc16_kermit, pb_uint32


def _pb_bytes(field, value):
    from extras.ace_protocol_v2 import pb_varint
    return pb_varint((field << 3) | 2) + pb_varint(len(value)) + value


def _response_frame(seq, cmd, payload, corrupt_crc=False):
    inner = bytes((0x80, seq & 0xff, (seq >> 8) & 0xff,
                   cmd & 0xff, len(payload))) + payload
    crc = crc16_kermit(inner)
    if corrupt_crc:
        crc ^= 1
    return (b'\xff\xaa' + inner + bytes((crc & 0xff, crc >> 8, 0xfe)))


class _FakeSerial:
    responder = None
    open_error = None
    seen_requests = []

    def __init__(self, **kwargs):
        if self.open_error is not None:
            raise self.open_error
        self.timeout = kwargs.get('timeout', 0.05)
        self.rx = bytearray()
        self.requests = []
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self.rx)

    def reset_input_buffer(self):
        self.rx.clear()

    def write(self, packet):
        seq = packet[3] | (packet[4] << 8)
        cmd = packet[5]
        self.requests.append((seq, cmd))
        self.seen_requests.append((seq, cmd))
        responder = type(self).responder
        if responder is not None:
            self.rx.extend(responder(seq, cmd))
        return len(packet)

    def flush(self):
        pass

    def read(self, size=1):
        if not self.rx:
            time.sleep(min(self.timeout, 0.003))
            return b''
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    def close(self):
        self.is_open = False


class V2ProbeTests(unittest.TestCase):
    def setUp(self):
        self.serial_module = types.ModuleType('serial')
        self.serial_module.Serial = _FakeSerial
        self.serial_patch = mock.patch.dict(
            sys.modules, {'serial': self.serial_module})
        self.serial_patch.start()
        _FakeSerial.responder = self._valid_responder
        _FakeSerial.open_error = None
        _FakeSerial.seen_requests = []
        self.temp_path = tempfile.NamedTemporaryFile(delete=False).name

    def tearDown(self):
        self.serial_patch.stop()
        _FakeSerial.responder = None
        _FakeSerial.open_error = None
        pathlib.Path(self.temp_path).unlink(missing_ok=True)

    @staticmethod
    def _valid_responder(seq, cmd):
        if cmd == Cmd.DISCOVER_DEVICE:
            payload = (pb_uint32(1, 0x12345678)
                       + pb_uint32(2, 0x23456789)
                       + pb_uint32(3, 0x3456789a))
        elif cmd == Cmd.GET_INFO:
            payload = (_pb_bytes(1, b'V1.1.31')
                       + _pb_bytes(2, b'V1.0.0'))
        else:
            return b''
        return _response_frame(seq, cmd, payload)

    def test_accepts_only_discovery_and_info_with_identity(self):
        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertTrue(result['ok'])
        self.assertEqual(result['category'], 'verified')
        self.assertEqual(result['model'], 'ACE 2 Pro')
        self.assertEqual(result['firmware'], 'V1.1.31')
        self.assertEqual(result['uid'], (0x12345678, 0x23456789,
                                         0x3456789a))
        self.assertEqual(_FakeSerial.seen_requests,
                         [(1, Cmd.DISCOVER_DEVICE), (2, Cmd.GET_INFO)])

    def test_open_permission_error_is_distinct(self):
        _FakeSerial.open_error = PermissionError(
            errno.EACCES, 'Permission denied')

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'permission_denied')

    def test_no_reply_is_distinct(self):
        _FakeSerial.responder = lambda seq, cmd: b''

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'no_response')

    def test_bad_crc_is_reported_as_invalid_response(self):
        def bad_crc_responder(seq, cmd):
            frame = self._valid_responder(seq, cmd)
            return frame[:-3] + bytes((frame[-3] ^ 1,)) + frame[-2:]

        _FakeSerial.responder = bad_crc_responder

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'invalid_response')
        self.assertIn('CRC failure', result['error'])

    def test_sequence_mismatch_is_not_accepted(self):
        _FakeSerial.responder = lambda seq, cmd: self._valid_responder(
            seq + 1, cmd)

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'invalid_response')
        self.assertIn('sequence mismatch', result['error'])

    def test_command_mismatch_is_not_accepted(self):
        _FakeSerial.responder = lambda seq, cmd: self._valid_responder(
            seq, Cmd.GET_INFO if cmd == Cmd.DISCOVER_DEVICE else
            Cmd.DISCOVER_DEVICE)

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'invalid_response')
        self.assertIn('command/sequence mismatch', result['error'])

    def test_missing_uid_is_not_accepted(self):
        def no_uid_responder(seq, cmd):
            if cmd == Cmd.DISCOVER_DEVICE:
                return _response_frame(seq, cmd, b'')
            return self._valid_responder(seq, cmd)

        _FakeSerial.responder = no_uid_responder

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'invalid_identity')

    def test_device_io_error_is_reported_as_disconnection(self):
        class DisconnectingSerial(_FakeSerial):
            def write(self, packet):
                raise OSError(errno.EIO, 'Input/output error')

        self.serial_module.Serial = DisconnectingSerial

        result = AceProtocolV2.probe_device(self.temp_path, query_timeout=0.1)

        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'device_disconnected')

    def test_generic_candidate_falls_back_to_tty_usb_nodes(self):
        with mock.patch('extras.ace_protocol_v2.os.path.isdir',
                        return_value=False), \
                mock.patch('extras.ace_protocol_v2.glob.glob',
                           side_effect=lambda pattern: {
                               '/dev/ttyUSB*': ['/dev/ttyUSB0'],
                               '/dev/ttyACM*': [],
                           }[pattern]), \
                mock.patch('extras.ace_protocol_v2.os.path.realpath',
                           side_effect=lambda path: path), \
                mock.patch.object(AceProtocolV2, '_read_usb_ids',
                                  return_value=('0403', '6001')):
            self.assertEqual(
                AceProtocolV2.discover_usb_serial_candidates(),
                ['/dev/ttyUSB0'])

    def test_open_visible_tty_is_detected(self):
        def fake_listdir(path):
            path = path.replace('\\', '/')
            if path == '/proc':
                return ['123']
            if path == '/proc/123/fd':
                return ['7']
            return []

        with mock.patch('extras.ace_protocol_v2.os.path.isdir',
                        return_value=True), \
                mock.patch('extras.ace_protocol_v2.os.listdir',
                           side_effect=fake_listdir), \
                mock.patch('extras.ace_protocol_v2.os.readlink',
                           return_value='/dev/ttyUSB0'), \
                mock.patch('extras.ace_protocol_v2.os.path.realpath',
                           side_effect=lambda path: path):
            self.assertTrue(AceProtocolV2.serial_port_in_use(
                '/dev/ttyUSB0'))


if __name__ == '__main__':
    unittest.main()

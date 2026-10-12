# ace_gen1_tunnel.py - Gen-1 (ACE Pro) tag reads through the community
# firmware's RC522 tunnel. Plain helper module, imported lazily by ace.py -
# deliberately NOT a Klipper extra: no [ace_gen1_tunnel] config section
# exists, so an old config cannot hit the unknown-section halt, and a
# printer whose install predates this file gets one clear log line and the
# ordinary status-only path instead of a dead Klipper.
#
# The tunnel (Godless50/ACE-PRO-v1.-NFC-UID, docs/GEN1_TAG_TUNNEL.md in
# this repo). The community firmware (CV1.3.87x) hooks the ACE's
# `filament_recognition` handler and dispatches host-issued operations from
# its packed `index` parameter:
#
#   packed = 0x80000000 | reader<<24 | op<<16 | (a1 & 0x3F)<<8 | (a2 & 0xFF)
#
# and it MUST go on the wire in the SIGNED 32-bit form (packed - 2**32):
# the firmware parses parameters with a signed strtol, so the unsigned
# value is clamped and the operation is silently lost. Replies arrive in
# `result.code` - and a `result.code` only exists when the stub ran, which
# is also the support probe.
#
# Ops: 0 read reg (a1) | 1 write reg (a1 = reg, a2 = value)
#      2 FIFO write (a2, a1 = index) | 3 PCD command (a1 = TX bytes,
#      a2 = command, 0x0C = TRANSCEIVE) | 4 FIFO read | 5 RX bits
#      6 REQA+anticollision+SELECT (0 = card ACTIVE) | 7 acquire (hold the
#      reader, returns the saved recognition state) | 8 release (a2 = the
#      saved state). Ops 7/8 wrap a whole read session: the hold is what
#      keeps the stock recognition task from resetting the chip between
#      the commands, and the release is what leaves the unit in its normal
#      state.
#
# Antenna map (corrected - see the tunnel report, section 4): there are
# TWO reader antennas, TWO bays each. Antenna 1 covers slots 0 and 1
# (reader channels 0 and 2), antenna 2 covers slots 2 and 3 (channels 1
# and 3). The reader CHANNEL for a slot is the bit-swap 0,1,2,3 -> 0,2,1,3,
# i.e. channel = ((slot & 1) << 1) | ((slot >> 1) & 1) (slot_channel()).
# The partner slot sharing the same antenna is SLOT ^ 1; the partner
# reader CHANNEL is CHANNEL ^ 2. The tag must face the coil; ace.py rotates
# the spool with feed/unwind to bring it there (_gen1_search_read).
#
# Read sequence (tunnel-verified host order, inside ONE acquire/release):
#   acquire -> SELECT -> TXMODE |= 0x80 -> RXMODE |= 0x80 ->
#   BitFraming = 0 -> FIFO writes -> TRANSCEIVE -> RX bits -> FIFO reads
#   -> release.
# A full 16-byte reply is rx_bits == 0x80 (RxCRCEn strips the tag CRC); a
# page-0 read is pages 0..3, from which the 7-byte UID is taken with both
# ISO14443-3 BCC check bytes verified.
#
# Failure policy: every failure is "no tunnel" / "no tag" - never an error
# the user sees, never a blocking call outside the greenlet that runs it.
# Tunnel ops are ONLY ever sent when the firmware string matches the known
# tunnel builds (CV1.3.87x) AND a probe op answers through the stub; a
# stock (or older community) unit sees no tunnel traffic at all.

import logging

# The V2 reader's genuine OpenSpool decoder and BCC-checked UID extraction
# are reused verbatim here instead of copied (one implementation, one set of
# verified semantics). ace_rc522 imports only json/logging, so this is safe
# both from Klipper and from the self-check's synthetic package.
from .ace_rc522 import AceTagReader

# --- host contract (docs/GEN1_TAG_TUNNEL.md, REPORT-RC522-TUNNEL-EN.md) ----

TUNNEL_MAGIC = 0x80000000
# Firmware string prefix of the known tunnel builds. Stock ACE Pro reports
# V1.3.863, the UID-only community build CV1.3.863 - neither matches, so
# neither is ever sent a tunnel op.
GEN1_TUNNEL_FW_PREFIX = 'CV1.3.87'
# The verified tunnel reference (REPORT-RC522-TUNNEL-EN.md): CV1.3.871.
GEN1_TUNNEL_FW_REFERENCE = 'CV1.3.871'

# Ops.
OP_READ_REG = 0
OP_WRITE_REG = 1
OP_FIFO_WRITE = 2
OP_PCD_XCV = 3
OP_FIFO_READ = 4
OP_RX_BITS = 5
OP_SELECT = 6
OP_ACQUIRE = 7
OP_RELEASE = 8

# RC522 registers this client touches (the full map is in the tunnel docs).
REG_COMIRQ = 0x04
COMIRQ_RX = 0x20             # ComIrqReg RxIRq: a reception ended
REG_ERROR = 0x06
REG_FIFOLEVEL = 0x0A
REG_BITFRAMING = 0x0D
REG_TXMODE = 0x12
REG_RXMODE = 0x13
REG_VERSION = 0x37
PCD_TRANSCEIVE = 0x0C
VERSION_REG_VALUE = 0xA1     # MFRC522-family VersionReg answer

# Replies.
SELECT_CARD_ACTIVE = 0       # op 6 status when a card is in ACTIVE state
FULL_FRAME_BITS = 0x80       # op 5 for a full 16-byte reply
RELEASE_OK = 0               # op 8 result

# NTAG READ(0x30) of `page` returns 16 bytes = pages page..page+3.
NTAG_READ = 0x30
# ISO14443-3 HLTA: puts the selected card to sleep. A halted card ignores
# the REQA that op 6 sends, so the other card on a shared antenna answers
# the next SELECT. No reply is expected.
HLTA = (0x50, 0x00)
NTAG_CC_MAGIC = 0xE1         # capability container, page 3 byte 0
# The user pages an OpenSpool NDEF record lives in. 4..39 covers a NTAG213
# (0x12 * 8 = 144 user bytes) and the NDEF area of a NTAG216 alike; a
# bigger tag is truncated at the same boundary multiACE's V2 reader uses.
OPENSPOOL_FIRST_PAGE = 4
OPENSPOOL_LAST_PAGE = 39
# Anycubic slot-record magic (first two bytes of page 4 on their tags,
# mirrored here only for classification - this client never writes).
ANYCUBIC_MAGIC = b'\x7b\x00'

DEFAULT_TIMEOUT = 3.0        # seconds per tunnel command
# A TRANSCEIVE (op 3) holds the firmware until its own wait ends: ~6.9 s on
# the tunnel build, data or not. The reply wait must cover it, or the next
# op is queued into a unit that is still blocked.
XCV_TIMEOUT = 10.0
POLL_INTERVAL = 0.005        # reply poll granularity (greenlet-yield safe)


class SessionAborted(Exception):
    """A tunnel session stopped by the caller's abort check."""


class NoReply(Exception):
    """An op got no reply in time: the session ends (release only), no
    further op is sent into a unit that may still be blocked."""


def slot_channel(slot):
    """The RC522 reader channel for an ACE slot (bay order 0..3).

    Two antennas, two bays each: antenna 1 covers slots 0/1 (reader
    channels 0/2), antenna 2 covers slots 2/3 (channels 1/3). The channel
    is the bit-swap of the slot - 0,1,2,3 -> 0,2,1,3:

        channel = ((slot & 1) << 1) | ((slot >> 1) & 1)

    The partner sharing the slot's antenna is `slot ^ 1`; its reader
    channel is `channel ^ 2`. Never pass the raw slot as the reader."""
    s = int(slot) & 0x3
    return ((s & 1) << 1) | ((s >> 1) & 1)


def pack_index(op, a1=0, a2=0, reader=0):
    """The unsigned packed tunnel request (see the module docstring)."""
    return (TUNNEL_MAGIC | ((int(reader) & 0x3) << 24)
            | ((int(op) & 0xFF) << 16)
            | ((int(a1) & 0x3F) << 8) | (int(a2) & 0xFF))


def as_signed32(value):
    """The wire form the firmware actually parses (signed strtol)."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & TUNNEL_MAGIC else value


def parse_code(response):
    """The op's byte result from a JSON-RPC reply, or None.

    STRICTLY `result.code`: a stock reply carries `code` at the TOP level
    (`{"result":{},"code":0,"msg":"success"}`) and must never be mistaken
    for a tunnel answer - only the stub's own writer puts the byte inside
    `result`. Masked to 8 bits (the stub formats a byte)."""
    if not isinstance(response, dict):
        return None
    result = response.get('result')
    if not isinstance(result, dict) or 'code' not in result:
        return None
    try:
        return int(result['code']) & 0xFF
    except (TypeError, ValueError):
        return None


def firmware_supports_tunnel(firmware):
    """True when the runtime firmware string is a known tunnel build.

    This is only the cheap pre-gate that keeps tunnel traffic away from
    stock/older units; the authoritative gate is a probe op that actually
    answers through the stub (Gen1TagTunnel.tunnel_available)."""
    txt = (firmware or '').strip().upper()
    return txt.startswith(GEN1_TUNNEL_FW_PREFIX)


def uid_from_page0(data):
    """The 7-byte NTAG UID from a page-0 read (16 bytes), hex, or ''.

    Thin delegation to the V2 reader's BCC-verified extractor
    (ace_rc522.AceTagReader.uid_from_page0): both ISO14443-3 BCC check
    bytes must verify, and an all-zero UID is rejected. Same rule the V2
    reader applies, one implementation."""
    return AceTagReader.uid_from_page0(data)


def classify_page0(data):
    """Coarse format label for a page-0 read: 'anycubic', 'ntag' or
    'unknown' (a card answered SELECT but the page read is not an
    NTAG/Anycubic layout - a MIFARE chip, or a corrupted read)."""
    try:
        if len(data) >= 2 and bytes(data[0:2]) == ANYCUBIC_MAGIC:
            return 'anycubic'
        if len(data) >= 16 and data[12] == NTAG_CC_MAGIC:
            return 'ntag'
    except (TypeError, IndexError):
        pass
    return 'unknown'


class Gen1TagTunnel:
    """Drive one ACE Pro's RC522 tunnel through `filament_recognition`.

    Runs in a greenlet (reactor.pause is used for the reply poll);
    `send_request_to` is multiACE's ordinary V1 request path, so the
    heartbeat and other traffic interleave on the same serial port with no
    extra locking. One instance per ACE is cached by ace.py; the support
    probe result is cached per (idx, firmware string)."""

    METHOD = 'filament_recognition'

    def __init__(self, ace, idx=0, timeout=DEFAULT_TIMEOUT):
        self.ace = ace
        self.idx = int(idx)
        self.timeout = float(timeout)
        # (firmware string, supported bool) from the last probe - re-probed
        # when the firmware string changes (a re-flash in the same session).
        self._support = None
        self._no_tunnel_said = set()
        self._release_fail_said = False
        # Why the last read_slot() returned None, for the caller's log line.
        self.last_reason = ''
        # What the reader chip saw on the last failed page read.
        self.last_page_diag = ''
        # Optional callable set by ace.py: returns a reason string when the
        # session must stop now (the firmware starts an insert of its own
        # and needs the reader back). Checked before every op except the
        # release. `aborted` carries the reason of the last abort.
        self.abort_check = None
        self.aborted = ''
        self.no_reply = False

    # -- plumbing ----------------------------------------------------------

    def _pause(self, seconds):
        self.ace.reactor.pause(seconds)

    def _req(self, packed, timeout=None):
        """One tunnel request: send + poll the reply box. Returns the raw
        JSON-RPC reply dict or None (timeout / no reply / send failed).
        The callback is invoked with KEYWORD arguments by the dispatcher
        (callback(self=<ace>, response=<ret>)); **kw tolerates both
        calling styles."""
        box = {}

        def _cb(*args, **kw):
            box['r'] = kw.get('response', args[-1] if args else None)

        try:
            self.ace.send_request_to(
                self.idx,
                {'method': self.METHOD,
                 'params': {'index': as_signed32(packed)}}, _cb)
        except Exception as e:
            logging.info('[multiACE] gen1 tag tunnel: send failed on ACE %d: '
                         '%s', self.idx, e)
            return None
        deadline = (self.ace.reactor.monotonic()
                    + (self.timeout if timeout is None else float(timeout)))
        while 'r' not in box and self.ace.reactor.monotonic() < deadline:
            self._pause(POLL_INTERVAL)
        return box.get('r')

    def _op(self, op, a1=0, a2=0, reader=0, timeout=None):
        """One packed op, result byte or None. Raises SessionAborted when
        the abort check fires (never for the release)."""
        if op != OP_RELEASE and self.abort_check is not None:
            why = self.abort_check()
            if why:
                self.aborted = str(why)
                raise SessionAborted(self.aborted)
        if timeout is None and op == OP_PCD_XCV:
            timeout = XCV_TIMEOUT
        r = self._req(pack_index(op, a1, a2, reader), timeout=timeout)
        if r is None and op != OP_RELEASE:
            self.no_reply = True
            raise NoReply('op %d got no reply' % op)
        return parse_code(r)

    # -- support -----------------------------------------------------------

    def _fw_string(self):
        try:
            return (self.ace._ace_models.get(self.idx) or ('', ''))[1]
        except Exception:
            return ''

    def support_state(self):
        """(firmware, supported) from the last probe, or None - status
        surfacing only, never triggers traffic."""
        return self._support

    def _note_no_tunnel(self, fw, probed=False):
        key = (fw, bool(probed))
        if key in self._no_tunnel_said:
            return
        self._no_tunnel_said.add(key)
        if not firmware_supports_tunnel(fw):
            logging.info(
                '[multiACE] gen1 tag tunnel: ACE %d firmware %r has no '
                'RC522 tunnel (needs the community tunnel build %s) - the '
                'unit keeps its normal status-only tag path',
                self.idx, fw or '?', GEN1_TUNNEL_FW_PREFIX + 'x')
        else:
            logging.info(
                '[multiACE] gen1 tag tunnel: ACE %d firmware %r matches the '
                'tunnel builds but a probe op did not answer through the '
                'stub - no tunnel traffic from here',
                self.idx, fw or '?')

    def tunnel_available(self, timeout=None):
        """True only after a probe op answered through the stub.

        Gate 1 (cheap): the firmware string must match the known tunnel
        builds. Gate 2 (authoritative): op 0 read VersionReg 0x37 must come
        back with a `result.code` - a stock reply has no `result.code`, so
        a stock (or older community) unit can never pass. The result is
        cached per firmware string; a probe is one command."""
        fw = self._fw_string()
        if not firmware_supports_tunnel(fw):
            self._note_no_tunnel(fw)
            self._support = (fw, False)
            return False
        if self._support is not None and self._support[0] == fw:
            return bool(self._support[1])
        try:
            v = self._op(OP_READ_REG, REG_VERSION, reader=0,
                         timeout=timeout)
        except SessionAborted as e:
            # Not a verdict on the firmware: probe again next time.
            self.last_reason = 'aborted: %s' % e
            return False
        except NoReply:
            v = None
        ok = v is not None
        self._support = (fw, ok)
        if not ok:
            self._note_no_tunnel(fw, probed=True)
        elif v != VERSION_REG_VALUE:
            # The stub answered (a code exists) but reader 0 does not read
            # 0xA1 - a wiring/chip problem on that channel, not a missing
            # tunnel. Say it once; the read will simply find no card.
            logging.info(
                '[multiACE] gen1 tag tunnel: ACE %d VersionReg on reader 0 '
                'reads 0x%02X (expected 0x%02X) - tunnel present, check '
                'that channel if reads fail', self.idx, v, VERSION_REG_VALUE)
        return ok

    # -- reader session ----------------------------------------------------

    def acquire(self):
        """op 7: hold the reader. Returns the saved recognition state byte,
        or None (no tunnel / lost reply - the caller must not proceed)."""
        return self._op(OP_ACQUIRE)

    def release(self, state):
        """op 8: restore the state acquire() returned. Mandatory: without
        it the unit stays paused and reports status=busy. Logs once when a
        release fails (the unit may need a power cycle - klippy.log must
        say why)."""
        ok = self._op(OP_RELEASE, a2=int(state) & 0xFF) == RELEASE_OK
        if not ok and not self._release_fail_said:
            self._release_fail_said = True
            logging.warning(
                '[multiACE] gen1 tag tunnel: ACE %d release (op 8, state '
                '%d) did not confirm - the stock recognition state was not '
                'restored; a power-cycle of the ACE clears it',
                self.idx, int(state) & 0xFF)
        return ok

    def select(self, reader):
        """op 6: bring a card to ACTIVE. True when a card is in the field
        (status 0) - works for ANY ISO14443A tag, unlike the firmware's own
        Anycubic-only identification."""
        return self._op(OP_SELECT, reader=reader) == SELECT_CARD_ACTIVE

    def read_page(self, page, reader=0, count=16, timeout=None):
        """One NTAG READ(0x30) of `page`; 16 data bytes or None.

        The exact tunnel host order: TXMODE |= 0x80 (the op-1 write to
        0x12 also runs the firmware's full reader bring-up), RXMODE |=
        0x80 (RxCRCEn verifies and strips the tag's 2 CRC bytes), then
        BitFraming = 0, the bare 30 <page> frame pushed byte by byte, the
        TRANSCEIVE, RX bits, FIFO reads. Any lost op returns None - a
        partial FIFO is not a page."""
        v = self._op(OP_READ_REG, REG_TXMODE, reader=reader, timeout=timeout)
        if v is None:
            return None
        self._op(OP_WRITE_REG, REG_TXMODE, v | 0x80, reader=reader,
                 timeout=timeout)
        v = self._op(OP_READ_REG, REG_RXMODE, reader=reader, timeout=timeout)
        if v is None:
            return None
        self._op(OP_WRITE_REG, REG_RXMODE, v | 0x80, reader=reader,
                 timeout=timeout)
        self._op(OP_WRITE_REG, REG_BITFRAMING, 0x00, reader=reader,
                 timeout=timeout)
        for i, b in enumerate((NTAG_READ, int(page) & 0xFF)):
            self._op(OP_FIFO_WRITE, i, b, reader=reader, timeout=timeout)
        self._op(OP_PCD_XCV, 2, PCD_TRANSCEIVE, reader=reader, timeout=timeout)
        bits = self._op(OP_RX_BITS, reader=reader, timeout=timeout)
        # A real page reply is 16 bytes = 0x80 rx bits; anything else is a
        # partial reply or NO reply (the FIFO then still holds the previous
        # transceive - never hand that to the decoder).
        if bits != FULL_FRAME_BITS:
            # What the chip saw instead. On the reference unit op 5 can come
            # back without a code although the reply arrived (HW 2026-10-03:
            # ComIrqReg 0x64 = RxIRq, ErrorReg 0, FIFOLevel 16). A full,
            # error-free 16-byte reception counts as the page then.
            err = self._op(OP_READ_REG, REG_ERROR, reader=reader,
                           timeout=timeout)
            irq = self._op(OP_READ_REG, REG_COMIRQ, reader=reader,
                           timeout=timeout)
            lvl = self._op(OP_READ_REG, REG_FIFOLEVEL, reader=reader,
                           timeout=timeout)
            self.last_page_diag = ('rx_bits=%s ErrorReg=%s ComIrqReg=%s '
                                   'FIFOLevel=%s' % tuple(
                                       '-' if v is None else '0x%02X' % v
                                       for v in (bits, err, irq, lvl)))
            full_fifo = (bits is None and err == 0 and irq is not None
                         and (irq & COMIRQ_RX) and lvl is not None
                         and (lvl & 0x7F) == count)
            if not full_fifo:
                return None
        out = bytearray()
        for i in range(count):
            b = self._op(OP_FIFO_READ, i, reader=reader, timeout=timeout)
            if b is None:
                return None
            out.append(b)
        return bytes(out)

    def halt(self, reader=0, timeout=None):
        """Send HLTA to the currently selected card (TxCRC is on after a
        read_page, which set TXMODE bit 7). The card answers nothing; the
        op results are ignored. Best effort: if the halted card wakes up
        again (a full reader bring-up re-powers the field), the caller sees
        its UID again and treats the read as 'only the known card'."""
        self._op(OP_WRITE_REG, REG_BITFRAMING, 0x00, reader=reader,
                 timeout=timeout)
        for i, b in enumerate(HLTA):
            self._op(OP_FIFO_WRITE, i, b, reader=reader, timeout=timeout)
        self._op(OP_PCD_XCV, len(HLTA), PCD_TRANSCEIVE, reader=reader,
                 timeout=timeout)

    def read_openspool(self, reader):
        """Read the NTAG user pages and decode an OpenSpool NDEF record.
        Returns the identity dict or None. One 16-byte READ per 4 pages;
        stops at the first failed chunk (the record, when present, sits at
        the start of the user area). `userdata_complete` tells whether
        every chunk was read, i.e. whether a missing record is a fact."""
        data = bytearray()
        page = OPENSPOOL_FIRST_PAGE
        self.userdata_complete = False
        while page <= OPENSPOOL_LAST_PAGE:
            chunk = self.read_page(page, reader=reader)
            if chunk is None:
                break
            data += chunk
            page += 4
        else:
            self.userdata_complete = True
        return AceTagReader._openspool_decode(bytes(data)) if data else None

    def read_slot(self, slot, page=0, userdata=True, skip_uids=()):
        """The full operator/auto read: acquire -> SELECT on the slot's
        reader channel -> READ -> release. Returns a dict or None:

            {'slot', 'reader', 'page', 'data' (bytes), 'uid' (bare upper
             hex or ''), 'format' ('openspool' | 'anycubic' | 'ntag' |
             'unknown'), 'openspool' (dict or None), 'saved',
             'userdata_complete' (all user pages read)}

        `reader` is the RC522 channel (slot_channel(slot): 0,1,2,3 ->
        0,2,1,3), never the raw slot. `userdata` also reads the OpenSpool
        user pages when page 0 shows a plain NTAG capability container.
        None = no card / no reply / no tunnel; the release runs on every
        path.

        `skip_uids`: UIDs (upper hex) already known to belong to the
        partner slot on the shared antenna. When the selected card is one
        of them it is halted and the field is selected once more, so the
        other card can answer. Only for page 0 (the UID comes from it)."""
        slot = int(slot) & 0x3
        self.last_reason = ''
        self.aborted = ''
        self.no_reply = False
        try:
            return self._read_slot(slot, page, userdata, skip_uids)
        except SessionAborted as e:
            self.last_reason = 'aborted: %s' % e
            return None
        except NoReply as e:
            self.last_reason = 'no reply (%s) - session ended' % e
            return None

    def _read_slot(self, slot, page, userdata, skip_uids):
        channel = slot_channel(slot)
        page = int(page) & 0xFF
        saved = self.acquire()
        if saved is None:
            self.last_reason = 'reader not acquired (op 7 got no reply)'
            return None
        try:
            if not self.select(channel):
                self.last_reason = ('no card answered SELECT on channel %d'
                                    % channel)
                return None
            skip = {str(u).strip().upper() for u in (skip_uids or ())
                    if u} if page == 0 else set()
            data = self.read_page(page, reader=channel)
            if data is None:
                self.last_reason = ('card selected, but the page %d read '
                                    'failed (%s)' % (page,
                                                     self.last_page_diag))
                return None
            uid = uid_from_page0(data) if page == 0 else ''
            if uid and uid in skip:
                logging.info('[multiACE] gen1 tag tunnel: ACE %d slot %d: '
                             'partner card %s in the field - halting it',
                             self.idx, slot, uid)
                self.halt(channel)
                if not self.select(channel):
                    self.last_reason = ('only the partner slot\'s known '
                                        'card %s answered' % uid)
                    return None
                data = self.read_page(page, reader=channel)
                if data is None:
                    self.last_reason = ('second card selected after '
                                        'halting %s, but the page read '
                                        'failed' % uid)
                    return None
                uid = uid_from_page0(data)
                if uid in skip:
                    self.last_reason = ('only the partner slot\'s known '
                                        'card %s answered (halt did not '
                                        'hold)' % uid)
                    return None
            fmt = classify_page0(data)
            out = {'slot': slot, 'reader': channel, 'page': page,
                   'data': data, 'uid': uid, 'format': fmt,
                   'openspool': None, 'saved': saved,
                   'userdata_complete': False}
            if (userdata and uid and page == 0
                    and len(data) >= 16 and data[12] == NTAG_CC_MAGIC):
                op = self.read_openspool(channel)
                out['userdata_complete'] = bool(
                    getattr(self, 'userdata_complete', False))
                if op:
                    out['openspool'] = op
                    out['format'] = 'openspool'
            return out
        finally:
            self.release(saved)

    # -- helpers for callers ----------------------------------------------

    @staticmethod
    def hex_dump(data):
        """'04 22 52 FC ...' for console/status lines."""
        try:
            return ' '.join('%02X' % b for b in bytes(data))
        except (TypeError, ValueError):
            return ''

# multiACE Driver Development Guide

This guide explains how to add support for a new filament unit (an "MMU")
to multiACE. It walks through every step using the driver for the
**Creality CFS Nano** as the worked example, because that driver was built
exactly this way: a new protocol, a generic USB-RS485 dongle, a box that
does not map one to one onto the commands multiACE already speaks.

Read it together with the code it describes:

| File | What it is |
|---|---|
| `klipper/extras/ace_protocol.py` | `AceProtocol` base class, gate constants, `TELEMETRY_METHODS` |
| `klipper/extras/ace_unit.py` | the capability fields (`STATIC_FIELDS`) and `caps_for` |
| `klipper/extras/ace_drivers.py` | the driver registry and its config switches |
| `klipper/extras/ace_protocol_cfs.py` | the CFS Nano codec, the example of this guide |
| `klipper/extras/ace_protocol_v1.py`, `ace_protocol_v2.py` | the ACE Pro and ACE 2 codecs |
| `tests/units/contract.py` | the codec contract as an executable checker |
| `tests/units/fake_cfs.py`, `tests/units/scenarios.py` | a fake device and the scenarios that run it |

## 1. How multiACE sees a unit

multiACE talks to up to four units. Each unit has four lanes (slots) and
sits on one serial port. The core (`ace.py` and its mixins) never speaks a
device protocol itself. It sends **host requests** in one fixed vocabulary
and reads **host responses** and a **status dict** back:

```
ace.py  --{'method': 'feed_filament', 'params': {...}}-->  codec  --> wire
ace.py  <--{'id', 'code', 'msg', 'result'}---------------  codec  <-- wire
```

A **driver** is two things:

1. a **codec class**, a subclass of `AceProtocol`, that finds the unit's
   ports, opens them, turns host requests into wire traffic and wire
   traffic back into host responses;
2. a **registry entry** in `ace_drivers.py` with a config switch, so every
   user can turn the driver off.

What a unit can and cannot do is described by **capabilities** (caps), a
fixed set of fields per codec. The core branches on caps, never on the
kind of unit. A new driver is therefore mostly a codec plus an honest caps
table; it should need no "if this is a CFS" anywhere in the core.

## 2. Ground rules

These rules come from real failures. A driver that breaks one of them can
stop a running print or the whole printer.

1. **Never block the Klipper reactor.** The reactor runs the printer's
   motion. Serial reads and writes, waits and retries belong in the codec's
   own threads (the threaded engine, section 5) or are bounded to a few
   milliseconds. A blocked reactor shows up as "Timer too close" and an MCU
   shutdown.
2. **A unit never ends the printer.** A dead port, a garbled frame, a
   missing answer: the codec raises `IOError`/`OSError` (the core
   reconnects or pauses) or simply does not answer (the 8 s watchdog
   reconnects). It never raises anything else out of a callback, and never
   calls a Klipper shutdown.
3. **Missing is not zero.** If the device does not report a value, the key
   is absent. Never send `0` for an unknown temperature, `ready` for an
   unknown motion state, or an all-zero colour for an unknown identity
   that has a type. The core treats a present value as a measurement.
4. **Only string keys** in every dict the codec returns, at every depth.
   Klipper's status encoder shuts the printer down on an int key.
5. **Device truth beats the host cache.** A status answer must come from a
   poll that is newer than the last motion command (section 4.4).
6. **Unknown requests are refused, never reinterpreted.** A method the
   unit cannot do is answered `unsupported` before it reaches the wire.
7. **Every config option you add is read unconditionally.** Klipper halts
   with "Option ... is not valid" when an option is present in the config
   but the code never read it (for example because it was only read when
   the driver is on).

## 3. Step by step

### 3.1 Find out what the device can do

Before writing a codec, measure the device. For the Nano this was a small
standalone probe script on a USB-RS485 dongle that sent single frames and
printed the hexdump of every reply. Write down per command: the request
bytes, the reply bytes, how long the reply takes, whether the command
blocks until a motion ends, and what the device does physically. **Verify
every motion by a physical measurement or a distance sensor, never by the
reply alone**: devices acknowledge commands they then ignore.

What the Nano measurements gave (all values are what the codec relies on):

- Frames `F7 ADDR LEN STATUS FUNC DATA CRC8`, `LEN = len(DATA) + 3`,
  CRC-8 polynomial 0x07, init 0, over `LEN..DATA`; 230400 baud, 8N1, half
  duplex. The host sends `STATUS 0xFF` (0x00 for addressing commands).
- `GET_SLAVE_INFO` (0xA1) broadcast to 0xFF answers with a device type
  (8 for the Nano), a mode byte and a 12-byte unique id. The box then
  answers on its own fixed address. It is mute for about 30 s after power
  on.
- Presence: 0x08 returns a bit mask, bit n = slot n holds filament. Box
  state: 0x0A, last byte = idle, print, locked after retract, locked after
  feed. A 0x0A reply with status 0x30 carries an insert/remove event per
  slot instead.
- Motion: 0x10 `<mask> 00` feeds until the box stops by itself (~44 mm/s);
  `<mask> 02` stops a running feed; `<mask> 05 <mm>` feeds a dosed
  distance (~3.7 mm/s); 0x11 `<mask> <mm>` retracts 5..255 mm; 0x04
  `<mask> 00` is print mode (clutch free, the extruder pulls the spool),
  0x04 `00 01` returns to feed mode and clears a lock.
- 0x0E returns four big-endian floats: a distance wheel per slot in mm.
- A feed the box ends by itself replies with status 0x09 (wheel stood while
  the motor ran) or 0x0A (maximum distance ran out). A command the box
  cannot take now replies with status 0x03.

### 3.2 Map the device onto the host vocabulary

The core speaks this vocabulary (contract R2, `tests/units/contract.py`):

- **Required** for any unit that moves filament: `get_info`, `get_status`,
  `feed_filament`, `unwind_filament`, `stop_feed_filament`,
  `start_feed_assist`, `stop_feed_assist`.
- **Optional**, behind caps: `discover_device`, `update_feeding_speed`,
  `drying`, `drying_stop`, `set_valve`, `set_feed_check`,
  `feed_or_rollback_raw`, `get_feed_info`, `get_filament_info`,
  `filament_identify`, `filament_recognition`, `stop_unwind_filament` and
  a few diagnostics.

Lengths are mm, speeds mm/s; converting them is the codec's job.

The Nano had no one-to-one command for most of these, so the mapping is a
small design of its own:

| Host request | Nano |
|---|---|
| `discover_device` | 0xA1 broadcast, learn the address; the unique id becomes `uid1..3` |
| `get_info` | 0x14 version string, model `Creality CFS Nano` |
| `get_status` | 0x08 presence + 0x0A box state, assembled into one status dict |
| `feed_filament` (long) | unlock, `10 <mask> 00`, resumed when the box stops early after real progress |
| `feed_filament` (short, the seat press) | `10 <mask> 05 <mm>` |
| `stop_feed_filament` | `10 <mask> 02` |
| `unwind_filament` | `11 <mask> <mm>` in pieces of at most 255 mm |
| `start_feed_assist` | print mode `04 <mask> 00`: the box has no feed assist, it freewheels |
| everything else | `code -2`, `msg unsupported:<method>`, nothing on the wire |

Decide this table before writing code; it is also where you find the caps
that are `False` for your unit.

### 3.3 Write the codec class

Create `klipper/extras/ace_protocol_<name>.py` with a subclass of
`AceProtocol`. It must import nothing from `ace.py` (the core imports it,
not the other way round). The class attributes and methods:

```python
class AceProtocolCfs(AceProtocol):
    NAME = 'cfs'                 # unit kind, used in logs and get_status
    DEFAULT_BAUD = 230400
    USB_IDS = (('1a86', '7523'),)
    PROBE_BEFORE_CLAIM = True    # see 3.4
    CAPS_STATIC = {...}          # every field of ace_unit.STATIC_FIELDS
    SERIAL_KWARGS = {'timeout': 0.05, 'write_timeout': 0.25,
                     'exclusive': True}

    @classmethod
    def discover(cls): ...               # candidate port paths
    @classmethod
    def open_transport(cls, path, baud, **kw): ...   # a pyserial-like object
    @classmethod
    def probe_device(cls, path, query_timeout=0.4): ...  # 3.4
    def encode_request(self, request, next_id=None): ...  # host request -> bytes
    def decode_frames(self, buffer): ...                  # bytes -> list of responses
    def initial_handshake_requests(self): ...             # sent on every connect
    def make_default_info(self): ...                      # status dict before the first poll
```

- `discover()` returns only paths this codec may own (by-path names whose
  USB vendor:product matches). The core removes ports Klipper itself uses
  (`[mcu]` serial lines, `[ace] exclude_ports`) after discovery.
- `decode_frames(buffer)` consumes complete frames from the bytearray it is
  given and leaves a partial frame in it.
- `initial_handshake_requests()` for the Nano is `discover_device`, then
  `get_info`.
- `make_default_info()` returns a status dict with four empty slots.

**When the wire does not match the host vocabulary.** For the ACE units
`encode_request` builds one wire frame per host request. The Nano needs
several box commands for one host request and its bus takes only one
command at a time. So `open_transport` returns a `CfsLink`, an object with
the pyserial surface (`write`, `read`, `in_waiting`, `fileno`,
`is_open`, `close`, `reset_input_buffer`). `encode_request` simply
serialises the host request as JSON; `CfsLink.write()` receives it, runs the
box transaction(s) half duplex inside that call and queues exactly one host
response, which `read()` hands back and `decode_frames` decodes. This keeps
all box timing inside the codec's thread. Use this pattern whenever one
host request is not one wire frame.

**Long motions as jobs.** A feed of two metres takes about a minute. The
Nano's link starts a motion as a job, answers the host request at once and
advances the job on every later `write()` (the 1 Hz status heartbeat calls
`get_status`). While a job runs, `get_status` reports `status: 'busy'`,
which is what the core waits on. When the job ends, the link logs why
(`length reached`, `stopped by the host`, `stopped by the box, status 09`,
`timeout`) and returns the box to the wanted mode.

### 3.4 Discovery on a generic dongle

The Nano sits behind a generic CH340 USB-RS485 adapter. Such a chip can
belong to anything, so the codec sets `PROBE_BEFORE_CLAIM = True` and
implements `probe_device(path, query_timeout)`. The core then claims a
discovered path only after the probe answered `{'ok': True, ...}`. The
probe:

- opens the port, sends 0xA1, checks the device type, reads the version;
- returns `{'ok', 'category', 'error', 'uid', 'firmware'}`; `category` is
  one of `verified`, `no_response`, `invalid_response`, `invalid_identity`,
  `port_in_use`, `permission_denied`, `serial_error`;
- always closes the port, is bounded in time, and never raises.

The core runs it only while the printer is idle, in a worker thread with the
reactor paused in short steps, one probe per scan, and caches the result
per path (a mute box after power on is asked again soon, a port that keeps
silent backs off).

If the probe tells two devices of the same kind apart, also give the class
`uid_key(words)` (and set the cap `has_stable_id`). The key decides which
MMU place (1..4) the unit lands on, independent of the USB port. The Nano's
key is built from its 12-byte unique id, the ACE 2's from its 96-bit MCU id.
A unit without such an id is known by its port.

**Port access.** A generic dongle is usually `root:dialout 0660`, and the
Klipper user is not in `dialout`. The Nano driver ships a udev rule
(`deploy/99-multiace-rs485.rules`) that the installer and the firmware bake
install. A new dongle type needs the same.

### 3.5 Fill in the caps

`CAPS_STATIC` must name every field in `ace_unit.STATIC_FIELDS`. A missing
field reads as `None`, which the core treats as "unknown" and usually as
"no". Be conservative: a cap that claims too much makes the core rely on a
signal that does not exist.

| Field | Meaning | Nano |
|---|---|---|
| `transport_engine` | `reactor_fd` (reader on the reactor thread) or `threaded` (reader and writer threads) | `threaded` |
| `blocking_write_on_reactor` | the write runs on the reactor thread | False |
| `reconnectable` | the core may close and reopen the port | True |
| `presence_sensor` | the unit reports per-slot presence | True |
| `empty_token` | the slot status word that means empty (compared by prefix) | `empty` |
| `has_slot_status_truth` | per-slot motion state (`feeding`, `assisting`, `feed_error`, ...) | False |
| `busy_wait_floor` | waits for ready use a 60 s floor (long motions) | True |
| `fa_monitor` | the feed-assist monitor may watch slot states | False |
| `needs_fa_prestop` | an assist must be stopped before a motor command | False |
| `has_rollback_assist` | feed mode 3 (rollback assist) exists | False |
| `no_freewheel` | the extruder cannot pull the spool without assist | False |
| `freewheel_print_mode` | `start_feed_assist` means "clutch free", also sent before an unload pull | True |
| `self_stops_on_resistance` | a feed ends by itself at resistance | True |
| `has_decoder` | a lane encoder via `get_feed_info` | False |
| `locate_method` | `led`, `jog` or `unlock` (how ACE_LOCATE identifies the unit) | `unlock` |
| `calibration_feed` | `chunked` (stepwise) or `single` (one feed, the unit reports the distance) | `single` |
| `tag_identity_in_status` | the status carries the slot identity, no per-slot tag query | True |
| `fw_tag_pair_dedupe` | the firmware reader may report the neighbour's tag | False |
| `has_humidity`, `has_dryer`, `has_exhaust_valve`, `has_feed_check` | optional hardware | False |
| `flash_engine` | `iap_v1`, `ota_v2` or `none` | `none` |
| `v2_command_set` | the ACE 2 diagnostics apply | False |
| `lanes_concurrent` | two lanes may move at the same time | False (one motor) |
| `speed_unit` | how speed parameters are honoured (`mm_s`, `none`) | `none` |
| `busy_reply` | how "busy" is reported (`code0_forbidden`, `error_code`, `code1_forbidden`) | `code1_forbidden` |
| `has_stable_id` | `uid_key` tells devices apart | True |
| `v1_fa_health_log` | the ACE Pro log-only assist monitor | False |

The firmware-derived caps in `ace_unit.DYNAMIC_FIELDS` (tag tunnels,
community firmware) are computed only for the ACE codecs; a new codec gets
`False` for them.

If your unit needs a behaviour the core does not have a cap for yet, add a
new cap (field in `STATIC_FIELDS`, value in every codec, a reason in
`REASONS`) and branch the core on it. The Nano added three this way:
`freewheel_print_mode`, `locate_method`, `calibration_feed`. Never add a
branch on `NAME`.

### 3.6 Honour the response and status contract

`tests/units/contract.py` is the authoritative list. The parts every new
codec touches:

**Responses** are `{'id': int, 'code': int, 'msg': str, 'result': dict}`
with the request's `id`.

- Success is exactly `code 0`, `msg 'success'`.
- Busy (the unit cannot take the command now) is `code 1`,
  `msg 'FORBIDDEN'`. The Nano maps reply status 0x03 to this.
- Not supported is `code -2`, `msg 'unsupported:<method>'`.
- At most one response per request. A request that gets no answer from
  the device gets no response at all (callers have their own deadlines,
  the watchdog covers silence). Never return `None` as a response.

**Status dict** (`get_status.result`):

- `status`: `'busy'` only while a motor really runs, else `'ready'`. An
  active feed assist or print mode is not busy.
- `slots`: exactly four dicts, `index` 0..3, each with `status` (presence
  word: prefix `empty` = empty; a unit without a presence sensor says
  `nosensor`, never `unknown`), `rfid` (int), `sku`, `type`, `brand` (str),
  `color` (three ints). The Nano has no tags, so it reports empty strings,
  `rfid 0`, `color [0, 0, 0]`.
- `temp`, `dryer_status`, `humidity`, `slot_status`: only when the unit
  really measures them (see the caps). Otherwise the key is absent.
- Never set host keys (`subtype`, `uid`, `tag_format`, `tag`, `colors`);
  the core fills those from its own merge chain.
- Return a new dict for every poll.

**Freshness rule.** A status answer may only come from device reads that
are newer than the last motion command. The core clears its busy stamp
when a status arrives, so an old snapshot that says `ready` would let the
next command run into a still-moving unit. If no fresh read is possible,
do not answer; the watchdog then handles the silence. The Nano link
answers `get_status` only after a fresh 0x08 presence read.

**Optional status extensions.** A codec may add keys the core reads only
when present. The Nano adds:

- `box_state` / `box_event` (diagnostics);
- `motion`: `{'seq', 'slot', 'kind', 'mm', 'running'}` for the running or
  last motion, with `target`/`short` for a retract and `stop` (`slip` or
  `distance`) when the box ended a feed by itself. The core uses it to
  extend a feed deadline while the unit still reports the feed as running,
  to read the real distance for the bowden calibration, to mark a retract
  that came back short as not verified, and to word a failed load as slip
  at the MMU instead of "no transport".

If you add an extension like this, document its keys and make every
reader tolerate its absence.

### 3.7 Threading and failures

- With `transport_engine: 'threaded'` the core runs the codec's reader and
  writer in background threads and delivers responses on the reactor
  thread. Everything the link does in `write()` runs in the writer thread,
  which is why the Nano link may wait for replies there.
- A dead port must surface as an exception from `read()`/`in_waiting`
  (the Nano raises `LinkDead`, an `IOError`). The core then reconnects
  (three tries with backoff), and pauses a print only if the unit feeds a
  loaded head. Never report a dead port by setting `is_open` to False: the
  reader would stop silently and nobody would notice.
- `write()` must not raise for a request it already sent; the core would
  repeat it after the reconnect and a motion would run twice.
- `close()` returns only when the port is free; the core reopens right
  after.

### 3.8 Register the driver

Add one entry to `DRIVERS` in `ace_drivers.py`:

```python
Driver('cfs', AceProtocolCfs, 'enable_cfs', False, label='CFS Nano'),
```

- `default=False` and an empty `tested` note for a driver nobody on the
  project has run on hardware (a test enforces this). It ships opt-in and
  is switched on by default in a later version once it has field time
  without problems.
- Order matters: discovery gives a path to the first enabled codec that
  claims it.
- Ship the switch commented in `config/extended/ace.cfg`
  (`#enable_cfs: false`, a test enforces this). Extra options of your
  driver (the Nano has `cfs_usb_ids`) go there too and are read
  unconditionally in `ace.py`.

A disabled driver probes no port and connects no unit.

### 3.9 Packing lists

A new module must reach every install path. Add it to:
`install_multiace.sh` (check, copy, chmod), `uninstall_multiace.sh`, the
post-commit hook (`tools/git-hooks/post-commit`, `TRACKED_FILES`),
`managed/manifest.json`, the release workflow and `CORE_FILES` in `ace.py`
(kept sorted). `python3 -m tests.units.tools.module_lists` checks that all
copies agree.

## 4. Testing

Hardware access is not required to start; the unit test harness runs the
real `ace.py` against fake devices on a virtual clock.

1. **A fake device** (`tests/units/fake_cfs.py`): models the device at the
   byte level, answers frames, moves its lanes on the virtual clock, and
   has knobs for the failure cases you measured (box stops at resistance,
   a silent stop, a mute start, a slow restart, a rejected command).
   Answer immediately in the fake; the link blocks the writer thread while
   it waits, and the virtual clock does not advance during that wait.
2. **Harness seams** (`tests/units/harness.py`): patch your codec's
   `discover`, the raw open and `probe_device` so they reach the fake.
3. **Codec tests** (`tests/units/test_cfs_codec.py`): frames and CRC
   against the bytes you measured, the status from presence and box state,
   every motion job against the fake (feed with resume, stop, dosed feed,
   retract in pieces, busy box, dead port).
4. **Scenarios** (`tests/units/scenarios.py`): connect, driver disabled,
   insert/remove, unsupported motion. Each scenario's trace is frozen as a
   golden file; the contract checker runs on every request and response and
   fails the test on any violation.
5. Run the whole suite: `python3 -m pytest tests -q`. A new driver must not
   change any existing golden except where the driver registry itself is
   listed (the switch in the config read, the driver in `drivers`).

Then hardware:

- `enable_<name>: true`, restart the Klipper **service** (a firmware
  restart does not reload Python modules).
- Check the startup lines: the `drivers:` line names your driver, the probe
  line says verified, the unit connects on an MMU place.
- Insert and remove spools, then load, unload and swap on the bench,
  verifying every distance physically.
- A long print with swaps before the default is ever switched on.

## 5. Contributing

New drivers come as pull requests into the tree; multiACE does not load
external driver code. Keep the PR to the driver: the codec, its registry
entry and config switch, the packing lists, the fake device, the tests and
the docs. A change the core needs (a new cap, a new optional status key)
goes in the same PR with a short explanation of why no existing cap fits.
State plainly what you measured on hardware and what you did not.

# External humidity for auto-dry - `ACE_SET_HUMIDITY`

An **external sensor stack** (a BLE feeder on the printer host, Home
Assistant, an MQTT bridge, anything that can send G-code through Moonraker)
can push a humidity reading into multiACE. While that reading is fresh it
drives **humidity-controlled drying on any generation** - including the ACE
Pro (Gen 1), which has no humidity sensor of its own.

The reading lives in multiACE's own per-unit store; it is **never** written
into `_info_per_ace` (the 1 Hz heartbeat rebuilds that dict). No device
traffic is involved in pushing it - the ACE may be mid-reconnect, the value
just ages.

Companion documents: `ENGINE_API.md` (the gcode/status contract),
`SEND_TO_MULTIACE.md` (the web upload endpoint - unrelated).

## The command

```
ACE_SET_HUMIDITY ACE=<n> RH=<float> [TEMP=<float>] [TTL=<seconds>]
```

| Parameter | Required | Range | Meaning |
|---|---|---|---|
| `ACE` | yes | 0..3, and the unit must exist | index of the ACE the reading belongs to |
| `RH` | yes | 0..100 | relative humidity in % |
| `TEMP` | no | 0..100 °C | sensor temperature - informational, nothing regulates on it |
| `TTL` | no | > 0, clamped to 60..7200 s | how long this reading stays valid; default **900 s** |

Behaviour:

- The command is **idempotent and cheap**: it validates, stores, logs one
  line, and answers. It sends nothing to the ACE and reads nothing from it.
- Values outside the ranges are refused with a clear message naming the
  parameter (a level-200 error, so the touchscreen/Fluidd shows the reason).
- A `TTL` that validates but falls outside 60..7200 s is **clamped** and the
  clamp is named in the response and in klippy.log.
- The reading is **not persisted**: monotonic timestamps and a store that
  dies with Klipper is the only version whose age can be trusted. After a
  Klipper restart the feeder simply pushes again.

## The REST call the feeder runs

Exactly the call our own feeder makes (`ace_rh_feeder.moonraker.set_rh`),
with `ttl` from the feeder's config:

```sh
curl -s -X POST http://<printer-ip>/printer/gcode/script \
     -H 'Content-Type: application/json' \
     -d '{"script": "ACE_SET_HUMIDITY ACE=0 RH=42.5 TEMP=23.4 TTL=1800"}'
```

A push can also be observed from the console; the answer looks like:

```
[multiACE] ACE 0 external humidity: 42.5%rH, temp 23.4 C, valid for 1800s
```

The feeder should push on its own schedule (every 1-5 min is plenty - the
sensor itself moves slowly). Nothing else has to be done per push.

## TTL semantics (the important part)

- **Fresh wins over the internal sensor.** While a reading is inside its TTL,
  `_ace_humidity()` returns it - for an ACE 2 that *replaces* its own sensor
  value; for an ACE Pro it is the only reading there is. When the reading is
  stale or absent, control falls back to the internal value and the reported
  source changes accordingly.
- **Expiry stops a cycle we started from it.** If auto-dry started drying
  because of a pushed reading (any generation), and that reading then expires,
  multiACE stops the cycle - it never keeps heating on a value nobody is
  refreshing. One log line names the reason:
  `auto-dry STOP ACE n (external humidity reading expired (age 901s, TTL 900s))`.
- The check runs in the existing auto-dry tick, once per minute
  (`AUTO_DRY_INTERVAL`), so a cycle can overrun its TTL by at most one tick -
  no new thread, no new timer.
- **Only cycles multiACE started are touched.** A cycle started by hand, or a
  follower running its master's cycle, is never stopped by this path. A cycle
  that was started from the internal sensor keeps running even if an external
  reading expires - it never depended on it.
- **Refresh extends the cycle.** Pushing a new reading resets the TTL; a
  cycle started from external keeps running as long as readings keep coming.
- **An ACE 2 keeps its own sensor.** After an external reading expires, the
  normal internal rule applies again: if the ACE 2's own value is still
  above `rh_start`, a new cycle starts on the next tick, this time marked
  `internal`. A Gen 1 (no sensor of its own) stays off until a new reading
  arrives.
- **Restart safety.** Which cycles were started from an external reading is
  persisted (like the auto-dry ownership itself). After a Klipper restart the
  store is empty by design, so the first tick stops such a cycle with
  `external humidity reading gone (none since restart)` instead of letting it
  run to the device backstop. The feeder's next push can start a new cycle.
- If auto-dry is switched **off** while such a cycle runs, expiry still stops
  it; `enabled=0` is not a way to dry on.

## Auto-dry configuration for a Gen 1 (ACE Pro)

With a reading source present, a Pro can be its own master - the old "pick an
ACE 2 to follow" rule is relaxed **while it has an external reading**:

```
ACE_SET_HUMIDITY ACE=1 RH=58 TTL=1800     ; push first (the store must exist)
ACE_SET_AUTO_DRY ACE=1 ENABLE=1 RH_START=50 RH_END=40 TEMP=50
```

- `MASTER=-1` (or `MASTER` = the unit's own index, the convention the local
  feeder patch used) means "regulate on my own (external) reading".
- `MASTER=<ace>` keeps the follower role; a follower ignores its own pushed
  reading while it follows.
- The config command still refuses to enable a unit that has neither a master
  nor any pushed reading (it would be "on" while doing nothing), and still
  refuses `RH_START`/`RH_END` on a unit with no reading.

## Status (`get_status` / Moonraker)

`humidity` stays what it always was - the **device's own sensor value**.
Additive keys per ACE say where the effective reading comes from:

| Key | Value |
|---|---|
| `external_humidity` | last pushed value, reported even when stale (`null` if never pushed) |
| `external_humidity_age` | seconds since the last push (`null` if never pushed) |
| `external_humidity_ttl` | TTL of that reading in seconds |
| `external_humidity_fresh` | `true` = this reading is what control uses (external) |
| `external_humidity_cycle` | `true` = a running cycle was started from it |
| `humidity_source` | `"external"`, `"internal"` or `null` |
| `humidity_effective` | the value control actually uses right now |

Read it over REST:

```sh
curl -s 'http://<printer-ip>/printer/objects/query?ace'
```

## Tests

The project has no test suite; this is the test-in-a-script, next to the
Gen-1 flasher self-check:

```sh
python3 tests/ace_set_humidity_selfcheck.py
```

It imports the real `ace.py` and drives the real methods on a faked instance
(no Klipper, no hardware): command parsing/validation/clamping, no writes to
`_info_per_ace`, fresh-over-internal preference, fallback on staleness,
expiry-stop for V1 and V2, refresh, restart, roles, and the relaxed config
gates.

## Open points for the maintainer

1. **Default TTL and clamp range** - implemented as 900 s default,
   60..7200 s clamp. Our feeder sends 1800 s by config. Happy to change the
   numbers; the 60 s floor is tied to `AUTO_DRY_INTERVAL` (below it a reading
   can expire before the tick ever sees it).
2. **UI wording / placement** - the status keys above are enough for the web
   to render e.g. `42 %rH (external, 3 min old)` and to distinguish the
   device sensor from a pushed value. Decisions wanted: whether the existing
   humidity badge should show the *effective* value for a Pro, and the label
   for `humidity_source`.
3. **`ACE_SET_AUTO_DRY` role wording** - a Pro with a reading now gets the
   threshold message (`start/stop`) instead of the follower line, and its
   master dropdown can list another Pro that has readings. Whether the UI
   should offer "no master" explicitly is a UI question.
4. **Translations** - `msg.ace_humidity_set` was added to `en.json` and
   `de.json` (like the auto-dry messages); `zh.json` has no auto-dry keys at
   all, so it falls back to English.
5. **Hardware test** - needs one session on a real V1 + V2: push, watch the
   cycle start, kill the feeder, confirm the stop within ~1 minute (and that
   a hand-started cycle is untouched).

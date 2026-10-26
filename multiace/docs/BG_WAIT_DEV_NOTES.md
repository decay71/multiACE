# ACE_BG_WAIT Engineering Notes & Development Log

This document records the architectural decisions, hardware behaviors, real-world bug fixes, and testing workflows discovered while developing `ACE_BG_WAIT` on the Snapmaker U1 / multiACE platform.

---

## 1. Background Swap Lifecycle & Phase Progression

The background swap engine in `multiace/klipper/extras/ace_bg_swap.py` executes in an asynchronous greenlet. All state transitions are centralized through `_set_state(head, new_state)` and logged to the console:

```
[IDLE]
  │
  ▼
[QUEUED]      - Target head opted in and scheduled for background swap.
  │
  ▼
[FA_STOP]     - Forward-assist disarmed on background ACE unit.
  │
  ▼
[HEAT]        - Nozzle warms to unload/tip-forming temperature setpoint.
  │
  ▼
[PULL]        - Stall-free cold pull choreography moves executed while parked.
  │
  ▼
[RETRACT]     - ACE motor rewinds strand (~800-900mm) back into the unit.
  │
  ▼
[BOOKKEEP]    - Head marked empty via official APIs (sensor False, head_source cleared).
  │
  ▼
[LD_FEED]     - Target slot filament feeds through bowden tube to toolhead sensor.
  │
  ▼
[LD_GRIP]     - Extruder gears spin with forward-assist to seat tip into melt zone.
  │
  ▼
[LD_PRIME]    - Purge extruded through the open dock into catch bin (fine-chunked).
  │
  ▼
[DONE]        - Background swap complete. Arrival toolchange (T) is a 0-second no-op.
```

### Loaded Head vs. Empty Head Behavior
- **Loaded Head**: Executes both halves: **Unload** (`HEAT` $\to$ `PULL` $\to$ `RETRACT` $\to$ `BOOKKEEP`) followed by **Load** (`LD_FEED` $\to$ `LD_GRIP` $\to$ `LD_PRIME`).
- **Empty Head**: Bypasses the unload half entirely and proceeds directly to `LD_FEED`.

---

## 2. Real-World Traps Encountered & Solutions

### Trap 1: Klipper Boolean Parsing (`load_enabled: transport`)
- **Symptom**: `!! Unable to parse option 'load_enabled' in section 'ace_bg_swap'`.
- **Cause**: Standard Klipper `config.getboolean` strictly rejects non-boolean values. When `load_enabled` was configured to `transport` in `ace.cfg`, Klipper failed to initialize and halted.
- **Solution**: Replaced `config.getboolean('load_enabled', True)` in `ace_bg_swap.py` with custom string parsing:
  - Strings `'false'`, `'0'`, `'no'`, `'off'` $\to$ `load_enabled = False` (unload-only mode).
  - Strings `'true'`, `'1'`, `'yes'`, `'on'`, `'transport'` $\to$ `load_enabled = True` (full background load).

### Trap 2: Backward Compatibility with Older Printer Firmware
- **Symptom**:
  - `Option 'macro_layout_enabled' is not valid in section 'ace'`
  - `Section 'ace_macro_layout' is not a valid config section`
- **Cause**: Newer git commits introduced automatic frontend macro grouping in `ace_macro_layout.py`. Because the printer runs an older firmware/module set that does not have this file or the corresponding `config.getboolean` call in `ace.py`, Klipper halted.
- **Solution**: Commented out `#macro_layout_enabled: true` and the `#[ace_macro_layout]` section in `multiace/config/extended/ace.cfg`.

### Trap 3: Transient Phase Timing (`BOOKKEEP`)
- **Symptom**: `ACE_BG_AWAIT_PHASE` timed out at 120s when awaiting `BOOKKEEP`.
- **Cause**: `BOOKKEEP` performs quick Python dictionary updates and takes $<1\text{ms}$. Because `ACE_BG_AWAIT_PHASE` polled every 100ms with strict equality (`cur_phase == target_phase`), the engine passed through `BOOKKEEP` into `LD_FEED` between ticks.
- **Solution for Next Session**: Implement monotonic phase ladder comparison (`PHASE_ORDER`). Awaiting a phase succeeds if that phase **or any later phase** in the sequence is reached.


### Trap 4: Combiner / Splitter Clearance & Retraction Math
- **Symptom**: New filament feed jams at the combiner junction (`feed start: code=2 msg=error_2 - retry`).
- **Cause**: During background unload, `ace_bg_swap.py` calculates the ACE rollback as:
  `rest = max(0, full - int(reclaimed))`. With `COLD_PULL_NORMAL`, `reclaimed = 71mm`. When `swap_retract_length` was 900mm, the ACE only rolled back $900 - 71 = 829\text{mm}$, which left the filament tip sitting ~10mm short of clearing the physical combiner junction.
- **Solution**: Setting `swap_retract_length = 1050mm` gives $1050 - 71 = 979\text{mm}$ of ACE rollback, pulling the strand completely past the combiner and into its dedicated tube.
- **Hardware Verified**: Subsequent feeds succeeded in 15 seconds flat with zero resistance, clean sensor stops, and normal forward assist.


### Trap 5: Mid-Sequence Pick Abort Purge Bed Deposition
- **Symptom**: If an arriving toolhead is commanded while the docked head is in `PULL`, the carriage latches onto the head while it is pushing its +57mm cold-pull plug, dragging a purge worm across the bed.
- **Cause**: The step generation packet for +57mm is flushed to the MCU at move schedule time. Python's `_check_docked` only runs between moves. Furthermore, when `ACE_BG_WAIT` returned `INELIGIBLE` during early heat, it previously left the background greenlet running in the background. The greenlet reached target 2 seconds later and queued the 57mm push right as the carriage grabbed the head!
- **Solution**:
  1. Active Ineligibility Abort: If `ACE_BG_WAIT` decides `INELIGIBLE`, it adds the head to `_abort_requested`, shuts off the nozzle heater immediately, and synchronously awaits greenlet exit before returning.
  2. The carriage is held until the background greenlet confirms it is dead and stationary.
  3. Sized the dynamic gating threshold to $\le 25.0^\circ\text{C}$ ($\sim 4-5\text{s}$ window) so late-heat swaps hold at the discard chute rather than aborting.


---

## 3. Test Harnesses Implemented

### A. Bench Test Harness (`TEST_BG_SWAP_BENCH`)
- **Usage**:
  ```gcode
  TEST_BG_SWAP_BENCH ACTIVE_HEAD=1 TARGET_HEAD=0 TARGET_SLOT=0 [SIMULATE=0|1]
  ```
- **Modes**:
  - `SIMULATE=0`: Full hardware test (heats nozzle, executes cold pull, retracts, and attempts load).
  - `SIMULATE=1`: Motion & safety dry run (homes, picks up `ACTIVE_HEAD`, parks at discard position, calls `ACE_BG_WAIT` skeleton without heating or moving motors).

### B. Interruption Test Suite (`TEST_BG_SWAP_INTERRUPT`)
- **Usage**:
  ```gcode
  TEST_BG_SWAP_INTERRUPT ACTIVE_HEAD=0 TARGET_HEAD=3 TARGET_SLOT=0 AT=<PHASE> [DELAY=<seconds>]
  ```
- **Supported `AT=` Phases & Engine Handover Reactions**:
  - `HEAT`: Aborts heat greenlet, turns off heater; arrival `T` executes standard inline unload.
  - `PULL`: Catches carriage pickup between pull chunks; arrival `T` runs inline cold pull recovery.
  - `RETRACT`: Interlock holds arrival `T` until ACE rollback completes, then hands over safely.
  - `BOOKKEEP`: Interruption occurs right when old strand is clear; arrival `T` runs clean inline load from scratch.
  - `LD_FEED`: Feed terminates; filament left staged in bowden tube; arrival `T` continues load without re-pushing full bowden length.
  - `LD_GRIP`: Tip already through gears; head bookkept loaded; arrival `T` continues inline.
  - `LD_PRIME`: Dock purge aborts; records missing mm in `_bg_prime_deficit`; arrival `T` tops up purge deficit.
  - `DONE`: Full swap completed; arrival `T` is an instantaneous no-op.

### Trap 6: Slicer M73 Integer Minute Resolution & Pyodide Browser Cache
- **Symptom**: Preflight scan stamped only 77 out of 122 background swaps, skipping 45 swaps with `; multiACE bg-swap HEAD=0: window too small (0.0 min < 0.5 min)`.
- **Cause**: OrcaSlicer / Snapmaker Orca emits `M73` remaining print times exclusively in whole integer minutes (`R237`, `R236`, `R235`...). Any swaps occurring within the same elapsed minute have a window of $\Delta R = 0.0\text{ min}$. Any floating-point threshold $> 0.0$ (such as `0.5` or `1.0`) mathematically skips 100% of same-minute swaps. Furthermore, multiACE runs preflight inside the user's browser via WebAssembly (Pyodide); modifying `post_process_virtual_toolheads.py` on the printer is not reflected until the browser tab performs a hard refresh (`Ctrl + F5`).
- **Solution**: Set `BG_UNLOAD_MIN_WINDOW_MIN = 0` in both `/home/lava/multiace/tools/` and `/home/lava/printer_data/config/tools/` to allow all valid intervals to be captured. Combined with hard-refreshing the browser, all 122 background swaps and arrival holds are cleanly stamped (0 skips).

### Trap 7: Embedded SoC Resource Starvation Over Userspace Tailscale
- **Symptom**: SSH connections and web dashboard uploads hang or time out at ~90% when streaming files or accessing files concurrently.
- **Cause**: The Snapmaker U1 runs an embedded ARM Cortex-A7 SoC with single-core constraints. Tailscale runs via userspace `wireguard-go` (`tun/tap`). Encrypting and decrypting concurrent streams (Cursor Remote SSH, Fluidd WebSockets, camera feed, and large G-code uploads) saturates the CPU, dropping network packets.
- **Solution**: Use the printer's direct local LAN IP (`192.168.68.52`) for large G-code uploads and automation tasks, bypassing userspace VPN encryption overhead.

---

## 3. Test Harnesses Implemented

### A. Bench Test Harness (`TEST_BG_SWAP_BENCH`)
- **Usage**:
  ```gcode
  TEST_BG_SWAP_BENCH ACTIVE_HEAD=1 TARGET_HEAD=0 TARGET_SLOT=0 [SIMULATE=0|1]
  ```
- **Modes**:
  - `SIMULATE=0`: Full hardware test (heats nozzle, executes cold pull, retracts, and attempts load).
  - `SIMULATE=1`: Motion & safety dry run (homes, picks up `ACTIVE_HEAD`, parks at discard position, calls `ACE_BG_WAIT` skeleton without heating or moving motors).

### B. Interruption Test Suite (`TEST_BG_SWAP_INTERRUPT`)
- **Usage**:
  ```gcode
  TEST_BG_SWAP_INTERRUPT ACTIVE_HEAD=0 TARGET_HEAD=3 TARGET_SLOT=0 AT=<PHASE> [DELAY=<seconds>]
  ```
- **Supported `AT=` Phases & Engine Handover Reactions**:
  - `HEAT`: Aborts heat greenlet, turns off heater; arrival `T` executes standard inline unload.
  - `PULL`: Catches carriage pickup between pull chunks; arrival `T` runs inline cold pull recovery.
  - `RETRACT`: Interlock holds arrival `T` until ACE rollback completes, then hands over safely.
  - `BOOKKEEP`: Interruption occurs right when old strand is clear; arrival `T` runs clean inline load from scratch.
  - `LD_FEED`: Feed terminates; filament left staged in bowden tube; arrival `T` continues load without re-pushing full bowden length.
  - `LD_GRIP`: Tip already through gears; head bookkept loaded; arrival `T` continues inline.
  - `LD_PRIME`: Dock purge aborts; records missing mm in `_bg_prime_deficit`; arrival `T` tops up purge deficit.
  - `DONE`: Full swap completed; arrival `T` is an instantaneous no-op.

### C. Gating Test Harness (`TEST_BG_WAIT_GATING`)
- **Usage**:
  ```gcode
  TEST_BG_WAIT_GATING ACTIVE_HEAD=1 TARGET_HEAD=0 TARGET_SLOT=0 TEST_AFTER_S=10.0 [DEBUG=1]
  ```
- Verifies dynamic thermal gating threshold ($\le 25.0^\circ\text{C}$) and ineligibility abort behavior.

### D. Park & Restore Harness (`TEST_BG_WAIT_PARK_RESTORE`)
- **Usage**:
  ```gcode
  TEST_BG_WAIT_PARK_RESTORE ACTIVE_HEAD=1 TARGET_HEAD=0 DWELL=3.0 DEBUG=1
  ```
- Validates Option A self-contained position snapshot, Z-hop (+2mm), discard chute parking, and sub-millimeter coordinate restoration.

---

## 4. Recovered Tip-Forming Token Sequences (Historical Reference)

From printer klippy logs (`/oem/klippylogs/klippy.log.2` through `.log.10`):

1. **Option 1 (`prime: 70@600`)**:
   ```ini
   pla: primetemp:215, prime:70@600, unloadtemp:215, loadtemp:215, bgtemp:250, -15@1800, -22@2400, -6.2@1200, -3.2@720, 20@1200, -20@940, 40@1740, -43@2400, 20@680, -20@420, -21.1@2700
   ```

2. **Option 2 (`prime: 40@600`)**:
   ```ini
   pla: primetemp:250, prime:40@600, unloadtemp:208, loadtemp:215, bgtemp:250, -15@1800, -22@2400, -6.2@1200, -3.2@720, 20@1200, -20@940, 41@1740, -43@2400, 20@680, -20@420, -21.1@2700
   ```

3. **Option 3 (`prime: 30@600`)**:
   ```ini
   pla: primetemp:250, prime:30@600, unloadtemp:215, loadtemp:215, bgtemp:250, -15@1800, -22@2400, -6.2@1200, -3.2@720, 20@1200, -20@940, 42@1740, -43@2400, 20@680, -20@420, -21.1@2700
   ```

---

## 5. Milestone Completion Summary

- **Milestone 1 (Command Registration)**: `ACE_BG_WAIT` registered in `ace_bg_swap.py`.
- **Milestone 2 (Standalone Test Suite)**: Dedicated `test_bg_swap.cfg` harness implemented.
- **Milestone 3 (Dynamic Gating & State Progression)**: Monotonic phase ladder, `is_wait_eligible()`, and $\le 25.0^\circ\text{C}$ thermal threshold.
- **Milestone 4 (Self-Contained Motion)**: Discard chute parking, Z-hop, and coordinate restoration.
- **Milestone 5 (Non-Blocking Reactor Wait Loop)**: Reactor polling loop holding active head until target reaches `RETRACT`, active abort on ineligibility.
- **Milestone 6 (Preflight G-code Injection)**: `ACE_BG_WAIT` stamped immediately prior to arrival `T<n>`.
- **Preflight & Hardware Verification**: `BG_UNLOAD_MIN_WINDOW_MIN = 0` successfully verified on `axolt multicolor_PLA_3h57m.gcode`, capturing 122/122 background swaps with 100% arrival hold parity.


---

## 6. Maintainer Review Refinements & Main Integration (#196)

### A. Review Feedback from `decay` (PR #186)
1. **Restricted Early Ineligibility Abort Scope**:
   - Active abort in `ACE_BG_WAIT` restricted to `cur_phase in ('QUEUED', 'FA_STOP', 'HEAT')`.
   - If an arrival occurs while the docked head is in `RETRACT`, `BOOKKEEP`, or any load phase (`LD_FEED`, `LD_GRIP`, `LD_PRIME`), the engine passes through cleanly without interruption. The arrival toolchange `T<n>` continues safely via natural staging and motor interlocks.
2. **Explicit Abort Flag Lifecycle**:
   - Cleared `self._abort_requested.discard(head)` at the top of `cmd_ACE_BG_UNLOAD` and `cmd_ACE_BG_SWAP` to prevent residual/orphaned flags from interfering with subsequent operations.
3. **Preserved Preheat on Abort**:
   - Removed the explicit `set_temperature(..., 0.)` reset during early-heat abort. The head retains thermal progress, allowing the subsequent inline toolchange `T<n>` to continue heating from where it left off rather than reheating from ambient.
4. **Post-Processor Version Bump**:
   - Bumped `PP_FORMAT_VERSION = 5` in `post_process_virtual_toolheads.py` to document stamped `ACE_BG_WAIT` arrival holds.
5. **Maintainer Review Refinements (PR #186 - decay71 Feedback)**:
   - **Abort vs Pick Logging Separation**: Refactored `_check_docked` to report `"head %d was ABORTED by arrival wait - inline paths take over"` specifically when triggered by `_abort_requested`, and preserve `"head %d was PICKED mid-sequence - aborted, inline paths take over"` when triggered by toolhead carriage pickup (`toolhead.get_extruder() is ext`).
   - **Console Output Gating**: Removed console printing from `ACE_BG_WAIT` during normal runs (`DEBUG=0`) so verdicts are written purely to `klippy.log` (`logging.info`) to eliminate console clutter during toolchange bursts. Full verbose diagnostics remain accessible when passing `DEBUG=1`.
   - **Config-Level Debug Option**: Added optional `debug: False` in `[ace_bg_swap]` (defaulting to False for silent console). When set to `True`, `ACE_BG_WAIT` echoes full arrival diagnostics to the console across entire prints without needing G-code edits or manual command overrides.
   - **Runtime Debug Command (`ACE_BG_DEBUG`)**: Added `ACE_BG_DEBUG [ENABLE=0|1] [PERSIST=0]` command allowing the user to toggle arrival debug console output live during active prints from the Fluidd/Mainsail console on the fly, with standard multiACE write-through config persistence.




### B. Heater Intent Observer & Anomaly Resolution (Merged from Main / PR #196)
- **Problem**:
  - After background swap and purge, right before moving to the prime tower, the printer would drop to sub-200°C, then spike up to 250°C (wasting print time), before cooling back to print temp over the prime tower.
- **Root Cause Identified by `decay`**:
  - The short dip below 200°C was caused by the background op finishing and turning the heater off (`M104 S0`).
  - The subsequent 250°C spike occurred during the nozzle wipe after the load: the swap wipes at the heater target the arriving head had before the swap. When that target was $< 170^\circ\text{C}$ (i.e. 0°C from the background op), it fell back to the filament database load temperature (250°C for Generic PLA).
- **Solution (PR #196)**:
  - **Heater Intent Observer (`ace.py`)**: A wrapper around `pheaters.set_temperature` tracks every external slicer heating request (`M104`/`M109`).
  - **Heater Claim & Release (`ace_bg_swap.py`)**: Background ops claim the heater via `_heat_claim(head, heater)` and release it via `_release_heat(ace, head, heater, pheaters, why)`. Upon completion, the heater is restored to the newest foreign request since the claim (the slicer's active intent), rather than setting 0°C.
  - **Swap Target Fallback (`ace.py`)**: During a print, if a head arrives at a target below min extrude temp, it gets the last print temperature seen on that head, avoiding the 250°C database default.
- **Merge & Conflict Resolution**:
  - Merged `origin/main` into `draft-bg-swap-priority`.
  - Resolved conflicts in `multiace/klipper/extras/ace_bg_swap.py` at `HEAT` and `LD_FEED` by preserving `self._set_state(...)` alongside the new `_heat_claim(...)` and `_set_heat(...)` calls.
  - Verified with 11/11 unit tests passing in `tests/unit/test_bg_heat_intent.py`.



### C. Direct Dock Optimization & Abort Flag Cleanup
- **Hardware Finding**:
  - In a live print test on Snapmaker U1, releasing `ACE_BG_WAIT` executed a 1.77-second move restoring active coordinates back to the print bed (`X=15.06, Y=208.63`), followed 2ms later by `T0` commanding `park extruder2 !!!`, which drove 1.70 seconds back to the rear dock rail (`Y=332.2`).
  - Total wasted travel: ~3.5 seconds of redundant bed excursion for a toolhead that is immediately being docked.
- **Resolution**:
  - Added `RESTORE_POS` parameter to `ACE_BG_WAIT` (defaulting to `0`).
  - By default (`RESTORE_POS=0`), `ACE_BG_WAIT` leaves the carriage parked safely at the discard chute ($Y \approx 250$), allowing the subsequent `T<n>` to dock the released head directly along the rear axis ($Y = 332.2$) without redundant bed travel.
  - Passing `RESTORE_POS=1` (as used in `TEST_BG_WAIT_PARK_RESTORE`) preserves full coordinate restoration for bench tests.
  - Added `self._abort_requested.discard(head)` at the entry of `cmd_ACE_BG_WAIT` to prevent orphaned abort flags from lingering across cycles.

---

## 7. Future Architectural Roadmap: Multi-Head Concurrency (#197)

### Background & Engine Serialization
- Currently, `cmd_ACE_BG_SWAP` and `cmd_ACE_BG_UNLOAD` serialize background operations via a global check:
  ```python
  if self._busy:
      return _refuse('another bg op is running - bg ops are serialized (shared move queue)...')
  ```
- **Root Cause**: The stall-free cold-pull choreography uses a single shared C-level move queue (`self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)`). Appending concurrent moves for different extruder steppers causes Klipper MCU shutdowns (`stepcompress: Invalid sequence`).
- **Isolation of `ACE_BG_WAIT`**:
  - `ACE_BG_WAIT` was inspected and verified to already be strictly per-head:
    - Gating checks: `if head not in self._busy:`
    - Abort wait loop: `while head in self._busy:`
  - `ACE_BG_WAIT` does not use the global truthiness of `self._busy` and will require zero modifications when concurrent background swaps land.

### Decoupling Plan (Tracked in GitHub Issue #197)
1. **Per-Head `trapq` and Kinematics**:
   ```python
   self.trapq = {h: ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free) for h in range(4)}
   self.stepper_kinematics = {h: ffi_main.gc(ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free) for h in range(4)}
   ```
2. **Per-Head Dispatch Gating**:
   Update `cmd_ACE_BG_SWAP` and `cmd_ACE_BG_UNLOAD` from `if self._busy:` to `if head in self._busy:`.
3. **Execution Model**:
   With PR #196's per-head heater intent observer and `_dwell_fan_prev[head]` already isolated per head, concurrent background operations across all idle heads can run simultaneously without stepper conflicts.


---

## 8. Development Notes: Tip-Forming Syntax Alignment (v1.30b-pre vs Main)

- **Context**:
  - In beta firmware/module versions (`v1.30b-pre1`), extended parameters (`prime:<mm>[@<f>]`, `primetemp:<C>`, and `bgtemp:<C>`) were introduced in `ace_tipform.py` to allow pre-purge hot priming before cooling into a tip pull.
  - While `ace_tipform.py` on the printer parses these tokens cleanly, our branch adheres strictly to `origin/main` (stable `v1.27b` baseline).
- **PR Scope Isolation**:
  - To respect maintainer boundaries and keep PRs single-responsibility, changes to `ace.py` tip-forming dispatch and `app.js` UI builders for 1.30 tokens are omitted from `draft-bg-swap-priority`.
  - The 11 custom pull moves and custom `unloadtemp:` / `loadtemp:` parameters continue to execute reliably under custom tip-forming mode. Full 1.30 pre-prime execution will integrate upstream under decay71's planned 1.30 release roadmap.


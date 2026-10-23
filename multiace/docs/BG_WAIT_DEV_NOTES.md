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

---

## 4. Next Session Pickup Point

When resuming development:

1. **Fix Phase Ladder Progression in `ace_bg_swap.py`**:
   - In `multiace/klipper/extras/ace_bg_swap.py`, update `cmd_ACE_BG_AWAIT_PHASE`:
     ```python
     PHASE_ORDER = [
         'IDLE', 'QUEUED', 'FA_STOP', 'HEAT', 'PULL',
         'RETRACT', 'BOOKKEEP', 'LD_FEED', 'LD_GRIP', 'LD_PRIME', 'DONE'
     ]
     ```
   - Check if index of `cur_phase` >= index of `target_phase` so transient phases (like `BOOKKEEP`) trigger immediately without timing out.
2. **Verify Fix on Printer**:
   - Run `TEST_BG_SWAP_INTERRUPT ACTIVE_HEAD=0 TARGET_HEAD=3 TARGET_SLOT=0 AT=BOOKKEEP`.
   - Confirm it triggers immediately when `RETRACT` / `BOOKKEEP` completes.
3. **Push Commits**:
   - Once verified, push local commits to `origin/draft-bg-swap-priority`.
4. **Begin Milestone 3: Dynamic Gating Logic**:
   - Implement `is_wait_eligible(head)` helper in `ace_bg_swap.py`.
   - Implement dynamic temperature query comparing live temp to tip-forming target (`(target - temp) <= 15.0°C`).

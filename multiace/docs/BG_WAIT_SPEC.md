# ACE_BG_WAIT Architectural Specification & Roadmap

## 1. Intent
`ACE_BG_WAIT` is a non-blocking Klipper G-code command that pauses an incoming toolhead's arrival until a background swap on a docked toolhead reaches the `PULL` phase or a late `HEAT` thermal threshold, keeping the docked head's cold pull from being aborted and minimizing cycle time.

## 2. Strict Constraints & Architectural Guardrails
* **Zero Edits to `ace.py`:** All new logic, command registrations, and phase accessors must live exclusively in `ace_bg_swap.py`. Access ACE state via `self.printer.lookup_object('ace', None)` using safe `getattr` fallbacks.
* **Option A Coordinate Restoration:** `ACE_BG_WAIT` must be self-contained. Snapshot active print coordinates (`gcode_move`), move to the discard position while waiting, and restore coordinates cleanly before returning. Do not mutate arrival `T` macros.
* **Dynamic Tip-Forming Delta:** Do NOT hardcode target temperatures. Query the active heater's live temperature and compare it against the heater's current target (derived from slot/toolhead tip-forming configuration). Eligibility is gated strictly to `(target - temp) <= 25.0°C` when heating up, or when in the `PULL` phase.
* **Graceful Fallback:** Never raise a Python exception or call `M112`/pause the print on timeout or abort. If an eligibility condition is not met or a timeout expires, log a debug message and return cleanly to allow the standard inline swap logic to proceed.
* **Non-Blocking Execution:** Any waiting must use safe Klipper reactor polling delays, never synchronous `time.sleep()`.

## 3. Implementation Roadmap

### - [x] Milestone 1: Command Skeleton & Registration
- [x] 1.1: Register `ACE_BG_WAIT` command in `ace_bg_swap.py`.
- [x] 1.2: Parse and validate `HEAD` parameter with error handling.
- [x] 1.3: Add debug console response and verify registration via console.


### Milestone 2: Bench Test Harness Macro
- [x] 2.1: Implement dry-run test macro (`TEST_BG_SWAP_BENCH`) in printer config.
- [x] 2.2: Add steps to pick up alternate head (e.g., `T1`) and move to safe resting coordinates.
- [x] 2.3: Trigger background swap on docked head (`HEAD=0`) under controlled conditions.
- [x] 2.4: Verify state inspection workflow without running live prints.

### Milestone 3: Phase Accessor & Dynamic Gating Logic
- [x] 3.1: Implement `phase(head)` helper exposing `self.state[head]` (returning `'IDLE'` fallback).
- [x] 3.2: Implement `is_wait_eligible(head)` helper gating on `PULL` or late `HEAT`.
- [x] 3.3: Add dynamic extruder heater query comparing live temp to configured tip-forming setpoint.
- [x] 3.4: Verify live phase transitions output correctly via test harness.

### Milestone 4: Self-Contained Park & Restore (Option A)
- [x] 4.1: Snapshot current toolhead coordinates prior to park move.
- [x] 4.2: Move active toolhead to designated discard position upon wait eligibility.
- [x] 4.3: Restore previous coordinates cleanly upon wait completion or exit.
- [x] 4.4: Verify arrival `T` macro receives expected toolhead state.

### Milestone 5: Wait Loop & Timeout Handling
- [x] 5.1: Implement reactor polling loop checking for phase transition to `RETRACT`.
- [x] 5.2: Add timeout guard sized to cover remaining PULL work (including waittemp/passive cooldown) to prevent premature aborts or indefinite print stalls.
- [x] 5.3: Implement graceful fallback exit on timeout or pass-through.

### Milestone 6: Post-Processor Integration & Verification
- [x] 6.1: Update `post_process_virtual_toolheads.py` to stamp `ACE_BG_WAIT HEAD=n` prior to arrival `T`.
- [x] 6.2: Mirror identical injection condition inside `head_mode_bg_stats` for consistent statistics.
- [x] 6.3: Test sliced multi-material G-code to verify command injection and accurate stats.
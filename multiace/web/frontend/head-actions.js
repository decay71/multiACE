// @MULTIACE_FLEXI_HOOK Flexi code below uses Flexi
/* Per-toolhead Load/Unload selection.
 *
 * The LABEL comes from the toolhead filament sensor only. Historical
 * operation states (load_finish, inited), spool assignments, PathCheck and
 * route existence are deliberately absent from sensorState(): those say what
 * happened or what is configured, not whether filament is at the extruder
 * right now.
 *
 * Authorization/dispatch is a separate decision. loadTarget()/unloadTarget()
 * keep an ACE head on ACE_LOAD_HEAD/ACE_UNLOAD_HEAD, a stock-feeder head on
 * the native side-feeder path, and a Flexi head on the production workflow.
 * When an ACE head has no recorded routed source they ask the operator to
 * choose a slot instead of guessing one.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.MultiACEHeadActions = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PRESENT = "present";
  const ABSENT = "absent";
  const STALE = "stale";
  const UNAVAILABLE = "unavailable";
  const REPORTED = new Set([PRESENT, ABSENT, STALE, UNAVAILABLE]);

  function isStockHead(th) {
    return !!(th && (th.feeder || th.manual));
  }

  // ctx: {nowMs, freshAtMs, maxAgeS} - freshness of the whole state snapshot.
  // A snapshot older than maxAgeS cannot prove the sensor still reads what it
  // read then, so it degrades to "stale" (rendered as Status unavailable).
  function sensorState(th, ctx) {
    const c = ctx || {};
    const now = Number.isFinite(c.nowMs) ? c.nowMs : Date.now();
    const freshAt = Number.isFinite(c.freshAtMs) ? c.freshAtMs : 0;
    const maxAge = (Number.isFinite(c.maxAgeS) && c.maxAgeS > 0) ? c.maxAgeS : 15;
    if (freshAt > 0 && now - freshAt > maxAge * 1000) return STALE;

    const reported = th && th.filament_sensor && th.filament_sensor.state;
    if (REPORTED.has(reported)) return reported;

    // Older backend / partial payload: fall back to the raw toolhead sensor.
    if (!th || th.module_exist === false) return UNAVAILABLE;
    if (th.filament_at_extruder === true) return PRESENT;
    if (th.filament_at_extruder === false) return ABSENT;
    return UNAVAILABLE;
  }

  function actionFor(state) {
    if (state === PRESENT) return "unload";
    if (state === ABSENT) return "load";
    return "unavailable";
  }

  // During a running load/unload the sensor can clear before the operation
  // finishes. Keep showing the operation that is actually running (and the
  // controls stay disabled); the sensor decides again once it ends.
  function displayAction(th, ctx) {
    const op = ctx && ctx.op;
    if (op === "loading") return "load";
    if (op === "unloading") return "unload";
    return actionFor(sensorState(th, ctx));
  }

  function headIdx(th) {
    return th && th.idx != null ? Number(th.idx) : null;
  }

  function validSource(th) {
    return !!(th && th.head_source_known
      && Number.isInteger(Number(th.ace)) && Number(th.ace) >= 0
      && Number.isInteger(Number(th.slot)) && Number(th.slot) >= 0);
  }

  // ctx: {mode, soleAce}
  function loadTarget(th, ctx) {
    const c = ctx || {};
    const mode = c.mode || "multi";
    const idx = headIdx(th);
    if (idx === null) return {kind: "blocked", reason: "missing-head"};

    // @MULTIACE_FLEXI_PATCH Flexi load/unload uses the production workflow, not the stock feeder path.
    if (mode === "flexi") return {kind: "flexi", args: {HEAD: idx}};
    if (isStockHead(th)) return {kind: "stock", args: {HEAD: idx}};
    if (validSource(th)) {
      return {kind: "ace",
              args: {HEAD: idx, ACE: Number(th.ace), SLOT: Number(th.slot)}};
    }
    // multi/normal: slot N feeds head N. Only unambiguous with one usable
    // ACE; with several units the caller must pick the slot.
    if (mode !== "head" && Number.isInteger(c.soleAce) && c.soleAce >= 0) {
      return {kind: "ace", args: {HEAD: idx, ACE: c.soleAce, SLOT: idx}};
    }
    return {kind: "guide",
            reason: mode === "head" ? "choose-head-slot" : "choose-ace-slot"};
  }

  // ctx: {mode}
  function unloadTarget(th, ctx) {
    const c = ctx || {};
    const mode = c.mode || "multi";
    const idx = headIdx(th);
    if (idx === null) return {kind: "blocked", reason: "missing-head"};

    // @MULTIACE_FLEXI_PATCH Flexi load/unload uses the production workflow, not the stock feeder path.
    if (mode === "flexi") return {kind: "flexi", args: {HEAD: idx}};
    if (isStockHead(th)) return {kind: "stock", args: {HEAD: idx}};
    if (th && th.head_source_known) return {kind: "ace", args: {HEAD: idx}};
    // Filament is at the toolhead but no authoritative source/path is
    // recorded. Retracting an arbitrary slot would pull the wrong spool.
    return {kind: "reconcile", reason: "no-recorded-source"};
  }

  return {PRESENT, ABSENT, STALE, UNAVAILABLE, isStockHead, sensorState,
          actionFor, displayAction, loadTarget, unloadTarget};
});

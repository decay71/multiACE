/* multiACE in-browser preflight — Pyodide worker.
 *
 * Runs the UNMODIFIED Python post-processor + preflight_core in the browser
 * (CPython-WASM via Pyodide), in a Web Worker so the UI thread stays free
 * during the ~20 s parse/rewrite of large files.
 *
 * WHY Pyodide and not a JS port: a JavaScript re-implementation of the matcher
 * (material-strict matching, swap-aware/Belady layout, head-mode pinning,
 * ACE_SWAP_HEAD injection, the structural auto-load anchor) is a SECOND source
 * of truth that silently drifts from the Python the printer backend runs. By
 * loading the same .py here we keep ONE implementation — backend and browser
 * compute byte-identical results, no differ, no drift.
 *
 * Message contract (matches the frontend wiring):
 *   <- {type:"init", pyodideIndexURL, postprocessSrc, coreSrc}
 *   -> {type:"ready"}                                   (or {type:"error"})
 *   <- {type:"analyze", jobId, file, liveSlots, headCtx}
 *   -> {type:"analyze-done", jobId, report}             (+ {type:"progress"})
 *   <- {type:"rewrite", jobId, file, liveSlots, headCtx, mode, remapOverride,
 *                        headAssignment, headPlan}
 *   -> {type:"rewrite-done", jobId, text}               (+ {type:"progress"})
 *   <- {type:"clear", jobId}        ->  {type:"cleared", jobId}
 *
 * liveSlots / headCtx are produced by the main thread from /multiace/api/state
 * (the printer is still the source of live ACE/slot identity). headCtx =
 * {mode, ace_head, feeders:[{head,material,color}]}.
 */
"use strict";

let pyodide = null;
let ready = false;
let initPromise = null;

// Cache the uploaded File per job so a later "rewrite" reuses it without the
// main thread re-sending the (possibly 100+ MB) blob.
const files = new Map();
const slotsByJob = new Map();
const ctxByJob = new Map();

function progress(jobId, stage, percent) {
  self.postMessage({type: "progress", jobId, stage, percent});
}

// One-time Pyodide bring-up: load the runtime, then write the two unmodified
// .py sources into the in-memory FS and import them. The post-processor is pure
// stdlib (no pip/micropip needed), so a bare Pyodide can run it as-is.
async function ensureInit(msg) {
  if (ready) return;
  if (!initPromise) {
    initPromise = (async () => {
      const indexURL = msg.pyodideIndexURL ||
        "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/";
      // pyodide.js defines loadPyodide() on the worker global.
      importScripts(indexURL + "pyodide.js");
      pyodide = await self.loadPyodide({indexURL});
      // Drop the two modules onto the FS and import them. preflight_core takes
      // the post-processor module as a parameter, so we only import both and
      // hand pp into the core functions — no cross-import between the files.
      pyodide.FS.mkdirTree("/multiace");
      pyodide.FS.writeFile(
        "/multiace/post_process_virtual_toolheads.py", msg.postprocessSrc);
      pyodide.FS.writeFile("/multiace/preflight_core.py", msg.coreSrc);
      pyodide.runPython(`
import sys, json
sys.path.insert(0, "/multiace")
import post_process_virtual_toolheads as _pp
import preflight_core as _core
`);
      ready = true;
    })();
  }
  await initPromise;
}

// Analyze: meta-parse + build the report (multi or head-mode preview).
async function doAnalyze(jobId, file, liveSlots, headCtx) {
  files.set(jobId, file);
  slotsByJob.set(jobId, liveSlots);
  ctxByJob.set(jobId, headCtx);
  progress(jobId, "analyze", 5);
  const text = await file.text();
  progress(jobId, "analyze", 40);

  const py = pyodide;
  py.globals.set("_gtext", text);
  py.globals.set("_live", JSON.stringify(liveSlots || []));
  py.globals.set("_hctx", JSON.stringify(headCtx || {mode: "multi"}));
  py.globals.set("_fname", file.name || "upload.gcode");
  py.globals.set("_fsize", file.size || text.length);
  const reportJson = py.runPython(`
_live_slots = json.loads(_live)
_head_ctx   = json.loads(_hctx)
_colors, _types, _naces, _used, _plan, _meta = _core.parse_meta(
    _pp, _gtext.splitlines(True))
_report = _core.build_report(
    _pp,
    slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    plan_proxy=_plan, live_slots=_live_slots, head_ctx=_head_ctx,
    token="", filename=_fname, size=int(_fsize), meta=_meta)
json.dumps(_report)
`);
  // free the big string from the Python globals
  py.runPython("del _gtext, _plan\n");
  progress(jobId, "done", 100);
  return JSON.parse(reportJson);
}

// Rewrite: run the full pipeline in MEMFS, return the print-ready gcode text.
async function doRewrite(jobId, msg) {
  const file = files.get(jobId) || msg.file;
  const liveSlots = slotsByJob.get(jobId) || msg.liveSlots || [];
  const headCtx = ctxByJob.get(jobId) || msg.headCtx || {mode: "multi"};
  const mode = msg.mode || "slicer";
  if (!file) throw new Error("missing file");

  progress(jobId, "analyze", 2);
  const text = await file.text();

  const py = pyodide;
  py.FS.mkdirTree("/preflight");
  py.FS.writeFile("/preflight/src.gcode", text);
  py.globals.set("_live", JSON.stringify(liveSlots));
  py.globals.set("_hctx", JSON.stringify(headCtx));
  py.globals.set("_mode", mode);
  py.globals.set("_remap", JSON.stringify(msg.remapOverride || null));
  py.globals.set("_hassign", JSON.stringify(msg.headAssignment || null));
  py.globals.set("_hplan", msg.headPlan || "loadout");

  // Bridge the streaming-stage progress out to the main thread. set_stage maps
  // a coarse (stage, percent); the fine per-file callbacks stay no-ops for now
  // (coarse stages are enough; wiring the byte-level cb across the boundary is
  // a later refinement).
  const onStage = (stage, percent) => progress(jobId, stage, percent);
  py.globals.set("_on_stage", onStage);

  progress(jobId, "rewrite", 10);
  const outText = py.runPython(`
_live_slots = json.loads(_live)
_head_ctx   = json.loads(_hctx)
_remap_ov   = json.loads(_remap)
_hassign_ov = json.loads(_hassign)
_colors, _types, _naces, _used, _plan, _meta = _core.parse_meta(
    _pp, open("/preflight/src.gcode", "r", encoding="utf-8", errors="replace"))
_final = _core.rewrite_pipeline(
    _pp,
    src_path="/preflight/src.gcode",
    tmp_a="/preflight/a.gcode", tmp_b="/preflight/b.gcode",
    slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    live_slots=_live_slots, head_ctx=_head_ctx, mode=_mode,
    remap_override=_remap_ov, head_assignment=_hassign_ov, head_plan=_hplan,
    meta=_meta,
    set_stage=lambda s, p: _on_stage(s, p))
open(_final, "r", encoding="utf-8", errors="replace").read()
`);
  // tidy MEMFS so a second print doesn't accumulate
  try { py.FS.unlink("/preflight/src.gcode"); } catch (e) {}
  try { py.FS.unlink("/preflight/a.gcode"); } catch (e) {}
  try { py.FS.unlink("/preflight/b.gcode"); } catch (e) {}
  progress(jobId, "done", 100);
  return outText;
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  const jobId = msg.jobId || "job";
  try {
    if (msg.type === "init") {
      await ensureInit(msg);
      self.postMessage({type: "ready"});
      return;
    }
    if (msg.type === "analyze") {
      await ensureInit(msg);
      const report = await doAnalyze(jobId, msg.file, msg.liveSlots, msg.headCtx);
      self.postMessage({type: "analyze-done", jobId, report});
      return;
    }
    if (msg.type === "rewrite") {
      await ensureInit(msg);
      const text = await doRewrite(jobId, msg);
      self.postMessage({type: "rewrite-done", jobId, text});
      return;
    }
    if (msg.type === "clear") {
      files.delete(jobId);
      slotsByJob.delete(jobId);
      ctxByJob.delete(jobId);
      self.postMessage({type: "cleared", jobId});
      return;
    }
  } catch (err) {
    self.postMessage({
      type: "error", jobId,
      message: err && err.message ? err.message : String(err),
    });
  }
};

/* multiACE in-browser preflight - Pyodide worker.
 *
 * Runs the UNMODIFIED Python post-processor + preflight_core in the browser
 * (CPython-WASM via Pyodide), in a Web Worker so the UI thread stays free
 * during the ~20 s parse/rewrite of large files.
 *
 * WHY Pyodide and not a JS port: a JavaScript re-implementation of the matcher
 * (material-strict matching, swap-aware/Belady layout, head-mode pinning,
 * ACE_SWAP_HEAD injection, the structural auto-load anchor) is a SECOND source
 * of truth that silently drifts from the Python the printer backend runs. By
 * loading the same .py here we keep ONE implementation - backend and browser
 * compute byte-identical results, no differ, no drift.
 *
 * Message contract (matches the frontend wiring):
 *   <- {type:"init", pyodideIndexURL, postprocessSrc, coreSrc, swapCostSrc}
 *   -> {type:"ready"}                                   (or {type:"error"})
 *   <- {type:"analyze", jobId, file, liveSlots, headCtx, costParams}
 *   -> {type:"analyze-done", jobId, report}             (+ {type:"progress"})
 *   <- {type:"rewrite", jobId, file, liveSlots, headCtx, mode, remapOverride,
 *                        headAssignment, headPlan, headCopies,
 *                        printPrefs:{bedMesh, camera, flowCal}}
 *   -> {type:"rewrite-done", jobId, blob}               (+ {type:"progress"})
 *   <- {type:"estimate", jobId, plan, mapping, targetIds, copyIds}
 *   -> {type:"estimate-done", jobId, result}    ({estimate, timeline} for the
 *                        edited mapping, or null when there is no estimate)
 *   <- {type:"estimate-all", jobId, costParams}   (cost settings changed)
 *   -> {type:"estimate-all-done", jobId, plans}   ({plan: {estimate, timeline}}
 *                        for EVERY plan, each against the mapping the user
 *                        last left it on)
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
// Per-job estimate capture (cost model, header, events), filled by doAnalyze
// and read by a later "estimate" recompute (preflight_core.recompute_estimate).
// The cost model's inputs come with each analyze, because this worker outlives
// many preflight runs in one tab and ace.cfg may have changed in between.
let costParams = null;

function progress(jobId, stage, percent) {
  self.postMessage({type: "progress", jobId, stage, percent});
}

// The uploaded File is mounted read-only via WORKERFS instead of being read
// into a string: Python then iterates it line by line straight from the
// Blob, so memory no longer grows with the file. file.text() + splitlines
// held the whole file twice (a UTF-16 JS string plus ~13 M Python line
// strings for a 340 MB file) and killed the tab (Chrome "Aw, Snap").
// Returns the path of the file inside the mount; unmountJob() releases it.
function mountJob(jobId, file) {
  const FS = pyodide.FS;
  const dir = "/in/" + String(jobId).replace(/[^A-Za-z0-9_-]/g, "_");
  unmountJob(dir);
  FS.mkdirTree(dir);
  FS.mount(FS.filesystems.WORKERFS,
           {blobs: [{name: "src.gcode", data: file}]}, dir);
  return {dir, path: dir + "/src.gcode"};
}

function unmountJob(dir) {
  const FS = pyodide.FS;
  try { FS.unmount(dir); } catch (e) {}
  try { FS.rmdir(dir); } catch (e) {}
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
      // hand pp into the core functions - no cross-import between the files.
      pyodide.FS.mkdirTree("/multiace");
      pyodide.FS.writeFile(
        "/multiace/post_process_virtual_toolheads.py", msg.postprocessSrc);
      pyodide.FS.writeFile("/multiace/preflight_core.py", msg.coreSrc);
      // swap_cost is optional: an install that predates it still gets a
      // working preflight, just without the estimate (preflight_core imports
      // it softly for exactly that reason).
      if (msg.swapCostSrc) {
        pyodide.FS.writeFile("/multiace/swap_cost.py", msg.swapCostSrc);
      }
      pyodide.runPython(`
import sys, json
sys.path.insert(0, "/multiace")
import post_process_virtual_toolheads as _pp
import preflight_core as _core
import codecs
_captures = {}

def _chunk_lines(path, size=1 << 20):
    # Lines of a (mounted) file, read in 1 MB blocks and split with
    # str.splitlines(True) - the same line rules the old whole-text
    # splitlines used, with only one block in memory at a time.
    # Faster than iterating the WORKERFS file line by line (150 MB:
    # 9 s vs 39 s; the old file.text() path took 22 s and 758 MB heap).
    # The last line of a block waits for the next one, so a CRLF or a
    # multi-byte character split across the block edge stays whole.
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    rest = ""
    with open(path, "rb") as fh:
        while True:
            b = fh.read(size)
            text = rest + dec.decode(b, final=not b)
            if not b:
                if text:
                    yield from text.splitlines(True)
                return
            lines = text.splitlines(True)
            rest = lines.pop() if lines else ""
            yield from lines
`);
      ready = true;
    })();
  }
  await initPromise;
}

// Analyze: meta-parse + build the report (multi or head-mode preview).
async function doAnalyze(jobId, file, liveSlots, headCtx, freshCostParams) {
  if (freshCostParams !== undefined) costParams = freshCostParams;
  files.set(jobId, file);
  slotsByJob.set(jobId, liveSlots);
  ctxByJob.set(jobId, headCtx);
  progress(jobId, "analyze", 5);

  const py = pyodide;
  const mnt = mountJob(jobId, file);
  let reportJson;
  try {
    py.globals.set("_src", mnt.path);
    py.globals.set("_live", JSON.stringify(liveSlots || []));
    py.globals.set("_hctx", JSON.stringify(headCtx || {mode: "multi"}));
    py.globals.set("_fname", file.name || "upload.gcode");
    py.globals.set("_fsize", file.size || 0);
    py.globals.set("_cost", JSON.stringify(costParams || {}));
    py.globals.set("_job_id", jobId);
    progress(jobId, "analyze", 10);
    // parse_meta takes any line iterator and keeps only the head/tail
    // windows and the plan lines - the same call the rewrite makes.
    reportJson = py.runPython(`
_live_slots = json.loads(_live)
_head_ctx   = json.loads(_hctx)
_colors, _types, _naces, _used, _plan, _meta, _hdr = _core.parse_meta(
    _pp, _chunk_lines(_src), with_header=True)
_capture = {}
_report = _core.build_report(
    _pp,
    slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    plan_proxy=_plan, live_slots=_live_slots, head_ctx=_head_ctx,
    token="", filename=_fname, size=int(_fsize), meta=_meta,
    header_text=_hdr, cost_params=json.loads(_cost),
    estimate_capture=_capture)
_captures[_job_id] = _capture
json.dumps(_report)
`);
    py.runPython("del _plan, _hdr\n");
  } finally {
    unmountJob(mnt.dir);
  }
  progress(jobId, "done", 100);
  return JSON.parse(reportJson);
}

// Estimate: recompute {estimate, timeline} for an edited mapping or target
// assignment, reusing the ctx captured by doAnalyze - no gcode re-parse.
function doEstimate(jobId, plan, mapping, targetIds, copyIds) {
  const py = pyodide;
  py.globals.set("_job_id", jobId);
  py.globals.set("_plan", plan || "");
  py.globals.set("_mapping", JSON.stringify(mapping || null));
  py.globals.set("_target_ids", JSON.stringify(targetIds || null));
  py.globals.set("_copy_ids", JSON.stringify(copyIds || null));
  return JSON.parse(py.runPython(`
json.dumps(_core.recompute_estimate(
    _captures.get(_job_id),
    mapping=json.loads(_mapping), target_ids=json.loads(_target_ids),
    copy_ids=json.loads(_copy_ids), plan=(_plan or None)))
`));
}

// Estimate-all: the cost settings moved - re-run EVERY plan against the new
// params, reusing the captured ctx. The new params also become this worker's
// costParams, so a rewrite that follows stamps what the user just saw.
function doEstimateAll(jobId, newCostParams) {
  const py = pyodide;
  if (newCostParams !== undefined && newCostParams !== null) {
    costParams = newCostParams;
  }
  py.globals.set("_job_id", jobId);
  py.globals.set("_cost", JSON.stringify(costParams || {}));
  return JSON.parse(py.runPython(`
json.dumps(_core.recompute_all_estimates(
    _captures.get(_job_id), json.loads(_cost)))
`));
}

// Read a MEMFS file into a Blob in slices: no single copy of the whole
// file as a JS string or one big array. The Blob crosses to the main
// thread by reference.
function memfsToBlob(path, slice = 8 << 20) {
  const FS = pyodide.FS;
  const size = FS.stat(path).size;
  const fd = FS.open(path, "r");
  const parts = [];
  try {
    for (let pos = 0; pos < size; pos += slice) {
      const n = Math.min(slice, size - pos);
      const buf = new Uint8Array(n);
      FS.read(fd, buf, 0, n, pos);
      parts.push(buf);
    }
  } finally {
    FS.close(fd);
  }
  return new Blob(parts, {type: "application/octet-stream"});
}

// Rewrite: run the full pipeline in MEMFS, return the print-ready gcode as a
// Blob.
async function doRewrite(jobId, msg) {
  const file = files.get(jobId) || msg.file;
  const liveSlots = slotsByJob.get(jobId) || msg.liveSlots || [];
  const headCtx = ctxByJob.get(jobId) || msg.headCtx || {mode: "multi"};
  const mode = msg.mode || "slicer";
  if (!file) throw new Error("missing file");

  progress(jobId, "analyze", 2);

  const py = pyodide;
  py.FS.mkdirTree("/preflight");
  // Source straight from the File (WORKERFS), no JS string, no MEMFS copy.
  const mnt = mountJob(jobId, file);
  py.globals.set("_src", mnt.path);
  py.globals.set("_live", JSON.stringify(liveSlots));
  py.globals.set("_hctx", JSON.stringify(headCtx));
  py.globals.set("_mode", mode);
  py.globals.set("_remap", JSON.stringify(msg.remapOverride || null));
  py.globals.set("_hassign", JSON.stringify(msg.headAssignment || null));
  py.globals.set("_hplan", msg.headPlan || "loadout");
  py.globals.set("_hcopies", JSON.stringify(msg.headCopies || null));
  const prefs = msg.printPrefs || {};
  py.globals.set("_prefs", JSON.stringify({
    bed_mesh: !!prefs.bedMesh, camera: !!prefs.camera,
    flow_cal: !!prefs.flowCal}));

  // Bridge the streaming-stage progress out to the main thread. set_stage maps
  // a coarse (stage, percent); the fine per-file callbacks stay no-ops for now
  // (coarse stages are enough; wiring the byte-level cb across the boundary is
  // a later refinement).
  const onStage = (stage, percent) => progress(jobId, stage, percent);
  py.globals.set("_on_stage", onStage);

  progress(jobId, "rewrite", 10);
  let outBlob;
  try {
  const finalPath = py.runPython(`
_live_slots = json.loads(_live)
_head_ctx   = json.loads(_hctx)
_remap_ov   = json.loads(_remap)
_hassign_ov = json.loads(_hassign)
_hcopies_ov = json.loads(_hcopies)
_colors, _types, _naces, _used, _plan, _meta = _core.parse_meta(
    _pp, _chunk_lines(_src))
_final = _core.rewrite_pipeline(
    _pp,
    src_path=_src,
    tmp_a="/preflight/a.gcode", tmp_b="/preflight/b.gcode",
    slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    live_slots=_live_slots, head_ctx=_head_ctx, mode=_mode,
    remap_override=_remap_ov, head_assignment=_hassign_ov, head_plan=_hplan,
    meta=_meta, head_copies=_hcopies_ov,
    set_stage=lambda s, p: _on_stage(s, p))
_pf = json.loads(_prefs)
if _pf["bed_mesh"] or _pf["camera"] or _pf["flow_cal"]:
    # Same streaming pass as the printer backend (preflight_core).
    _on_stage("print_prefs", 84)
    _other = ("/preflight/b.gcode" if _final == "/preflight/a.gcode"
              else "/preflight/a.gcode")
    _core.prepend_print_prefs(_final, _other, _pf["bed_mesh"],
                              _pf["camera"], _pf["flow_cal"])
    _final = _other
_final
`);
    // The stage the pipeline did not end on is dead weight: drop it before
    // the result is copied out.
    for (const p of ["/preflight/a.gcode", "/preflight/b.gcode"]) {
      if (p !== finalPath) { try { py.FS.unlink(p); } catch (e) {} }
    }
    outBlob = memfsToBlob(finalPath);
  } finally {
    unmountJob(mnt.dir);
    // tidy MEMFS so a second print doesn't accumulate
    try { py.FS.unlink("/preflight/a.gcode"); } catch (e) {}
    try { py.FS.unlink("/preflight/b.gcode"); } catch (e) {}
  }
  progress(jobId, "done", 100);
  return outBlob;
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
      const report = await doAnalyze(
        jobId, msg.file, msg.liveSlots, msg.headCtx, msg.costParams);
      self.postMessage({type: "analyze-done", jobId, report});
      return;
    }
    if (msg.type === "estimate") {
      await ensureInit(msg);
      const result = doEstimate(
        jobId, msg.plan, msg.mapping, msg.targetIds, msg.copyIds);
      self.postMessage({type: "estimate-done", jobId, result});
      return;
    }
    if (msg.type === "estimate-all") {
      await ensureInit(msg);
      const plans = doEstimateAll(jobId, msg.costParams);
      self.postMessage({type: "estimate-all-done", jobId, plans});
      return;
    }
    if (msg.type === "rewrite") {
      await ensureInit(msg);
      const blob = await doRewrite(jobId, msg);
      self.postMessage({type: "rewrite-done", jobId, blob});
      return;
    }
    if (msg.type === "clear") {
      files.delete(jobId);
      slotsByJob.delete(jobId);
      ctxByJob.delete(jobId);
      try { pyodide.runPython("_captures.pop(" + JSON.stringify(jobId) + ", None)"); } catch (e) {}
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

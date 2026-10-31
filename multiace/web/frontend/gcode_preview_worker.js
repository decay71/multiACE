/* multiACE g-code preview — parser worker.
 *
 * Runs entirely client-side, on the file the user already selected. No
 * upload, no Moonraker, no printer required - the original design uploaded
 * the file to the printer's own gcodes folder and embedded a URL fragment
 * into that printer's web UI, which needs an actual printer behind it and
 * does nothing on a laptop with no printer attached. This does not.
 *
 * LARGE FILES. The file is read in READ_CHUNK slices and parsed line by
 * line as it arrives: it is never held as one string. (file.text() plus a
 * split("\n") held a 340 MB print twice - once as a UTF-16 string, once as
 * ~13 M line strings - and killed the tab.) What the parse keeps is bounded
 * by MAX_SEGMENTS / MAX_TRAVELS whatever the file size, so memory follows
 * the preview, not the file. Time still follows the file, so a file over
 * MAX_FILE_BYTES is refused up front with a message instead of parsing for
 * minutes.
 *
 * Message contract:
 *   <- {type:"parse", file}
 *   -> {type:"progress", percent}
 *   -> {type:"done", data: {...}}   (typed arrays, transferred)
 *   -> {type:"error", message}
 *
 * OUTPUT SHAPE - transferable typed arrays, one entry per SEGMENT:
 *
 *   positions   Float32Array  6 per segment (x0,y0,z0, x1,y1,z1)
 *   dims        Float32Array  2 per segment (width, height)
 *   feedrate    Float32Array  1 per segment (mm/s)
 *   role        Uint8Array    1 per segment (;TYPE: enum, see ROLES)
 *   tool        Uint8Array    1 per segment
 *   layerOf     Uint32Array   1 per segment
 *   layerRanges Uint32Array   2 per layer   (first, count)
 *   layerZ      Float32Array  1 per layer
 *   travels     Float32Array  6 per travel segment
 *   toolchanges [{t, x, y, z, layer}]  (hundreds, not millions - stay objects)
 *   bounds      {minX,minY,minZ,maxX,maxY,maxZ}
 *
 * This replaces nested JS arrays of {t, segs:[...]} objects, and it is the
 * single biggest performance lever available here: it removes the
 * structured-clone cost on the way out of the worker AND the per-segment
 * object allocation on the way in.
 *
 * `layerRanges` is what makes a layer RANGE cheap: because segments are
 * appended in file order and a layer's segments are contiguous, drawing a
 * band of layers is one contiguous slice - no per-frame filtering, no
 * per-frame scan.
 *
 * WE READ THE DIMENSIONS, WE DO NOT ESTIMATE THEM. The target slicer
 * (Snapmaker Orca, i.e. OrcaSlicer) emits `;WIDTH:`, `;HEIGHT:`, `;TYPE:`
 * and `;Z:` inline - the sample fixture carries 4454, 1816, 2044 and 205 of
 * them respectively. Volume-based estimation survives only as the fallback
 * for slicers that stay silent, because a guessed width is a guessed
 * picture.
 *
 * Toolchange detection is UNCHANGED and must stay unchanged: it mirrors
 * post_process_virtual_toolheads.parse_toolchanges exactly. The
 * "; Change Tool X -> Tool Y" comment is authoritative (it survives the
 * multiACE rewrite, so this parses a raw slicer file or an already-
 * rewritten one), and bare `T<n>` lines are trusted only until the first
 * such comment appears anywhere in the file.
 */
"use strict";

/* Read the file in slices of this size. */
const READ_CHUNK = 8 << 20;
/* Largest file the preview will parse. Also checked by the caller so the
 * refusal is instant; kept here too because the worker is the one that
 * would spend the time. */
const MAX_FILE_BYTES = 512 * 1024 * 1024;

const TOOLCHANGE_RE = /^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)/i;
const BARE_T_RE = /^T(\d{1,2})\b/;
const LAYER_RE = /^;LAYER_CHANGE\b/i;
const ZCOMMENT_RE = /^;Z:\s*([-\d.]+)/i;
const WIDTH_RE = /^;WIDTH:\s*([\d.]+)/i;
const HEIGHT_RE = /^;HEIGHT:\s*([\d.]+)/i;
const TYPE_RE = /^;TYPE:\s*(.+?)\s*$/i;
const MOVE_RE = /^G[0-3]\b/i;   // G2/G3 arcs are drawn as a straight chord -
                                // rare in FDM slicer output, and treating
                                // them as a move keeps X/Y/Z/E tracking
                                // correct even though the curve itself is
                                // simplified.

/* ;TYPE: values, as an enum the renderer colours by. The eight in the
 * sample fixture are all here; the rest are the names OrcaSlicer emits for
 * features this particular file happens not to contain. Anything
 * unrecognised lands on 0 rather than being dropped - an unknown feature
 * still printed something, and a preview that silently hides geometry is
 * worse than one that colours it grey. */
const ROLES = [
  "Other",                  // 0
  "Outer wall",             // 1
  "Inner wall",             // 2
  "Overhang wall",          // 3
  "Sparse infill",          // 4
  "Internal solid infill",  // 5
  "Top surface",            // 6
  "Bottom surface",         // 7
  "Ironing",                // 8
  "Bridge infill",          // 9
  "Gap infill",             // 10
  "Skirt",                  // 11
  "Support",                // 12
  "Support interface",      // 13
  "Prime tower",            // 14
  "Custom",                 // 15
];
const ROLE_BY_NAME = (() => {
  const m = Object.create(null);
  ROLES.forEach((name, i) => { m[name.toLowerCase()] = i; });
  // Aliases: other slicers in the same lineage, and Orca's own spellings
  // for the same feature.
  Object.assign(m, {
    "external perimeter": 1, "perimeter": 2, "overhang perimeter": 3,
    "internal infill": 4, "solid infill": 5, "top solid infill": 6,
    "bottom solid infill": 7, "bridge": 9, "internal bridge infill": 9,
    "bridge infill": 9, "gap fill": 10, "brim": 11,
    "support material": 12, "support material interface": 13,
    "wipe tower": 14,
  });
  return m;
})();
function roleOf(name) {
  const i = ROLE_BY_NAME[String(name || "").trim().toLowerCase()];
  return i === undefined ? 0 : i;
}

/* Above this many segments the parser decimates uniformly across the WHOLE
 * file rather than truncating it - a preview that quietly stops halfway is
 * a lie, and one that eats a gigabyte of RAM is not a preview. The renderer
 * reports the fact in the legend when it engages (see `decimated`). The
 * cost is roughly 42 bytes per segment, so this cap is ~63 MB of typed
 * array; the 2.8 MB sample fixture does not come close. */
const MAX_SEGMENTS = 1500000;
/* Travel moves are decimated the same way (they are drawn only on request,
 * but they are parsed always, and a 340 MB print has millions). */
const MAX_TRAVELS = 750000;

/* Geometrically-grown typed array. `push` onto a JS array and converting at
 * the end costs a full second copy plus the boxing; this grows in place. */
class Grow {
  constructor(Ctor, stride, initial) {
    this.Ctor = Ctor;
    this.stride = stride;
    this.a = new Ctor(Math.max(stride, (initial | 0) * stride));
    this.n = 0;                       // count of ITEMS, not of elements
  }
  _room(items) {
    const need = (this.n + items) * this.stride;
    if (need <= this.a.length) return;
    const next = new this.Ctor(Math.max(this.a.length * 2, need));
    next.set(this.a);
    this.a = next;
  }
  // Fixed arities rather than rest args: this is the hot loop, and
  // `push(...vals)` allocates an array on every single segment.
  push1(v) { this._room(1); this.a[this.n * this.stride] = v; this.n++; }
  push2(a, b) {
    this._room(1);
    const o = this.n * this.stride;
    this.a[o] = a; this.a[o + 1] = b;
    this.n++;
  }
  push6(a, b, c, d, e, f) {
    this._room(1);
    const o = this.n * this.stride;
    this.a[o] = a; this.a[o + 1] = b; this.a[o + 2] = c;
    this.a[o + 3] = d; this.a[o + 4] = e; this.a[o + 5] = f;
    this.n++;
  }
  // Keep every `stride`-th item, in place. Used by the decimator.
  keepEveryOther() {
    const s = this.stride;
    let w = 0;
    for (let r = 0; r < this.n; r += 2, w++) {
      if (r === w) continue;
      const ro = r * s, wo = w * s;
      for (let i = 0; i < s; i++) this.a[wo + i] = this.a[ro + i];
    }
    this.n = w;
  }
  trimmed() { return this.a.subarray(0, this.n * this.stride).slice(); }
}

function createParser() {
  let x = 0, y = 0, z = 0, e = 0;
  let absXY = true, absE = true;
  let feed = 0;                  // mm/min as the file states it
  let curT = 0;
  let sawChange = false;
  let layerIdx = 0;

  // The slicer's own words for the segment about to be extruded. Null
  // means "not stated" - which is what selects the volumetric fallback,
  // and only then.
  let curWidth = null, curHeight = null, curRole = 0;

  const positions = new Grow(Float32Array, 6, 1 << 14);
  const dims      = new Grow(Float32Array, 2, 1 << 14);
  const feedrate  = new Grow(Float32Array, 1, 1 << 14);
  const role      = new Grow(Uint8Array,   1, 1 << 14);
  const tool      = new Grow(Uint8Array,   1, 1 << 14);
  const layerOf   = new Grow(Uint32Array,  1, 1 << 14);
  const travels   = new Grow(Float32Array, 6, 1 << 12);
  // Travels need their own layer index: they are filtered by the same
  // layer-range slider, and they are not interleaved with extrusions in
  // the extrusion arrays.
  const travelOf  = new Grow(Uint32Array,  1, 1 << 12);

  const layerZ = [null];
  const toolchanges = [];
  const bounds = {
    minX: Infinity, minY: Infinity, minZ: Infinity,
    maxX: -Infinity, maxY: -Infinity, maxZ: -Infinity,
  };

  // Uniform decimation (see MAX_SEGMENTS). `stride` is how many candidate
  // segments one kept segment now stands for.
  let stride = 1, sinceKept = 0, dropped = 0;
  // The same for travel moves, which have their own cap.
  let travelStride = 1, travelSince = 0, travelDropped = 0;

  const FILAMENT_AREA = Math.PI * (1.75 / 2) * (1.75 / 2);  // mm^2

  function ensureLayer(idx) {
    while (layerZ.length <= idx) layerZ.push(null);
  }
  function bound(px, py, pz) {
    if (px < bounds.minX) bounds.minX = px;
    if (px > bounds.maxX) bounds.maxX = px;
    if (py < bounds.minY) bounds.minY = py;
    if (py > bounds.maxY) bounds.maxY = py;
    if (pz < bounds.minZ) bounds.minZ = pz;
    if (pz > bounds.maxZ) bounds.maxZ = pz;
  }

  // A straight wall is many G1s that render identically as one. Merging
  // them here rather than in the renderer means the saving is paid once
  // and every frame benefits. Only merges within one layer, one tool, one
  // role, and matching dimensions - anything else would change the picture.
  const COLLINEAR_EPS = 0.9995;
  let lastLayerOfSeg = -1;
  function tryExtendLast(x0, y0, z0, x1, y1, z1, w, h, f) {
    const i = positions.n - 1;
    if (i < 0) return false;
    if (lastLayerOfSeg !== layerIdx) return false;
    if (tool.a[i] !== curT || role.a[i] !== curRole) return false;
    if (dims.a[i * 2] !== w || dims.a[i * 2 + 1] !== h) return false;
    if (feedrate.a[i] !== f) return false;
    const o = i * 6;
    // Must start where the last one ended.
    if (positions.a[o + 3] !== x0 || positions.a[o + 4] !== y0
        || positions.a[o + 5] !== z0) return false;
    const ax = positions.a[o + 3] - positions.a[o];
    const ay = positions.a[o + 4] - positions.a[o + 1];
    const az = positions.a[o + 5] - positions.a[o + 2];
    const bx = x1 - x0, by = y1 - y0, bz = z1 - z0;
    const la = Math.sqrt(ax * ax + ay * ay + az * az);
    const lb = Math.sqrt(bx * bx + by * by + bz * bz);
    if (la < 1e-9 || lb < 1e-9) return false;
    const dot = (ax * bx + ay * by + az * bz) / (la * lb);
    if (dot < COLLINEAR_EPS) return false;
    positions.a[o + 3] = x1;
    positions.a[o + 4] = y1;
    positions.a[o + 5] = z1;
    return true;
  }

  function halve() {
    positions.keepEveryOther();
    dims.keepEveryOther();
    feedrate.keepEveryOther();
    role.keepEveryOther();
    tool.keepEveryOther();
    layerOf.keepEveryOther();
    stride *= 2;
  }

  function addTravel(x0, y0, z0, x1, y1, z1) {
    if (travelStride > 1) {
      travelSince++;
      if (travelSince < travelStride) { travelDropped++; return; }
      travelSince = 0;
    }
    travels.push6(x0, y0, z0, x1, y1, z1);
    travelOf.push1(layerIdx);
    if (travels.n >= MAX_TRAVELS) {
      const before = travels.n;
      travels.keepEveryOther();
      travelOf.keepEveryOther();
      travelStride *= 2;
      travelDropped += before - travels.n;
    }
  }

  function addSegment(x0, y0, z0, x1, y1, z1, w, h, f) {
    if (tryExtendLast(x0, y0, z0, x1, y1, z1, w, h, f)) return;
    if (stride > 1) {
      sinceKept++;
      if (sinceKept < stride) { dropped++; return; }
      sinceKept = 0;
    }
    positions.push6(x0, y0, z0, x1, y1, z1);
    dims.push2(w, h);
    feedrate.push1(f);
    role.push1(curRole);
    tool.push1(curT & 0xff);
    layerOf.push1(layerIdx);
    lastLayerOfSeg = layerIdx;
    if (positions.n >= MAX_SEGMENTS) {
      const before = positions.n;
      halve();
      dropped += before - positions.n;
      // The merge target just moved; do not extend across a halving.
      lastLayerOfSeg = -1;
    }
  }

  // One line of the file. Called in file order; a line is whole (the reader
  // joins a line that straddles two slices before it gets here).
  function feedLine(raw) {
    const line = raw.trim();
    if (!line) return;

    let m = TOOLCHANGE_RE.exec(line);
    if (m) {
      sawChange = true;
      curT = parseInt(m[1], 10);
      toolchanges.push({t: curT, x, y, z, layer: layerIdx});
      return;
    }

    if (line.charCodeAt(0) === 59 /* ';' */) {
      if (LAYER_RE.test(line)) {
        layerIdx++;
        ensureLayer(layerIdx);
        return;
      }
      let c = ZCOMMENT_RE.exec(line);
      if (c) { ensureLayer(layerIdx); if (layerZ[layerIdx] === null) layerZ[layerIdx] = parseFloat(c[1]); return; }
      c = WIDTH_RE.exec(line);
      if (c) { curWidth = parseFloat(c[1]); return; }
      c = HEIGHT_RE.exec(line);
      if (c) { curHeight = parseFloat(c[1]); return; }
      c = TYPE_RE.exec(line);
      if (c) { curRole = roleOf(c[1]); return; }
      return;
    }

    if (!sawChange) {
      m = BARE_T_RE.exec(line);
      if (m) {
        curT = parseInt(m[1], 10);
        toolchanges.push({t: curT, x, y, z, layer: layerIdx});
        return;
      }
    }

    if (MOVE_RE.test(line)) {
      let nx = null, ny = null, nz = null, ne = null, nf = null;
      // One scan of the line instead of five separate regex constructions
      // per move - this is the hot loop of the whole parse.
      for (let k = 0; k < line.length; k++) {
        const ch = line.charCodeAt(k);
        if (ch === 59 /* ';' */) break;
        let slot = -1;
        if (ch === 88 || ch === 120) slot = 0;       // X x
        else if (ch === 89 || ch === 121) slot = 1;  // Y y
        else if (ch === 90 || ch === 122) slot = 2;  // Z z
        else if (ch === 69 || ch === 101) slot = 3;  // E e
        else if (ch === 70 || ch === 102) slot = 4;  // F f
        if (slot < 0) continue;
        let j = k + 1;
        const start = j;
        if (line.charCodeAt(j) === 45 /* - */ || line.charCodeAt(j) === 43) j++;
        let seen = false;
        while (j < line.length) {
          const d = line.charCodeAt(j);
          if ((d >= 48 && d <= 57) || d === 46) { j++; seen = true; }
          else break;
        }
        if (!seen) continue;
        const v = parseFloat(line.slice(start, j));
        if (!Number.isFinite(v)) { k = j - 1; continue; }
        if (slot === 0) nx = v;
        else if (slot === 1) ny = v;
        else if (slot === 2) nz = v;
        else if (slot === 3) ne = v;
        else nf = v;
        k = j - 1;
      }

      if (nf !== null) feed = nf;

      const x0 = x, y0 = y, z0 = z;
      if (nx !== null) x = absXY ? nx : x + nx;
      if (ny !== null) y = absXY ? ny : y + ny;
      if (nz !== null) {
        z = absXY ? nz : z + nz;
        ensureLayer(layerIdx);
        if (layerZ[layerIdx] === null) layerZ[layerIdx] = z;
      }

      let dE = 0;
      if (ne !== null) {
        const e0 = e;
        e = absE ? ne : e + ne;
        dE = e - e0;
      }

      if (nx !== null || ny !== null || nz !== null) {
        if (dE > 1e-5) {
          const dx = x - x0, dy = y - y0, dz = z - z0;
          const len = Math.sqrt(dx * dx + dy * dy + dz * dz);
          // Read, don't guess. The fallback only runs for slicers that
          // said nothing.
          let h = curHeight;
          if (h === null) {
            const prevZ = layerIdx > 0 ? layerZ[layerIdx - 1] : null;
            const zz = layerZ[layerIdx];
            h = (prevZ !== null && zz !== null && zz > prevZ) ? (zz - prevZ) : 0.2;
          }
          let w = curWidth;
          if (w === null) {
            w = (len > 1e-6 && h > 1e-6)
              ? (FILAMENT_AREA * dE) / (h * len)
              : 0.42;
            if (!Number.isFinite(w)) w = 0.42;
            w = Math.min(2.0, Math.max(0.05, w));
          }
          addSegment(x0, y0, z0, x, y, z, w, h, feed / 60);
          bound(x0, y0, z0); bound(x, y, z);
        } else {
          addTravel(x0, y0, z0, x, y, z);
        }
      }
    } else if (/^G90\b/i.test(line)) {
      absXY = true;
    } else if (/^G91\b/i.test(line)) {
      absXY = false;
    } else if (/^M82\b/i.test(line)) {
      absE = true;
    } else if (/^M83\b/i.test(line)) {
      absE = false;
    } else if (/^G92\b/i.test(line)) {
      const mm = /E(-?[0-9.]+)/i.exec(line);
      if (mm) e = parseFloat(mm[1]);
    }
  }

  function finish() {
  if (!isFinite(bounds.minX)) {
    // No extrusion moves at all (empty or travel-only file) - a zero-size
    // box divides by zero the moment the renderer computes a fit scale.
    // The UI says so in words rather than showing an empty grid.
    bounds.minX = bounds.minY = bounds.minZ = 0;
    bounds.maxX = bounds.maxY = 1;
    bounds.maxZ = 1;
  }

  // layerRanges is built at the end, from layerOf, because decimation can
  // rewrite the segment list underneath us. Segments are appended in file
  // order and a layer's segments are contiguous, so (first, count) per
  // layer is all the renderer needs to draw any band of layers as one
  // contiguous slice.
  const layerCount = layerZ.length;
  function rangesFrom(idx, count) {
    const out = new Uint32Array(layerCount * 2);
    const a = idx.a;
    let li = -1;
    for (let s = 0; s < count; s++) {
      const l = a[s];
      if (l >= layerCount) continue;
      if (l !== li) {
        // Layers with no geometry keep first=0,count=0, which draws
        // nothing - correct, and the slider still shows them.
        out[l * 2] = s;
        li = l;
      }
      out[l * 2 + 1]++;
    }
    return out;
  }
  const ranges = rangesFrom(layerOf, layerOf.n);
  const travelRanges = rangesFrom(travelOf, travelOf.n);

  return {
    positions: positions.trimmed(),
    dims: dims.trimmed(),
    feedrate: feedrate.trimmed(),
    role: role.trimmed(),
    tool: tool.trimmed(),
    layerOf: layerOf.trimmed(),
    layerRanges: ranges,
    layerZ: Float32Array.from(layerZ.map(v => (v === null ? 0 : v))),
    travels: travels.trimmed(),
    travelRanges,
    segmentCount: positions.n,
    travelCount: travels.n,
    layerCount,
    toolchanges,
    bounds,
    roles: ROLES,
    // Honest about it when the cap engaged: silently dropping geometry
    // from a preview someone is using to check a print is not acceptable.
    decimated: (stride > 1 || travelStride > 1)
      ? {stride, dropped, travelDropped} : null,
  };
  }

  return {feed: feedLine, finish};
}

/* Read `file` slice by slice and hand every line to a parser. Memory is one
 * slice plus the partial last line, whatever the size of the file. */
async function parseFile(file, onProgress) {
  const parser = createParser();
  const dec = new TextDecoder("utf-8");
  const size = file.size || 0;
  let rest = "";
  let lastPct = -1;
  for (let pos = 0; pos < size; pos += READ_CHUNK) {
    const buf = await file.slice(pos, pos + READ_CHUNK).arrayBuffer();
    const text = rest + dec.decode(buf, {stream: pos + READ_CHUNK < size});
    let from = 0;
    for (;;) {
      const nl = text.indexOf("\n", from);
      if (nl < 0) break;
      parser.feed(text.slice(from, nl));
      from = nl + 1;
    }
    rest = text.slice(from);
    const pct = Math.round(((pos + buf.byteLength) / size) * 100);
    if (onProgress && pct !== lastPct) { lastPct = pct; onProgress(pct); }
  }
  if (rest) parser.feed(rest);
  return parser.finish();
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  if (msg.type !== "parse") return;
  try {
    if (msg.file.size > MAX_FILE_BYTES) {
      throw new Error("file too large for the preview");
    }
    const data = await parseFile(msg.file, (pct) => {
      self.postMessage({type: "progress", percent: pct});
    });
    // Transfer, don't clone: the arrays are the whole point.
    const transfer = [
      data.positions.buffer, data.dims.buffer, data.feedrate.buffer,
      data.role.buffer, data.tool.buffer, data.layerOf.buffer,
      data.layerRanges.buffer, data.layerZ.buffer, data.travels.buffer,
      data.travelRanges.buffer,
    ];
    self.postMessage({type: "done", data}, transfer);
  } catch (err) {
    self.postMessage({
      type: "error",
      message: err && err.message ? err.message : String(err),
    });
  }
};

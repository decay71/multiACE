/* multiACE g-code preview — main-thread glue + renderer.
 *
 * Pairs with gcode_preview_worker.js, which parses off the UI thread into
 * transferable typed arrays. This file owns the worker lifecycle, the
 * camera, and the drawing; app.js only calls MultiAceGcodePreview.parse()
 * and drives a Renderer.
 *
 * WHY RAW WebGL2 AND NO LIBRARY. three.js is the obvious pick and it is
 * the wrong one here:
 *   - Offline. This is served off the printer, on a LAN that frequently
 *     has no route to the internet, so a library is ~600KB min vendored
 *     into the repo for one screen. vendor/ holds exactly one file.
 *   - We need ONE primitive: an extruded segment. Not a scene graph,
 *     materials, lights, loaders or a camera rig. Instanced rendering of a
 *     single mesh is a few hundred lines of GL and we own every byte of
 *     the hot path.
 * Written down so it is not relitigated silently: if the hand-rolled
 * camera stalls the work, vendoring three.js + OrbitControls lazily on
 * first preview open is the accepted fallback. Ship the feature over the
 * principle.
 *
 * A Canvas2D path is kept for browsers with no webgl2 - some printer
 * browsers are old. It is the previous top-down view, reading the new
 * data format, and the UI says which one you are looking at rather than
 * pretending they are the same thing.
 */
"use strict";

const MultiAceGcodePreview = (() => {
  let worker = null;

  // Largest file the preview will parse (the worker enforces it too). The
  // parse streams the file, so memory stays flat; this bounds the time.
  const MAX_FILE_BYTES = 512 * 1024 * 1024;

  function ensureWorker() {
    if (!worker) {
      worker = new Worker("gcode_preview_worker.js?v=3");
    }
    return worker;
  }

  // One parse in flight at a time - a second call while the first is still
  // running would cross-talk on the shared worker's onmessage handler.
  let inFlight = null;

  function parse(file, {onProgress} = {}) {
    if (inFlight) {
      return Promise.reject(new Error("a preview parse is already running"));
    }
    const w = ensureWorker();
    inFlight = new Promise((resolve, reject) => {
      const onMsg = (ev) => {
        const msg = ev.data || {};
        if (msg.type === "progress") {
          if (onProgress) onProgress(msg.percent);
          return;
        }
        w.removeEventListener("message", onMsg);
        inFlight = null;
        if (msg.type === "done") resolve(msg.data);
        else reject(new Error(msg.message || "parse failed"));
      };
      w.addEventListener("message", onMsg);
      w.postMessage({type: "parse", file});
    });
    return inFlight;
  }

  // ---- feature-type palette -------------------------------------------
  // OrcaSlicer's own colours, so the preview matches the slicer the file
  // came from - the point of the Feature view is recognition, and a
  // private palette would defeat it. Index matches the worker's ROLES.
  const ROLE_COLORS = [
    "#e6e6e6", // Other
    "#ff7d0a", // Outer wall
    "#ffe439", // Inner wall
    "#1f1fff", // Overhang wall
    "#b03030", // Sparse infill
    "#9654cc", // Internal solid infill
    "#f03f3f", // Top surface
    "#00b287", // Bottom surface
    "#ff8c69", // Ironing
    "#4c80ba", // Bridge infill
    "#ffffff", // Gap infill
    "#008870", // Skirt
    "#00ff00", // Support
    "#008000", // Support interface
    "#b3e3ab", // Prime tower
    "#5fd194", // Custom
  ];

  const VIEW_TYPES = ["filament", "feature", "speed", "width", "height"];

  function hexToRgb(hex) {
    const h = String(hex || "#888888").replace("#", "");
    if (h.length < 6) return [0.53, 0.53, 0.53];
    return [
      parseInt(h.slice(0, 2), 16) / 255,
      parseInt(h.slice(2, 4), 16) / 255,
      parseInt(h.slice(4, 6), 16) / 255,
    ];
  }

  // ---- 4x4 matrix helpers ---------------------------------------------
  // Column-major, the layout GL wants. Only the three operations this
  // viewer actually performs.
  function mat4Identity() {
    return new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]);
  }
  function mat4Perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  }
  function normalize3(v) {
    const l = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / l, v[1] / l, v[2] / l];
  }
  function cross3(a, b) {
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  }
  function mat4LookAt(eye, center, up) {
    const f = normalize3([center[0]-eye[0], center[1]-eye[1], center[2]-eye[2]]);
    const s = normalize3(cross3(f, up));
    const u = cross3(s, f);
    return new Float32Array([
      s[0], u[0], -f[0], 0,
      s[1], u[1], -f[1], 0,
      s[2], u[2], -f[2], 0,
      -(s[0]*eye[0]+s[1]*eye[1]+s[2]*eye[2]),
      -(u[0]*eye[0]+u[1]*eye[1]+u[2]*eye[2]),
       (f[0]*eye[0]+f[1]*eye[1]+f[2]*eye[2]),
      1,
    ]);
  }
  function mat4Mul(a, b) {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) {
      for (let r = 0; r < 4; r++) {
        o[c*4+r] = a[r]*b[c*4] + a[4+r]*b[c*4+1] + a[8+r]*b[c*4+2] + a[12+r]*b[c*4+3];
      }
    }
    return o;
  }

  // ---- shaders ---------------------------------------------------------
  // One vertex shader for both LODs; uLod picks between a camera-facing
  // ribbon (2 triangles, cheap, for wide layer ranges) and a real box with
  // width AND height (8 triangles - the Orca look, and what makes layer
  // height legible at all).
  const VS = `#version 300 es
precision highp float;
in vec3 aCorner;        // (t along segment, side sign, up sign)
in vec3 aP0;
in vec3 aP1;
in vec2 aDims;          // width, height
in float aFeed;
in float aTool;
in float aRole;

uniform mat4 uMVP;
uniform vec3 uCamPos;
uniform int  uLod;      // 0 ribbon, 1 solid
uniform int  uView;     // 0 filament, 1 feature, 2 speed, 3 width, 4 height
uniform vec2 uRange;    // min,max for the quantitative views
uniform vec3 uToolColors[16];
uniform vec3 uRoleColors[16];
uniform float uIsolate; // tool index to isolate, or -1

out vec3 vColor;
out vec3 vNormal;
out float vAlpha;

vec3 ramp(float t) {
  t = clamp(t, 0.0, 1.0) * 4.0;
  if (t < 1.0) return mix(vec3(0.0,0.0,1.0), vec3(0.0,1.0,1.0), t);
  if (t < 2.0) return mix(vec3(0.0,1.0,1.0), vec3(0.0,1.0,0.0), t - 1.0);
  if (t < 3.0) return mix(vec3(0.0,1.0,0.0), vec3(1.0,1.0,0.0), t - 2.0);
  return mix(vec3(1.0,1.0,0.0), vec3(1.0,0.0,0.0), t - 3.0);
}

void main() {
  vec3 seg = aP1 - aP0;
  float segLen = length(seg);
  vec3 dir = segLen > 1e-6 ? seg / segLen : vec3(1.0, 0.0, 0.0);
  vec3 p = mix(aP0, aP1, aCorner.x);

  float hw = max(aDims.x, 0.02) * 0.5;
  float hh = max(aDims.y, 0.02) * 0.5;

  vec3 nrm;
  if (uLod == 0) {
    // Camera-facing quad: cheapest thing that still has the right width.
    vec3 toEye = normalize(uCamPos - p);
    vec3 side = cross(dir, toEye);
    if (length(side) < 1e-6) side = vec3(0.0, 0.0, 1.0);
    side = normalize(side);
    p += side * (aCorner.y * hw);
    nrm = toEye;
  } else {
    // Real box. Z is up in g-code space, so the cross-section is built
    // from world up rather than from the camera - that is what makes
    // layer height read as height.
    vec3 up = abs(dir.z) > 0.99 ? vec3(0.0, 1.0, 0.0) : vec3(0.0, 0.0, 1.0);
    vec3 side = normalize(cross(dir, up));
    vec3 up2 = normalize(cross(side, dir));
    p += side * (aCorner.y * hw) + up2 * (aCorner.z * hh);
    nrm = normalize(side * aCorner.y + up2 * aCorner.z);
  }

  int tool = int(aTool + 0.5);
  int role = int(aRole + 0.5);
  vec3 col;
  if (uView == 0)      col = uToolColors[tool & 15];
  else if (uView == 1) col = uRoleColors[role & 15];
  else {
    float v = uView == 2 ? aFeed : (uView == 3 ? aDims.x : aDims.y);
    float span = max(uRange.y - uRange.x, 1e-6);
    col = ramp((v - uRange.x) / span);
  }

  vColor = col;
  vNormal = nrm;
  // Hovering a legend row isolates that filament: everything else drops
  // back rather than disappearing, so you still see WHERE it sits in the
  // object.
  vAlpha = (uIsolate < 0.0 || abs(aTool - uIsolate) < 0.5) ? 1.0 : 0.08;
  gl_Position = uMVP * vec4(p, 1.0);
}`;

  const FS = `#version 300 es
precision highp float;
in vec3 vColor;
in vec3 vNormal;
in float vAlpha;
uniform vec3 uCamDir;
out vec4 outColor;
void main() {
  // A headlight and a floor of ambient. Deliberately plain: the filament
  // colour must survive the shading, because it is the one thing on this
  // screen that is not ours to restyle.
  float lam = clamp(abs(dot(normalize(vNormal), -uCamDir)), 0.0, 1.0);
  vec3 c = vColor * (0.62 + 0.38 * lam);
  outColor = vec4(c, vAlpha);
}`;

  const LINE_VS = `#version 300 es
precision highp float;
in vec3 aPos;
uniform mat4 uMVP;
void main() { gl_Position = uMVP * vec4(aPos, 1.0); }`;

  const LINE_FS = `#version 300 es
precision highp float;
uniform vec4 uColor;
out vec4 outColor;
void main() { outColor = uColor; }`;

  const POINT_VS = `#version 300 es
precision highp float;
in vec3 aPos;
in vec3 aColor;
uniform mat4 uMVP;
uniform float uSize;
out vec3 vColor;
void main() {
  vColor = aColor;
  gl_Position = uMVP * vec4(aPos, 1.0);
  gl_PointSize = uSize;
}`;

  const POINT_FS = `#version 300 es
precision highp float;
in vec3 vColor;
out vec4 outColor;
void main() {
  // A ring, not a disc: a filled dot hides the toolpath underneath it,
  // and where the swap happens IN the object is the whole question.
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d) * 2.0;
  if (r > 1.0 || r < 0.55) discard;
  outColor = vec4(vColor, 1.0);
}`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(s);
      gl.deleteShader(s);
      throw new Error("shader: " + log);
    }
    return s;
  }
  function program(gl, vsSrc, fsSrc) {
    const p = gl.createProgram();
    const vs = compile(gl, gl.VERTEX_SHADER, vsSrc);
    const fs = compile(gl, gl.FRAGMENT_SHADER, fsSrc);
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(p);
      gl.deleteProgram(p);
      throw new Error("link: " + log);
    }
    return p;
  }

  // Ribbon: one quad, 2 triangles. (t, side, up) - up unused.
  const RIBBON_VERTS = new Float32Array([
    0, -1, 0,  1, -1, 0,  1, 1, 0,
    0, -1, 0,  1,  1, 0,  0, 1, 0,
  ]);
  // Solid: 8 corners, 4 side quads = 8 triangles. No end caps - paths are
  // continuous, so the caps are almost always inside the next segment.
  const SOLID_VERTS = (() => {
    const corners = [[-1,-1], [1,-1], [1,1], [-1,1]];
    const v = [];
    for (let c = 0; c < 4; c++) {
      const a = corners[c], b = corners[(c + 1) % 4];
      // (0,a) (0,b) (1,b) / (0,a) (1,b) (1,a)
      v.push(0, a[0], a[1],  0, b[0], b[1],  1, b[0], b[1]);
      v.push(0, a[0], a[1],  1, b[0], b[1],  1, a[0], a[1]);
    }
    return new Float32Array(v);
  })();

  // Above this many visible segments, `auto` drops to the ribbon LOD. The
  // honest answer is that "solid, always" does not survive a 100MB file in
  // a browser, and a preview that locks the tab is not a preview.
  const SOLID_LIMIT = 400000;

  class Renderer {
    constructor(canvas) {
      this.canvas = canvas;
      this.data = null;
      this.colorForT = () => "#888888";
      this.toolchangeColor = null;   // (tc) => hex, set by app.js

      this.layerLo = 0;
      this.layerHi = 0;
      this.moveLimit = Infinity;
      this.viewType = "filament";
      this.lod = "auto";
      this.showTravel = false;
      this.showToolchanges = true;
      this.showPlate = true;
      this.isolateTool = -1;

      // Orbit camera. Spherical around the model's own centre.
      this.cam = {yaw: -0.9, pitch: 0.62, dist: 300,
                  target: [0, 0, 0], pan: [0, 0, 0]};

      this.gl = null;
      this.ctx2d = null;
      try {
        this.gl = canvas.getContext("webgl2", {
          antialias: true, alpha: false, depth: true,
          preserveDrawingBuffer: false,
        });
      } catch (_) { this.gl = null; }
      this.mode = this.gl ? "gl" : "2d";
      if (this.gl) this._initGL();
      else this.ctx2d = canvas.getContext("2d");
      this._bindPointer();
    }

    // --- setters (the UI drives these) ---------------------------------
    setData(data, colorForT) {
      this.data = data;
      if (colorForT) this.colorForT = colorForT;
      this.layerLo = 0;
      this.layerHi = Math.max(0, (data.layerCount || 1) - 1);
      this.moveLimit = Infinity;
      this._computeRanges();
      this._frameCamera();
      if (this.gl) this._uploadGL();
      this._buildPlate();
      this._buildToolchanges();
    }
    setLayerRange(lo, hi) {
      const last = Math.max(0, (this.data ? this.data.layerCount : 1) - 1);
      this.layerLo = Math.max(0, Math.min(lo, last));
      this.layerHi = Math.max(this.layerLo, Math.min(hi, last));
      this._buildToolchanges();
    }
    setMoveLimit(n) { this.moveLimit = (n === null || n === undefined) ? Infinity : n; }
    setViewType(v) { if (VIEW_TYPES.includes(v)) this.viewType = v; }
    setLod(v) { this.lod = v; }
    setShowTravel(v) { this.showTravel = !!v; }
    setShowToolchanges(v) { this.showToolchanges = !!v; this._buildToolchanges(); }
    setShowPlate(v) { this.showPlate = !!v; }
    setIsolateTool(t) { this.isolateTool = (t === null || t === undefined) ? -1 : t; }
    setToolchangeColor(fn) { this.toolchangeColor = fn; this._buildToolchanges(); }
    // Re-point the per-tool colour lookup without re-uploading geometry
    // buffers - colorForT is consulted per draw for the moving geometry
    // already, so only the cached toolchange markers need a rebuild here.
    setColorForT(fn) { this.colorForT = fn; this._buildToolchanges(); }
    setPreset(name) {
      if (name === "top")        { this.cam.yaw = -Math.PI / 2; this.cam.pitch = 1.5533; }
      else if (name === "front") { this.cam.yaw = -Math.PI / 2; this.cam.pitch = 0.0; }
      else                       { this.cam.yaw = -0.9; this.cam.pitch = 0.62; }
      this.cam.pan = [0, 0, 0];
      this._frameCamera(true);
    }

    // How many segments the top visible layer holds - the move slider's
    // range, published so the UI does not have to know the data layout.
    topLayerMoveCount() {
      const d = this.data;
      if (!d) return 0;
      return d.layerRanges[this.layerHi * 2 + 1] || 0;
    }
    visibleSegmentCount() {
      const r = this._visibleRange();
      return r ? r.count : 0;
    }
    effectiveLod() {
      if (this.lod === "ribbon" || this.lod === "solid") return this.lod;
      return this.visibleSegmentCount() > SOLID_LIMIT ? "ribbon" : "solid";
    }

    _computeRanges() {
      const d = this.data;
      const n = d.segmentCount;
      let fMin = Infinity, fMax = -Infinity;
      let wMin = Infinity, wMax = -Infinity;
      let hMin = Infinity, hMax = -Infinity;
      for (let i = 0; i < n; i++) {
        const f = d.feedrate[i];
        if (f > 0) { if (f < fMin) fMin = f; if (f > fMax) fMax = f; }
        const w = d.dims[i * 2], h = d.dims[i * 2 + 1];
        if (w < wMin) wMin = w; if (w > wMax) wMax = w;
        if (h < hMin) hMin = h; if (h > hMax) hMax = h;
      }
      const fix = (a, b, dflt) => (isFinite(a) && isFinite(b) && b > a)
        ? [a, b] : [dflt[0], dflt[1]];
      this.ranges = {
        speed: fix(fMin, fMax, [0, 1]),
        width: fix(wMin, wMax, [0, 1]),
        height: fix(hMin, hMax, [0, 1]),
      };
      this.toolsPresent = (() => {
        const seen = new Set();
        for (let i = 0; i < n; i++) seen.add(d.tool[i]);
        return [...seen].sort((a, b) => a - b);
      })();
      this.rolesPresent = (() => {
        const seen = new Set();
        for (let i = 0; i < n; i++) seen.add(d.role[i]);
        return [...seen].sort((a, b) => a - b);
      })();
    }

    _frameCamera(keepAngles) {
      const b = this.data.bounds;
      this.cam.target = [(b.minX + b.maxX) / 2, (b.minY + b.maxY) / 2,
                         (b.minZ + b.maxZ) / 2];
      const span = Math.max(b.maxX - b.minX, b.maxY - b.minY, b.maxZ - b.minZ, 1);
      this.cam.dist = span * 2.1;
      if (!keepAngles) this.cam.pan = [0, 0, 0];
    }

    // --- GL setup -------------------------------------------------------
    _initGL() {
      const gl = this.gl;
      this.progSeg = program(gl, VS, FS);
      this.progLine = program(gl, LINE_VS, LINE_FS);
      this.progPoint = program(gl, POINT_VS, POINT_FS);

      this.uSeg = {};
      for (const n of ["uMVP", "uCamPos", "uCamDir", "uLod", "uView",
                       "uRange", "uIsolate"]) {
        this.uSeg[n] = gl.getUniformLocation(this.progSeg, n);
      }
      this.uToolColors = gl.getUniformLocation(this.progSeg, "uToolColors");
      this.uRoleColors = gl.getUniformLocation(this.progSeg, "uRoleColors");
      this.uLineMVP = gl.getUniformLocation(this.progLine, "uMVP");
      this.uLineColor = gl.getUniformLocation(this.progLine, "uColor");
      this.uPointMVP = gl.getUniformLocation(this.progPoint, "uMVP");
      this.uPointSize = gl.getUniformLocation(this.progPoint, "uSize");

      this.aSeg = {
        corner: gl.getAttribLocation(this.progSeg, "aCorner"),
        p0: gl.getAttribLocation(this.progSeg, "aP0"),
        p1: gl.getAttribLocation(this.progSeg, "aP1"),
        dims: gl.getAttribLocation(this.progSeg, "aDims"),
        feed: gl.getAttribLocation(this.progSeg, "aFeed"),
        tool: gl.getAttribLocation(this.progSeg, "aTool"),
        role: gl.getAttribLocation(this.progSeg, "aRole"),
      };
      this.aLinePos = gl.getAttribLocation(this.progLine, "aPos");
      this.aPointPos = gl.getAttribLocation(this.progPoint, "aPos");
      this.aPointColor = gl.getAttribLocation(this.progPoint, "aColor");

      const mk = (data) => {
        const b = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, b);
        gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
        return b;
      };
      this.bufRibbon = mk(RIBBON_VERTS);
      this.bufSolid = mk(SOLID_VERTS);
      this.bufPos = gl.createBuffer();
      this.bufDims = gl.createBuffer();
      this.bufFeed = gl.createBuffer();
      this.bufTool = gl.createBuffer();
      this.bufRole = gl.createBuffer();
      this.bufTravel = gl.createBuffer();
      this.bufPlate = gl.createBuffer();
      this.bufTcPos = gl.createBuffer();
      this.bufTcColor = gl.createBuffer();
      this.plateCount = 0;
      this.tcCount = 0;

      gl.enable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    }

    _uploadGL() {
      const gl = this.gl, d = this.data;
      const up = (buf, arr) => {
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, arr, gl.STATIC_DRAW);
      };
      up(this.bufPos, d.positions);
      up(this.bufDims, d.dims);
      up(this.bufFeed, d.feedrate);
      up(this.bufTool, d.tool);
      up(this.bufRole, d.role);
      up(this.bufTravel, d.travels);
    }

    _buildPlate() {
      if (!this.gl || !this.data) return;
      const b = this.data.bounds;
      const step = 10;
      const x0 = Math.floor(b.minX / step) * step - step;
      const x1 = Math.ceil(b.maxX / step) * step + step;
      const y0 = Math.floor(b.minY / step) * step - step;
      const y1 = Math.ceil(b.maxY / step) * step + step;
      const v = [];
      for (let x = x0; x <= x1; x += step) v.push(x, y0, 0, x, y1, 0);
      for (let y = y0; y <= y1; y += step) v.push(x0, y, 0, x1, y, 0);
      const arr = new Float32Array(v);
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bufPlate);
      gl.bufferData(gl.ARRAY_BUFFER, arr, gl.STATIC_DRAW);
      this.plateCount = arr.length / 3;
    }

    // Toolchange markers carry FEASIBILITY, not just position: the colour
    // says whether the loaded spools can actually serve that swap. That is
    // the question a slicer's preview cannot answer and multiACE can - see
    // toolchangeColor, fed from the preflight report.
    _buildToolchanges() {
      if (!this.data) return;
      const list = this.data.toolchanges.filter(
        tc => tc.layer >= this.layerLo && tc.layer <= this.layerHi);
      this.visibleToolchanges = list;
      if (!this.gl) return;
      const pos = new Float32Array(list.length * 3);
      const col = new Float32Array(list.length * 3);
      list.forEach((tc, i) => {
        pos[i*3] = tc.x; pos[i*3+1] = tc.y;
        pos[i*3+2] = tc.z + 0.2;
        const hex = this.toolchangeColor
          ? this.toolchangeColor(tc)
          : this.colorForT(tc.t);
        const c = hexToRgb(hex);
        col[i*3] = c[0]; col[i*3+1] = c[1]; col[i*3+2] = c[2];
      });
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bufTcPos);
      gl.bufferData(gl.ARRAY_BUFFER, pos, gl.DYNAMIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.bufTcColor);
      gl.bufferData(gl.ARRAY_BUFFER, col, gl.DYNAMIC_DRAW);
      this.tcCount = list.length;
    }

    // The whole reason layerRanges exists: a range draw is one contiguous
    // slice, so there is no per-frame filtering and no per-frame scan.
    _visibleRange() {
      const d = this.data;
      if (!d || !d.segmentCount) return null;
      const r = d.layerRanges;
      let first = -1;
      for (let l = this.layerLo; l <= this.layerHi; l++) {
        if (r[l * 2 + 1] > 0) { first = r[l * 2]; break; }
      }
      if (first < 0) return null;
      let end = first;
      for (let l = this.layerLo; l <= this.layerHi; l++) {
        const c = r[l * 2 + 1];
        if (!c) continue;
        const e = r[l * 2] + c;
        if (e > end) end = e;
      }
      // The move slider scrubs WITHIN the top visible layer.
      const topFirst = r[this.layerHi * 2];
      const topCount = r[this.layerHi * 2 + 1];
      if (topCount && isFinite(this.moveLimit)) {
        const lim = Math.max(0, Math.min(this.moveLimit, topCount));
        end = topFirst + lim;
        if (end < first) end = first;
      }
      return {first, count: Math.max(0, end - first)};
    }

    _travelRange() {
      const d = this.data;
      if (!d || !d.travelCount) return null;
      const r = d.travelRanges;
      let first = -1, end = 0;
      for (let l = this.layerLo; l <= this.layerHi; l++) {
        const c = r[l * 2 + 1];
        if (!c) continue;
        if (first < 0) first = r[l * 2];
        const e = r[l * 2] + c;
        if (e > end) end = e;
      }
      if (first < 0) return null;
      return {first, count: end - first};
    }

    // Call after the canvas element's on-screen size changes - dpr-aware
    // so it stays sharp on HiDPI without redoing the scale math per draw.
    resize() {
      const c = this.canvas;
      const rect = c.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.round(rect.width * dpr));
      const h = Math.max(1, Math.round(rect.height * dpr));
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    }

    _camEye() {
      const {yaw, pitch, dist, target, pan} = this.cam;
      const cp = Math.cos(pitch);
      const t = [target[0] + pan[0], target[1] + pan[1], target[2] + pan[2]];
      return {
        eye: [t[0] + dist * cp * Math.cos(yaw),
              t[1] + dist * cp * Math.sin(yaw),
              t[2] + dist * Math.sin(pitch)],
        center: t,
      };
    }

    draw() {
      if (!this.data) return;
      if (this.mode === "gl") this._drawGL();
      else this._draw2D();
    }

    _drawGL() {
      const gl = this.gl;
      const W = this.canvas.width, H = this.canvas.height;
      gl.viewport(0, 0, W, H);
      gl.clearColor(0.047, 0.047, 0.055, 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

      const {eye, center} = this._camEye();
      const span = Math.max(this.cam.dist, 1);
      const proj = mat4Perspective(45 * Math.PI / 180, W / H,
                                   span * 0.01, span * 12);
      const view = mat4LookAt(eye, center, [0, 0, 1]);
      const mvp = mat4Mul(proj, view);
      const camDir = normalize3([center[0]-eye[0], center[1]-eye[1], center[2]-eye[2]]);

      if (this.showPlate && this.plateCount) {
        gl.useProgram(this.progLine);
        gl.uniformMatrix4fv(this.uLineMVP, false, mvp);
        gl.uniform4f(this.uLineColor, 0.35, 0.39, 0.42, 0.5);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.bufPlate);
        gl.enableVertexAttribArray(this.aLinePos);
        gl.vertexAttribPointer(this.aLinePos, 3, gl.FLOAT, false, 0, 0);
        gl.vertexAttribDivisor(this.aLinePos, 0);
        gl.drawArrays(gl.LINES, 0, this.plateCount);
      }

      if (this.showTravel) {
        const tr = this._travelRange();
        if (tr && tr.count) {
          gl.useProgram(this.progLine);
          gl.uniformMatrix4fv(this.uLineMVP, false, mvp);
          gl.uniform4f(this.uLineColor, 1, 1, 1, 0.16);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.bufTravel);
          gl.enableVertexAttribArray(this.aLinePos);
          gl.vertexAttribPointer(this.aLinePos, 3, gl.FLOAT, false, 0,
                                 tr.first * 24);
          gl.vertexAttribDivisor(this.aLinePos, 0);
          gl.drawArrays(gl.LINES, 0, tr.count * 2);
        }
      }

      const vis = this._visibleRange();
      if (vis && vis.count) {
        const solid = this.effectiveLod() === "solid";
        gl.useProgram(this.progSeg);
        gl.uniformMatrix4fv(this.uSeg.uMVP, false, mvp);
        gl.uniform3fv(this.uSeg.uCamPos, new Float32Array(eye));
        gl.uniform3fv(this.uSeg.uCamDir, new Float32Array(camDir));
        gl.uniform1i(this.uSeg.uLod, solid ? 1 : 0);
        gl.uniform1i(this.uSeg.uView, VIEW_TYPES.indexOf(this.viewType));
        const rg = this.viewType === "speed" ? this.ranges.speed
                 : this.viewType === "width" ? this.ranges.width
                 : this.ranges.height;
        gl.uniform2f(this.uSeg.uRange, rg[0], rg[1]);
        gl.uniform1f(this.uSeg.uIsolate, this.isolateTool);

        const tc = new Float32Array(48);
        for (let i = 0; i < 16; i++) {
          const c = hexToRgb(this.colorForT(i));
          tc[i*3] = c[0]; tc[i*3+1] = c[1]; tc[i*3+2] = c[2];
        }
        gl.uniform3fv(this.uToolColors, tc);
        const rc = new Float32Array(48);
        for (let i = 0; i < 16; i++) {
          const c = hexToRgb(ROLE_COLORS[i] || "#e6e6e6");
          rc[i*3] = c[0]; rc[i*3+1] = c[1]; rc[i*3+2] = c[2];
        }
        gl.uniform3fv(this.uRoleColors, rc);

        gl.bindBuffer(gl.ARRAY_BUFFER, solid ? this.bufSolid : this.bufRibbon);
        gl.enableVertexAttribArray(this.aSeg.corner);
        gl.vertexAttribPointer(this.aSeg.corner, 3, gl.FLOAT, false, 0, 0);
        gl.vertexAttribDivisor(this.aSeg.corner, 0);

        // WebGL2 has no baseInstance, so the layer band is selected by
        // offsetting the instance attribute pointers instead - same
        // effect, one draw call, no copying.
        const inst = (loc, buf, size, type, stride, byteOff) => {
          gl.bindBuffer(gl.ARRAY_BUFFER, buf);
          gl.enableVertexAttribArray(loc);
          gl.vertexAttribPointer(loc, size, type, false, stride, byteOff);
          gl.vertexAttribDivisor(loc, 1);
        };
        // p0 and p1 are the two halves of one 24-byte record, so they read
        // the same buffer at the same stride, twelve bytes apart.
        const segOff = vis.first * 24;
        inst(this.aSeg.p0, this.bufPos, 3, gl.FLOAT, 24, segOff);
        inst(this.aSeg.p1, this.bufPos, 3, gl.FLOAT, 24, segOff + 12);
        inst(this.aSeg.dims, this.bufDims, 2, gl.FLOAT, 0, vis.first * 8);
        inst(this.aSeg.feed, this.bufFeed, 1, gl.FLOAT, 0, vis.first * 4);
        inst(this.aSeg.tool, this.bufTool, 1, gl.UNSIGNED_BYTE, 0, vis.first);
        inst(this.aSeg.role, this.bufRole, 1, gl.UNSIGNED_BYTE, 0, vis.first);

        gl.drawArraysInstanced(gl.TRIANGLES,
                               0, solid ? 24 : 6, vis.count);

        for (const loc of Object.values(this.aSeg)) {
          gl.vertexAttribDivisor(loc, 0);
        }
      }

      if (this.showToolchanges && this.tcCount) {
        gl.useProgram(this.progPoint);
        gl.uniformMatrix4fv(this.uPointMVP, false, mvp);
        gl.uniform1f(this.uPointSize, 11 * Math.min(window.devicePixelRatio || 1, 2));
        gl.bindBuffer(gl.ARRAY_BUFFER, this.bufTcPos);
        gl.enableVertexAttribArray(this.aPointPos);
        gl.vertexAttribPointer(this.aPointPos, 3, gl.FLOAT, false, 0, 0);
        gl.vertexAttribDivisor(this.aPointPos, 0);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.bufTcColor);
        gl.enableVertexAttribArray(this.aPointColor);
        gl.vertexAttribPointer(this.aPointColor, 3, gl.FLOAT, false, 0, 0);
        gl.vertexAttribDivisor(this.aPointColor, 0);
        gl.drawArrays(gl.POINTS, 0, this.tcCount);
      }
    }

    // --- Canvas2D fallback ----------------------------------------------
    // The previous top-down view, reading the new format. Not a
    // second-class copy of the 3D one and not pretending to be it: the UI
    // says which one you are looking at.
    _draw2D() {
      const ctx = this.ctx2d, d = this.data;
      const W = this.canvas.width, H = this.canvas.height;
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = "#0c0c0e";
      ctx.fillRect(0, 0, W, H);

      const b = d.bounds;
      const bw = Math.max(1e-6, b.maxX - b.minX);
      const bh = Math.max(1e-6, b.maxY - b.minY);
      const scale = Math.min((W * 0.92) / bw, (H * 0.92) / bh);
      const offX = (W - bw * scale) / 2 - b.minX * scale;
      // Canvas Y grows downward, g-code Y grows toward the back of the
      // bed - flip so the print reads the way it prints.
      const offY = H - ((H - bh * scale) / 2 - b.minY * scale);

      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      if (this.showTravel) {
        const tr = this._travelRange();
        if (tr) {
          ctx.strokeStyle = "rgba(255,255,255,0.12)";
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          for (let i = tr.first; i < tr.first + tr.count; i++) {
            const o = i * 6;
            ctx.moveTo(d.travels[o] * scale + offX, offY - d.travels[o+1] * scale);
            ctx.lineTo(d.travels[o+3] * scale + offX, offY - d.travels[o+4] * scale);
          }
          ctx.stroke();
        }
      }

      const vis = this._visibleRange();
      if (vis) {
        // Batch by colour: a stroke() per segment is what makes a Canvas2D
        // path view slow, not the segment count.
        const byColor = new Map();
        for (let i = vis.first; i < vis.first + vis.count; i++) {
          const col = this._color2D(i);
          let arr = byColor.get(col);
          if (!arr) { arr = []; byColor.set(col, arr); }
          arr.push(i);
        }
        for (const [col, idxs] of byColor) {
          ctx.strokeStyle = col;
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          for (const i of idxs) {
            const o = i * 6;
            ctx.moveTo(d.positions[o] * scale + offX, offY - d.positions[o+1] * scale);
            ctx.lineTo(d.positions[o+3] * scale + offX, offY - d.positions[o+4] * scale);
          }
          ctx.stroke();
        }
      }

      if (this.showToolchanges) {
        ctx.lineWidth = 1.5;
        for (const tc of (this.visibleToolchanges || [])) {
          const cx = tc.x * scale + offX, cy = offY - tc.y * scale;
          ctx.beginPath();
          ctx.arc(cx, cy, 4, 0, Math.PI * 2);
          ctx.strokeStyle = this.toolchangeColor
            ? this.toolchangeColor(tc) : this.colorForT(tc.t);
          ctx.fillStyle = "rgba(0,0,0,0.55)";
          ctx.fill();
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    _color2D(i) {
      const d = this.data;
      if (this.viewType === "filament") return this.colorForT(d.tool[i]);
      if (this.viewType === "feature") return ROLE_COLORS[d.role[i]] || "#e6e6e6";
      const v = this.viewType === "speed" ? d.feedrate[i]
              : this.viewType === "width" ? d.dims[i * 2] : d.dims[i * 2 + 1];
      const rg = this.ranges[this.viewType];
      const t = Math.max(0, Math.min(1, (v - rg[0]) / Math.max(rg[1] - rg[0], 1e-6)));
      const s = t * 4;
      const lerp = (a, b, f) => Math.round(a + (b - a) * f);
      const stops = [[0,0,255],[0,255,255],[0,255,0],[255,255,0],[255,0,0]];
      const k = Math.min(3, Math.floor(s));
      const f = s - k;
      const a = stops[k], b2 = stops[k+1];
      return `rgb(${lerp(a[0],b2[0],f)},${lerp(a[1],b2[1],f)},${lerp(a[2],b2[2],f)})`;
    }

    // --- pointer: orbit / pan / zoom, mouse and touch alike -------------
    _bindPointer() {
      const c = this.canvas;
      const pts = new Map();
      let lastPinch = 0;
      const onDown = (ev) => {
        c.setPointerCapture(ev.pointerId);
        pts.set(ev.pointerId, {x: ev.clientX, y: ev.clientY,
                               button: ev.button, shift: ev.shiftKey});
        lastPinch = 0;
      };
      const onMove = (ev) => {
        const p = pts.get(ev.pointerId);
        if (!p) return;
        const dx = ev.clientX - p.x, dy = ev.clientY - p.y;
        p.x = ev.clientX; p.y = ev.clientY;
        if (pts.size >= 2) {
          const [a, b] = [...pts.values()];
          const dist = Math.hypot(a.x - b.x, a.y - b.y);
          if (lastPinch) this._zoom(Math.pow(0.99, dist - lastPinch));
          lastPinch = dist;
          this._pan(dx / 2, dy / 2);
        } else if (p.button === 1 || p.button === 2 || p.shift || ev.shiftKey) {
          this._pan(dx, dy);
        } else {
          this.cam.yaw -= dx * 0.008;
          this.cam.pitch = Math.max(-1.55, Math.min(1.55,
            this.cam.pitch + dy * 0.008));
        }
        this._emitDraw();
      };
      const onUp = (ev) => {
        pts.delete(ev.pointerId);
        lastPinch = 0;
        try { c.releasePointerCapture(ev.pointerId); } catch (_) {}
      };
      const onWheel = (ev) => {
        ev.preventDefault();
        this._zoom(ev.deltaY > 0 ? 1.12 : 1 / 1.12);
        this._emitDraw();
      };
      c.addEventListener("pointerdown", onDown);
      c.addEventListener("pointermove", onMove);
      c.addEventListener("pointerup", onUp);
      c.addEventListener("pointercancel", onUp);
      c.addEventListener("wheel", onWheel, {passive: false});
      c.addEventListener("contextmenu", (e) => e.preventDefault());
      this._unbind = () => {
        c.removeEventListener("pointerdown", onDown);
        c.removeEventListener("pointermove", onMove);
        c.removeEventListener("pointerup", onUp);
        c.removeEventListener("pointercancel", onUp);
        c.removeEventListener("wheel", onWheel);
      };
    }
    _zoom(f) {
      this.cam.dist = Math.max(1, Math.min(this.cam.dist * f, 1e5));
    }
    _pan(dx, dy) {
      // Pan in the camera's own screen plane, scaled by distance so the
      // model tracks the finger at any zoom.
      const {yaw} = this.cam;
      const k = this.cam.dist * 0.0016;
      const rightX = -Math.sin(yaw), rightY = Math.cos(yaw);
      this.cam.pan[0] -= (dx * rightX) * k;
      this.cam.pan[1] -= (dx * rightY) * k;
      this.cam.pan[2] += dy * k;
    }
    onDraw(fn) { this._drawCb = fn; }
    _emitDraw() {
      if (this._drawCb) this._drawCb();
      else this.draw();
    }

    dispose() {
      if (this._unbind) this._unbind();
      this.data = null;
    }
  }

  return {parse, Renderer, ROLE_COLORS, VIEW_TYPES, MAX_FILE_BYTES};
})();

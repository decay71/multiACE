// Runs the g-code preview worker under node and prints a summary as JSON.
//   node gcode_preview_driver.js <worker.js> <file.gcode> [chunk] [fakeSize]
// `chunk` overrides READ_CHUNK (to hit slice boundaries); `fakeSize` makes the
// file claim that size, to exercise the size limit without a huge file.
const fs = require("fs"), vm = require("vm");

const [workerPath, gcode, chunk, fakeSize] = process.argv.slice(2);
let src = fs.readFileSync(workerPath, "utf8");
if (chunk) src = src.replace("const READ_CHUNK = 8 << 20;", `const READ_CHUNK = ${chunk};`);

const buf = fs.readFileSync(gcode);
const file = {
  size: fakeSize ? Number(fakeSize) : buf.length,
  slice: (a, b) => ({arrayBuffer: async () => {
    const part = buf.subarray(a, Math.min(b, buf.length));
    return part.buffer.slice(part.byteOffset, part.byteOffset + part.length);
  }}),
};

const self = {postMessage: (m) => {
  if (m.type === "done") {
    const d = m.data;
    let h = 0;
    for (const v of d.positions) h = (h * 31 + Math.round(v * 1000)) | 0;
    console.log(JSON.stringify({
      segments: d.segmentCount, travels: d.travelCount, layers: d.layerCount,
      toolchanges: d.toolchanges.length, bounds: d.bounds, hash: h,
      decimated: d.decimated}));
  } else if (m.type === "error") {
    console.log(JSON.stringify({error: m.message}));
  }
}};
vm.runInNewContext(src, {self, TextDecoder, Float32Array, Uint8Array,
  Uint32Array, Math, Object, Number, parseInt, parseFloat, isFinite, String});
self.onmessage({data: {type: "parse", file}});

// @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
// optional fourth operating mode; original MultiACE modes must not depend on it.
/* Browser-only inspection. Native Flexi remains the print admission authority. */
(function (root) {
  // FLEXI-INTEGRATION: accepted large-file ceiling. Files above the Pyodide
  // budget are dispatched to the server-side disk-backed planner; this is not
  // a browser text-processing allowance.
  const limit = 1024 * 1024 * 1024;
  const allowed = {
    G0:"XYZEF", G1:"XYZEF", G2:"XYZEFIJKR", G3:"XYZEFIJKR", G4:"PS",
    G17:"", G18:"", G19:"", G21:"", G28:"XYZ", G90:"", G91:"", G92:"XYZE",
    M82:"", M83:"", M104:"ST", M109:"STR", M106:"SP", M107:"P", M140:"S",
    M190:"SR", M204:"SPRT", M205:"XYZEJST", M220:"S", M221:"S", M400:"", M73:"PR", M117:"",
  };

  function createInspector(flexi = {}) {
    const production = flexi.production || {};
    const inventory = new Map((production.inventory || []).map(row => [row.key, row]));
    const errors = [], tools = [], seen = new Set(), owners = new Map();
    let issueCount = 0, activeHead = null, firstTool = null;
    const groups = new Map();
    let lineNumber = null, commandText = "";
    const issue = (message, category = "Filament readiness", severity = "Block") => {
      issueCount++;
      if (errors.length < 100) errors.push(message);
      const key = severity + ":" + category;
      if (!groups.has(key)) groups.set(key, {severity, category, count: 0, examples: []});
      const group = groups.get(key);
      group.count++;
      if (group.examples.length < 8 && !group.examples.some(example => example.message === message)) {
        group.examples.push({line: lineNumber, command: commandText, message});
      }
    };
    for (const [head, owner] of Object.entries(production.workflow?.owners || {})) {
      if (owner?.nozzle_verified && Array.isArray(owner.source)) owners.set(Number(head), owner.source.join(":"));
    }
    if (flexi.journal_error) issue("Flexi journal is unavailable. Preserve it for recovery.", "Recovery");
    if (flexi.interlocks?.recovery_required?.length || Object.keys(flexi.interlocks?.nozzle_recovery || {}).length) {
      issue("An interrupted operation requires physical reconciliation.", "Recovery");
    }
    function qualify(key, actions, number) {
      const row = inventory.get(key);
      if (!actions.every(action => row?.validated_actions?.includes(action))) {
        issue(`Line ${number}: required filament movement settings are incomplete for ACE slot ${key}.`);
      }
      if (row?.present !== true) issue(`Line ${number}: ACE slot ${key} is not confirmed present.`);
    }
    function scan(line, number) {
      lineNumber = number;
      commandText = line.split(";", 1)[0].trim().slice(0, 160);
      if (line.length > 16 * 1024 * 1024) { issue(`Line ${number} is oversized.`); return; }
      const code = line.split(";", 1)[0].trim();
      if (!code) return;
      const match = /^([GMT])(\d+)/i.exec(code);
      if (!match) {
        const name = code.split(/\s/, 1)[0];
        const known = /^(?:PRINT_START|PRINT_END|EXCLUDE_OBJECT_(?:DEFINE|START|END)|DEFECT_DETECTION_(?:START|END)|SET_PRINT_STATS_INFO|TIMELAPSE_(?:START|END|TAKE_FRAME))$/i.test(name);
        issue(known ? name + " is a startup or slicer command. Its printer-side behavior needs review."
                    : name + " has no verified Flexi command mapping.", "Startup and macros", "Review");
        return;
      }
      const value = Number(match[2]), command = match[1].toUpperCase() + value;
      let tail = code.slice(match[0].length).trim();
      if (command.startsWith("T")) {
        activeHead = null;
        if (tail) { issue(`Line ${number}: tool selection parameters are not supported.`); return; }
        if (!Number.isInteger(value) || value > 31) { issue("Unsupported tool index.", "Routing"); return; }
        if (!seen.has(value)) { seen.add(value); tools.push(value); }
        if (firstTool === null) firstTool = value;
        let target = production.tool_targets?.[value];
        // Prepared Flexi files use the native virtual tools T4..T19. The
        // printer normally publishes tool_targets, but the browser can still
        // verify the saved route from the same Flexi state when that status is
        // unavailable (for example in the simulated preview).
        if (!target && Number.isInteger(value) && value >= 4 && value <= 31) {
          const ace = Math.floor((value - 4) / 4), slot = (value - 4) % 4;
          const key = ace + ":" + slot;
          if (Number.isInteger(flexi.routes?.[key])) {
            target = {source:[ace, slot], head:flexi.routes[key]};
          }
        }
        const source = Array.isArray(target?.source) ? target.source.join(":") : null;
        const head = target?.head;
        if (!source || !Number.isInteger(head) || head < 0 || head > 3 || flexi.routes?.[source] !== head) {
          const candidates = Object.entries(flexi.routes || {}).filter(([,h]) => h === value).map(([key]) => key);
          issue(candidates.length ? `T${value} has configured branches (${candidates.join(", ")}), but no valid active source selection.`
                                  : `T${value} has no authoritative Flexi route.`, "Routing");
          return;
        }
        const old = owners.get(head);
        const actions = ["print"];
        if (old == null) actions.push("load");
        else if (old !== source) { actions.push("switch"); qualify(old, ["switch"], number); }
        qualify(source, actions, number);
        if (tools.length === 1 && firstTool === value && old !== source) {
          issue("The initial tool needs a verified nozzle load before print start.");
        }
        owners.set(head, source);
        activeHead = head;
        return;
      }
      if (!Object.hasOwn(allowed, command)) {
        issue(`${command} is outside the verified Flexi command set.`, "Unsupported commands", "Review");
        return;
      }
      if (command === "M117") return;
      const params = new Map();
      while (tail) {
        const word = /^([A-Z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))?/i.exec(tail);
        if (!word) break;
        const key = word[1].toUpperCase(), n = Number(word[2] ?? 0);
        if (params.has(key) || !allowed[command].includes(key) ||
            (word[2] == null && command !== "G28") || !Number.isFinite(n)) break;
        params.set(key, n);
        tail = tail.slice(word[0].length).trimStart();
      }
      if (tail) { issue(`Line ${number}: ambiguous, duplicate or unsupported parameters.`); return; }
      if (/^G[0-3]$/.test(command) && params.has("E") && firstTool === null) {
        issue("Extrusion occurs before explicit tool selection.", "Extrusion targeting");
      }
      if (command === "M104" || command === "M109") {
        if (activeHead === null) {
          const addressed = params.get("T");
          issue(Number.isInteger(addressed) && addressed >= 0 && addressed < 4
            ? `${command} explicitly addresses physical T${addressed}; verify the startup sequence before submission.`
            : `${command} has no confirmed physical heater target before tool selection.`, "Heater targeting",
            Number.isInteger(addressed) && addressed >= 0 && addressed < 4 ? "Review" : "Block");
        }
        else if (params.has("T") && params.get("T") !== activeHead) {
          issue(`Line ${number}: heater target differs from the selected physical tool.`);
        }
        if (params.has("S") && params.has("R")) issue(`Line ${number}: ambiguous heater temperature.`);
      }
    }
    return {scan, finish() {
      lineNumber = null; commandText = "";
      if (!tools.length) issue("No explicit tool selection was found.", "Routing");
      return {tools, firstTool, errors, issueCount, groups: [...groups.values()]};
    }};
  }

  function inspect(text, flexi = {}) {
    if (typeof text !== "string" || !text.trim()) throw new Error("Choose a non-empty G-code file.");
    const inspector = createInspector(flexi);
    text.split(/\r\n?|\n/).forEach((line, index) => inspector.scan(line, index + 1));
    return inspector.finish();
  }

  // Same retained line types as preflight_core._PLAN_KEEP_RE. The actual
  // metadata parser and planner still run in the upstream Python modules.
  const planLine = /^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$|M73\b|;?\s*flush_(volumes_matrix|multiplier)\s*=|;\s*multiACE (processed:|auto-load:))/i;
  async function scanFile(file, flexi, progress = () => {}) {
    if (!file.size) throw new Error("Choose a non-empty G-code file.");
    const inspector = flexi ? createInspector(flexi) : null;
    const decoder = new TextDecoder("utf-8", {fatal:true});
    const reader = file.stream().getReader();
    const head = [], tail = [], middle = [];
    let carry = "", oversized = false, line = 0, bytes = 0, retained = 0, planningLimited = false;
    const lineLimit = 8192, tailLimit = 2000, planLimit = 8 * 1024 * 1024;
    function consume(part, end) {
      if (!oversized) {
        if (carry.length + part.length > lineLimit) oversized = true;
        else carry += part;
      }
      if (!end) return;
      const raw = oversized ? " ".repeat(2049) : carry.replace(/\r$/, "");
      line++;
      inspector?.scan(raw, line);
      const bounded = oversized ? "" : raw;
      if (oversized) planningLimited = true;
      if (line <= 300) head.push(bounded);
      else {
        const index = (line - 301) % tailLimit;
        const displaced = tail[index];
        if (displaced && planLine.test(displaced)) {
          retained += displaced.length + 1;
          if (retained > planLimit) planningLimited = true;
          if (!planningLimited) middle.push(displaced);
        }
        tail[index] = bounded;
      }
      carry = ""; oversized = false;
    }
    function chunk(text) {
      let start = 0, end;
      while ((end = text.indexOf("\n", start)) !== -1) {
        consume(text.slice(start, end), true);
        start = end + 1;
      }
      consume(text.slice(start), false);
    }
    let lastProgress = -1;
    try {
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        chunk(decoder.decode(value, {stream:true}));
        bytes += value.byteLength;
        const percent = Math.floor(bytes / file.size * 85);
        if (percent !== lastProgress) { lastProgress = percent; progress(percent); }
      }
      chunk(decoder.decode());
      if (carry || oversized) consume("", true);
    } finally { await reader.cancel(); reader.releaseLock(); }
    const start = line > 300 + tailLimit ? (line - 300) % tailLimit : 0;
    const planText = planningLimited ? "" : [...head, ...middle, ...tail.slice(start), ...tail.slice(0, start)].join("\n");
    return {planText, planningLimited, bytes, lines:line, compatibility:inspector?.finish() || null};
  }
  const api = {inspect, createInspector, scanFile, limit};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FlexiPreflight = api;
})(globalThis);

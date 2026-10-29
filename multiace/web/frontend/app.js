const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } = Vue;
// Behind nginx the UI lives at /multiace; the dev server serves it from the
// root. Deriving the base from the path keeps one bundle working in both.
const BASE = location.pathname.startsWith("/multiace") ? "/multiace" : "";
const API = BASE + "/api";
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
             + location.host + BASE + "/ws";
const SCREEN = "/screen";
createApp({
  setup() {
    // Bowden-path calibration tab (physicsG, #168).
    const CALIBRATION_TAB = true;
    // Orca profile section in Config (profile download + tool map).
    const ORCA_SECTION = true;
    const _validTabs = new Set(["dashboard", "control", "history", "spools", "config", "devices"]);
    if (CALIBRATION_TAB) _validTabs.add("calibration");
    const _storedTab = localStorage.getItem("multiace.tab");
    const _isPluginTab = (s) => typeof s === "string" && s.startsWith("plugin:");
    const tab = ref(
      (_validTabs.has(_storedTab) || _isPluginTab(_storedTab))
        ? _storedTab
        : "dashboard"
    );
    // Opening the spools tab pays any Spoolman debt left over from an
    // unreachable instance (NAS off overnight: the print-end sync failed,
    // the debt survives on disk but nothing retried it). Backend-side the
    // call is idle-only, rate-limited and push-only - during a print the
    // 3 min tick owns the window, so this stays silent then. Fire and
    // forget: the numbers refresh with the next state poll.
    function spoolmanIdlePush() {
      fetch(`${API}/spoolman/push`, {method: "POST"}).catch(() => {});
    }
    watch(tab, (v) => {
      localStorage.setItem("multiace.tab", v);
      if (v === "spools") spoolmanIdlePush();
    });
    const plugins = reactive({items: [], loaded: false});
    async function refreshPlugins() {
      try {
        const r = await fetch(`${API}/integrations`);
        if (!r.ok) return;
        const j = await r.json();
        plugins.items = j.plugins || [];
      } catch (_) {
      } finally {
        plugins.loaded = true;
        if (_isPluginTab(tab.value)) {
          const pname = tab.value.slice("plugin:".length);
          if (!plugins.items.find(p => p.name === pname)) {
            tab.value = "dashboard";
          }
        }
      }
    }
    // The navigation entries, in order. The top bar renders this list, so
    // a feature adds a tab by adding one entry here instead of editing the
    // markup.
    const navItems = computed(() => {
      const items = [];
      if (state.mode !== "normal") {
        items.push({key: "dashboard", label: t("ui.tabs.dashboard")});
      }
      // Print Hub children, in their fixed order: Control, History, Queue,
      // Favorites. Each feature adds its own entry here.
      items.push({key: "control", group: "printhub", label: t("ui.tabs.control")});
      items.push({key: "history", group: "printhub",
                  label: t("ui.tabs.history")});
      items.push({key: "spools", label: t("ui.tabs.spools")});
      // Config children: Settings (the tab key stays "config"), Devices
      // (unit places, names, firmware flash) and Calibration (bowden path).
      items.push({key: "config", group: "config", label: t("ui.tabs.settings")});
      items.push({key: "devices", group: "config", label: t("ui.tabs.devices")});
      if (CALIBRATION_TAB && state.mode !== "normal") {
        items.push({key: "calibration", group: "config",
                    label: t("ui.tabs.calibration")});
      }
      for (const p of plugins.items) {
        items.push({key: "plugin:" + p.name, label: p.label});
      }
      return items;
    });
    // Groups of tabs. An entry with `group: "<key>"` is a child of that group:
    // the top bar shows ONE entry for the group (placed before the entry named
    // in `before`) and a second row lists the children while one of them is
    // open. A group with no children is not shown, so adding a feature's tab
    // is the only thing that makes its group appear.
    const NAV_GROUPS = [
      {key: "printhub", labelKey: "ui.tabs.print_hub", before: "spools"},
      {key: "config", labelKey: "ui.tabs.config", after: "spools"},
    ];
    const groupLast = reactive({});
    for (const g of NAV_GROUPS) {
      try { groupLast[g.key] = localStorage.getItem("multiace.tabGroup." + g.key) || ""; }
      catch (e) { groupLast[g.key] = ""; }
    }
    const groupOf = (key) => {
      const it = navItems.value.find(i => i.key === key);
      return it && it.group ? it.group : null;
    };
    const activeGroup = computed(() => groupOf(tab.value));
    // Remember the last child opened in each group, per browser.
    watch(tab, (v) => {
      const g = groupOf(v);
      if (!g) return;
      groupLast[g] = v;
      try { localStorage.setItem("multiace.tabGroup." + g, v); } catch (e) {}
    });
    const topItems = computed(() => {
      const out = [];
      const addGroup = (g) => {
        const children = navItems.value.filter(i => i.group === g.key);
        if (children.length) {
          out.push({key: "group:" + g.key, group: g.key, children, label: t(g.labelKey)});
        }
      };
      for (const it of navItems.value) {
        if (it.group) continue;
        for (const g of NAV_GROUPS) if (g.before === it.key) addGroup(g);
        out.push(it);
        for (const g of NAV_GROUPS) if (g.after === it.key) addGroup(g);
      }
      return out;
    });
    const subItems = computed(() =>
      activeGroup.value ? navItems.value.filter(i => i.group === activeGroup.value) : []);
    const topActive = (it) => it.group ? activeGroup.value === it.group : tab.value === it.key;
    // A group opens the child that was open last, else its first child.
    function openNav(it) {
      if (!it.group) { tab.value = it.key; return; }
      const last = groupLast[it.group];
      tab.value = (it.children.find(c => c.key === last) || it.children[0]).key;
    }
    function pluginIframeSrc(p) {
      const u = (p && p.ui_url) || "/";
      return `/plugin/${p.name}${u.startsWith("/") ? u : "/" + u}`;
    }
    const language = ref(localStorage.getItem("multiace.lang") || "en");
    const languages = ref([{code: "en", name: "English"}]);
    const catalog = reactive({});
    const indexBase = ref(0);
    function t(key, params) {
      const parts = key.split('.');
      let v = catalog;
      for (const p of parts) {
        if (v == null) return key;
        v = v[p];
      }
      if (typeof v !== "string") return key;
      if (!params) return v;
      return v.replace(/\{(\w+)\}/g, (_, k) => params[k] != null ? params[k] : `{${k}}`);
    }
    function escapeHtml(v) {
      return String(v).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
      })[c]);
    }
    // t() for a string that is rendered as HTML (the confirm dialog): the
    // catalog text keeps its markup, the substituted values are escaped.
    function tHtml(key, params) {
      if (!params) return t(key);
      const safe = {};
      for (const k of Object.keys(params)) {
        safe[k] = params[k] != null ? escapeHtml(params[k]) : params[k];
      }
      return t(key, safe);
    }
    function dispIdx(n) {
      if (n == null) return "-";
      return Number(n) + indexBase.value;
    }
    // Subtype label for display: hide the implicit defaults (empty / Basic /
    // generic) so only a meaningful subtype (Matte, Silk, HF, ...) shows.
    function subText(sku) {
      const s = (sku || "").trim();
      // 'none' is stock print_task_config's placeholder - the backend
      // filters it at the source now, this is the display-edge belt for any
      // path (old backend, hand-typed value) that still carries it.
      if (!s || ["basic", "generic", "none"].includes(s.toLowerCase())) return "";
      return s;
    }
    // Provenance badge label for an identity source:
    // rfid = read from tag, override = user-set, derived = from print job.
    // Empty/raw slots have no badge.
    function sourceLabel(src) {
      if (src === "rfid") return t("ui.common.source_rfid");
      if (src === "override") return t("ui.common.source_override");
      if (src === "derived") return t("ui.common.source_derived");
      return "";
    }
    async function loadCatalog(lang) {
      try {
        const r = await fetch(`${API}/i18n/${lang}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        for (const k of Object.keys(catalog)) delete catalog[k];
        Object.assign(catalog, data);
        document.documentElement.lang = lang;
        if (conn.value.state === "init" || conn.value.state === "warn") {
          conn.value = {
            state: conn.value.state,
            text: conn.value.state === "ok"   ? t("ui.header.live")
                : conn.value.state === "warn" ? t("ui.header.offline")
                : conn.value.state === "err"  ? t("ui.header.ws_error")
                :                               t("ui.header.connecting"),
          };
        }
      } catch (e) {
        console.warn("i18n load failed", e);
      }
    }
    async function loadLanguageList() {
      try {
        const r = await fetch(`${API}/i18n`);
        if (!r.ok) return;
        const j = await r.json();
        if (Array.isArray(j.languages) && j.languages.length) {
          languages.value = j.languages;
        }
      } catch (_) {}
    }
    async function setLanguage(lang) {
      language.value = lang;
      localStorage.setItem("multiace.lang", lang);
      await loadCatalog(lang);
      // Drive the Klipper-side _t() catalog too (pause/error messages) and
      // persist as ace__language, so display popup + Fluidd follow the UI
      // language. Live reload - no Klipper restart needed.
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "MULTIACE_SET_LANGUAGE", args: {LANG: lang}}),
        });
      } catch (_) {}
    }
    const version = ref("");
    const printerName = ref("");
    const printerFw = ref("");
    const conn = ref({state: "init", text: ""});
    const connClass = computed(() => ({
      ok:   conn.value.state === "ok",
      warn: conn.value.state === "warn",
      err:  conn.value.state === "err",
    }));
    const connText = computed(() => conn.value.text);
    const screenAvailable = ref(false);
    const state = reactive({
      ace_status: null, ace_temp: null,
      printer_state: null,
      active_device: null, device_count: 0,
      mode: "normal",
      ace_head: 3,
      ace_heads: [],
      head_feeder: {},
      head_ace: {},
      dryer: null,
      swap_in_progress: false,
      aces: [], toolheads: [], wiring: [],
      save_variables: {},
      bg_swap: {available: false, enabled_heads: [], busy: [], version: null},
      pickup_cleaning: false,
      preflight_max_copies: 1,
      preflight_copies_strict: false,
      tool_map: {},
      confirm_commands: false,
      spoolman_url: "",
      spoolman_auto: false,
      spool_mode: "local",
      spoollink: false,
      spoollink_agent: false,
      airprint_detection: false,
      // Default TRUE, like the engine: the checkbox must not read as off
      // for the instant before the first state arrives.
      purge_matrix: true,
      // Default TRUE, like the engine, for the pre-first-state instant.
      pa_sync: true,
      rc522: false,
      devices: [], units_restart_needed: false, driver_switches: [],
      locate_busy: false,
      tag_write_format: "openspool",
      tag_write_uid_sku: true,
      tag_op: {busy: false, kind: "", seq: 0, result: null},
      nozzle_keys: [],
      quad_replenish: false,
      quad_first: false,
      spools: {},
      spool_binding: {},
      head_tag_seen: {},
      auto_dry_masters: [],
      tipform: {available: false, mode: null, tables: []},
      calibration: {state: "idle", session_id: 0},
      unload_all_active: false,
      // Send-to-multiACE inbox: a slicer-pushed gcode waiting for pickup.
      preflight_inbox: {pending: false, name: null, size: 0, ts: 0},
    });
    const loadError = ref("");
    const notifications = ref([]);
    // True while every visible notification is warn-level (reconnect
    // attempts etc.) - the strip then wears the amber frame instead of
    // red; one real error flips it back to red.
    const notifWarnOnly = computed(() =>
      notifications.value.length > 0 &&
      notifications.value.every(n => n.level === 'warn' || n.level === 'info'));
    const _notifIds = new Set();
    // Backend stamps ts as epoch (printer clock runs UTC) - format in the
    // BROWSER so the user sees local time. HH:MM:SS, fixed width.
    function notifTime(n) {
      if (!n || !n.ts) return '';
      const d = new Date(n.ts * 1000);
      const p = (x) => String(x).padStart(2, '0');
      return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }
    function _addNotif(n) {
      if (!n || n.id == null) return;
      if (_notifIds.has(n.id)) return;
      _notifIds.add(n.id);
      if (n.replaces_info) {
        // Progress line of a running op: it supersedes the earlier ones
        // (the backend dropped them too, so polling will not bring them
        // back; their ids stay in the set on purpose).
        notifications.value = notifications.value.filter(x => x.level !== 'info');
      }
      if (n.ttl) {
        // A [done] line: success dismisses itself.
        setTimeout(() => dismissNotification(n.id), n.ttl * 1000);
      }
      notifications.value.push(n);
      if (notifications.value.length > 20) {
        const dropped = notifications.value.splice(0, notifications.value.length - 20);
        for (const d of dropped) _notifIds.delete(d.id);
      }
    }
    function onGcodeError(m) {
      _addNotif({id: m.id, ts: m.ts, msg: m.msg, raw: m.raw, level: m.level || 'error',
                 replaces_info: !!m.replaces_info, ttl: m.ttl || 0});
    }
    async function loadNotifications() {
      try {
        const r = await fetch(`${API}/notifications`);
        if (!r.ok) return;
        const j = await r.json();
        for (const n of (j.notifications || [])) _addNotif(n);
      } catch (_) {}
    }
    async function dismissNotification(id) {
      const idx = notifications.value.findIndex(n => n.id === id);
      if (idx >= 0) {
        notifications.value.splice(idx, 1);
        _notifIds.delete(id);
      }
      try { await fetch(`${API}/notifications/${id}`, {method: "DELETE"}); } catch (_) {}
    }
    async function dismissAllNotifications() {
      const ids = notifications.value.map(n => n.id);
      notifications.value = [];
      for (const id of ids) _notifIds.delete(id);
      try { await fetch(`${API}/notifications`, {method: "DELETE"}); } catch (_) {}
    }
    // Loading splash (index.html #ma-splash): goes with the first state
    // answer, good or failed, so an error still shows. The cap keeps the
    // page reachable if the backend never answers. showSplash brings it
    // back when the tab returns after a long time in the background.
    function showSplash() {
      const old = document.getElementById("ma-splash");
      if (old && !old.classList.contains("ma-splash-out")) return;
      if (old) old.remove();
      const el = document.createElement("div");
      el.id = "ma-splash";
      el.setAttribute("aria-hidden", "true");
      el.innerHTML = '<div class="ma-logo">m<span>U</span>lt<span>1</span>ACE<span>+</span></div>'
        + '<div class="ma-spin"></div>';
      document.body.appendChild(el);
    }
    function hideSplash() {
      const el = document.getElementById("ma-splash");
      if (!el || el.classList.contains("ma-splash-out")) return;
      el.classList.add("ma-splash-out");
      setTimeout(() => el.remove(), 250);
    }
    setTimeout(hideSplash, 8000);
    // Live states arrive once a second. They are applied at most once per
    // frame and not at all while the tab is hidden: a throttled background
    // tab otherwise renders every queued state in a row on return and stays
    // black for seconds. The fallback timer keeps a visible page that gets
    // no frames (e.g. the panel iframe scrolled out of view) updating.
    let _pendingState = null, _stateRaf = 0, _stateTimer = 0;
    function _flushState() {
      cancelAnimationFrame(_stateRaf); clearTimeout(_stateTimer);
      _stateRaf = 0; _stateTimer = 0;
      const s = _pendingState;
      _pendingState = null;
      if (s) applyState(s);
    }
    function queueState(s) {
      _pendingState = s;
      if (document.hidden || _stateRaf) return;
      _stateRaf = requestAnimationFrame(_flushState);
      _stateTimer = setTimeout(_flushState, 1000);
    }
    // Back after more than RETURN_SPLASH_MS hidden: show the splash, let it
    // paint, then render the held state and fade the splash out.
    const RETURN_SPLASH_MS = 30000;
    let _hiddenSince = document.hidden ? Date.now() : 0;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { _hiddenSince = Date.now(); return; }
      const away = _hiddenSince ? Date.now() - _hiddenSince : 0;
      _hiddenSince = 0;
      if (away < RETURN_SPLASH_MS || !_pendingState) { if (_pendingState) queueState(_pendingState); return; }
      showSplash();
      requestAnimationFrame(() => setTimeout(() => {
        _flushState();
        requestAnimationFrame(hideSplash);
      }, 0));
    });
    function applyState(s) {
      if (!s) return;
      hideSplash();
      // Klippy is down (firmware_restart / reboot in progress, Moonraker up).
      // Show a "please restart printer" hint instead of the raw 503, and keep
      // the last good dashboard visible. Recovers on the next state once
      // Klipper is back. Reuses loadError (no extra UI); cleared below when a
      // real state arrives so the hint never sticks.
      if (s.klippy === 'disconnected') {
        loadError.value = t('ui.common.please_restart');
        // A restart is in progress -> the pending config/mode change is being
        // applied; drop the persistent "restart needed" banner.
        rebootNeeded.value = false;
        restartNoteDown();
        return;
      }
      if (loadError.value === t('ui.common.please_restart')) loadError.value = "";
      restartNoteBack();
      state.ace_status    = s.ace_status ?? null;
      state.ace_temp      = s.ace_temp ?? null;
      state.printer_state = s.printer_state ?? null;
      state.active_device = s.active_device ?? null;
      state.device_count  = s.device_count ?? 0;
      state.mode          = s.mode || "normal";
      state.pickup_cleaning = !!s.pickup_cleaning;
      state.preflight_max_copies = Math.max(1, Math.min(4, Number(s.preflight_max_copies || 1)));
      state.preflight_copies_strict = !!s.preflight_copies_strict;
      state.tool_map = (s.tool_map && typeof s.tool_map === "object") ? s.tool_map : {};
      state.confirm_commands = !!s.confirm_commands;
      state.spoolman_url = s.spoolman_url || "";
      state.spoolman_auto = !!s.spoolman_auto;
      state.spool_mode = s.spool_mode || "local";
      state.spoollink = !!s.spoollink;
      state.spoollink_agent = !!s.spoollink_agent;
      state.airprint_detection = !!s.airprint_detection;
      state.quad_replenish = !!s.quad_replenish;
      state.quad_first = !!s.quad_first;
      state.purge_matrix = (s.purge_matrix === undefined)
        ? true : !!s.purge_matrix;
      state.pa_sync = (s.pa_sync === undefined) ? true : !!s.pa_sync;
      // RC522 tag read/write available - gates the picker's write button.
      state.rc522 = !!s.rc522;
      state.tag_write_format = (s.tag_write_format === "anycubic")
        ? "anycubic" : "openspool";
      state.tag_write_uid_sku = (s.tag_write_uid_sku === undefined)
        ? true : !!s.tag_write_uid_sku;
      state.tag_op = (s.tag_op && typeof s.tag_op === "object")
        ? {busy: !!s.tag_op.busy, kind: String(s.tag_op.kind || ""),
           seq: Number(s.tag_op.seq || 0),
           result: (s.tag_op.result && s.tag_op.result.kind)
             ? {ok: !!s.tag_op.result.ok, kind: String(s.tag_op.result.kind),
                seq: Number(s.tag_op.result.seq || 0),
                msg: String(s.tag_op.result.msg || "")}
             : null}
        : {busy: false, kind: "", seq: 0, result: null};
      state.nozzle_keys = Array.isArray(s.nozzle_keys) ? s.nozzle_keys : [];
      state.spools = s.spools || {};
      state.spool_binding = s.spool_binding || {};
      // Feeder-reader card per non-ACE head; the head picker's Read button
      // judges its result by this field.
      state.head_tag_seen = s.head_tag_seen || {};
      state.devices = Array.isArray(s.devices) ? s.devices : [];
      devNameSettle(state.devices);
      state.units_restart_needed = !!s.units_restart_needed;
      state.driver_switches = Array.isArray(s.driver_switches) ? s.driver_switches : [];
      state.locate_busy = !!s.locate_busy;
      state.auto_dry_masters = Array.isArray(s.auto_dry_masters)
        ? s.auto_dry_masters : [];
      // What Klipper RUNS (the cfg side comes from /api/tipform). Missing
      // here, state.tipform kept its declared default {available:false} for
      // ever: tipformRestartPending then read the live mode as "stock"
      // against a cfg mode of "custom" and showed "restart Klipper to
      // apply" permanently, right after a restart too. Same class as
      // auto_dry_masters above - backend sends it, the state object
      // declares it, applyState must copy it.
      state.tipform = (s.tipform && typeof s.tipform === "object")
        ? s.tipform
        : {available: false, mode: null, tables: []};
      state.ace_head      = (typeof s.ace_head === "number") ? s.ace_head : 3;
      state.ace_heads     = Array.isArray(s.ace_heads) ? s.ace_heads : [];
      state.head_feeder   = (s.head_feeder && typeof s.head_feeder === "object") ? s.head_feeder : {};
      state.head_ace      = (s.head_ace && typeof s.head_ace === "object") ? s.head_ace : {};
      state.dryer         = s.dryer ?? null;
      state.swap_in_progress = !!s.swap_in_progress;
      state.calibration    = (s.calibration && typeof s.calibration === "object")
        ? s.calibration : {state: "idle", session_id: 0};
      state.unload_all_active = !!s.unload_all_active;
      state.aces          = Array.isArray(s.aces) ? s.aces : [];
      state.toolheads     = Array.isArray(s.toolheads) ? s.toolheads : [];
      syncCalibrationDefaults();
      state.wiring        = Array.isArray(s.wiring) ? s.wiring : [];
      state.save_variables = s.save_variables || {};
      state.bg_swap       = (s.bg_swap && typeof s.bg_swap === "object")
        ? s.bg_swap
        : {available: false, enabled_heads: [], busy: [], version: null};
      state.preflight_inbox = (s.preflight_inbox && typeof s.preflight_inbox === "object")
        ? s.preflight_inbox
        : {pending: false, name: null, size: 0, ts: 0};
      _maybeAutoOpenInbox();
      if (typeof s.display_index_base === "number") {
        indexBase.value = s.display_index_base;
      }
      for (const a of state.aces) {
        // Duration only - the temperature is a printer-side setting now
        // (ace.auto_dry.temp), shared by the manual start and auto-dry.
        if (!dryerCfg[a.idx]) dryerCfg[a.idx] = {duration: 240};
      }
    }
    async function reloadState() {
      try {
        const r = await fetch(`${API}/state`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        loadError.value = j.error || "";
        applyState(j);
      } catch (e) {
        loadError.value = String(e);
      } finally {
        hideSplash();
      }
    }
    const macroLog = ref("");
    let _macroLogTimer = null;
    function setMacroLog(msg) {
      macroLog.value = msg || "";
      if (_macroLogTimer) { clearTimeout(_macroLogTimer); _macroLogTimer = null; }
      if (msg) {
        _macroLogTimer = setTimeout(() => {
          macroLog.value = "";
          _macroLogTimer = null;
        }, 5000);
      }
    }
    const dryerCfg = reactive({});
    const cmdQueue = ref([]);
    const visibleQueue = computed(() => cmdQueue.value.filter(it => !it.silent));
    const cmdPaused = ref(false);
    let cmdQueueRunning = false;
    function _newId() {
      return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    }
    function _argsKey(args) {
      const a = args || {};
      return Object.keys(a).sort().map(k => `${k}=${a[k]}`).join('|');
    }
    function enqueue(name, args, opts) {
      return new Promise((resolve) => {
        // A new command after a FAILED one is a new intention: drop the
        // failed entry and whatever still waited
        // behind it - that chain (e.g. the load after a failed unload) was
        // blocked for good, and its red notification has been seen - lift
        // the error pause and let the new command run. A pause pressed by
        // hand is not touched.
        if (cmdPaused.value && cmdPausedByError) {
          const keep = [], dropped = [];
          for (const it of cmdQueue.value) {
            (it.status === 'error' || it.status === 'queued' ? dropped : keep).push(it);
          }
          cmdQueue.value = keep;
          // Callers awaiting a dropped item (loadSlot awaits its unload
          // before the load) must not hang: settle them as "not done".
          for (const it of dropped) { if (it._resolve) it._resolve(false); }
          cmdPaused.value = false;
          cmdPausedByError = false;
        }
        const key = _argsKey(args);
        const dup = cmdQueue.value.find(it =>
          (it.status === 'queued' || it.status === 'running')
          && it.cmd === name
          && _argsKey(it.args) === key);
        if (dup) { resolve(false); return; }
        const it = reactive({
          id: _newId(),
          cmd: name,
          args: args || {},
          status: 'queued',
          error: '',
          silent: !!(opts && opts.silent),
          _resolve: resolve,
        });
        cmdQueue.value.unshift(it);
        // In the panel the dispatch waits one microtask, so a synchronous
        // burst reaches the dispatcher TOGETHER. Every case that matters is
        // such a burst - load-on-an-occupied-head enqueues unload+load in
        // one run, a loadout its whole action list, the picker save+refresh.
        // Called synchronously the first command was already on its way
        // before the second existed, so the batch check below never saw
        // more than one item (measured). Two presses seconds apart still
        // go one by one - by then the first is at the printer anyway.
        if (panelMode) queueMicrotask(_scheduleAdvance);
        else _scheduleAdvance();
      });
    }
    function removeFromQueue(id) {
      const idx = cmdQueue.value.findIndex(i => i.id === id);
      if (idx < 0) return;
      const it = cmdQueue.value[idx];
      if (it.status === 'running') return;
      cmdQueue.value.splice(idx, 1);
      if (it._resolve) it._resolve(false);
      _scheduleAdvance();
    }
    // true while the pause came from a FAILED command (not from the pause
    // button): a new enqueue lifts that pause by itself, see enqueue.
    let cmdPausedByError = false;
    function pauseQueue() { cmdPaused.value = true; cmdPausedByError = false; }
    function resumeQueue() {
      cmdPaused.value = false;
      cmdPausedByError = false;
      _scheduleAdvance();
    }
    function _scheduleAdvance() {
      if (cmdQueueRunning) return;
      // A batch is on its way: its items are still 'queued' until the POST
      // resolves, so without this the next pass would send them a second
      // time, one by one.
      if (sendingAll.value) return;
      if (cmdPaused.value) return;
      if (cmdQueue.value.length === 0) return;
      // Klipper processes gcode serially: a Load/Unload swap holds
      // its slot for 5-15 min. POSTing /api/macro while
      // state.swap_in_progress would just block waiting for the
      // current swap and eventually hit httpx's ReadTimeout. Let
      // queued items wait visible in the queue; a watcher on
      // state.swap_in_progress re-invokes us when Klipper clears.
      if (state.swap_in_progress) return;
      const arr = cmdQueue.value;
      // In the panel the queue is a liability rather than a safety net:
      // Fluidd drops the camera tile's iframe whenever it pauses its
      // streams (a browser tab switch does exactly that), and everything
      // still waiting in browser memory dies with it. A Load on an occupied
      // head would then unload and never load - press Load, end up empty.
      // So as soon as there is more than one command, hand the whole run to
      // the printer as a single script: Klipper owns the sequence from then
      // on and the tile may vanish. One command needs none of this - it is
      // dispatched immediately and already lives at the printer. sendingAll
      // guards against a second batch while the first is still posting.
      if (panelMode && arr.filter(it => it.status === 'queued').length > 1) {
        sendAllToPrinter();
        return;
      }
      let target = null;
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i].status === 'queued') { target = arr[i]; break; }
        if (arr[i].status === 'error')  { return; }
      }
      if (!target) {
        _scheduleIdleClear();
        return;
      }
      _runItem(target);
    }
    function _scheduleIdleClear() {
      const stillActive = cmdQueue.value.some(
        it => it.status === 'queued' || it.status === 'running');
      if (stillActive) return;
      if (cmdPaused.value) cmdPaused.value = false;
      cmdPausedByError = false;
    }
    async function _runItem(it) {
      cmdQueueRunning = true;
      it.status = 'running';
      const parts = [it.cmd];
      for (const [k, v] of Object.entries(it.args || {})) {
        parts.push(`${k}=${v}`);
      }
      const script = parts.join(' ');
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: it.cmd, args: it.args || {}}),
        });
        const j = await r.json();
        if (!r.ok || j.detail) {
          it.status = 'error';
          it.error = String(j.detail || `HTTP ${r.status}`);
          it.silent = false;
          cmdPaused.value = true;
          cmdPausedByError = true;
        } else {
          const idx = cmdQueue.value.indexOf(it);
          if (idx >= 0) cmdQueue.value.splice(idx, 1);
          it.status = 'done';
        }
      } catch (e) {
        it.status = 'error';
        it.error = String(e);
        it.silent = false;
        cmdPaused.value = true;
        cmdPausedByError = true;
      } finally {
        cmdQueueRunning = false;
        if (it._resolve) it._resolve(it.status !== 'error');
      }
      _scheduleAdvance();
    }
    function run(name, args) { return enqueue(name, args); }

    const calibrationUi = reactive({
      ace: 0, slot: 0, head: 0, scope: 'ace',
      // ACE transport varies run to run, so the saved values carry a
      // generous margin: the load is stopped by the toolhead sensor
      // anyway, and a swap retract that runs long only costs seconds
      // while one that stops short leaves filament in the hub.
      loadAllowance: 150, swapAllowance: 100,   // load: 200 without encoder (below)
      panel: 'setup', panelTouched: false, recalibrate: false,
      busy: '', cancelRequested: false, error: '', saved: false,
      hubAdjust: false,
    });
    const calibrationSelectionTouched = reactive({
      ace: false, slot: false, head: false,
    });
    const CAL_MEASURING = ['prepared', 'feeding', 'feed_paused', 'at_sensor',
      'retract_ready', 'retracting', 'feed_jogging', 'swap_marked',
      'returning', 'return_paused'];
    const CAL_VERIFYING = ['verifying_feed', 'verify_toolhead',
      'verify_toolhead_adjust', 'verify_toolhead_adjusting',
      'verifying_splitter', 'verify_splitter', 'verify_splitter_adjusting',
      'verifying_return', 'verify_paused'];
    const CAL_MOVING = ['feeding', 'retracting', 'returning', 'feed_jogging',
      'homing', 'verifying_feed', 'verifying_splitter', 'verifying_return',
      'verify_toolhead_adjusting', 'verify_splitter_adjusting'];
    const CAL_PARKED = ['idle', 'complete', 'verified'];
    // Stopped mid-way with the position kept: can be carried on.
    const CAL_PAUSED = ['feed_paused', 'return_paused', 'verify_paused'];
    const connectedCalibrationAces = computed(() =>
      state.aces.filter(a => a && a.connected));
    const calibrationSelectedAce = computed(() =>
      state.aces.find(a => Number(a.idx) === Number(calibrationUi.ace)) || null);
    // Load margin default per unit: without an encoder the feed slips
    // while it pushes into the head (ACE Pro: ~4% of the route, +100 mm on
    // 2.3 m), so the budget gets more room there. Set only when the kind of
    // the selected unit changes, so a typed value stays.
    watch(() => aceHasDecoder(calibrationSelectedAce.value), (hasEncoder) => {
      calibrationUi.loadAllowance = hasEncoder ? 150 : 200;
    }, {immediate: true});
    const calibrationSelectedToolhead = computed(() =>
      state.toolheads.find(
        h => Number(h.idx) === Number(calibrationUi.head)) || null);
    const calibrationSelectedSlot = computed(() =>
      calibrationSelectedAce.value?.slots?.find(
        slot => Number(slot.idx) === Number(calibrationUi.slot)) || null);
    const calibrationVerifyMoving = computed(() =>
      ['verifying_feed', 'verifying_splitter', 'verifying_return']
        .includes(state.calibration?.state));
    const calibrationVerifyActive = computed(() =>
      CAL_VERIFYING.includes(state.calibration?.state));
    const calibrationMoving = computed(() =>
      CAL_MOVING.includes(state.calibration?.state));
    // Calibration filament is (believed to be) somewhere in the tube.
    const calibrationRouteOccupied = computed(() => {
      const c = state.calibration || {};
      const position = Number(c.tip_position_mm);
      return !CAL_PARKED.includes(c.state || 'idle')
        && Number.isFinite(position) && position > 0;
    });
    const calibrationRouteLocked = computed(() => {
      const c = state.calibration || {};
      return c.ace != null && c.slot != null && c.head != null
        && (calibrationRouteOccupied.value
          || !CAL_PARKED.includes(c.state || 'idle'));
    });
    const calibrationSelectedHeadSensorKnown = computed(() =>
      typeof calibrationSelectedToolhead.value?.filament_at_extruder === 'boolean');
    const calibrationSelectedHeadClear = computed(() =>
      calibrationSelectedToolhead.value?.filament_at_extruder === false);
    const calibrationPrinterFree = computed(() =>
      state.mode !== 'normal'
        && !['printing', 'paused'].includes(state.printer_state)
        && !state.swap_in_progress
        && !(state.bg_swap?.busy || []).length);
    const calibrationCanPrepare = computed(() => {
      const ace = calibrationSelectedAce.value;
      const toolhead = calibrationSelectedToolhead.value;
      return !!ace && ace.connected
        && !!toolhead && !toolhead.manual && !toolhead.feeder
        && calibrationSelectedHeadSensorKnown.value
        && calibrationPrinterFree.value
        && !calibrationMoving.value;
    });
    // Why a new measurement cannot start right now ('' = it can).
    const calibrationStartBlockedReason = computed(() => {
      if (calibrationRouteOccupied.value) return t('ui.calibration.blk_occupied');
      if (!calibrationSelectedAce.value?.connected) return t('ui.calibration.blk_ace_off');
      if (calibrationSelectedSlot.value?.state !== 'ready') {
        return t('ui.calibration.blk_slot_ready');
      }
      if (!calibrationSelectedHeadSensorKnown.value) {
        return t('ui.calibration.blk_sensor_na');
      }
      if (calibrationSelectedToolhead.value?.manual
          || calibrationSelectedToolhead.value?.feeder) {
        return t('ui.calibration.blk_not_ace_head');
      }
      if (!calibrationSelectedHeadClear.value) return t('ui.calibration.blk_head_loaded');
      if (state.mode === 'normal') return t('ui.calibration.blk_normal_mode');
      if (['printing', 'paused'].includes(state.printer_state)) {
        return t('ui.calibration.blk_printing');
      }
      if (state.swap_in_progress || (state.bg_swap?.busy || []).length) {
        return t('ui.calibration.blk_busy');
      }
      if (![...CAL_PARKED, 'cancelled', 'failed']
          .includes(state.calibration?.state || 'idle')) {
        return t('ui.calibration.blk_session');
      }
      return '';
    });
    const calibrationCanStart = computed(() =>
      !calibrationStartBlockedReason.value && !calibrationMoving.value);
    const calibrationVerifyBlockedReason = computed(() => {
      if (!calibrationRouteKnown.value) return t('ui.calibration.blk_no_values');
      // Klipper still runs the values from before the save.
      return calibrationStartBlockedReason.value;
    });
    const calibrationCanVerifyConfigured = computed(() =>
      !calibrationVerifyBlockedReason.value && !calibrationMoving.value);
    const calibrationProposed = computed(() => {
      const c = state.calibration || {};
      const allowance = (v) => Math.max(0, Number(v) || 0);
      const retract = c.retract_length_mm == null ? null : Number(c.retract_length_mm);
      // A session primed from saved values already carries its margin;
      // adding it again would grow the value on every verify + save.
      const swapFromConfig = c.values_source === 'saved';
      let swap = null, capped = false;
      if (c.swap_retract_length_mm != null) {
        const base = Number(c.swap_retract_length_mm);
        // plus whatever was pulled back further at the hub stop in verify
        swap = (swapFromConfig
          ? base : base + allowance(calibrationUi.swapAllowance))
          + Math.max(0, Number(c.verify_hub_extra_mm) || 0);
        // The hub clearance must stay short of the full retract.
        if (retract != null) {
          const cap = Math.max(base, retract - 10);
          capped = swap > cap;
          swap = Math.min(swap, cap);
        }
      }
      return {
        load: c.load_length_mm == null ? null
          : Number(c.load_length_mm) + allowance(calibrationUi.loadAllowance),
        swap, retract, capped, swapFromConfig,
      };
    });
    const calibrationConfiguredRoute = computed(() => {
      const ace = Number(calibrationUi.ace);
      const slot = Number(calibrationUi.slot);
      const aceCfg = configForm.perAce?.[ace] || {};
      const slotCfg = aceCfg.perSlot?.[slot] || {};
      const effective = (key) => {
        for (const value of [slotCfg[key], aceCfg[key], configForm[key]]) {
          if (value !== '' && value != null && Number.isFinite(Number(value))
              && Number(value) > 0) return Number(value);
        }
        return null;
      };
      return {
        load: effective('retract_length'),
        feedLimit: effective('load_length'),
        swap: effective('swap_retract_length'),
      };
    });
    const calibrationSessionMatchesSelection = computed(() => {
      const c = state.calibration || {};
      return Number(c.ace) === Number(calibrationUi.ace)
        && Number(c.slot) === Number(calibrationUi.slot)
        && Number(c.head) === Number(calibrationUi.head);
    });
    const calibrationRouteAnchors = computed(() => {
      const c = state.calibration || {};
      const measuredLoad = !calibrationSessionMatchesSelection.value
        || c.load_length_mm == null ? null : Number(c.load_length_mm);
      const measuredSwap = !calibrationSessionMatchesSelection.value
        || c.swap_retract_length_mm == null
        ? null : Number(c.swap_retract_length_mm);
      // Saved values only stand in when this route really is calibrated
      // (per slot or per MMU). The global [ace] defaults would otherwise
      // draw a hub position nobody measured.
      const cfg = calibrationRouteCalibrated.value
        ? calibrationConfiguredRoute.value : {};
      const load = Number.isFinite(measuredLoad) && measuredLoad > 0
        ? measuredLoad : (cfg.load ?? null);
      const swap = Number.isFinite(measuredSwap) && measuredSwap > 0
        ? measuredSwap : (cfg.swap ?? null);
      return {load, swap, source: measuredLoad > 0 && measuredSwap > 0
        ? 'measured' : 'configured'};
    });
    const _calSavedAt = (cfg, key) => cfg && cfg[key] !== '' && cfg[key] != null
      && Number.isFinite(Number(cfg[key])) && Number(cfg[key]) > 0;
    // A route counts as CALIBRATED only when a value was actually SAVED at
    // the per-ACE or per-slot level - NOT the shipped global [ace] default
    // (retract_length/swap_retract_length), which is always present and
    // otherwise makes every fresh install read as calibrated.
    const calibrationRouteCalibrated = computed(() => {
      const aceCfg = configForm.perAce?.[Number(calibrationUi.ace)] || {};
      const slotCfg = aceCfg.perSlot?.[Number(calibrationUi.slot)] || {};
      const saved = (key) => _calSavedAt(slotCfg, key) || _calSavedAt(aceCfg, key);
      return saved('retract_length') && saved('swap_retract_length');
    });
    // Slots of the selected ACE that carry their own values.
    const calibrationOwnValueSlots = computed(() => {
      const aceCfg = configForm.perAce?.[Number(calibrationUi.ace)] || {};
      return (aceCfg.perSlot || [])
        .map((slotCfg, idx) => ({slotCfg, idx}))
        .filter(({slotCfg}) => ['load_length', 'swap_retract_length', 'retract_length']
          .some(key => _calSavedAt(slotCfg, key)))
        .map(({idx}) => idx);
    });
    function calibrationSlotBadge(slot) {
      if (!slot || slot.state !== 'ready') return slot?.state || '';
      const aceCfg = configForm.perAce?.[Number(calibrationUi.ace)] || {};
      if (calibrationOwnValueSlots.value.includes(Number(slot.idx))) {
        return t('ui.calibration.slot_own');
      }
      if (_calSavedAt(aceCfg, 'retract_length')) return t('ui.calibration.slot_shared');
      return t('ui.calibration.slot_none');
    }
    const calibrationRouteKnown = computed(() => {
      const {load, swap} = calibrationRouteAnchors.value;
      const valid = Number.isFinite(load) && Number.isFinite(swap)
        && load > 0 && swap > 0 && swap < load;
      if (!valid) return false;
      // Known = measured in this session, OR a real per-ACE/per-slot
      // calibration is saved. The global default alone does not count.
      return calibrationRouteAnchors.value.source === 'measured'
        || calibrationRouteCalibrated.value;
    });
    const calibrationMeasureStep = computed(() => {
      const s = state.calibration?.state;
      if (['prepared', 'feeding', 'feed_paused'].includes(s)) return 1;
      if (['at_sensor', 'retract_ready', 'retracting', 'feed_jogging'].includes(s)) return 2;
      if (['swap_marked', 'returning', 'return_paused'].includes(s)) return 3;
      return 0;
    });
    const calibrationVerifyStep = computed(() => {
      const c = state.calibration || {};
      const s = c.state === 'verify_paused' ? c.verify_paused_from : c.state;
      if (['verifying_feed', 'verify_toolhead', 'verify_toolhead_adjust',
        'verify_toolhead_adjusting'].includes(s)) return 1;
      if (['verifying_splitter', 'verify_splitter',
        'verify_splitter_adjusting'].includes(s)) return 2;
      if (s === 'verifying_return') return 3;
      return 0;
    });
    const calibrationTabStatus = computed(() => {
      const c = state.calibration || {};
      const measuring = CAL_MEASURING.includes(c.state);
      const verifying = CAL_VERIFYING.includes(c.state);
      return {
        setup: t('ui.calibration.st_route_ready'),
        calibrate: measuring ? t('ui.calibration.st_running')
          : calibrationRouteKnown.value ? t('ui.calibration.st_values') : t('ui.calibration.st_required'),
        verify: verifying ? t('ui.calibration.st_running')
          : c.state === 'verified' ? t('ui.calibration.st_passed')
            : calibrationRouteKnown.value ? t('ui.calibration.st_available') : t('ui.calibration.st_needs_values'),
        results: calibrationUi.saved ? t('ui.calibration.st_saved')
          : calibrationResultCase.value === 'passed' ? t('ui.calibration.st_passed')
            : calibrationResultCase.value ? t('ui.calibration.st_review') : t('ui.calibration.st_no_result'),
      };
    });
    // One human-readable status for the route header.
    const calibrationStatus = computed(() => {
      const s = state.calibration?.state || 'idle';
      if (CAL_MOVING.includes(s)) return {text: t('ui.calibration.status_moving'), kind: 'busy'};
      if (CAL_PAUSED.includes(s)
          || (['cancelled', 'failed'].includes(s) && calibrationRouteOccupied.value)) {
        return {text: t('ui.calibration.status_stopped'), kind: 'attention'};
      }
      if (['at_sensor', 'retract_ready', 'swap_marked', 'verify_toolhead',
        'verify_toolhead_adjust', 'verify_splitter'].includes(s)) {
        return {text: t('ui.calibration.status_waiting'), kind: 'attention'};
      }
      return calibrationRouteKnown.value
        ? {text: t('ui.calibration.route_known'), kind: 'ready'}
        : {text: t('ui.calibration.route_needs'), kind: 'attention'};
    });
    // A stopped move is never a dead end: null, or what the user can do.
    const calibrationRecovery = computed(() => {
      const c = state.calibration || {};
      if (CAL_PAUSED.includes(c.state)) return {canResume: true};
      if (['cancelled', 'failed'].includes(c.state)
          && calibrationRouteOccupied.value) return {canResume: false};
      return null;
    });
    const calibrationNothingToSave = computed(() => {
      const c = state.calibration || {};
      return c.state === 'verified' && c.values_source === 'saved'
        && !c.verify_remeasured_mm && !c.verify_hub_extra_mm;
    });
    // The verify keeps the saved values; this only flags a run whose sensor
    // came outside the tolerance but inside the margin.
    const calibrationVerifyOutsideTolerance = computed(() =>
      !!(state.calibration || {}).verify_outside_tolerance);
    const calibrationResultCase = computed(() => {
      const c = state.calibration || {};
      if (!['complete', 'verified'].includes(c.state)) return '';
      if (c.state === 'complete') return 'measured';
      if (calibrationNothingToSave.value) return 'passed';
      return c.verify_remeasured_mm || c.verify_hub_extra_mm
        ? 'changed' : 'measured';
    });
    const calibrationTipPositionMm = computed(() => {
      if (!calibrationRouteKnown.value
          && !CAL_MEASURING.includes(state.calibration?.state)
          && !calibrationRouteOccupied.value) return null;
      const c = state.calibration || {};
      if (c.tip_position_mm != null && Number.isFinite(Number(c.tip_position_mm))) {
        return Math.max(0, Number(c.tip_position_mm));
      }
      const load = calibrationRouteAnchors.value.load;
      if (calibrationSelectedToolhead.value?.filament_at_extruder === true) return load;
      if (['complete', 'verified'].includes(c.state)) return 0;
      if (calibrationSelectedSlot.value?.state === 'ready'
          && calibrationSelectedToolhead.value?.filament_at_extruder === false) return 0;
      return null;
    });
    // Where the marker is DRAWN. A position taken from the lane encoder is
    // drawn as is. A time estimate runs ahead of the filament (the ACE
    // starts late), so while moving the marker is held back by a margin and
    // does not show the hub as passed before the filament is there.
    const CALIBRATION_ESTIMATE_MARGIN_MM = 75;
    const calibrationTipEstimated = computed(() =>
      calibrationMoving.value
        && state.calibration?.position_source !== 'encoder');
    const calibrationTipDrawnMm = computed(() => {
      const position = calibrationTipPositionMm.value;
      if (position == null || !calibrationTipEstimated.value) return position;
      const feeding = ['feeding', 'feed_jogging', 'verifying_feed']
        .includes(state.calibration?.state);
      const load = calibrationRouteAnchors.value.load;
      return feeding
        ? Math.max(0, position - CALIBRATION_ESTIMATE_MARGIN_MM)
        : Math.min(Number.isFinite(load) ? load : position,
                   position + CALIBRATION_ESTIMATE_MARGIN_MM);
    });
    // Hub position on the route, in mm from the park position (null while
    // it is not known yet, e.g. during the first measurement).
    const calibrationHubMm = computed(() => {
      const {load, swap} = calibrationRouteAnchors.value;
      return Number.isFinite(load) && Number.isFinite(swap) && swap > 0 && swap < load
        ? load - swap : null;
    });
    const calibrationRouteLengthMm = computed(() => {
      const c = state.calibration || {};
      const load = calibrationRouteAnchors.value.load;
      if (Number.isFinite(load) && load > 0) return load;
      return Number(c.max_feed_mm) > 0 ? Number(c.max_feed_mm) : null;
    });
    const calibrationTipSegment = computed(() => {
      const position = calibrationTipDrawnMm.value;
      const hub = calibrationHubMm.value;
      if (position == null) return 'ace';
      // Hub not measured yet: the two tubes are just the two halves of
      // the route, so the marker still ends at the toolhead.
      if (hub == null) {
        const total = calibrationRouteLengthMm.value;
        return total > 0 && position / total > 0.5 ? 'toolhead' : 'ace';
      }
      return position <= hub ? 'ace' : 'toolhead';
    });
    const calibrationTipSegmentPercent = computed(() => {
      const position = calibrationTipDrawnMm.value;
      if (position == null) return 50;
      const hub = calibrationHubMm.value;
      const total = calibrationRouteLengthMm.value;
      let percent = 0;
      if (hub == null) {
        const fraction = total > 0 ? position / total : 0;
        percent = fraction <= 0.5 ? 200 * fraction : 200 * (fraction - 0.5);
      } else if (position <= hub) {
        percent = hub > 0 ? 100 * position / hub : 0;
      } else {
        percent = total - hub > 0 ? 100 * (position - hub) / (total - hub) : 0;
      }
      return Math.max(0, Math.min(100, percent));
    });
    // Tube columns sized from the real distances, so the marker moves at
    // one speed along the whole route.
    const calibrationRouteGrid = computed(() => {
      const hub = calibrationHubMm.value;
      const total = calibrationRouteLengthMm.value;
      if (hub == null || !(total > hub)) return 'auto 1fr auto 1fr auto';
      return `auto minmax(56px, ${Math.round(hub)}fr) auto `
        + `minmax(56px, ${Math.round(total - hub)}fr) auto`;
    });
    const calibrationTipLabel = computed(() => {
      const c = state.calibration || {};
      const position = calibrationTipPositionMm.value;
      if (position == null) {
        return calibrationRouteKnown.value
          ? t('ui.calibration.tip_pos_na') : t('ui.calibration.tip_no_anchors');
      }
      const mm = Math.round(position);
      if (c.state === 'homing') return t('ui.calibration.tip_homing', {mm});
      if (calibrationMoving.value) {
        return t(calibrationTipEstimated.value
          ? 'ui.calibration.tip_moving' : 'ui.calibration.tip_moving_enc', {mm});
      }
      if (calibrationRecovery.value) return t('ui.calibration.tip_stopped', {mm});
      if (['at_sensor', 'verify_toolhead'].includes(c.state)) return t('ui.calibration.tip_at_head');
      if (c.state === 'verify_splitter') return t('ui.calibration.tip_at_splitter');
      if (mm <= 0) return t('ui.calibration.tip_parked');
      return t('ui.calibration.tip_at', {mm});
    });

    watch(() => state.calibration?.state, (next, previous) => {
      if (!['verify_splitter', 'verify_splitter_adjusting'].includes(next)) {
        calibrationUi.hubAdjust = false;
      }
      if (CAL_VERIFYING.includes(next)) {
        calibrationSetPanel('verify', false);
      } else if (CAL_MEASURING.includes(next)) {
        calibrationSetPanel('calibrate', false);
      } else if (next === 'verified' && previous && previous !== 'verified') {
        calibrationSetPanel('results', false);
      } else if (next === 'complete' && previous === 'returning') {
        calibrationSetPanel('results', false);
      }
    });

    function calibrationSetPanel(panel, touched=true) {
      if (!['setup', 'calibrate', 'verify', 'results'].includes(panel)) return;
      calibrationUi.panel = panel;
      if (touched) calibrationUi.panelTouched = true;
    }
    function calibrationContinueRoute() {
      calibrationSetPanel(calibrationRouteKnown.value ? 'verify' : 'calibrate');
    }
    function calibrationBeginRecalibration() {
      calibrationUi.recalibrate = true;
      calibrationUi.error = '';
      calibrationSetPanel('calibrate');
    }
    function calibrationKeepSaved() {
      calibrationUi.recalibrate = false;
      calibrationUi.error = '';
    }
    // Where the result is saved - decided on the Results panel, at the
    // moment of saving, not before anything has been measured.
    function calibrationSetScope(scope) {
      if (calibrationUi.busy || !['ace', 'slot'].includes(scope)) return;
      calibrationUi.scope = scope;
    }

    function syncCalibrationDefaults() {
      if (calibrationRouteLocked.value) {
        const c = state.calibration || {};
        calibrationUi.ace = Number(c.ace);
        calibrationUi.slot = Number(c.slot);
        calibrationUi.head = Number(c.head);
        return;
      }
      const connected = state.aces.filter(ace => ace?.connected);
      if (!connected.length) return;
      let selected = connected.find(
        ace => Number(ace.idx) === Number(calibrationUi.ace));
      if (!selected || (connected.length === 1 && !calibrationSelectionTouched.ace)) {
        selected = connected[0];
        calibrationUi.ace = Number(selected.idx);
      }
      if (!calibrationSelectionTouched.slot) {
        const ready = (selected.slots || []).find(slot => slot.state === 'ready');
        if (ready) calibrationUi.slot = Number(ready.idx);
      }
      if (state.mode === 'multi') {
        calibrationUi.head = Number(calibrationUi.slot);
        return;
      }
      if (connected.length !== 1) return;
      const mappedHeads = (state.ace_heads || []).filter(head =>
        Number(state.head_ace?.[String(head)] ?? state.head_ace?.[head] ?? head)
          === Number(selected.idx));
      const mapped = mappedHeads.find(head => {
        const toolhead = state.toolheads.find(
          item => Number(item.idx) === Number(head));
        return toolhead && !toolhead.manual && !toolhead.feeder;
      }) ?? mappedHeads[0];
      if (mapped != null && (!calibrationSelectionTouched.head
          || !mappedHeads.some(head => Number(head) === Number(calibrationUi.head)))) {
        calibrationUi.head = Number(mapped);
      }
    }
    // Unload the selected toolhead so the measurement starts from a clear
    // sensor. Has its own out-of-band cancel (calibrationCancelPrepare).
    async function calibrationPrepare() {
      calibrationUi.busy = 'prepare';
      calibrationUi.cancelRequested = false;
      calibrationUi.error = '';
      try {
        if (calibrationSelectedHeadClear.value) return;
        const ok = await run('ACE_UNLOAD_HEAD', {
          HEAD: Number(calibrationUi.head),
          ACE: Number(calibrationUi.ace),
          SLOT: Number(calibrationUi.slot),
          CALIBRATION_PREPARE: 1,
        });
        await reloadState();
        if (calibrationUi.cancelRequested) {
          throw new Error(
            t('ui.calibration.err_prep_cancelled'));
        }
        if (!ok || !calibrationSelectedHeadClear.value) {
          throw new Error(
            t('ui.calibration.err_prep_unclear'));
        }
      } catch (e) {
        calibrationUi.error = String(e?.message || e);
      } finally {
        calibrationUi.busy = '';
        calibrationUi.cancelRequested = false;
      }
    }
    async function calibrationCancelPrepare() {
      if (calibrationUi.busy !== 'prepare' || calibrationUi.cancelRequested) return;
      calibrationUi.cancelRequested = true;
      calibrationUi.error = '';
      try {
        const r = await fetch(`${API}/calibration/unload-cancel`, {method: 'POST'});
        let j = null;
        try { j = await r.json(); } catch (_) {}
        if (!r.ok) throw new Error(j?.detail || `HTTP ${r.status}`);
      } catch (e) {
        calibrationUi.cancelRequested = false;
        calibrationUi.error = String(e?.message || e);
      }
    }

    function _calibrationRouteReset() {
      calibrationUi.error = '';
      calibrationUi.saved = false;
      calibrationUi.recalibrate = false;
    }
    function calibrationSelectAce(idx) {
      if (calibrationRouteLocked.value || calibrationUi.busy) return;
      calibrationSelectionTouched.ace = true;
      calibrationUi.ace = Number(idx);
      if (state.mode === 'head') {
        const h = (state.ace_heads || []).find(
          head => Number(state.head_ace?.[String(head)] ?? state.head_ace?.[head] ?? head)
            === Number(idx));
        if (h != null) calibrationUi.head = Number(h);
      }
      _calibrationRouteReset();
    }
    function calibrationSelectSlot(idx) {
      if (calibrationRouteLocked.value || calibrationUi.busy) return;
      calibrationSelectionTouched.slot = true;
      calibrationUi.slot = Number(idx);
      if (state.mode === 'multi') calibrationUi.head = Number(idx);
      _calibrationRouteReset();
    }
    function calibrationSelectHead(idx) {
      if (calibrationRouteLocked.value || calibrationUi.busy) return;
      calibrationSelectionTouched.head = true;
      calibrationUi.head = Number(idx);
      _calibrationRouteReset();
    }
    async function calibrationRequest(path, body, busy) {
      calibrationUi.busy = busy;
      calibrationUi.error = '';
      try {
        const r = await fetch(`${API}/calibration/${path}`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body || {}),
        });
        let j = null;
        try { j = await r.json(); } catch (_) {}
        if (!r.ok) throw new Error(j?.detail || `HTTP ${r.status}`);
        await reloadState();
        return true;
      } catch (e) {
        let message = String(e?.message || e);
        if (body?.action === 'verify_start'
            && /complete calibration before verification/i.test(message)) {
          message = t('ui.calibration.err_stale_klipper');
        }
        calibrationUi.error = message;
        return false;
      } finally {
        calibrationUi.busy = '';
      }
    }
    // Poll until the session state satisfies `done` (a stop is booked a
    // moment after the command returns, once the lane has come to rest).
    async function _calibrationWaitState(done, timeoutMs=4000) {
      const until = Date.now() + timeoutMs;
      while (!done(state.calibration?.state) && Date.now() < until) {
        await new Promise(resolve => setTimeout(resolve, 150));
        await reloadState();
      }
      return done(state.calibration?.state);
    }
    async function calibrationStart() {
      calibrationUi.saved = false;
      calibrationSetPanel('calibrate', false);
      const ok = await calibrationRequest('start', {
        ace: Number(calibrationUi.ace),
        slot: Number(calibrationUi.slot),
        head: Number(calibrationUi.head),
        scope: 'ace',
      }, 'start');
      if (ok) await calibrationAction('feed');
    }
    // What a verification tests: the values Save would write (margins
    // included) when this route has a result in the session, otherwise the
    // saved ones from the config form - which is also what makes a just
    // saved route verifiable before the printer has restarted.
    const calibrationVerifyValues = computed(() => {
      const c = state.calibration || {};
      if (['complete', 'verified'].includes(c.state)
          && calibrationSessionMatchesSelection.value) {
        const p = calibrationProposed.value;
        if (p.load != null && p.swap != null && p.retract != null) {
          return {load_budget: Math.max(p.load, p.retract), swap: p.swap,
                  retract: p.retract};
        }
      }
      const cfg = calibrationConfiguredRoute.value;
      if (!(cfg.load > 0 && cfg.swap > 0)) return null;
      return {load_budget: Math.max(Number(cfg.feedLimit) || 0, cfg.load),
              swap: cfg.swap, retract: cfg.load};
    });
    async function calibrationVerifyConfigured() {
      if (!calibrationCanVerifyConfigured.value) return false;
      // A verify run can re-measure the toolhead distance, so its result
      // has to be applicable again.
      calibrationUi.saved = false;
      calibrationSetPanel('verify', false);
      // Where the result gets saved is a UI choice made at save time; the
      // printer must not read a different choice as a different route.
      const sessionScope = calibrationSessionMatchesSelection.value
        ? (state.calibration?.scope || 'ace') : 'ace';
      return calibrationRequest('action', {
        action: 'verify_start',
        session_id: Number(state.calibration?.session_id || 0),
        ace: Number(calibrationUi.ace),
        slot: Number(calibrationUi.slot),
        head: Number(calibrationUi.head),
        scope: sessionScope,
        ...(calibrationVerifyValues.value || {}),
      }, 'verify_start');
    }
    async function calibrationAction(action, length=null) {
      return calibrationRequest('action', {
        action, length,
        session_id: Number(state.calibration?.session_id || 0),
      }, action);
    }
    // Hub mark and return in one go - after marking there is nothing else
    // the user could want to do.
    async function calibrationMarkAndReturn() {
      if (await calibrationAction('mark_swap')) {
        await calibrationAction('return_park');
      }
    }
    async function calibrationUndoMark() {
      if (state.calibration?.state === 'returning') {
        if (!await calibrationAction('stop')) return;
        await _calibrationWaitState(s => s !== 'returning');
      }
      await calibrationAction('mark_undo');
    }
    // Drag handle on the "clear the hub" step: nothing moves while
    // dragging; on release ONE bounded move covers the difference (a
    // pull-back never past the park position, a forward move never past the
    // toolhead-sensor start). Below 5 mm nothing is sent.
    const CALIBRATION_MIN_MOVE_MM = 5;
    // `moving` is the move a release or a step button started: its target,
    // length and direction, shown until the ACE has stopped.
    const calibrationDrag = reactive({active: false, targetMm: null, moving: null});
    const CAL_TRACK_MOVES = ['retracting', 'feed_jogging'];
    const calibrationTipNowMm = computed(() => {
      const c = state.calibration || {};
      // while a move runs the session reports the tip live
      if (CAL_TRACK_MOVES.includes(c.state) && c.tip_position_mm != null) {
        return Math.max(0, Number(c.tip_position_mm));
      }
      return Math.max(0, Number(c.load_length_mm || 0)
        - Number(c.commanded_retract_mm || 0));
    });
    // The saved hub clearance point as a reference mark, if there is one.
    // Only a hub clearance really saved for this route is drawn; the global
    // default says nothing about where this route's hub is.
    const calibrationTrackHubMm = computed(() => {
      if (!calibrationRouteCalibrated.value) return null;
      const load = Number(state.calibration?.load_length_mm || 0);
      const swap = calibrationConfiguredRoute.value.swap;
      return load > 0 && swap > 0 && swap < load ? load - swap : null;
    });
    function calibrationTrackPercent(mm) {
      const load = Number(state.calibration?.load_length_mm || 0);
      return load > 0 ? Math.max(0, Math.min(100, 100 * Number(mm) / load)) : 0;
    }
    const calibrationDragLabel = computed(() => {
      const moving = calibrationDrag.moving;
      if (moving && calibrationDrag.targetMm == null) {
        return t('ui.calibration.drag_moving', {
          mm: moving.mm, dir: t(moving.dir),
          left: Math.round(Math.abs(moving.target - calibrationTipNowMm.value)),
        });
      }
      if (calibrationDrag.targetMm == null) return '';
      const diff = calibrationDrag.targetMm - calibrationTipNowMm.value;
      if (Math.abs(diff) < CALIBRATION_MIN_MOVE_MM) return t('ui.calibration.drag_small');
      return t('ui.calibration.drag_release', {
        mm: Math.abs(Math.round(diff)),
        dir: t(diff < 0 ? 'ui.calibration.dir_ace' : 'ui.calibration.dir_head'),
      });
    });
    function _calibrationTrackMm(evt) {
      const rect = evt.currentTarget.getBoundingClientRect();
      const load = Number(state.calibration?.load_length_mm || 0);
      const fraction = rect.width > 0 ? (evt.clientX - rect.left) / rect.width : 0;
      return Math.round(Math.max(0, Math.min(1, fraction)) * load);
    }
    function _calibrationCanDrag() {
      return ['at_sensor', 'retract_ready'].includes(state.calibration?.state)
        && !calibrationMoving.value && !calibrationUi.busy;
    }
    function calibrationDragStart(evt) {
      if (!_calibrationCanDrag()) return;
      evt.preventDefault();
      try { evt.currentTarget.setPointerCapture(evt.pointerId); } catch (_) {}
      calibrationDrag.active = true;
      calibrationDrag.targetMm = _calibrationTrackMm(evt);
    }
    function calibrationDragMove(evt) {
      if (calibrationDrag.active) calibrationDrag.targetMm = _calibrationTrackMm(evt);
    }
    function calibrationDragCancel() {
      calibrationDrag.active = false;
      calibrationDrag.targetMm = null;
    }
    async function calibrationDragEnd() {
      if (!calibrationDrag.active) return;
      const target = calibrationDrag.targetMm;
      calibrationDragCancel();
      if (target != null) await calibrationMoveTo(target);
    }
    async function calibrationMoveTo(targetMm) {
      const c = state.calibration || {};
      const load = Number(c.load_length_mm || 0);
      const retract = Number(c.commanded_retract_mm || 0);
      const target = Math.max(0, Math.min(load, Math.round(targetMm)));
      const diff = target - (load - retract);
      if (Math.abs(diff) < CALIBRATION_MIN_MOVE_MM) return false;
      const mm = diff < 0 ? -diff : Math.min(diff, retract);
      calibrationDrag.moving = {
        target: (load - retract) + Math.sign(diff) * mm, mm,
        dir: diff < 0 ? 'ui.calibration.dir_ace' : 'ui.calibration.dir_head',
      };
      const ok = await calibrationAction(diff < 0 ? 'retract' : 'feed_jog', mm);
      if (!ok || !CAL_TRACK_MOVES.includes(state.calibration?.state)) {
        calibrationDrag.moving = null;
      }
      return ok;
    }
    // The step buttons go through the same path, so they get the same
    // "moving" label.
    function calibrationJog(mm) {
      return calibrationMoveTo(calibrationTipNowMm.value + mm);
    }
    watch(() => state.calibration?.state, (s) => {
      if (calibrationDrag.moving && !CAL_TRACK_MOVES.includes(s)) {
        calibrationDrag.moving = null;
      }
    });
    async function calibrationApply() {
      const c = state.calibration || {};
      const v = calibrationProposed.value;
      calibrationUi.error = '';
      if (!['complete', 'verified'].includes(c.state)
          || v.load == null || v.swap == null
          || v.retract == null) {
        calibrationUi.error = t('ui.calibration.err_incomplete');
        return;
      }
      if (v.retract < v.swap) {
        calibrationUi.error =
          t('ui.calibration.err_retract_small');
        return;
      }
      const ace = Number(c.ace);
      const slot = Number(c.slot);
      if (configForm.ace_device_count <= ace) {
        configForm.ace_device_count = ace + 1;
      }
      _ensurePerAceLength();
      const target = configForm.perAce[ace];
      if (!target) {
        calibrationUi.error = t('ui.calibration.err_no_section');
        return;
      }
      if (calibrationUi.scope === 'slot') {
        target.perSlot[slot].load_length = v.load;
        target.perSlot[slot].swap_retract_length = v.swap;
        target.perSlot[slot].retract_length = v.retract;
      } else {
        target.load_length = v.load;
        target.swap_retract_length = v.swap;
        target.retract_length = v.retract;
      }
      calibrationUi.busy = 'apply';
      await saveConfigForm();
      calibrationUi.busy = '';
      // saveConfigForm reports through configLog: a line starting with the
      // check mark is the only success it writes. It overwrites the log
      // first, so an earlier save's identical line cannot pass for this one.
      calibrationUi.saved = String(configLog.value || '').startsWith('✓');
      if (!calibrationUi.saved) {
        calibrationUi.error = configLog.value || t('ui.calibration.err_save');
      } else {
        _calibrationClearBarClose();
      }
    }

    // After saving: on to the next slot or ACE. The saved values reach
    // loads and swaps with the next printer restart, which can wait until
    // everything is calibrated; verification already uses them (it sends
    // the values from the page).
    async function calibrationNextRoute() {
      await calibrationAction('reset');
      calibrationUi.saved = false;
      calibrationUi.recalibrate = false;
      calibrationSetPanel('setup');
    }

    // Clearing saved values: the three keys go blank at the level Save
    // writes (this slot's own values or the ACE's shared ones), through the
    // same config save. No G-code; like Save it takes effect with the next
    // printer restart. Undo writes the old values back to that same level.
    const CAL_CLEAR_KEYS = [
      {key: 'load_length', label: 'row_load'},
      {key: 'swap_retract_length', label: 'm_splitter'},
      {key: 'retract_length', label: 'row_retract'},
    ];
    const calibrationClear = reactive({
      open: false, undo: null, restored: false, sha1: '',
    });
    let _calibrationRestoredTimer = null;
    // Where the selected slot gets its values: own, the ACE's shared, none.
    const calibrationSavedKind = computed(() => {
      const aceCfg = configForm.perAce?.[Number(calibrationUi.ace)] || {};
      if (calibrationOwnValueSlots.value.includes(Number(calibrationUi.slot))) return 'own';
      return _calSavedAt(aceCfg, 'retract_length') ? 'shared' : 'none';
    });
    // The three values at each level for the selected route, the level
    // each one is taken from marked as in use.
    const calibrationSavedLevels = computed(() => {
      const ace = Number(calibrationUi.ace);
      const slot = Number(calibrationUi.slot);
      const aceCfg = configForm.perAce?.[ace] || {};
      const slotCfg = aceCfg.perSlot?.[slot] || {};
      const levels = [
        {src: 'slot', cfg: slotCfg,
         name: t('ui.calibration.values_level_slot', {slot: dispIdx(slot)})},
        {src: 'ace', cfg: aceCfg,
         name: t('ui.calibration.values_level_ace', {ace: dispIdx(ace)})},
        {src: 'default', cfg: configForm,
         name: t('ui.calibration.values_level_default')},
      ];
      const inUse = CAL_CLEAR_KEYS.map(({key}) =>
        levels.find(level => _calSavedAt(level.cfg, key))?.src);
      return levels.map(level => ({
        src: level.src, name: level.name,
        values: CAL_CLEAR_KEYS.map(({key}) =>
          _calSavedAt(level.cfg, key) ? Number(level.cfg[key]) : null),
        set: CAL_CLEAR_KEYS.some(({key}) => _calSavedAt(level.cfg, key)),
        inUse: inUse.includes(level.src),
      }));
    });
    function _calibrationSlotList(slots) {
      const names = slots.map(slot => dispIdx(slot));
      if (names.length < 2) return names.join('');
      return t('ui.calibration.clear_list_and', {
        rest: names.slice(0, -1).join(', '), last: names[names.length - 1]});
    }
    // What a clear at the current scope removes, and what each key falls
    // back to afterwards. Calibrated or not follows calibrationRouteCalibrated.
    const calibrationClearPlan = computed(() => {
      const ace = Number(calibrationUi.ace);
      const slot = Number(calibrationUi.slot);
      const level = calibrationUi.scope === 'slot' ? 'slot' : 'ace';
      const aceCfg = configForm.perAce?.[ace] || {};
      const slotCfg = aceCfg.perSlot?.[slot] || {};
      const levelCfg = level === 'slot' ? slotCfg : aceCfg;
      const rows = CAL_CLEAR_KEYS.map(({key, label}) => {
        const fromAce = level === 'slot' && _calSavedAt(aceCfg, key);
        const after = fromAce ? Number(aceCfg[key])
          : _calSavedAt(configForm, key) ? Number(configForm[key]) : null;
        return {
          key, label, config: level === 'slot' ? `${key}_${slot}` : key,
          old: _calSavedAt(levelCfg, key) ? Number(levelCfg[key]) : null,
          after, src: fromAce ? 'ace' : 'default',
        };
      });
      const calibrated = (own, shared) => ['retract_length', 'swap_retract_length']
        .every(key => _calSavedAt(own, key) || _calSavedAt(shared, key));
      const affected = [], keeps = [];
      if (level === 'ace') {
        (aceCfg.perSlot || []).forEach((own, idx) => {
          if (calibrated(own, aceCfg) && !calibrated(own, null)) affected.push(idx);
          else if (calibrationOwnValueSlots.value.includes(idx)) keeps.push(idx);
        });
      }
      return {
        level, ace, slot, rows,
        saved: rows.some(row => row.old != null),
        staysCalibrated: level === 'slot' && calibrated(null, aceCfg),
        affected, keeps,
      };
    });
    // Why Clear cannot run right now (null = it can). `blocked` marks the
    // reasons that are a temporary refusal rather than nothing to clear.
    const calibrationClearBlocked = computed(() => {
      const plan = calibrationClearPlan.value;
      if (calibrationMoving.value) {
        return {blocked: true, why: t('ui.calibration.clear_blk_moving', {
          ace: dispIdx(state.calibration?.ace ?? plan.ace)})};
      }
      if (!plan.saved) {
        return {blocked: false, why: plan.level === 'slot'
          ? t('ui.calibration.clear_blk_none_slot')
          : t('ui.calibration.clear_blk_none_ace', {ace: dispIdx(plan.ace)})};
      }
      return null;
    });
    // The open run of this route goes with the values it was measured
    // against. Filament still out of the ACE: the run is cancelled instead,
    // which keeps the tip position, so the return to the ACE stays offered.
    const calibrationClearSession = computed(() => {
      const c = state.calibration || {};
      if (!calibrationSessionMatchesSelection.value || calibrationMoving.value
          || (c.state || 'idle') === 'idle') return null;
      if (calibrationRouteOccupied.value) {
        return {
          action: ['cancelled', 'failed'].includes(c.state) ? '' : 'cancel',
          mm: Math.round(Number(c.tip_position_mm) || 0),
        };
      }
      return {action: 'reset', mm: 0};
    });
    const calibrationClearLead = computed(() => {
      const plan = calibrationClearPlan.value;
      if (plan.level === 'slot') {
        return plan.staysCalibrated
          ? t('ui.calibration.clear_falls_ace', {ace: dispIdx(plan.ace)})
          : t('ui.calibration.clear_falls_default');
      }
      if (!plan.affected.length) {
        return t('ui.calibration.clear_affected_none', {ace: dispIdx(plan.ace)});
      }
      return t(plan.affected.length === 1
        ? 'ui.calibration.clear_affected_one' : 'ui.calibration.clear_affected_many',
      {slots: _calibrationSlotList(plan.affected)});
    });
    function calibrationClearOpen() {
      if (calibrationClearBlocked.value || calibrationUi.busy) return;
      calibrationClear.open = true;
    }
    function _calibrationClearBarClose() {
      calibrationClear.undo = null;
      calibrationClear.restored = false;
      clearTimeout(_calibrationRestoredTimer);
    }
    // Writes the three keys at one level and saves the config the way Save
    // does. On a failed save the form gets its previous values back.
    async function _calibrationWriteLevel(level, ace, slot, values, busy) {
      const aceCfg = configForm.perAce?.[ace];
      const target = level === 'slot' ? aceCfg?.perSlot?.[slot] : aceCfg;
      if (!target) {
        calibrationUi.error = t('ui.calibration.err_no_section');
        return false;
      }
      const previous = {};
      for (const {key} of CAL_CLEAR_KEYS) {
        previous[key] = target[key];
        target[key] = values[key];
      }
      calibrationUi.busy = busy;
      calibrationUi.error = '';
      await saveConfigForm();
      calibrationUi.busy = '';
      const ok = String(configLog.value || '').startsWith('✓');
      if (!ok) {
        Object.assign(target, previous);
        calibrationUi.error = configLog.value || t('ui.calibration.err_save');
      }
      return ok;
    }
    async function calibrationClearConfirm() {
      const plan = calibrationClearPlan.value;
      const session = calibrationClearSession.value;
      if (calibrationClearBlocked.value || calibrationUi.busy) return;
      calibrationClear.open = false;
      const old = {};
      for (const row of plan.rows) old[row.key] = row.old == null ? '' : row.old;
      const blank = {load_length: '', swap_retract_length: '', retract_length: ''};
      if (!await _calibrationWriteLevel(plan.level, plan.ace, plan.slot, blank, 'clear')) return;
      _calibrationClearBarClose();
      calibrationClear.undo = {level: plan.level, ace: plan.ace, slot: plan.slot, values: old};
      calibrationClear.sha1 = config.sha1;
      calibrationUi.saved = false;
      if (session?.action) await calibrationAction(session.action);
    }
    // Config only: a run reset by the clear stays gone.
    async function calibrationClearUndo() {
      const undo = calibrationClear.undo;
      if (!undo || calibrationUi.busy) return;
      if (!await _calibrationWriteLevel(undo.level, undo.ace, undo.slot, undo.values, 'undo')) return;
      _calibrationClearBarClose();
      calibrationClear.restored = true;
      calibrationClear.sha1 = config.sha1;
      _calibrationRestoredTimer = setTimeout(() => { calibrationClear.restored = false; }, 6000);
    }

    function clearAllErrors() {
      cmdQueue.value = cmdQueue.value.filter(it => it.status !== 'error');
      cmdPaused.value = false;
      if (notifications.value.length) {
        dismissAllNotifications();
      }
      _scheduleAdvance();
    }
    const sendingAll = ref(false);
    async function sendAllToPrinter() {
      // .reverse() is load-bearing: new items are unshifted to the FRONT and
      // the single-command path walks the array from the END, so the array
      // order is newest-first while the queue itself runs oldest-first.
      // Handing the filtered array to the batch as-is sent the script
      // BACKWARDS - a Load on an occupied head became load-then-unload,
      // i.e. it ended up empty. filter() already returns a copy, so this
      // does not touch the queue.
      const items = cmdQueue.value.filter(it => it.status === 'queued').reverse();
      if (!items.length) return;
      const commands = items.map(it => ({name: it.cmd, args: it.args || {}}));
      sendingAll.value = true;
      try {
        const r = await fetch(`${API}/macro-batch`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({commands}),
        });
        if (!r.ok) {
          let msg = `${r.status} ${r.statusText}`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        for (const it of items) {
          const idx = cmdQueue.value.indexOf(it);
          if (idx >= 0) cmdQueue.value.splice(idx, 1);
          if (it._resolve) it._resolve(true);
        }
        setMacroLog(t("ui.queue.send_all_done", {count: commands.length}));
      } catch (e) {
        setMacroLog(`${t("ui.queue.send_all_failed")}: ${e.message || e}`);
        confirm({
          title: t("ui.queue.send_all_failed"),
          message: escapeHtml(e.message || e),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
      } finally {
        sendingAll.value = false;
      }
    }
    function fmtArgs(args) {
      if (!args) return "";
      const parts = [];
      for (const [k, v] of Object.entries(args)) {
        const s = String(v);
        parts.push(`${k}=${s.length > 12 ? s.slice(0, 12) + '…' : s}`);
      }
      return parts.join(' ');
    }
    function cmdLabel(it) {
      const a = it.args || {};
      const di = (n) => dispIdx(Number(n));
      switch (it.cmd) {
        case 'SET_PRINT_FILAMENT_CONFIG':
          return t('ui.queue.lbl_display', {head: di(a.CONFIG_EXTRUDER ?? 0)});
        case 'ACE_LOAD_HEAD':
          return t('ui.queue.lbl_load', {head: di(a.HEAD ?? 0), ace: di(a.ACE ?? 0)});
        case 'ACE_SWAP_HEAD':
          return t('ui.queue.lbl_swap', {head: di(a.HEAD ?? 0), ace: di(a.ACE ?? 0)});
        case 'ACE_UNLOAD_HEAD':
          return t('ui.queue.lbl_unload', {head: di(a.HEAD ?? 0)});
        case 'ACE_UNLOAD_ALL_HEADS':
          return t('ui.queue.lbl_unload_all');
        case 'ACE_SWITCH':
          return `MMU ${di(a.TARGET ?? 0)}`
            + ((a.AUTOLOAD == 1 || a.AUTOLOAD === true)
               ? ' ' + t('ui.queue.lbl_autoload') : '');
        case 'ACE_DRY':
          return t('ui.queue.lbl_dry', {ace: di(a.ACE ?? 0), temp: a.TEMP, min: a.DURATION});
        case 'ACE_STOP_DRYING':
          return t('ui.queue.lbl_stop_dry', {ace: di(a.ACE ?? 0)});
      }
      return null;
    }
    function slotTitle(ace, slot) {
      const bits = [`MMU ${dispIdx(ace.idx)} / Slot ${dispIdx(slot.idx)}`];
      if (slot.material) bits.push(slot.material);
      if (slot.brand) bits.push(slot.brand);
      bits.push(slot.state);
      if (slot.color) bits.push(slot.color);
      return bits.join(" · ");
    }
    const wiringContainerEl = ref(null);
    const slotEls = {};
    const thEls = {};
    const layoutTick = ref(0);
    function setSlotEl(ace, slot, el) {
      const k = `${ace}_${slot}`;
      if (el) slotEls[k] = el; else delete slotEls[k];
    }
    function setThEl(idx, el) {
      if (el) thEls[idx] = el; else delete thEls[idx];
    }
    const wiringPaths = ref([]);
    const wiringViewBox = ref("0 0 100 100");
    function recomputeWiring() {
      const c = wiringContainerEl.value;
      if (!c) { wiringPaths.value = []; return; }
      const cb = c.getBoundingClientRect();
      wiringViewBox.value = `0 0 ${cb.width} ${cb.height}`;
      const lines = [];
      const square = uiTheme.value === "new";
      for (const w of state.wiring) {
        const sEl = slotEls[`${w.ace}_${w.slot}`];
        const tEl = thEls[w.toolhead];
        if (!sEl || !tEl) continue;
        const sb = sEl.getBoundingClientRect();
        const tb = tEl.getBoundingClientRect();
        const x1 = sb.left + sb.width / 2 - cb.left;
        const y1 = sb.bottom - cb.top;
        const x2 = tb.left + tb.width / 2 - cb.left;
        const y2 = tb.top - cb.top;
        if (!square) {
          const midY = (y1 + y2) / 2;
          lines.push({
            d: `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`,
            color: w.color || "#888", width: 4,
          });
          continue;
        }
        // New design: right-angled, the horizontal run in the gap between
        // the ACE cards and the toolhead block (the cards hide the rest).
        const aceEl = sEl.closest(".ace-grid");
        const headEl = c.querySelector(".th-section-head") || tEl;
        const bandTop = (aceEl ? aceEl.getBoundingClientRect().bottom : y1) - cb.top;
        const bandBot = headEl.getBoundingClientRect().top - cb.top;
        lines.push({x1, y1, x2, y2, bandTop, bandBot,
                    color: w.color || "#888", width: 2});
      }
      if (square) {
        // Stagger the horizontal runs inside the band so lines that cross
        // the same stretch stay apart.
        const sq = lines.filter((l) => l.d === undefined)
          .sort((a, b) => a.x2 - b.x2);
        sq.forEach((l, i) => {
          const h = Math.max(0, l.bandBot - l.bandTop);
          const y = l.bandTop + h * (i + 1) / (sq.length + 1);
          l.d = `M${l.x1},${l.y1} V${y} H${l.x2} V${l.y2}`;
        });
      }
      wiringPaths.value = lines;
    }
    function scheduleWiringRecompute() {
      nextTick(() => {
        recomputeWiring();
        requestAnimationFrame(recomputeWiring);
      });
    }
    // Resume the queue automatically the moment Klipper's swap flag
    // flips back to false. Without this the queue would only advance
    // on the next user action.
    watch(() => state.swap_in_progress, (v) => { if (!v) _scheduleAdvance(); });

    watch(() => state.wiring, scheduleWiringRecompute, {deep: true});
    watch(() => state.aces.length, scheduleWiringRecompute);
    watch(() => state.toolheads.length, scheduleWiringRecompute);
    watch(() => tab.value, (v) => { if (v === "dashboard") scheduleWiringRecompute(); });
    function switchAce(idx) {
      run("ACE_SWITCH", {TARGET: idx});
    }
    // --- Panel mode (?panel=1) -------------------------------------------
    // Compact embeddable view, for Fluidd's "HTTP Page" camera type. A MODE
    // of this app, not a second interface: same state, macros, queue,
    // confirmations (window.confirm works fine inside an iframe) and i18n.
    const _q = new URLSearchParams(location.search);
    const panelMode = _q.get("panel") === "1";
    // Visual design, per browser: "new" (dark), "light" (the same design in
    // light shades) or "classic". Pure presentation - style-new.css only
    // applies under html[data-theme="new"], the light shade additionally
    // under data-shade="light", and the few extra elements the new design
    // needs (spool gauges) render in both of its shades. uiTheme is the
    // LAYOUT ("new" for dark and light alike), uiThemeChoice the button.
    const uiThemeChoice = ref((() => {
      try {
        const v = localStorage.getItem("multiace.uiTheme");
        return v === "classic" || v === "light" ? v : "new";
      } catch (e) { return "new"; }
    })());
    const uiTheme = computed(() => uiThemeChoice.value === "classic" ? "classic" : "new");
    const uiLight = computed(() => uiThemeChoice.value === "light");
    function setUiTheme(v) {
      uiThemeChoice.value = v === "classic" || v === "light" ? v : "new";
      try { localStorage.setItem("multiace.uiTheme", uiThemeChoice.value); } catch (e) {}
    }
    watch(() => uiThemeChoice.value, scheduleWiringRecompute);
    watch(uiThemeChoice, v => {
      const el = document.documentElement;
      if (v === "classic") delete el.dataset.theme;
      else el.dataset.theme = "new";
      if (v === "light") el.dataset.shade = "light";
      else delete el.dataset.shade;
    }, {immediate: true});
    // Spool gauge fill in percent: the bound spool's remaining weight over a
    // 1000 g reference, never below 4 so a nearly empty spool still shows a
    // sliver. null = no weight known (the gauge then shows full, no label).
    function spoolPct(sp) {
      if (!sp || sp.weight_g === undefined || sp.weight_g === null) return null;
      return Math.max(4, Math.min(100, Math.round(Number(sp.weight_g) / 10)));
    }
    // New design: per-head settings (ACE, feeder/manual, BG) fold away
    // behind a gear button on the toolhead card. View state only.
    const thSettingsOpen = reactive({});
    function slotGaugePct(aceIdx, slot) {
      if (!slot || slot.state === "empty") return 0;
      const p = spoolPct(spoolForSlot(aceIdx, slot.idx));
      return p === null ? 100 : p;
    }
    function slotGaugeLabel(aceIdx, slot) {
      if (!slot || slot.state === "empty") return "";
      const p = spoolPct(spoolForSlot(aceIdx, slot.idx));
      return p === null ? "" : p + " %";
    }
    // Toolhead gauge: empty without filament; loaded = the fill level of
    // the spool feeding it (source slot's bound spool, or the head-bound
    // spool for feeder/manual), full when no spool is bound.
    function headGaugePct(th) {
      if (!((th.head_source_known && !th.load_failed) || th.filament_at_extruder)) return 0;
      const sp = (th.ace != null && th.slot != null)
        ? spoolForSlot(th.ace, th.slot) : spoolForHead(th.idx);
      const p = spoolPct(sp);
      return p === null ? 100 : p;
    }
    function headGaugeText(th) {
      if (toolheadOps.value[th.idx]) return "";
      if (th.load_failed) return t("ui.dashboard.load_failed");
      if (th.material) return [th.material, subText(th.subtype)].filter(Boolean).join(" ");
      if (!th.head_source_known && !th.filament_at_extruder && !th.feeder && !th.manual)
        return t("ui.common.empty");
      return "";
    }
    // Relative luminance of a #rrggbb colour, null when it is not one.
    function colorLum(c) {
      const m = /^#?([0-9a-f]{6})/i.exec(String(c || ""));
      if (!m) return null;
      const n = parseInt(m[1], 16);
      const lin = x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
      return 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
    }
    // Very dark filament colours would vanish in the dark gauge. The light
    // shade keeps every colour (white shows against the grey core) and only
    // needs its own grey for "no colour".
    function gaugeColor(c) {
      if (!c) return uiLight.value ? "#b3bcbf" : "#4a4f52";
      const m = /^#?([0-9a-f]{6})/i.exec(String(c));
      if (!m) return c;
      if (uiLight.value) return "#" + m[1];
      return colorLum(c) < 0.03 ? "#4a4f52" : "#" + m[1];
    }
    // The full UI is this same page without ?panel=1. Derived from the
    // current URL rather than hardcoded, so it stays right whether we are
    // served under /multiace/ or off the root in a dev setup. Opened in a
    // new tab on purpose - a plain link would navigate the camera TILE to
    // the full interface, inside a 190px box.
    const fullUiHref = location.pathname;
    // The panel is transparent so the Fluidd camera card paints the
    // background behind it - but opened DIRECTLY there is nothing behind
    // it and transparent falls through to the browser's white. Flag the
    // embedded case instead; the stylesheet keeps the app's own dark
    // background by default, so the standalone view never flashes white.
    if (panelMode) {
      try {
        if (window.self !== window.top) {
          document.body.classList.add("panel-embedded");
        }
      } catch (e) {
        // Cross-origin access to window.top throws - that only happens
        // when we ARE embedded, so treat it as such.
        document.body.classList.add("panel-embedded");
      }
    }
    // Which unit the panel SHOWS. Never ACE_SWITCH - that changes the
    // printer's ACTIVE unit, and looking at another card must not do that.
    // null = follow the active unit until the user picks one.
    const _panelAcePinned = ref((() => {
      const p = parseInt(_q.get("ace"), 10);
      if (!isNaN(p)) return p;                       // ?ace=2 pins it
      const s = localStorage.getItem("multiace.panelAce");
      return s === null ? null : parseInt(s, 10);
    })());
    const panelAceIdx = computed(() => {
      const p = panelPage.value;
      return p && p.kind === 'ace' ? p.ace : null;
    });
    const panelAce = computed(() =>
      (state.aces || []).find(a => a.idx === panelAceIdx.value) || null);
    // Panel PAGES: the tab strip is the aces plus ONE "Feeder" tab when any
    // feeder/manual head exists - "1, 2, Feeder" (one tab per feeder head
    // would clutter the strip the moment two exist). Manual heads ride the
    // same page, and when ONLY manual heads exist the tab says Manual
    // instead.
    const panelFeederHeads = computed(() =>
      (state.toolheads || []).filter(t => t.feeder || t.manual));
    const panelPages = computed(() => {
      const pages = (visibleAces.value || []).map(a =>
        ({id: 'a' + a.idx, kind: 'ace', ace: a.idx,
          label: String(dispIdx(a.idx))}));
      if (panelFeederHeads.value.length) {
        const anyFeeder = panelFeederHeads.value.some(x => x.feeder);
        pages.push({id: 'feeders', kind: 'feeders',
                    label: t(anyFeeder ? 'ui.dashboard.feeder'
                                       : 'ui.dashboard.manual')});
      }
      return pages;
    });
    const panelPageId = computed(() => {
      const pages = panelPages.value;
      let pin = _panelAcePinned.value;
      if (pin !== null && /^\d+$/.test(String(pin))) pin = 'a' + pin;
      if (pin !== null && pages.some(p => p.id === pin)) return pin;
      // Default follows the ACTIVE device, like the old ace-only strip.
      if (state.active_device !== null
          && pages.some(p => p.id === 'a' + state.active_device)) {
        return 'a' + state.active_device;
      }
      return pages.length ? pages[0].id : null;
    });
    const panelPage = computed(() =>
      panelPages.value.find(p => p.id === panelPageId.value) || null);
    function setPanelPage(id) {
      _panelAcePinned.value = id;
      // Remembered per browser on purpose: which card you are looking at is
      // a VIEW preference, unlike confirm_commands (a safety setting, which
      // lives on the printer so phone and desktop cannot disagree).
      try { localStorage.setItem("multiace.panelAce", String(id)); } catch (e) {}
    }
    // Unload is per HEAD. Multi wires slot N to head N; in head mode only
    // the ACE head unloads, and only its own slots offer it.
    function panelSlotHead(aceIdx, slotIdx) {
      if (state.mode === "head") {
        const h = state.ace_head;
        return (headAceOf(h) === aceIdx) ? h : null;
      }
      return slotIdx;
    }
    // Which head this slot currently FEEDS, for the card's "T2" corner.
    // head_source is the truth in both modes: multi wires slot N to head N,
    // but that says nothing about whether it is loaded, and head mode maps
    // freely. null = feeds nothing right now, so the corner stays empty.
    function panelSlotHeadLoaded(aceIdx, slotIdx) {
      const th = (state.toolheads || []).find(
        t_ => t_.head_source_known && t_.ace === aceIdx && t_.slot === slotIdx);
      return th ? th.idx : null;
    }
    // The closest thing we have to "this slot is printing right now": there
    // is no active-extruder field in /api/state, but the ACE reports the slot
    // its feed assist is armed on, and the printing lane keeps FA armed the
    // whole print (CLAUDE.md 12 - the armed slot is the strongest
    // which-slot-feeds-this-head signal). -1 = nothing armed.
    function panelSlotActive(ace, slotIdx) {
      return !!ace && ace.feed_assist === slotIdx;
    }
    // Bottom line of a card. AFC prints the colour name there; our closest
    // equivalent is the spool's own name, else whatever brand the slot
    // reports. Empty string keeps the row (and thus the card height) stable.
    function panelSlotLabel(aceIdx, slotIdx) {
      const sp = spoolForSlot(aceIdx, slotIdx);
      if (sp && sp.label) return sp.label;
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const sl = a && (a.slots || []).find(s => s.idx === slotIdx);
      return (sl && sl.brand) || "";
    }
    // Is this slot's filament moving right now - the card blinks green while
    // loading, red while unloading, same signal as the dashboard's toolhead
    // card. head_source is what ties a SLOT card to a per-HEAD operation: it
    // is stamped BEFORE the feed and cleared only on a verified unload, so
    // the card that blinks is the one whose filament actually moves - in both
    // modes, and for a combiner slot whose index does not match its head.
    // toolheadOps is declared further down; this only runs at render time.
    function panelSlotOp(aceIdx, slotIdx) {
      const head = panelSlotHeadLoaded(aceIdx, slotIdx);
      return head === null ? null : (toolheadOps.value[head] || null);
    }
    // Content of the thumbnail line. In an all-cameras strip a tile is about
    // 190px wide, where four slot cards are neither readable nor tappable -
    // so below that the stylesheet swaps the cards for this: the lane that is
    // running, in its colour. WHEN that happens is decided in CSS alone (the
    // iframe is the viewport, so a media query sees the tile), which is why
    // this is a plain computed with no resize listener behind it.
    const panelMini = computed(() => {
      // Same signal as panelSlotActive - /api/state has no active-extruder
      // field, but the printing lane keeps feed assist armed for the whole
      // print. The shown unit is asked first, then the others: a swap can
      // leave the pinned card idle while a different unit is the one feeding.
      const aces = state.aces || [];
      const shown = panelAce.value;
      const order = shown ? [shown].concat(aces.filter(a => a !== shown)) : aces;
      for (const a of order) {
        const s = a.feed_assist;
        if (typeof s !== "number" || s < 0) continue;
        const slot = (a.slots || []).find(x => x.idx === s) || {};
        const head = panelSlotHeadLoaded(a.idx, s);
        return {
          color: slot.color || null,
          // The head is what you look for; if the armed slot feeds none
          // (possible between a swap's unload and load), name the unit
          // instead of inventing a head.
          main: head === null ? "MMU " + dispIdx(a.idx) : "T" + dispIdx(head),
          sub: slot.material || "",
        };
      }
      // Nothing armed: idle, or a print that has not reached its first load.
      return {color: null,
              main: shown ? "MMU " + dispIdx(shown.idx) : "multiACE", sub: ""};
    });
    // Ask back before anything that moves filament, when the printer has
    // "confirm commands" on. Deliberately the native confirm(): it blocks,
    // so a mis-click cannot slip past while the dialog renders, and it needs
    // no state of its own. Off -> always true, i.e. zero change.
    function _confirmCmd(key, params) {
      if (!state.confirm_commands) return true;
      return window.confirm(t(key, params || {}));
    }
    function loadAll(idx) {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.load_all", {ace: dispIdx(idx)})) return;
      run("ACE_SWITCH", {TARGET: idx, AUTOLOAD: 1});
    }
    function _phaseFor(channelState) {
      if (!channelState) return null;
      const s = String(channelState);
      if (s.endsWith('_finish') || s.endsWith('_fail')) return null;
      if (s === 'wait_insert' || s === 'inited' || s === 'test') return null;
      if (s.startsWith('unload_')) return 'unloading';
      if (s.startsWith('load_'))   return 'loading';
      if (s.startsWith('preload_')) return 'loading';
      if (s.startsWith('manual_sta_')) return 'loading';
      return null;
    }
    const toolheadOps = computed(() => {
      const ops = {};
      for (const t of state.toolheads) {
        const p = _phaseFor(t.channel_state);
        if (p) ops[t.idx] = p;
      }
      return ops;
    });
    // During an ACTIVE print, a user-initiated load/unload from the dashboard
    // would interleave its homing/moves into the running motion queue and ruin
    // the print: Klipper is single-threaded, but gcode injected via Moonraker
    // runs BETWEEN the SD-print lines, it is NOT ignored. The print's OWN swaps
    // (ACE_SWAP_HEAD from the gcode file) and runout-reloads do not go through
    // these buttons, so block the buttons here instead of in the engine (a
    // command-level block would also kill the print's own swap). Gated on
    // 'printing' ONLY - a PAUSED print still needs Load for runout recovery
    // (see needsReload). Drying the other ACE mid-print is a wanted feature and
    // stays enabled (the FA-preserve in _perform_switch + the V2 watchdog keep
    // the printing head fed).
    const isPrinting = computed(() => state.printer_state === 'printing');
    function _blockIfPrinting() {
      if (isPrinting.value) {
        setMacroLog(t("ui.dashboard.blocked_printing"));
        return true;
      }
      return false;
    }
    // Does this slot have filament at the ACE input? 'empty' is the backend's
    // own verdict (gate == 0 or an empty status, V1's 'empty1' included);
    // 'unknown' means the unit has not reported yet and must NOT block
    // anything. The engine refuses to load an empty slot, so offering the
    // button here only ever produces the useless half of the sequence: on
    // an occupied head loadSlot enqueues unload+load, the unload runs, the
    // load is refused, and the head stands empty for nothing.
    function slotIsEmpty(aceIdx, slotIdx) {
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const sl = a && (a.slots || []).find(s => s.idx === slotIdx);
      // 'nosensor': a unit without presence gate (caps) - nothing to load
      // from here, the lane has no truth to offer.
      return !!sl && (sl.state === 'empty' || sl.state === 'nosensor');
    }
    // Capability helpers: the backend derives these from the engine's caps
    // block when present and from protocol/firmware otherwise (older ace.py);
    // the fallbacks here cover a backend that predates the fields.
    function aceFlashEngine(a) {
      if (!a) return "";
      if (a.flash_engine !== undefined) return a.flash_engine || "";
      const p = (a.protocol || "").toLowerCase();
      return p === "v1" ? "iap_v1" : p === "v2" ? "ota_v2" : "";
    }
    function aceHasDryer(a) {
      if (!a) return false;
      if (a.has_dryer !== undefined) return !!a.has_dryer;
      return ["v1", "v2"].includes((a.protocol || "").toLowerCase());
    }
    function aceHasDecoder(a) {
      if (!a) return false;
      if (a.has_decoder !== undefined) return !!a.has_decoder;
      return (a.protocol || "").toLowerCase() === "v2";
    }
    // multi: slot N feeds head N. A head without ACE (manual/feeder) takes
    // nothing from a slot - a slot load there would load the head from its
    // side feeder instead, so the slot button is off for it.
    function slotHeadHasNoAce(slotIdx) {
      if (state.mode === 'head') return false;
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      return !!(th && (th.manual || th.feeder));
    }
    function isToolheadOccupied(aceIdx, slotIdx) {
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (!th) return false;
      if (th.head_source_known) {
        if (th.ace !== aceIdx) return false;
        // Multi twin of slotLoadedInHead's sensor rule: a head whose
        // toolhead sensor explicitly reads CLEAR is not really occupied -
        // head_source is retry/FA bookkeeping, not filament (failed load,
        // manual extraction: bowden off, lever, pull, cut, just load).
        // The engine's own already-loaded guard reads the same sensor.
        // toolheadOps keeps it "occupied" while a load/unload still RUNS
        // (the sensor clears ~30s before an unload finishes); a None/
        // undefined sensor stays conservative-occupied.
        if (th.filament_at_extruder === false && !toolheadOps.value[th.idx]) {
          return false;
        }
        return true;
      }
      return !!th.filament_at_extruder;
    }
    // Mid-print runout: print paused, the head still owns its ACE source
    // (head_source NOT cleared - that would break FA-rearm on resume), but the
    // toolhead motion sensor reads empty. ACE_LOAD_HEAD's "already loaded" guard
    // is gated on that same toolhead sensor, so a reload goes through during a
    // runout even though head_source is set. We just re-enable the load button.
    function needsReload(aceIdx, slotIdx) {
      if (state.printer_state !== 'paused') return false;
      // The slot to reload is the one a paused head is SOURCED from
      // (head_source ace/slot), not the toolhead whose index == the slot.
      // In head mode a head loads from any slot of its wired ACE, so the old
      // idx===slot lookup blinked "reload" under the wrong slot (slot==head).
      // toolheadOps gate: during a running unload the sensor clears ~30s
      // before head_source does - without the gate the button would flip
      // to "Reload" mid-op and invite a click into the half-finished unload.
      return state.toolheads.some(th =>
        th.head_source_known &&
        th.ace === aceIdx && th.slot === slotIdx &&
        th.filament_at_extruder === false &&
        !toolheadOps.value[th.idx]);
    }
    function unloadHead(idx) {
      if (_blockIfPrinting()) return;
      const th = (state.toolheads || []).find(x => Number(x.idx) === Number(idx));
      if (th && (th.manual || th.feeder)) return unloadHeadStaged(idx);
      if (!_confirmCmd("ui.confirm.unload_head", {head: dispIdx(idx)})) return;
      run("ACE_UNLOAD_HEAD", {HEAD: idx});
    }
    // Feeder/manual head: the touchscreen's unload has a "manual
    // intervention" stop between heating and the tip-pull, because the
    // user pulls the strand out of the side port WHILE it unloads (the
    // port lets filament through forward only). Mirror it: STAGE=prepare,
    // wait for the channel to report unload_heat_finish, show the dialog,
    // Next -> STAGE=doing, Cancel -> STAGE=cancel.
    function _channelStateOf(idx) {
      const th = (state.toolheads || []).find(x => Number(x.idx) === Number(idx));
      return th ? String(th.channel_state || "") : "";
    }
    // Resolves with the state that ended the wait; stopFn (optional) ends
    // it early, e.g. when the command behind the state has already returned.
    function _waitChannelState(idx, wanted, aborts, timeoutMs, stopFn) {
      return new Promise((resolve) => {
        const t0 = Date.now();
        const tick = () => {
          const cs = _channelStateOf(idx);
          if (wanted.includes(cs)) return resolve(cs);
          if (aborts.includes(cs) || Date.now() - t0 > timeoutMs
              || (stopFn && stopFn())) return resolve(cs || "timeout");
          setTimeout(tick, 500);
        };
        tick();
      });
    }
    // Returns "done", "cancel" or "fail" so Unload All can stop on a cancel.
    // Feeder/manual heads unload like the touchscreen does: Klipper runs
    // prepare and doing back to back (no STAGE), and the pull-out dialog
    // shows WHILE the head unloads and closes by itself. A head parked in
    // unload_heat_finish for longer than a status poll makes the
    // touchscreen fire its own AUTO_FEEDING_BATCH DOING, which the stock
    // macro refuses with a popup, so there is no Next step in between.
    async function unloadHeadStaged(idx, skipConfirm) {
      if (!skipConfirm
          && !_confirmCmd("ui.confirm.unload_head", {head: dispIdx(idx)})) return "cancel";
      let finished = false;
      const p = run("ACE_UNLOAD_HEAD", {HEAD: idx}).then(ok => { finished = true; return ok; });
      // Watch for the doing stage; a macro that ends first never got there.
      const cs = await _waitChannelState(idx, ["unload_doing"], ["unload_fail"],
                                         300000, () => finished);
      if (cs === "unload_doing") {
        const title = t("ui.dashboard.manual_intervention_title");
        confirm({
          title,
          message: tHtml("ui.dashboard.manual_intervention_body", {head: dispIdx(idx)}),
          okLabel: t("ui.common.ok"),
          dismissOnly: true,
        });
        await _waitChannelState(idx, [],
          ["unload_finish", "unload_fail", "load_finish", "preload_finish", "wait_insert"],
          300000, () => finished);
        if (confirmDialog.show && confirmDialog.title === title) confirmDialog.show = false;
      }
      const done = await p;
      if (!done) {
        _addNotif({id: "staged-" + Date.now(), ts: Date.now() / 1000, level: "error",
                   msg: t("ui.dashboard.manual_intervention_failed",
                          {head: dispIdx(idx), state: _channelStateOf(idx) || "?"})});
        return "fail";
      }
      return "done";
    }
    function _headHoldsFilament(th) {
      return !!(th && th.module_exist && (th.head_source_known || th.filament_at_extruder));
    }
    // Feeder and manual heads first, one by one with the same pull-out
    // dialog as their own Unload button; a cancel or failure stops the
    // whole run. The engine's Unload All then takes the ACE heads (the
    // emptied feeder/manual heads read clear and are skipped there).
    async function unloadAll() {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.unload_all")) return;
      const staged = (state.toolheads || [])
        .filter(th => (th.manual || th.feeder) && _headHoldsFilament(th))
        .map(th => th.idx);
      for (const h of staged) {
        if (await unloadHeadStaged(h, true) !== "done") return;
      }
      const rest = (state.toolheads || [])
        .some(th => !th.manual && !th.feeder && _headHoldsFilament(th));
      if (!staged.length || rest) run("ACE_UNLOAD_ALL_HEADS");
    }
    function cancelUnloadAll() {
      run("ACE_UNLOAD_ALL_CANCEL");
    }
    // The cancel command can only stop the ACE_UNLOAD_ALL_HEADS loop BETWEEN
    // heads, so the button must follow that loop and not "some head is
    // unloading": during a single unload (the prefix of a slot load, say)
    // it would appear and do nothing. Engine state rather
    // than a local flag, so a reload or a second browser sees the truth.
    const anyUnloading = computed(() => !!state.unload_all_active);
    // The three head-mode setters used to swallow the outcome entirely
    // (empty catch, no res.ok, fire-and-forget reload). A REFUSAL from the
    // engine - "unload it first", the 1:1 wiring collision - was therefore
    // invisible: the checkbox had already flipped in the DOM, the state said
    // otherwise, and nothing explained why. Same lesson as spoolMacro, so the
    // same shape: report the reason, and AWAIT the reload so the controls are
    // repainted from the truth before the caller returns.
    async function headSet(path, body, label) {
      try {
        const r = await fetch(`${API}/${path}`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const b = await r.json().catch(() => ({}));
          const detail = String(b.detail || `HTTP ${r.status}`);
          const m = detail.match(/[Ee]rror on '[^']*':\s*(.*)/);
          setMacroLog(`${label}: ${(m ? m[1] : detail).slice(0, 200)}`);
        }
      } catch (e) {
        setMacroLog(`${label}: ${e}`);
      }
      await reloadState();
    }
    // A checkbox bound with :checked (not v-model) KEEPS the user's click in
    // the DOM when the action is refused: the bound value never changed, so
    // Vue has nothing to patch, and the box shows the opposite of the truth
    // with no way back but a page reload (e.g. a refused feeder toggle
    // leaves the checkmark gone while the state still says feeder).
    // Reverting the DOM right away makes the box purely state-driven: it
    // moves only once the state has actually moved.
    function headToggle(ev, current, fn) {
      const wanted = ev.target.checked;
      ev.target.checked = current;
      return fn(wanted);
    }
    async function setHeadManual(idx, enable) {
      await headSet("head-manual", {head: idx, enable: !!enable},
                    t("ui.dashboard.manual"));
    }
    async function setHeadFeeder(idx, enable) {
      await headSet("head-feeder", {head: idx, enable: !!enable},
                    t("ui.dashboard.feeder"));
    }
    // head mode: background-swap opt-in per head (= the HARDWARE declaration
    // "this head's dock is open below"). Engine-persisted (ace__bg_heads),
    // direct macro call like the language dropdown - no command queue entry.
    function bgEnabledFor(idx) {
      // Called from the render, so it must survive a state object that the
      // running app.js never initialised - see the template guards.
      return ((state.bg_swap || {}).enabled_heads || [])
        .some(h => Number(h) === Number(idx));
    }
    async function setBgHead(idx, enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_BG_SET_HEAD",
                                args: {HEAD: idx, ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Spoolman. The URL is edited locally and applied with its own button -
    // typing into a field that fires a macro per keystroke would spam the
    // printer and persist half-typed URLs.
    const spoolmanUrl = ref("");
    const spoolmanBusy = ref(false);
    const spoolmanLast = ref(null);
    // With a Spoolman URL configured, Spoolman is the ONLY source of new
    // spools (either local or Spoolman - the local create paths hide, the
    // pickers gain the search below). Without one, everything stays local
    // as before. Existing local-only entries keep working either way -
    // connecting must not orphan data.
    // World gate: the three-way switch decides, not the URL - a configured
    // URL with mode 'local' stays a local world.
    const spoolmanConnected = computed(() => state.spool_mode !== "local");
    // URL presence, for the connection probe: the ping must work BEFORE
    // the mode can be switched (spoolman/spoollink need a live check).
    const spoolmanUrlSet = computed(() =>
      !!(state.spoolman_url || "").trim());
    // Spoolman search-over-everything: the collection stays in Spoolman
    // (10k spools must never enter the table/state payload); the backend
    // filters and returns a top-50. Selecting a row ADOPTS that one spool
    // into the local table and, from a picker, selects it there.
    const smQuery = ref("");
    const smRows = ref([]);
    const smBusy = ref(false);
    const smOpen = ref(false);
    let _smTimer = null;
    function smSearchDebounced() {
      smOpen.value = true;
      clearTimeout(_smTimer);
      _smTimer = setTimeout(smSearch, 300);
    }
    async function smSearch() {
      smBusy.value = true;
      try {
        const r = await fetch(`${API}/spoolman/search?q=${
          encodeURIComponent(smQuery.value.trim())}`);
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || r.status);
        smRows.value = j.rows || [];
      } catch (e) {
        smRows.value = [];
        setMacroLog(`Spoolman: ${e.message || e}`);
      } finally {
        smBusy.value = false;
      }
    }
    // Dismissing the result list: on a successful adopt, by closing the
    // whole picker, by Escape and by a click outside.
    // pointerdown, not blur: blur fires BEFORE the click on a result row, so
    // a blur-close would swallow the selection. And pointerdown inside the
    // .sm-search wrapper (both the tab's and the picker's carry that class)
    // is exactly the case to leave alone - the rows live in there.
    // The overlay stays untouched: its own @click.self already closes the
    // picker, which clears the search anyway, so the outcome is unchanged.
    function _smDocPointerDown(ev) {
      if (!smOpen.value) return;
      const t_ = ev && ev.target;
      if (t_ && t_.closest && t_.closest(".sm-search")) return;
      smOpen.value = false;
    }
    function _smDocKeydown(ev) {
      if (ev.key !== "Escape" || !smOpen.value) return;
      smOpen.value = false;
      // The picker has no Escape handler of its own, but stop here anyway so
      // one press never means "close the list AND the dialog".
      ev.stopPropagation();
    }
    // Binding is MANDATORY at adopt - an
    // unbound Spoolman entry must never exist, then nothing ever needs a
    // cleanup pass. In a picker the target is the picker's own slot/head
    // and is bound IMMEDIATELY (a cancel after adopt would otherwise leave
    // an orphan); in the tab a result is STAGED (smPick) and the adopt bar
    // demands a target before the button arms.
    const smPick = ref(null);
    async function _smAdoptCore(row) {
      const r = await fetch(`${API}/spoolman/adopt`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({spoolman_id: row.spoolman_id}),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.status);
      return String(j.id);
    }
    async function smAdopt(row) {
      if (!picker.show) { smPick.value = row; return; }
      smBusy.value = true;
      try {
        const id = await _smAdoptCore(row);
        if (picker.head !== null && picker.head !== undefined) {
          await spoolMacro("ACE_SPOOL_ASSIGN", {HEAD: picker.head, ID: id});
        } else {
          await spoolMacro("ACE_SPOOL_ASSIGN",
                           {ACE: picker.ace, SLOT: picker.slot, ID: id});
        }
        await reloadState();
        picker.spool = id;
        smOpen.value = false;
        smQuery.value = "";
        smRows.value = [];
      } catch (e) {
        setMacroLog(`Spoolman: ${e.message || e}`);
      } finally {
        smBusy.value = false;
      }
    }
    // A search row whose spool is ALREADY bound somewhere is no pick -
    // the target-list discipline applied to the spool side: in the picker
    // the click binds IMMEDIATELY, so a
    // taken row would displace the other binding or run into the engine's
    // red backstop. Bound to the CURRENT picker target stays clickable
    // (re-pick of what already sits here, the adopt then just refreshes).
    function smRowTaken(r) {
      const lid = r && r.local_id;
      if (!lid) return false;
      const key = spoolSlotKey((state.spools || {})[lid] || {});
      if (!key) return false;
      if (picker.show) {
        const own = (picker.head !== null && picker.head !== undefined)
          ? `h${picker.head}` : `${picker.ace}_${picker.slot}`;
        return key !== own;
      }
      return true;
    }
    // Where the taken spool sits - shown in the greyed row so the grey
    // explains itself instead of looking broken.
    function smRowWhere(r) {
      const lid = r && r.local_id;
      const sp = lid ? (state.spools || {})[lid] : null;
      return sp ? spoolSlotLabel(sp) : "";
    }
    // A Spoolman row is not edited locally - its identity lives in
    // Spoolman, a local edit would create two truths. The ONE manual
    // correction that stays is the weight against a scale, as a small
    // dialog instead of the full editor form.
    function spoolWeightDialog(sp) {
      confirm({
        title: spoolTitle(sp),
        message: t("ui.spools.weight_dlg_msg"),
        inputLabel: t("ui.spools.weight"),
        inputValue: (sp.weight_g === undefined || sp.weight_g === null)
          ? "" : String(Math.round(sp.weight_g)),
        validate: v => {
          const n = Number(String(v).trim());
          return (String(v).trim() !== "" && Number.isFinite(n)
                  && n >= 0 && n <= 10000)
            ? "" : t("ui.spools.weight_dlg_bad");
        },
        okLabel: t("ui.common.save"),
        onOk: ({value}) => {
          spoolMacro("ACE_SPOOL_SET",
                     {ID: sp.id, WEIGHT: Number(String(value).trim())});
        },
      });
    }
    // PA dialog: view/edit a spool's stored pressure-advance matrix
    // (pa_matrix, keys '{nozzle}_{volume_type}'). Our field even in
    // Spoolman mode (Spoolman does not carry PA until the sync build);
    // hidden in SpoolLink mode (PA belongs to the mod there, and the
    // engine command refuses too). Copy/Paste moves the whole matrix
    // between spools - the clipboard survives closing the dialog.
    const paDlg = ref(null);
    const paClip = ref(null);
    function spoolPaDialog(sp) {
      const m = (sp && sp.pa_matrix && typeof sp.pa_matrix === "object")
        ? sp.pa_matrix : {};
      paDlg.value = {
        sp,
        orig: {...m},
        rows: Object.keys(m).sort().map(k => ({key: k, value: String(m[k])})),
        newKey: "", newValue: "",
      };
      paSeedKey();
    }
    // Key SELECT options: the machine's real keys (engine nozzle_keys)
    // FIRST, then the standard catalog (0.2-0.8 x standard/high_flow) so
    // values for a not-currently-mounted nozzle can be entered too (a
    // uniform machine would otherwise show ONE option and an empty list
    // once taken). Rows already shown are filtered out. Empty machine
    // list (older Klipper without the field) -> free-text fallback.
    const PA_KEY_CATALOG = ["0.2_standard", "0.4_standard", "0.6_standard",
                            "0.8_standard", "0.2_high_flow", "0.4_high_flow",
                            "0.6_high_flow", "0.8_high_flow"];
    function paKeyOptions() {
      const d = paDlg.value;
      const have = d ? d.rows.map(r => r.key) : [];
      const all = [...(state.nozzle_keys || [])];
      for (const k of PA_KEY_CATALOG) if (!all.includes(k)) all.push(k);
      return all.filter(k => !have.includes(k));
    }
    function paSeedKey() {
      const d = paDlg.value;
      if (!d) return;
      const opts = paKeyOptions();
      if (opts.length && !opts.includes(d.newKey)) d.newKey = opts[0];
    }
    function paRowDel(i) {
      if (!paDlg.value) return;
      paDlg.value.rows.splice(i, 1);
      paSeedKey();
    }
    function paKeyValid(k) {
      // Mirrors ace.py _pa_key_valid (AST-cross-tested): '{dia}_{vt}',
      // dia a non-empty number (Number('') is 0 = finite - trap!), vt
      // lowercase/underscore.
      const p = String(k).split("_");
      return p.length >= 2 && p[0].trim() !== ""
        && Number.isFinite(Number(p[0]))
        && p.slice(1).join("_").length > 0
        && /^[a-z_]+$/.test(p.slice(1).join("_"));
    }
    function paValueValid(v) {
      const n = Number(String(v).trim());
      return String(v).trim() !== "" && Number.isFinite(n) && n >= 0 && n <= 5;
    }
    function paAddRow() {
      const d = paDlg.value;
      if (!d) return;
      const k = d.newKey.trim(), v = d.newValue.trim();
      if (!paKeyValid(k) || !paValueValid(v)) return;
      const i = d.rows.findIndex(r => r.key === k);
      if (i >= 0) d.rows[i].value = v;
      else d.rows.push({key: k, value: v});
      d.newKey = ""; d.newValue = "";
      paSeedKey();
    }
    function paCopy() {
      if (paDlg.value) paClip.value = paDlg.value.rows.map(r => ({...r}));
    }
    function paPaste() {
      if (paDlg.value && paClip.value) {
        paDlg.value.rows = paClip.value.map(r => ({...r}));
        paSeedKey();
      }
    }
    // PA from the dashboard picker: opens the same dialog for the picked
    // spool (layered overlay-top over the picker, like the weight confirm).
    function pickerPaDialog() {
      const sp = (state.spools || {})[picker.spool];
      if (sp) spoolPaDialog(sp);
    }
    // The head this spool feeds right now: an ACE slot binding resolves
    // through the toolhead that holds that slot with filament at the
    // extruder; a feeder/manual binding ('h<n>') names the head directly.
    // null = not feeding a loaded head (nothing to calibrate on).
    function paSpoolHead(sp) {
      if (!sp) return null;
      const key = Object.keys(state.spool_binding || {})
        .find(k => state.spool_binding[k] === sp.id);
      if (!key) return null;
      const ths = state.toolheads || [];
      if (key.startsWith("h")) {
        const h = Number(key.slice(1));
        const th = ths.find(t => t.idx === h);
        return (th && th.filament_at_extruder !== false) ? h : null;
      }
      const [a, sl] = key.split("_").map(Number);
      const th = ths.find(t => t.ace === a && t.slot === sl
                               && t.filament_at_extruder !== false);
      return th ? th.idx : null;
    }
    // A spool bound to an ACE slot that is NOT loaded anywhere: the load
    // the calibration would need first. Same head resolution as loadSlot
    // (multi: slot index, head mode: the head wired to the ACE); unload
    // first when that head holds another slot. null = no ACE binding, slot
    // empty, or already feeding a head (paSpoolHead answers then).
    function paSpoolLoadPlan(sp) {
      if (!sp || paSpoolHead(sp) !== null) return null;
      const key = Object.keys(state.spool_binding || {})
        .find(k => state.spool_binding[k] === sp.id);
      if (!key || key.startsWith("h")) return null;
      const [a, sl] = key.split("_").map(Number);
      if (!Number.isFinite(a) || !Number.isFinite(sl)) return null;
      if (slotIsEmpty(a, sl)) return null;
      const h = (state.mode === "head") ? aceHeadForAce(a) : sl;
      if (h === null || h === undefined) return null;
      const th = (state.toolheads || []).find(tt => tt.idx === h);
      if (th && (th.manual || th.feeder)) return null;
      const directLoad = !!(th && !th.filament_at_extruder
        && th.ace === a && th.slot === sl);
      const unload = !!(th && th.head_source_known && !directLoad
        && (state.mode === "head" || th.ace !== a));
      return {ace: a, slot: sl, head: h, unload};
    }
    const paCalibrating = ref(false);
    const paLoading = ref(false);
    function paCanCalibrate() {
      const d = paDlg.value;
      if (!d || paCalibrating.value || paLoading.value) return false;
      if (["printing", "paused"].includes(state.printer_state)) return false;
      return paSpoolHead(d.sp) !== null || paSpoolLoadPlan(d.sp) !== null;
    }
    async function paCalibrate() {
      const d = paDlg.value;
      if (!d || !paCanCalibrate()) return;
      let head = paSpoolHead(d.sp);
      if (head === null) {
        // Not loaded: ask, then load through the normal queue and only
        // calibrate when the load actually completed. The filament stays
        // loaded afterwards.
        const plan = paSpoolLoadPlan(d.sp);
        if (!plan) return;
        const yes = await new Promise(resolve => confirm({
          title: t("ui.spools.pa_calibrate"),
          message: tHtml(plan.unload ? "ui.spools.pa_load_ask_unload" : "ui.spools.pa_load_ask",
                     {ace: dispIdx(plan.ace), slot: dispIdx(plan.slot), head: dispIdx(plan.head)}),
          okLabel: t("ui.common.ok"),
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        }));
        if (!yes) return;
        paLoading.value = true;
        let loaded = false;
        try {
          if (plan.unload) {
            const u = enqueue("ACE_UNLOAD_HEAD", {HEAD: plan.head});
            const l = enqueue("ACE_LOAD_HEAD", {HEAD: plan.head, ACE: plan.ace, SLOT: plan.slot});
            loaded = (await u) && (await l);
          } else {
            loaded = await enqueue("ACE_LOAD_HEAD", {HEAD: plan.head, ACE: plan.ace, SLOT: plan.slot});
          }
          await reloadState();
        } finally {
          paLoading.value = false;
        }
        if (!loaded) return;
        head = paSpoolHead(d.sp);
        if (head === null) head = plan.head;
      }
      paCalibrating.value = true;
      try {
        // Blocks for the whole stock flow calibration; the dialog shows
        // the running state meanwhile. ACE_PA_CALIBRATE stores the result
        // on the spool itself, so a reload of the rows shows it.
        const ok = await spoolMacro("ACE_PA_CALIBRATE", {HEAD: head, FORCE: 1});
        const sp = (state.spools || {})[d.sp.id];
        if (ok && sp && paDlg.value && paDlg.value.sp.id === sp.id) {
          const m = (sp.pa_matrix && typeof sp.pa_matrix === "object") ? sp.pa_matrix : {};
          paDlg.value.sp = sp;
          paDlg.value.orig = {...m};
          paDlg.value.rows = Object.keys(m).sort().map(k => ({key: k, value: String(m[k])}));
          paSeedKey();
        }
      } finally {
        paCalibrating.value = false;
      }
    }
    function paDirty() {
      const d = paDlg.value;
      if (!d) return false;
      const want = {};
      for (const r of d.rows) want[r.key] = Number(String(r.value).trim());
      const ok = Object.keys(d.orig);
      if (ok.length !== Object.keys(want).length) return true;
      return ok.some(k => !(k in want) || Number(d.orig[k]) !== want[k]);
    }
    async function paSave() {
      const d = paDlg.value;
      if (!d) return;
      for (const r of d.rows) {
        if (!paValueValid(r.value)) {
          setMacroLog(t("ui.spools.pa_bad_value") + ": " + r.key);
          return;
        }
      }
      const want = {};
      for (const r of d.rows) want[r.key] = Number(String(r.value).trim());
      for (const k of Object.keys(d.orig)) {
        if (!(k in want))
          await spoolMacro("ACE_SPOOL_PA", {ID: d.sp.id, DELETE: k});
      }
      for (const [k, v] of Object.entries(want)) {
        if (d.orig[k] === undefined || Number(d.orig[k]) !== v)
          await spoolMacro("ACE_SPOOL_PA", {ID: d.sp.id, KEY: k, VALUE: v});
      }
      paDlg.value = null;
      reloadState();
    }
    const smPickTarget = ref("");
    async function smAdoptStaged() {
      const row = smPick.value;
      const key = smPickTarget.value;
      if (!row || !key) return;
      smBusy.value = true;
      try {
        const id = await _smAdoptCore(row);
        if (key.startsWith("h")) {
          await spoolMacro("ACE_SPOOL_ASSIGN",
                           {HEAD: Number(key.slice(1)), ID: id});
        } else {
          const [a, sl] = key.split("_").map(Number);
          await spoolMacro("ACE_SPOOL_ASSIGN", {ACE: a, SLOT: sl, ID: id});
        }
        smPick.value = null;
        smPickTarget.value = "";
        smOpen.value = false;
        smQuery.value = "";
        smRows.value = [];
      } catch (e) {
        setMacroLog(`Spoolman: ${e.message || e}`);
      } finally {
        smBusy.value = false;
      }
    }
    // Badge for a bound spool: Spoolman-backed shows "SM" in the Spoolman
    // orange, a purely local spool keeps the green SPULE badge.
    // Empty feeder/manual head: the tile must not wear the declared colour
    // as its SURFACE (a white identity on an empty feeder is
    // indistinguishable from loaded white) - it goes grey
    // with the colour demoted to the border. Explicit false only: an
    // unknown sensor keeps the colour rather than guessing "empty".
    function headTileEmpty(t_) {
      if (!t_) return false;
      if (t_.feeder) return t_.filament_detected === false;
      if (t_.manual) return t_.filament_at_extruder === false;
      return false;
    }
    function spoolBadgeCls(sp) {
      return sp && sp.spoolman_id ? "src-spoolman" : "src-spool";
    }
    function spoolBadgeLabel(sp) {
      // "SL" in the SpoolLink world, "SM" in plain Spoolman - same orange,
      // the letters alone carry the mode.
      if (sp && sp.spoolman_id) return state.spoollink ? "SL" : "SM";
      return t("ui.common.source_spool");
    }
    // The REAL connection state, probed - null while unknown, so the
    // checkmark can only ever appear after the instance actually answered.
    // The URL
    // watcher below re-probes on every URL change including the initial
    // state load; clicking the indicator re-checks by hand.
    const smPing = ref(null);
    const smPingInfo = ref("");
    async function spoolmanPing() {
      if (!spoolmanUrlSet.value) { smPing.value = null; return; }
      smPing.value = null;
      try {
        const r = await fetch(`${API}/spoolman/ping`);
        const j = await r.json().catch(() => ({}));
        smPing.value = !!j.ok;
        smPingInfo.value = j.ok ? (j.version || "") : (j.reason || "");
      } catch (e) {
        smPing.value = false;
        smPingInfo.value = String(e);
      }
    }
    watch(() => state.spoolman_url, (v) => {
      if (!spoolmanBusy.value) spoolmanUrl.value = v || "";
      spoolmanPing();
    }, {immediate: true});
    const spoolmanStatusText = computed(() => {
      const r = spoolmanLast.value;
      if (spoolmanBusy.value) return t("ui.spools.spoolman_running");
      if (!r) return "";
      if (r.error) return `${t("ui.common.error")}: ${r.error}`;
      return t("ui.spools.spoolman_result",
               {pulled: r.pulled || 0, pushed: r.pushed || 0})
             + (r.msg ? ` (${r.msg})` : "");
    });
    async function saveSpoolmanUrl() {
      const wasConnected = spoolmanUrlSet.value;
      const nowConnected = !!spoolmanUrl.value.trim();
      await spoolMacro("ACE_SET_SPOOLMAN", {URL: spoolmanUrl.value.trim()});
      spoolmanPing();
      // Entering the Spoolman world: adopt+bind every occupied slot whose
      // tag names a Spoolman spool (SM<id> scheme) - the counterpart of
      // the Klipper-side rebind that restores LOCAL bindings on the way
      // back. Convenience sweep: a failure
      // just leaves the manual search+adopt path.
      if (nowConnected && !wasConnected) {
        try {
          const r = await fetch(`${API}/spoolman/adopt_by_tags`,
                                {method: "POST"});
          const body = await r.json().catch(() => ({}));
          if (body.adopted)
            setMacroLog(t("ui.spools.tag_adopted", {n: body.adopted}));
          if (body.errors && body.errors.length)
            setMacroLog(`Spoolman: ${body.errors.join("; ")}`);
        } catch (e) { /* sweep only; search+adopt remains */ }
        await reloadState();
      }
    }
    async function setSpoolmanAuto(enable) {
      await spoolMacro("ACE_SET_SPOOLMAN", {AUTO: enable ? 1 : 0});
    }
    // --- ACE firmware update (Config tab). Two engines behind one card:
    // the ACE 2 goes through the OTA updater (backend/ace2_ota.py, based
    // on hakimio's), the Gen 1 ACE Pro through the IAP flasher
    // (backend/ace1_flash.py). The heavy lifting is Klipper (port
    // release/hold, both generations) + backend (flash thread); this is
    // upload, two buttons and a poll. The flash button goes through the
    // BIG RED own-risk dialog. ---
    const acefw = reactive({ace: "", version: "", password: "",
                            fileName: "", fileSize: 0, fileMd5: "",
                            busy: false,
                            status: null, uiError: "", force: false,
                            patchToOpen: false, patchTarget: ""});
    // The patch target the backend PRESELECTS, and the whole ladder it can
    // build. They are offered as a checkbox plus a picker on the matching
    // stock upload, instead of as direct dropdown targets: the base upload
    // is always stock 1.1.31, only the patch on top differs.
    const acefwPatchTarget = ref("");
    const acefwPatchTargets = ref([]);
    // Label per entry: version plus its size, because the size is what
    // distinguishes the builds that come up from the ones that do not.
    // A build whose "tested" note carries no date has never been flashed
    // and says so.
    function acefwTargetLabel(e) {
      const base = e.size ? `${e.version} (${e.size} B)` : e.version;
      return /^\d{4}-\d{2}-\d{2}/.test(e.tested || "")
        ? base : `${base} - ${t("ui.config.acefw_patch_untested")}`;
    }
    // The checkbox is shown only when stock 1.1.31 is the selected target
    // and the backend has the patch. Its base version is fixed at 1.1.31.
    const acefwCanPatch = computed(() =>
      !!acefwPatchTarget.value && acefw.version.trim() === "1.1.31");
    // Tested-versions allowlist - the
    // version SELECT offers exactly these, the byte gate lives in the
    // backend. Loaded once at mount; empty list = flashing impossible,
    // the dry run stays open (it is the release tool that produces the
    // CRC/MD5 for a NEW entry).
    const acefwVersions = ref([]);
    // Gen 1 (ACE Pro) tested images - a separate allowlist, because a
    // Gen-1 version string does not identify an image (stock and the
    // OpenCubic CFW both report 1.3.863). The entries are keyed by image
    // and the backend gates the upload byte-exactly on its md5.
    const acefwGen1Versions = ref([]);
    async function acefwLoadVersions() {
      try {
        const r = await fetch(`${API}/acefw/versions`);
        const b = await r.json().catch(() => ({}));
        acefwVersions.value = b.versions || [];
        acefwGen1Versions.value = b.gen1_versions || [];
        acefwPatchTarget.value = b.patch_target || "";
        acefwPatchTargets.value = b.patch_targets || [];
        // Preselect the backend's default, but never overwrite a choice the
        // user already made in this session.
        if (!acefw.patchTarget) acefw.patchTarget = acefwPatchTarget.value;
      } catch (e) { /* leave empty */ }
    }
    const acefwInput = ref(null);
    // Both generations are flashable from this card: Gen 2 through the
    // OTA engine, Gen 1 (ACE Pro) through the IAP flasher. The protocol
    // decides which half of the card is shown and which backend engine
    // runs - never a user flag.
    const acefwCandidates = computed(() =>
      (state.aces || [])
        .filter(a => ["iap_v1", "ota_v2"].includes(aceFlashEngine(a)))
        .map(a => {
          const v1 = aceFlashEngine(a) === "iap_v1";
          return {value: a.idx, v1: v1,
                  label: "MMU " + dispIdx(a.idx)
                         + " · " + t(v1 ? "ui.config.acefw_gen1"
                                        : "ui.config.acefw_gen2")
                         + (a.firmware && a.firmware !== "Unknown"
                            ? " · " + a.firmware : "")
                         + (a.connected ? "" : " " + t("ui.acefw.offline"))};
        }));
    const acefwIsV1 = computed(() => {
      const c = acefwCandidates.value.find(o => o.value === acefw.ace);
      return !!(c && c.v1);
    });
    // The version dropdown is an allowlist switchboard: Gen-2 entries are
    // version keys, Gen-1 entries are image ids (the same 1.3.863 can be
    // two different images). Options carry an `id` in both lists.
    const acefwVersionOptions = computed(() =>
      acefwIsV1.value ? acefwGen1Versions.value : acefwVersions.value);
    // Switching the selected ACE switches allowlists - drop a pick the new
    // list does not know (a Gen-2 version is not a Gen-1 image id).
    watch(() => acefw.ace, () => {
      if (!acefwVersionOptions.value.some(
            v => (v.id || v.version) === acefw.version))
        acefw.version = "";
      acefwPreselect();
    });
    function acefwPickFile() { acefwInput.value && acefwInput.value.click(); }
    async function acefwUpload(files) {
      const f = files && files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch(`${API}/acefw/upload`, {method: "POST", body: fd});
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
        acefw.fileName = body.name;
        acefw.fileSize = body.size;
        acefw.fileMd5 = body.md5 || "";
        acefw.versionGuess = body.version_guess || "";
        acefw.uiError = "";
        acefwPreselect();
      } catch (e) { acefw.uiError = `Upload: ${e.message || e}`; }
    }
    // Gen 1: the image is identified by its md5 (names lie - the tunnel
    // build is called ACE_V1.3.863_*), so an md5 match always sets the
    // entry and no match clears it. Gen 2: guess from the file name, only
    // when it is a tested, unambiguous version and nothing is picked yet.
    const acefwGen1Match = computed(() => !acefw.fileMd5 ? null :
      acefwGen1Versions.value.find(v => v.md5 === acefw.fileMd5) || null);
    const acefwGen1Unknown = computed(() =>
      acefwIsV1.value && !!acefw.fileMd5 && !acefwGen1Match.value);
    function acefwPreselect() {
      if (acefwIsV1.value) {
        if (!acefw.fileMd5) return;
        acefw.version = acefwGen1Match.value ? acefwGen1Match.value.id : "";
        return;
      }
      const matches = acefwVersionOptions.value.filter(
        v => v.version === acefw.versionGuess);
      if (!acefw.version.trim() && matches.length === 1)
        acefw.version = matches[0].id || matches[0].version;
    }
    let _acefwTimer = null;
    async function _acefwPoll() {
      try {
        const r = await fetch(`${API}/acefw/status`);
        acefw.status = await r.json();
      } catch (e) { /* next poll */ }
      const st = acefw.status && acefw.status.state;
      if (st === "done" || st === "error" || st === "idle") {
        if (_acefwTimer) { clearInterval(_acefwTimer); _acefwTimer = null; }
        acefw.busy = false;
        reloadState();
      }
    }
    async function _acefwStart(dry) {
      acefw.busy = true;
      acefw.uiError = "";
      acefw.status = {state: dry ? t("ui.acefw.dry_run") : t("ui.acefw.starting"),
                      pct: null, msg: t("ui.acefw.sending")};
      try {
        const r = await fetch(`${API}/acefw/flash`, {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ace: Number(acefw.ace),
                                version: acefw.version.trim(),
                                password: acefw.password || null,
                                dry_run: !!dry,
                                // Force skips only the "already on this
                                // version" check - the allowlist byte
                                // gate stays. For the re-flash test.
                                force: !!acefw.force,
                                // Patch stock 1.1.31 -> ACE2-Open before
                                // flashing (only when the checkbox applies).
                                patch_to_open: acefwCanPatch.value
                                               && !!acefw.patchToOpen,
                                // Empty = the backend's preselection.
                                patch_target: acefw.patchTarget || ""}),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          // 404 here = the backend has no acefw routes = it was not
          // restarted (a stale uvicorn still holds the port). Name that
          // explicitly instead of a bare "HTTP 404".
          if (r.status === 404)
            throw new Error("backend has no firmware routes - restart the "
                            + "multiace-web service (a stale process may "
                            + "still hold the port)");
          throw new Error(body.detail || `HTTP ${r.status}`);
        }
        if (!_acefwTimer) _acefwTimer = setInterval(_acefwPoll, 1500);
      } catch (e) {
        acefw.busy = false;
        acefw.status = null;
        acefw.uiError = `${e.message || e}`;
      }
    }
    // The dry run needs no version (it only reads the CURRENT one), so it
    // is available as soon as an ACE + file are chosen; the real flash
    // additionally needs the target version. Splitting the two is what
    // keeps the version field from silently disabling the dry run.
    function acefwCanTest() {
      return acefw.ace !== "" && !!acefw.fileName && !acefw.busy;
    }
    function acefwReady() {
      return acefwCanTest() && !!acefw.version.trim();
    }
    function acefwTest() { _acefwStart(true); }
    // What the confirm dialog names: the entry's label, not the raw key
    // (a Gen-1 key is an image id like '1.3.871-tunnel').
    function acefwSelectedLabel() {
      const v = acefwVersionOptions.value.find(
        o => (o.id || o.version) === acefw.version);
      return (v && (v.label || v.version)) || acefw.version.trim();
    }
    function acefwFlash() {
      const patching = acefwCanPatch.value && !!acefw.patchToOpen;
      const tgt = patching
        ? (acefw.patchTarget || acefwPatchTarget.value)
        : acefwSelectedLabel();
      // Patching adds a SECOND warning: it flashes a community-modified,
      // NOT byte-tested image.
      const extra = patching
        ? `<div class="acefw-danger">${tHtml("ui.config.acefw_patch_warn",
            {target: tgt})}</div>` : "";
      confirm({
        title: t("ui.config.acefw_confirm_title"),
        message: `<div class="acefw-danger">${tHtml("ui.config.acefw_confirm_msg",
                  {ace: dispIdx(Number(acefw.ace)),
                   version: tgt})}</div>${extra}`,
        okLabel: t("ui.config.acefw_confirm_ok"),
        onOk: () => _acefwStart(false),
      });
    }
    // Red, not grey: an error, or a flash whose unit came up mute.
    const acefwStatusBad = computed(() => {
      if (acefw.uiError) return true;
      const s = acefw.status;
      if (!s) return false;
      if (s.state === "error") return true;
      return !!(s.state === "done" && s.result &&
                s.result.app_alive === false);
    });
    const acefwStatusText = computed(() => {
      if (acefw.uiError) return `${t("ui.common.error")}: ${acefw.uiError}`;
      const s = acefw.status;
      if (!s || s.state === "idle") return "";
      if (s.state === "error")
        return `${t("ui.common.error")}: ${s.error || "?"}`;
      if (s.state === "done") {
        const res = s.result || {};
        if (res.dry_run) {
          const conn = res.connected
            ? t("ui.config.acefw_test_conn", {current: res.current || "?"})
            : t("ui.config.acefw_test_noconn");
          const img = res.image_ok
            ? t("ui.config.acefw_test_img",
                {size: res.size || 0, crc: res.crc || "?",
                 md5: res.md5 || "?", source: res.source || "?"})
            : t("ui.config.acefw_test_noimg", {reason: res.image_error || "?"});
          return `${conn} · ${img}`;
        }
        if (res.skipped)
          return t("ui.config.acefw_skipped", {v: res.current || "?"});
        // A matching version says the image is there, not that it runs:
        // GET_INFO is answered by the bootloader too, which is how the
        // 1.1.61O fault verified as OK three times while the unit was
        // mute. The backend probes an application-only command.
        if (res.app_alive === false)
          return t("ui.config.acefw_done_mute",
                   {v: res.new || res.target || "?"});
        return t("ui.config.acefw_done",
                 {v: res.new || res.target || "?"});
      }
      const pct = (s.pct === null || s.pct === undefined)
        ? "" : ` ${Math.round(s.pct)}%`;
      return `${s.state}${pct} - ${s.msg || ""}`;
    });
    async function spoolmanSync(pull, push) {
      if (spoolmanBusy.value) return;
      spoolmanBusy.value = true;
      spoolmanLast.value = null;
      try {
        const r = await fetch(`${API}/spoolman/sync`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({pull: !!pull, push: !!push}),
        });
        const body = await r.json().catch(() => ({}));
        spoolmanLast.value = r.ok ? body
          : {error: body.detail || `HTTP ${r.status}`};
      } catch (e) {
        spoolmanLast.value = {error: String(e)};
      } finally {
        spoolmanBusy.value = false;
        reloadState();
      }
    }
    // Humidity control per ACE 2. One field per call - the command merges
    // into the stored settings, so a partial update is exactly right here.
    // Values are CLAMPED before they go out: a number field a user typed
    // into without selecting its content produces 450 instead of 50, and a
    // typo should not become a printer-side error popup.
    const _AUTO_DRY_RANGE = {RH_START: [5, 95], RH_END: [1, 94],
                             TEMP: [35, 80], ADD_TIME: [0, 600]};
    // Master is an ACE INDEX (-1 = none), not a number to clamp into a range.
    // `state` is a reactive object, NOT a ref - state.value is undefined
    // here, and the TypeError would kill the whole dry-panel render on
    // every ACE Pro card.
    const autoDryMasters = () => state.auto_dry_masters || [];
    // Edit buffer. The inputs are bound to PRINTER state, and the state is
    // re-polled every few seconds - so a refresh landing mid-edit would throw
    // the typed value away, and a refresh landing between the send and the
    // new state would make the field jump back to the old number. While a
    // field is
    // being edited its buffer wins; it is released only once the printer
    // reports the new value, so there is no window where the old one shows.
    const autoDryEdit = reactive({});
    const _adKey = (idx, param) => `${idx}_${param}`;
    function autoDryValue(idx, param, live) {
      const k = _adKey(idx, param);
      return (k in autoDryEdit) ? autoDryEdit[k] : live;
    }
    // Fields the printer REFUSED. Same treatment as a locally-invalid pair:
    // the frame turns red and the entered value stays, instead of a popup
    // over a snapped-back field. Cleared as soon as the user edits again or
    // the value is accepted.
    const autoDryErr = reactive({});
    function autoDryFieldError(idx, param) {
      return !!autoDryErr[_adKey(idx, param)];
    }
    function autoDryInput(idx, param, ev) {
      autoDryEdit[_adKey(idx, param)] = ev.target.value;
      delete autoDryErr[_adKey(idx, param)];
    }
    // The threshold pair is only ever wrong TOGETHER, so it is validated
    // here and not on the printer: an error popup for a half-typed number is
    // noise, and the old round-trip snapped the field back to the stored
    // value, throwing the edit away. Instead both fields go red, the typed
    // value stays, and nothing is sent until the pair makes sense again.
    function autoDryPairInvalid(idx, live) {
      if (!live) return false;
      const s = Number(autoDryValue(idx, 'RH_START', live.rh_start));
      const e = Number(autoDryValue(idx, 'RH_END', live.rh_end));
      if (!Number.isFinite(s) || !Number.isFinite(e)) return true;
      return e >= s;
    }
    async function autoDryCommit(idx, param, ev, live) {
      const k = _adKey(idx, param);
      autoDryEdit[k] = ev.target.value;
      // Keep the buffer (and the red frame) instead of sending a pair the
      // printer would only reject. Scoped to the two threshold fields on
      // purpose: temp / run-on are independent of the pair, and blocking
      // them here would strand the ACE Pro card if the [ace] defaults
      // themselves ever carried a bad pair.
      if ((param === 'RH_START' || param === 'RH_END')
          && autoDryPairInvalid(idx, live)) return;
      const ok = await setAutoDry(idx, {[param]: ev.target.value});
      if (!ok) {
        // Keep the buffer AND frame the field: the printer said no, so the
        // value the user is looking at is the one to fix.
        autoDryErr[k] = true;
        return;
      }
      // Release only now: spoolMacro AWAITS the state refresh, so the next
      // render already carries the accepted value and the field cannot fall
      // back through the old one on its way there.
      delete autoDryEdit[k];
    }
    // Enabling with an invalid pair would arm the OLD stored thresholds while
    // the card shows the new ones - so it is refused too, and the checkbox is
    // put back at once rather than flickering until the next poll.
    async function autoDryEnable(idx, ev, live) {
      const on = ev.target.checked;
      if (on && autoDryPairInvalid(idx, live)) {
        ev.target.checked = false;
        return;
      }
      const ok = await setAutoDry(idx, {ENABLE: on ? 1 : 0});
      if (!ok) ev.target.checked = !on;
    }
    // Follower master. A refusal here is what blocks its enable, so the
    // dropdown is the field to frame.
    async function autoDrySetMaster(idx, ev) {
      const k = _adKey(idx, 'MASTER');
      delete autoDryErr[k];
      const ok = await setAutoDry(idx, {MASTER: ev.target.value});
      if (!ok) autoDryErr[k] = true;
    }
    async function setAutoDry(idx, args) {
      const out = {};
      for (const [k, v] of Object.entries(args)) {
        const r = _AUTO_DRY_RANGE[k];
        if (!r) { out[k] = v; continue; }
        const n = Number(v);
        if (!Number.isFinite(n)) continue;
        out[k] = Math.min(r[1], Math.max(r[0], Math.round(n)));
      }
      if (!Object.keys(out).length) return true;
      return await spoolMacro("ACE_SET_AUTO_DRY",
                              Object.assign({ACE: idx}, out));
    }
    async function setConfirmCommands(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_CONFIRM_COMMANDS",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Register the panel as a camera in Fluidd. A button, not something
    // the installer does - it changes the user's own dashboard, and the
    // backend leaves an existing entry of that name alone, so a second
    // click cannot overwrite a URL or aspect ratio they adjusted.
    const fluiddCamBusy = ref(false);
    const fluiddCamMsg = ref("");
    async function addFluiddCamera() {
      fluiddCamBusy.value = true;
      fluiddCamMsg.value = "";
      try {
        const r = await fetch(`${API}/fluidd-camera`, {method: "POST"});
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || r.status);
        fluiddCamMsg.value = j.existed
          ? t("ui.config.fluidd_panel_exists")
          : t("ui.config.fluidd_panel_added");
      } catch (e) {
        fluiddCamMsg.value = `${t("ui.common.error")}: ${e.message || e}`;
      } finally {
        fluiddCamBusy.value = false;
      }
    }
    async function setPickupCleaning(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_PICKUP_CLEANING",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    async function setPaSync(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_PA_SYNC",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    async function setAirprintDetection(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_AIRPRINT_DETECTION",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // ---- spool table ------------------------------------------------------
    // Klipper owns the table; every edit goes through gcode (/api/macro), the
    // UI never writes the file - one writer, no lost updates.
    // Starts EMPTY on purpose: the row doubles as a live FILTER for the
    // table while not editing, so pre-filled defaults would hide
    // entries before the user typed anything. Add uses fallbacks instead.
    // Colour empty as well: the swatch fills it on first interaction, so an
    // untouched row filters on nothing. A new spool is NOT bound to a slot -
    // binding happens in the row's slot dropdown or from the slot card.
    const spoolForm = reactive({material: "", color: "", vendor: "",
                               subtype: "", weight: "", label: "", sku: ""});
    // The RFID tag travels to Klipper as a gcode argument, and '#' TERMINATES
    // gcode arguments - so the marker '#' many users put in front of a
    // self-written tag must be stripped here. Harmless: the matcher compares
    // canonically and ignores a leading '#' on either side.
    function _skuArg(s) {
      return String(s || "").trim().replace(/^#+/, "");
    }
    // Mirror of ace.py _sku_canon - comparison form only, the stored value
    // stays what the user typed. Keep the two in step: a looser check here
    // would offer a code the printer then refuses.
    function _skuCanon(s) {
      return String(s || "").trim().replace(/^#+/, "").trim().toLowerCase();
    }
    // Which spool already holds this code, or null. `exceptId` lets an EDIT
    // keep its own code without reporting a collision with itself.
    function _skuHolder(sku, exceptId) {
      const want = _skuCanon(sku);
      if (!want) return null;                    // no code = never a collision
      for (const [id, sp] of Object.entries(state.spools || {})) {
        if (exceptId != null && String(id) === String(exceptId)) continue;
        if (_skuCanon(sp && sp.sku) === want) return id;
      }
      return null;
    }
    // Same semantics as ace.py _spool_unique_sku, INCLUDING the free case:
    // a code nobody holds comes back unchanged. Cross-checked against the
    // real Python function; returning base+"_2" for a free code would be a
    // silent divergence the next caller inherits.
    function _skuSuggest(sku) {
      const base = String(sku || "").trim();
      if (!base || !_skuHolder(base)) return base;
      let n = 2;
      while (_skuHolder(base + "_" + n)) n++;
      return base + "_" + n;
    }
    // The tag code of a spool being created is already taken (the second
    // factory spool of an article - factory tags are per ARTICLE, not per
    // spool). Ask instead of deciding: the suffix is only a PROPOSAL in an
    // editable field, because the useful answer is usually a code the user
    // then writes onto the tag himself, which restores auto-binding - the
    // suffixed entry can never be recognised (its tag still reads the
    // original). Taking the code AWAY from the other spool is deliberately
    // impossible, so the validation blocks, it does not warn. Resolves to
    // the chosen code
    // or null when cancelled; an empty code is allowed and means "no tag".
    function askFreeSku(sku) {
      return new Promise(resolve => {
        const holder = _skuHolder(sku);
        if (!holder) return resolve(_skuArg(sku));
        const other = (state.spools || {})[holder] || {};
        confirm({
          title: t("ui.spools.sku_taken_title"),
          message: tHtml("ui.spools.sku_taken_msg",
                     {sku: _skuArg(sku), id: holder, spool: spoolTitle(other)}),
          inputLabel: t("ui.spools.sku"),
          // Pre-strip the '#': it never survives to the table (gcode cuts at
          // '#'), so showing it would mean the field lies about what is
          // stored.
          inputValue: _skuArg(_skuSuggest(sku)),
          inputHint: t("ui.spools.sku_taken_hint"),
          validate: v => {
            const h = _skuHolder(v);
            return h ? t("ui.spools.sku_still_taken", {id: h}) : "";
          },
          okLabel: t("ui.spools.add"),
          onOk: ({value}) => resolve(_skuArg(value)),
          onCancel: () => resolve(null),
        });
      });
    }
    // Option lists for the spool form. Same sources as the slot picker (the
    // firmware filament DB via /api/materials + [ace_tipform] vendors), PLUS
    // whatever the existing spools already use - so the lists GROW with the
    // table instead of forcing free text for a vendor/subtype the printer
    // does not ship.
    function _mergeCase(base, extra) {
      const out = [...base];
      const low = new Set(out.map(x => String(x).toLowerCase()));
      for (const e of extra) {
        const v = String(e || "").trim();
        if (v && !low.has(v.toLowerCase())) { out.push(v); low.add(v.toLowerCase()); }
      }
      return out;
    }
    const spoolMaterials = computed(() =>
      _mergeCase(pickerMaterials.value,
                 spoolList().map(sp => sp.material)));
    const spoolVendors = computed(() => {
      // No material chosen yet (the row is a filter then): offer the vendors
      // of ALL materials instead of an empty list.
      const byMat = spoolForm.material
        ? (pickerDb.value[spoolForm.material] || {})
        : Object.assign({}, ...Object.values(pickerDb.value || {}));
      const base = Object.keys(byMat);
      const withGeneric = base.includes("Generic")
        ? ["Generic", ...base.filter(x => x !== "Generic")]
        : ["Generic", ...base];
      const mat = String(spoolForm.material || "").toUpperCase();
      return _mergeCase(
        _mergeCase(withGeneric, tipformVendorsByMaterial.value[mat] || []),
        spoolList().filter(sp => !sp.material
                           || sp.material === spoolForm.material)
                   .map(sp => sp.vendor));
    });
    const spoolSubtypes = computed(() => {
      const byVendor = spoolForm.material
        ? (pickerDb.value[spoolForm.material] || {})
        : Object.assign({}, ...Object.values(pickerDb.value || {}));
      // Build the base through _mergeCase too: a DB that lists "Basic"
      // itself would otherwise show it twice (Basic = firmware 'generic').
      const base = _mergeCase(["Basic"], byVendor[spoolForm.vendor] || []);
      return _mergeCase(base, spoolList().map(sp => sp.subtype));
    });
    const spoolImportMode = ref("merge");
    const spoolFileInput = ref(null);
    // Same pattern as the gcode upload: the template calls the trigger,
    // the ref name matches the ref= attribute (setup() refs bind by name).
    function triggerSpoolImport() {
      spoolFileInput.value && spoolFileInput.value.click();
    }

    function spoolList() {
      return Object.values(state.spools || {})
        .sort((a, b) => (parseInt(a.id, 10) || 0) - (parseInt(b.id, 10) || 0));
    }
    // The tab: the form is an EDITOR only (via
    // the row's pencil), never a filter and never a creator - new spools
    // come from the picker (local) or the Spoolman dialog. The list is
    // filtered by WORLD (connected -> only Spoolman-backed rows, local ->
    // only local rows; entries of the other world stay in the table but
    // out of sight) plus a free-text search over everything a row shows.
    const spoolFilterActive = computed(() => false);
    const spoolQuery = ref("");
    // The either/or worlds as ONE predicate (mirror of ace.py
    // _spool_in_world): with Spoolman connected only Spoolman-backed
    // entries exist for the UI, without it only local ones. Every surface
    // that OFFERS spools goes through this, so no picker keeps offering
    // the entries the list hides.
    function spoolWorldOk(sp) {
      return spoolmanConnected.value ? !!(sp && sp.spoolman_id)
                                     : !(sp && sp.spoolman_id);
    }
    // Sort of the tab list: every column header sorts
    // by ITS column - assigned (slot order, bound first: the list mirrors
    // the printer on top, the shelf below; the DEFAULT), label
    // (alphabetical), sku (tag codes, for scanning while writing tags),
    // weight (lightest first - what is about to run out sits on top,
    // unknown weights last).
    const spoolSort = ref("assigned");
    function _spoolBindOrder(sp) {
      const k = spoolSlotKey(sp);
      if (!k) return 9999;
      const m = /^(\d+)_(\d+)$/.exec(k);
      if (m) return Number(m[1]) * 10 + Number(m[2]);
      return k.startsWith("h") ? 800 + (Number(k.slice(1)) || 0) : 9998;
    }
    function _spoolTitleCmp(a, b) {
      return String(spoolTitle(a)).toLowerCase()
        .localeCompare(String(spoolTitle(b)).toLowerCase());
    }
    function spoolListShown() {
      let all = spoolList().filter(spoolWorldOk);
      const terms = spoolQuery.value.trim().toLowerCase().split(/\s+/)
        .filter(Boolean);
      if (terms.length) {
        all = all.filter(sp => {
          const hay = [sp.label, sp.vendor, sp.material, sp.subtype,
                       _skuArg(sp.sku), sp.color,
                       sp.spoolman_id ? "sm" + sp.spoolman_id : "",
                       spoolSlotLabel(sp)].join(" ").toLowerCase();
          return terms.every(t => hay.includes(t));
        });
      }
      const rows = [...all];
      if (spoolSort.value === "sku") {
        // Empty codes sort LAST - a tag scan looks for codes, not gaps.
        rows.sort((a, b) =>
          (_skuCanon(a.sku) || "￿")
            .localeCompare(_skuCanon(b.sku) || "￿")
          || _spoolTitleCmp(a, b));
      } else if (spoolSort.value === "label") {
        rows.sort(_spoolTitleCmp);
      } else if (spoolSort.value === "weight") {
        // Unknown weight is "I don't know", not 0 g - it must not mix in
        // with the genuinely empty spools at the top. 1e12, not Infinity:
        // Infinity - Infinity is NaN and NaN poisons a sort comparator.
        const w = sp => (sp.weight_g === undefined || sp.weight_g === null)
          ? 1e12 : Number(sp.weight_g);
        rows.sort((a, b) => (w(a) - w(b)) || _spoolTitleCmp(a, b));
      } else {
        rows.sort((a, b) => (_spoolBindOrder(a) - _spoolBindOrder(b))
                            || _spoolTitleCmp(a, b));
      }
      return rows;
    }
    // '+' next to the search: opens the empty form as a CREATOR -
    // local mode only, in Spoolman mode new spools come via search+adopt.
    // Save routes through spoolSave -> spoolAdd (unassigned entry,
    // collision dialog included).
    const spoolCreating = ref(false);
    function spoolNewForm() {
      spoolFormClear();
      spoolCreating.value = true;
    }
    function spoolFormClear() {
      spoolCreating.value = false;
      spoolEditId.value = "";
      spoolForm.material = ""; spoolForm.vendor = ""; spoolForm.subtype = "";
      spoolForm.label = ""; spoolForm.weight = ""; spoolForm.color = "";
      spoolForm.sku = "";
    }
    function spoolIdForSlot(aceIdx, slotIdx) {
      return (state.spool_binding || {})[`${aceIdx}_${slotIdx}`] || null;
    }
    function spoolForSlot(aceIdx, slotIdx) {
      const id = spoolIdForSlot(aceIdx, slotIdx);
      return id ? (state.spools || {})[id] || null : null;
    }
    // Feeder/manual heads bind their spool to the HEAD ('h<n>', engine key
    // domain) - they have no ACE slot, the spool sits at the side feeder.
    function spoolForHead(headIdx) {
      const id = (state.spool_binding || {})[`h${headIdx}`] || null;
      return id ? (state.spools || {})[id] || null : null;
    }
    function spoolSlotLabel(sp) {
      const key = Object.keys(state.spool_binding || {})
        .find(k => state.spool_binding[k] === sp.id);
      if (!key) return "";
      if (key.startsWith("h")) {
        const h = Number(key.slice(1));
        const th = (state.toolheads || []).find(t => t.idx === h) || {};
        return `T${dispIdx(h)} · ${t(th.manual ? "ui.dashboard.manual" : "ui.dashboard.feeder")}`;
      }
      const [a, sl] = key.split("_").map(Number);
      return `MMU ${dispIdx(a)} / ${dispIdx(sl)}`;
    }
    // Remaining weight is an estimate (extruded length x density), but it
    // renders WITHOUT a ~ (Spoolman/SpoolLink show plain grams too).
    function spoolWeightLabel(sp) {
      if (!sp || sp.weight_g === undefined || sp.weight_g === null) return "";
      return `${Math.round(sp.weight_g)} g`;
    }
    // Label AND the details - a label like "Rolle links" must not hide what
    // the spool actually is.
    function spoolDetails(sp) {
      const sub = sp.subtype && !/^(basic|generic)$/i.test(sp.subtype)
        ? sp.subtype : "";
      return [sp.vendor, sp.material, sub].filter(Boolean).join(" ");
    }
    function spoolTitle(sp) {
      const det = spoolDetails(sp);
      if (sp.label) return det ? `${sp.label} · ${det}` : sp.label;
      return det || `#${sp.id}`;
    }
    // Reports failures. Swallowing them would let a REJECTED setting look
    // like a saved one: the input is bound to the printer state, so it
    // silently snaps back to the old value and nothing says why.
    // The three-way world switch. Guarded client-side the same way the
    // options are disabled (connection / agent), so a stale button click
    // cannot select an impossible mode; Klipper validates again anyway.
    async function setSpoolMode(v) {
      if (!v || v === state.spool_mode) return;
      if (v !== "local" && smPing.value !== true) return;
      if (v === "spoollink" && !state.spoollink_agent) return;
      await spoolMacro("ACE_SET_SPOOLMAN", {MODE: v});
    }
    const spoolAddBusy = ref(false);
    // The sentence out of a failed /api/macro: Moonraker answers with a
    // JSON envelope (message + traceback) or Klipper's "Error on '...':"
    // wrapper; only the message is for the user.
    function macroErrorText(detail) {
      const d = String(detail || "");
      try {
        const j = JSON.parse(d);
        const m = j && j.error && j.error.message;
        if (m) return String(m);
      } catch (e) {}
      const jm = d.match(/"message":\s*"((?:[^"\\]|\\.)*)"/);
      if (jm) return jm[1].replace(/\\"/g, '"');
      const km = d.match(/[Ee]rror on '[^']*':\s*(.*)/);
      return (km ? km[1] : d).slice(0, 300);
    }
    async function spoolMacro(name, args) {
      // One ACE_SPOOL_ADD at a time: /api/macro blocks while the printer
      // runs long gcode (a swap holds the script queue ~150s), so every
      // extra + click would pile up behind it and each create a fresh spool
      // minutes later. Later clicks
      // are refused with a notice instead of queued; SET/ASSIGN etc. are
      // idempotent and stay unguarded.
      if (name === "ACE_SPOOL_ADD") {
        if (spoolAddBusy.value) {
          setMacroLog(t("ui.spools.add_pending"));
          return false;
        }
        spoolAddBusy.value = true;
      }
      let ok = false;
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, args}),
        });
        ok = r.ok;
        if (!ok) {
          const body = await r.json().catch(() => ({}));
          const detail = String(body.detail || `HTTP ${r.status}`);
          setMacroLog(`${name}: ${macroErrorText(detail).slice(0, 200)}`);
        }
      } catch (e) {
        setMacroLog(`${name}: ${e}`);
      }
      // AWAITED on purpose. Fire-and-forget here is what made edited fields
      // flip: the caller released its edit buffer as soon as this returned,
      // the state was still the OLD one, so the field showed the previous
      // value for a moment and only then the accepted one. Callers may rely
      // on the state being current when this resolves.
      try {
        await reloadState();
      } finally {
        if (name === "ACE_SPOOL_ADD") spoolAddBusy.value = false;
      }
      return ok;
    }
    // Edit mode: the same form edits an existing entry (spoolEditId set).
    const spoolEditId = ref("");
    function spoolEdit(sp) {
      spoolCreating.value = false;
      spoolEditId.value = sp.id;
      spoolForm.material = sp.material || "PLA";
      spoolForm.vendor = sp.vendor || "Generic";
      spoolForm.subtype = sp.subtype || "Basic";
      spoolForm.color = sp.color ? "#" + String(sp.color).replace("#", "") : "";
      spoolForm.weight = (sp.weight_g === undefined || sp.weight_g === null)
        ? "" : Math.round(sp.weight_g);
      spoolForm.label = sp.label || "";
      spoolForm.sku = sp.sku || "";
    }
    function spoolEditCancel() {
      spoolEditId.value = "";
      spoolForm.label = ""; spoolForm.weight = ""; spoolForm.sku = "";
    }
    function spoolSave() {
      if (!spoolEditId.value) { spoolAdd(); return; }
      const args = {ID: spoolEditId.value,
                    MATERIAL: spoolForm.material || "PLA",
                    VENDOR: spoolForm.vendor || "Generic",
                    SUBTYPE: spoolForm.subtype || "Basic",
                    LABEL: spoolForm.label || "",
                    SKU: _skuArg(spoolForm.sku),                        // "" clears it
                    COLOR: (spoolForm.color || "").replace("#", "")};   // "" clears it
      if (spoolForm.weight !== "") args.WEIGHT = spoolForm.weight;
      spoolMacro("ACE_SPOOL_SET", args);
      spoolEditCancel();
    }
    async function spoolAdd() {
      const args = {MATERIAL: spoolForm.material || "PLA"};   // empty = filter cleared
      if (spoolForm.color) args.COLOR = spoolForm.color.replace("#", "");
      if (spoolForm.vendor) args.VENDOR = spoolForm.vendor;
      if (spoolForm.subtype) args.SUBTYPE = spoolForm.subtype;
      if (spoolForm.label) args.LABEL = spoolForm.label;
      if (spoolForm.weight !== "") args.WEIGHT = spoolForm.weight;
      // Ask BEFORE sending when the code is taken; null = cancelled, and then
      // nothing is created and the form keeps what was typed.
      const sku = await askFreeSku(spoolForm.sku);
      if (sku === null) return;
      if (sku) args.SKU = sku;
      // No ACE/SLOT here: a fresh spool starts UNASSIGNED (the old defaults
      // silently bound every new entry to ACE 0 / Slot 0). Bind it in the
      // row's slot dropdown or from the slot card.
      await spoolMacro("ACE_SPOOL_ADD", args);
      // Clear the row after adding: it is the filter again, and a stale
      // filter would hide the entry that was just created. Only AFTER the
      // dialog resolved - clearing it up front would throw the input away
      // on a cancel.
      spoolFormClear();
    }
    // Every ACE/slot that is a REAL assignment target, as picker options.
    // Value is the internal "ace_slot" key (gcode takes 0-based), the label
    // shows display indices (index-base rule). `taken` flags a
    // target that already holds a spool: the markup greys it out,
    // except the row's own
    // current binding, which must stay pickable as the selected value.
    const spoolSlotOptions = computed(() => {
      const out = [];
      const bound = new Set(Object.keys(state.spool_binding || {}));
      // Head mode wires each ACE 1:1 to one head - an ACE whose head runs
      // as feeder/manual is not in use, so its slots are no targets ("wenn
      // head 4 als feeder laeuft, muss ich nicht aus der ace waehlen
      // koennen"). Multi keeps every unit: slot N of ANY ACE feeds head N.
      let usedAces = null;
      if (state.mode === "head") {
        usedAces = new Set();
        for (const th of state.toolheads || []) {
          if (!th.feeder && !th.manual) usedAces.add(Number(headAceOf(th.idx)));
        }
      }
      for (const a of state.aces || []) {
        if (usedAces && !usedAces.has(Number(a.idx))) continue;
        for (const sl of (a.slots || [])) {
          const key = `${a.idx}_${sl.idx}`;
          out.push({key, taken: bound.has(key),
                    label: `MMU ${dispIdx(a.idx)} / ${dispIdx(sl.idx)}`});
        }
      }
      // Feeder/manual heads take a spool too (bound to the head, 'h<n>') -
      // the side feeder has no reader, so this dropdown is THE way to bind.
      for (const th of state.toolheads || []) {
        if (th.feeder || th.manual) {
          const key = `h${th.idx}`;
          out.push({key, taken: bound.has(key),
                    label: `T${dispIdx(th.idx)} · ${t(th.manual ? "ui.dashboard.manual" : "ui.dashboard.feeder")}`});
        }
      }
      return out;
    });
    // Does a spool match what the slot currently declares (RFID or
    // override)? Only fields the spool actually carries are compared, so a
    // half-filled entry still matches on what it does know.
    // How well a spool fits what the dialog currently shows. ADDITIVE, not a
    // filter: a mismatch used to return 0 for everything, so "same material,
    // other colour" ranked identically to "nothing in common" and the list
    // order looked arbitrary. Material counts double - it is the harder fact
    // (the colour is a rendering of one value, and near-identical shades are
    // common). 3 = both, 2 = material, 1 = colour, 0 = neither.
    // Standard swatches for the colour fields. The touchscreen's own palette
    // lives in the closed screen firmware (the Klipper side carries no colour
    // list at all, not even in the filament DB), so this is OUR set of common
    // filament colours - it works like the display's picker, it is not the
    // same table. A swatch writes a DECLARED colour: '#000000' means black,
    // not "unknown" - clearing is what the spool form's x button is for.
    const FILAMENT_SWATCHES = [
      "#000000", "#ffffff", "#808080", "#c8c8c8",
      "#e02020", "#ff7800", "#f5d800", "#22a03c",
      "#0f6b2e", "#3aa0e0", "#1436c8", "#7b2fbe",
      "#ff5aa8", "#7a4a1e", "#d4af37", "#e8dcc0",
    ];
    function sameSwatch(a, b) { return _hex6(a) === _hex6(b); }
    // Colours this printer already knows: every spool of the table plus every
    // slot that reports one. Hitting one's OWN recurring colour exactly beats
    // re-mixing it by eye, and it costs no palette invention. Standard
    // swatches are filtered out so the row stays additional information.
    const knownColors = computed(() => {
      const std = new Set(FILAMENT_SWATCHES.map(_hex6));
      const out = [];
      const add = (c) => {
        const h = _hex6(c);
        if (!h || h.length !== 7 || std.has(h) || out.includes(h)) return;
        out.push(h);
      };
      for (const sp of Object.values(state.spools || {})) add(sp.color);
      for (const a of state.aces || []) for (const sl of (a.slots || [])) add(sl.color);
      return out.slice(0, 16);
    });
    // New design: the same known colours as chips with a name - the spool's
    // title, or the slot's material for a slot without a table spool.
    const knownColorChips = computed(() => {
      const names = {};
      for (const sp of Object.values(state.spools || {})) {
        const h = _hex6(sp.color);
        if (h && !names[h]) names[h] = spoolTitle(sp);
      }
      for (const a of state.aces || []) for (const sl of (a.slots || [])) {
        const h = _hex6(sl.color);
        if (h && !names[h]) names[h] = [sl.material, sl.subtype].filter(Boolean).join(" ");
      }
      return knownColors.value.map(h => ({hex: h, name: names[h] || h}));
    });
    // Hex field of the new colour block: only a complete #rrggbb is taken
    // over, half-typed input stays in the field.
    function pickerHexInput(v) {
      let s = String(v || "").trim();
      if (!s.startsWith("#")) s = "#" + s;
      if (/^#[0-9a-fA-F]{6}$/.test(s)) { picker.color = s.toLowerCase(); pickerTouch(); }
    }
    const pickerGaugePct = computed(() => {
      const w = picker.weight;
      if (w === "" || w === null || w === undefined) return 100;
      return spoolPct({weight_g: Number(w)}) || 4;
    });
    const pickerSubline = computed(() => [
      picker.material, picker.vendor, picker.subtype,
      (picker.weight !== "" && picker.weight !== null && picker.weight !== undefined)
        ? picker.weight + " g" : "",
    ].filter(Boolean).join(" \u00b7 "));
    function spoolMatchesSlot(sp, material, colorHex) {
      const hex = (c) => String(c || "").replace("#", "").toLowerCase();
      const eq = (a, b) => String(a || "").toLowerCase() === String(b || "").toLowerCase();
      let score = 0;
      if (sp.material && material && eq(sp.material, material)) score += 2;
      if (sp.color && colorHex && hex(sp.color) === hex(colorHex)) score += 1;
      return score;
    }
    // Spools offered in the slot picker. Entries matching what the slot
    // declares (RFID tag or override) come FIRST and are marked - the ACE
    // tag says WHAT is in the slot but carries no serial, so it can narrow
    // the choice, never make it (two identical rolls are indistinguishable
    // to it). Already-bound spools stay listed with their slot, since
    // picking one here moves it.
    const spoolPickerOptions = computed(() => {
      const mat = picker.material;
      const col = picker.color;
      const rows = spoolList().filter(spoolWorldOk).map(sp => {
        const at = spoolSlotLabel(sp);
        const w = spoolWeightLabel(sp);
        const score = spoolMatchesSlot(sp, mat, col);
        const bg = sp.color ? "#" + String(sp.color).replace("#", "") : "";
        return {id: sp.id, score, bg, fg: bg ? textOn(bg) : "",
                // New design dropdown: the same entry split into parts.
                name: spoolTitle(sp), at,
                meta: [sp.material, w].filter(Boolean).join(" \u00b7 "),
                // The tick means FULLY matching (material AND colour): the
                // colour is visible in the row itself, the material is not,
                // so it marks the one thing the fill cannot show.
                label: (score === 3 ? "\u2713 " : "")
                       + `${spoolTitle(sp)}${w ? " \u00b7 " + w : ""}`
                       + (at ? ` (${at})` : "")};
      });
      // A spool bound to ANOTHER slot is not offered at all. Moving a roll
      // means taking it out first, and that frees it (the gate-empty
      // release) - so picking a bound one can only be a mis-pick that
      // would silently move the spool and leave its old slot unbound.
      // Not merely sorted down: an option that must
      // never be chosen has no business being choosable. THIS slot's own
      // spool stays, or the dropdown would open with nothing selected and
      // saving would clear the binding.
      const here = (picker.head !== null && picker.head !== undefined)
        ? ((state.spool_binding || {})[`h${picker.head}`] || "")
        : (spoolIdForSlot(picker.ace, picker.slot) || "");
      const pick = rows.filter(r => r.id === here
                                    || !spoolSlotKey((state.spools || {})[r.id] || {}));
      // Sort by match quality only. The old "free spools first" key changed
      // whenever a binding changed, so the state poll re-ordered the list
      // UNDER an open dropdown; match quality depends on the picker fields,
      // i.e. only on what the user themselves edits.
      return pick.sort((a, b) => b.score - a.score);
    });
    // Weight as the form shows it: rounded grams, or "" when the spool has
    // none (and for "no spool" at all).
    function _spoolWeightField(sp) {
      if (!sp || sp.weight_g === undefined || sp.weight_g === null) return "";
      return String(Math.round(sp.weight_g));
    }
    // The button next to the grams field, in the two states the row can be
    // in. Bound spool -> correct ITS weight (the scale-check, same as the
    // table's "g"). No spool -> create one for this slot from what the
    // dialog already shows, incl. the tag's #ID, and bind it: a spool the
    // printer just read is then one click from being tracked, instead of
    // retyping the id in the Spools tab.
    // What the '+' would create, previewed on hover: only for a slot with
    // a READ tag -
    // the preview reads from the exact sources the create uses (slot tag
    // sku via _pickerSlot, the grams field, the picker target), so it can
    // never diverge from the click. No tag (incl. the head picker - a
    // side feeder has no reader) -> the plain label stays.
    function spoolNewTitle() {
      if (picker.spool) return t("ui.spools.correct_weight");
      const base = t("ui.spools.new_from_slot");
      const sl = _pickerSlot();
      const sku = (sl && sl.rfid_data && sl.rfid_data.sku)
        ? _skuArg(sl.rfid_data.sku) : "";
      if (!sku) return base;
      const parts = [sku];
      if (picker.weight !== "") parts.push(picker.weight + " g");
      const isHead = picker.head !== null && picker.head !== undefined;
      parts.push(isHead ? `T${dispIdx(picker.head)}`
                        : `MMU ${dispIdx(picker.ace)} / ${dispIdx(picker.slot)}`);
      return base + " · " + parts.join(" · ");
    }
    async function spoolCreateFromPicker() {
      const isHead = picker.head !== null && picker.head !== undefined;
      if ((picker.ace === null || picker.ace === undefined) && !isHead) return;
      if (picker.spool) {
        const sp = (state.spools || {})[picker.spool];
        // Empty is "I don't know", not "0 g" - and the table has no way to
        // unset a weight, so an emptied field is simply not sent. Otherwise
        // only on a real change: re-sending an unchanged weight would
        // rewrite the table (and roll its backup) for nothing.
        if (sp && picker.weight !== ""
            && String(picker.weight) !== _spoolWeightField(sp)) {
          await spoolMacro("ACE_SPOOL_SET",
                           {ID: picker.spool, WEIGHT: picker.weight});
        }
        return;
      }
      const sl = _pickerSlot();
      const args = {MATERIAL: picker.material || "PLA",
                    VENDOR: picker.vendor || "Generic",
                    SUBTYPE: picker.subtype || "Basic",
                    COLOR: (picker.color || "").replace("#", "")};
      // Bind target: the slot, or - from the head picker - the head itself
      // (feeder/manual; ACE_SPOOL_ADD HEAD= binds via the h<n> key).
      if (isHead) args.HEAD = picker.head;
      else { args.ACE = picker.ace; args.SLOT = picker.slot; }
      // The code comes off the INSERTED spool's tag, so this is exactly where
      // a second spool of the same article collides. Ask before sending;
      // cancel means no entry is created at all.
      // A UID-only read (MIFARE, no identity) has no rfid_data but a uid -
      // that is the per-chip key the new spool must carry to bind next time.
      const tagCode = (sl && sl.rfid_data && sl.rfid_data.sku
                       ? sl.rfid_data.sku : (sl && sl.uid ? sl.uid : ""));
      // A slot spool without any tag code can never bind by itself: say so
      // once, before it is created.
      if (!isHead && !tagCode) {
        const go = await new Promise(resolve => confirm({
          title: t("ui.spools.no_code_title"),
          message: t("ui.spools.no_code_msg"),
          okLabel: t("ui.spools.add"),
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        }));
        if (!go) return;
      }
      const sku = await askFreeSku(tagCode);
      if (sku === null) return;
      if (sku) args.SKU = sku;
      // Empty stays empty: no invented default, a wrong start weight
      // mis-tracks the whole spool while looking plausible.
      if (picker.weight !== "") args.WEIGHT = picker.weight;
      await spoolMacro("ACE_SPOOL_ADD", args);
      await reloadState();
      // ACE_SPOOL_ADD bound it server-side; mirror that into the dialog so
      // savePicker sees no change and does not send a redundant assign.
      // Guarded, or the spool watcher would clear the weight we just set.
      _pickerOpening = true;
      picker.spool = (isHead
        ? ((state.spool_binding || {})[`h${picker.head}`] || "")
        : (spoolIdForSlot(picker.ace, picker.slot) || ""));
      nextTick(() => { _pickerOpening = false; });
    }
    // Write the picked identity onto the physical tag in this ACE slot as
    // OpenSpool (rotates the lane to park the tag, writes, restores). Slot
    // target only (a head has no ACE tag); needs ACE2-Open firmware + a spool at
    // the gate. Always confirms - it physically moves the lane and
    // overwrites the tag. The engine's guards (idle, V2, gate, MIFARE
    // refusal, verify) are the real safety; this only gates the obvious.
    async function tagWrite() {
      const isHead = picker.head !== null && picker.head !== undefined;
      if (isHead || picker.ace === null || picker.ace === undefined) return;
      const material = (picker.material || "").trim();
      if (!material) return;
      if (!window.confirm(t("ui.dialog.write_tag_confirm",
              {ace: dispIdx(picker.ace), slot: dispIdx(picker.slot),
               material: material}))) return;
      const fmt = tagWriteFormat();
      const args = {ACE: picker.ace, SLOT: picker.slot, MATERIAL: material,
                    COLOR: (picker.color || "").replace("#", ""),
                    FORMAT: fmt};
      if (picker.vendor) args.BRAND = picker.vendor;
      // Anycubic layout carries the sub-type in its type string; UID_SKU
      // decides whether the card UID or the spool's own sku is written.
      if (fmt === "anycubic") {
        if (picker.subtype && picker.subtype !== "Basic")
          args.SUBTYPE = picker.subtype;
        args.UID_SKU = tagWriteUidSku() ? 1 : 0;
      }
      await spoolMacro("ACE_TAG_WRITE", args);
    }
    // Manual tag READ from the picker (a Read button next to Write so a
    // missed insert read can be retried from the web).
    // Same command the console uses; outcome lands in the picker bar.
    async function tagRead() {
      const isHead = picker.head !== null && picker.head !== undefined;
      if (isHead || picker.ace === null || picker.ace === undefined) return;
      await spoolMacro("ACE_TAG_READ", {ACE: picker.ace, SLOT: picker.slot});
    }
    // Outcome of the last tag WRITE or READ, shown in the picker bar for a
    // while after the op (success in the bar, not as an
    // alert). seq from the engine ties the result to its op; a result is
    // shown once (until dismissed by the timer or the picker closing).
    const _tagWriteSeen = ref(0);
    const tagWriteOutcome = computed(() => {
      const r = state.tag_op && state.tag_op.result;
      if (!r || (r.kind !== "write" && r.kind !== "read") || state.tag_op.busy) return null;
      if (r.seq <= _tagWriteSeen.value) return null;
      return r;
    });
    watch(() => tagWriteOutcome.value && tagWriteOutcome.value.seq, (seq) => {
      if (!seq) return;
      setTimeout(() => { if (_tagWriteSeen.value < seq) _tagWriteSeen.value = seq; },
                 12000);
    });
    function dismissTagWriteOutcome() {
      const r = state.tag_op && state.tag_op.result;
      if (r && r.seq > _tagWriteSeen.value) _tagWriteSeen.value = r.seq;
    }
    // Effective write format: the picker's choice, else [ace]
    // tag_write_format from the printer.
    function tagWriteFormat() {
      return picker.tagFormat || state.tag_write_format || "openspool";
    }
    function tagWriteUidSku() {
      return (picker.tagUidSku === null || picker.tagUidSku === undefined)
        ? !!state.tag_write_uid_sku : !!picker.tagUidSku;
    }
    // Config tab: the tag write defaults (write-through on the printer).
    async function setTagWrite(args) {
      await spoolMacro("ACE_SET_TAG_WRITE", args);   // reports refusals
    }
    function spoolSlotKey(sp) {
      return Object.keys(state.spool_binding || {})
        .find(k => state.spool_binding[k] === sp.id) || "";
    }
    // Filament decidedly AT the gate - the mirror of the engine's occupied
    // test in ACE_SPOOL_ASSIGN (gate == AVAILABLE). 'unknown' (unit not
    // reporting yet) must NOT count as occupied: the engine would allow
    // the move, so the UI may not be stricter than its backstop.
    function slotOccupied(aceIdx, slotIdx) {
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const sl = a && (a.slots || []).find(s => s.idx === slotIdx);
      return !!(sl && sl.state && sl.state !== 'empty'
                && sl.state !== 'unknown');
    }
    // The engine refuses to move a spool OUT of a physically occupied slot
    // (spool_bound_elsewhere). Mirrored into the dropdown so the red
    // message becomes unreachable from the web: while the row's spool
    // sits occupied, every OTHER target is greyed. Clearing stays
    // possible, and physically taking the roll out is what frees a move.
    // A head binding has no gate; the engine allows that move, so no lock.
    function spoolMoveLocked(sp, key) {
      const cur = spoolSlotKey(sp);
      if (!cur || key === cur) return false;
      const m = /^(\d+)_(\d+)$/.exec(cur);
      if (!m) return false;
      return slotOccupied(Number(m[1]), Number(m[2]));
    }
    // Assign straight from the row's dropdown. An empty pick clears the
    // binding (spool taken out); picking a slot that holds another spool
    // replaces it there - one spool per slot, the engine does the same.
    function spoolAssignTo(sp, key) {
      if (!key) {
        const cur = spoolSlotKey(sp);
        if (!cur) return;
        if (cur.startsWith("h")) {
          spoolMacro("ACE_SPOOL_ASSIGN", {HEAD: Number(cur.slice(1))});
          return;
        }
        const [a, sl] = cur.split("_").map(Number);
        spoolMacro("ACE_SPOOL_ASSIGN", {ACE: a, SLOT: sl});
        return;
      }
      if (key.startsWith("h")) {
        // Head binding (feeder/manual). Identity adoption goes through the
        // head's print_task_config - the same push the head picker saves
        // with - because a feeder head HAS no slot to override.
        const h = Number(key.slice(1));
        spoolMacro("ACE_SPOOL_ASSIGN", {HEAD: h, ID: sp.id});
        if (sp.material) {
          const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
          const hex = String(sp.color || "ffffff").replace("#", "");
          enqueue("SET_PRINT_FILAMENT_CONFIG", {
            CONFIG_EXTRUDER:     h,
            FILAMENT_TYPE:       dq(sp.material),
            FILAMENT_COLOR_RGBA: hex.toUpperCase() + "FF",
            VENDOR:              dq(sp.vendor || "Generic"),
            FILAMENT_SUBTYPE:    dq(sp.subtype || ""),
          });
        }
        return;
      }
      const [a, sl] = key.split("_").map(Number);
      spoolMacro("ACE_SPOOL_ASSIGN", {ACE: a, SLOT: sl, ID: sp.id});
      // ... and adopt the spool's identity for that slot, same as the
      // picker does - otherwise the slot would still show whatever was
      // declared before.
      if (sp.material) {
        fetch(`${API}/slot-override`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            ace: a, slot: sl,
            material: sp.material || "",
            color: "#" + String(sp.color || "000000").replace("#", ""),
            vendor: sp.vendor || "",
            subtype: sp.subtype || "",
          }),
        }).then(() => reloadState()).catch(() => {});
      }
    }
    function spoolUnassign(sp) {
      spoolAssignTo(sp, "");
    }
    function spoolDelete(sp) {
      confirm({
        title: t('ui.spools.delete_title'),
        message: tHtml('ui.spools.delete_msg', {name: spoolTitle(sp)}),
        okLabel: t('ui.common.delete'),
        onOk: () => spoolMacro("ACE_SPOOL_DELETE", {ID: sp.id, FORCE: 1}),
      });
    }
    function spoolExport() {
      window.open(`${API}/spools/export`, "_blank");
    }
    // Orca printer profile with virtual tools (T4.. = ACE/slot). Built on
    // the backend from the live state; the browser just downloads it.
    function orcaProfile() {
      window.open(`${API}/orca/profile`, "_blank");
    }
    // Virtual tools 4.. with their current ACE/slot target (tool_map entry
    // or the formula), for the Config-tab list. Display numbers 1-based.
    function virtualTools() {
      const count = Math.max(1, Math.min(8, Number(state.device_count || 1)));
      const out = [];
      // Tools 0-3 are the heads; tool n >= 4 = ACE (n-4)/4, slot (n-4)%4.
      for (let n = 4; n < Math.min(32, 4 + 4 * count); n++) {
        const ent = state.tool_map && state.tool_map[String(n)];
        const mapped = Array.isArray(ent) && ent.length === 2;
        const ace = mapped ? Number(ent[0]) : Math.floor((n - 4) / 4);
        const slot = mapped ? Number(ent[1]) : (n - 4) % 4;
        const a = (state.aces || [])[ace];
        const sl = a && a.slots ? a.slots[slot] : null;
        out.push({tool: n, ace, slot, mapped,
                  color: sl && sl.color ? sl.color : null,
                  material: sl ? (sl.material || sl.type || "") : ""});
      }
      return out;
    }
    // The same list in blocks of four (filaments 5-8, 9-12, ...) for the
    // 2x2 layout. No ACE label on a block: an entry can point elsewhere.
    function virtualToolGroups() {
      const groups = [];
      for (const vt of virtualTools()) {
        const g = Math.floor((vt.tool - 4) / 4);
        if (!groups[g]) groups[g] = {ace: g, tools: []};
        groups[g].tools.push(vt);
      }
      return groups.filter(Boolean);
    }
    // Approximate colour name for the tool-map list, from hue and lightness
    // ("dark purple", "light blue"). Slots only store a hex value, so this
    // is a reading aid next to the colour itself, not an identity.
    function colorName(hex) {
      const h6 = _hex6(hex);
      if (h6.length !== 7) return "";
      const [r, g, b] = [1, 3, 5].map(i => parseInt(h6.slice(i, i + 2), 16) / 255);
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      const l = (max + min) / 2;
      const d = max - min;
      const sat = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
      let hue = 0;
      if (d) {
        if (max === r) hue = 60 * (((g - b) / d) % 6);
        else if (max === g) hue = 60 * ((b - r) / d + 2);
        else hue = 60 * ((r - g) / d + 4);
        if (hue < 0) hue += 360;
      }
      const name = (k) => t("ui.config.orca_color." + k);
      if (l < 0.1) return name("black");
      if (l > 0.93) return name("white");
      if (sat < 0.15) {
        if (l < 0.35) return t("ui.config.orca_color.dark", {c: name("gray")});
        if (l > 0.7) return t("ui.config.orca_color.light", {c: name("gray")});
        return name("gray");
      }
      let key;
      if (hue < 15 || hue >= 345) key = "red";
      else if (hue < 40) key = l < 0.35 ? "brown" : "orange";
      else if (hue < 68) key = l < 0.3 ? "brown" : "yellow";
      else if (hue < 165) key = "green";
      else if (hue < 195) key = "turquoise";
      else if (hue < 255) key = "blue";
      else if (hue < 290) key = "purple";
      else key = "pink";
      // Pale warm tones read as beige, pale reds as pink, not "light red".
      if ((key === "orange" || key === "yellow") && l > 0.72 && sat < 0.6) key = "beige";
      if (key === "red" && l > 0.65) key = "pink";
      if (key === "brown" || key === "beige") return name(key);
      if (l < 0.3) return t("ui.config.orca_color.dark", {c: name(key)});
      if (l > 0.72 && key !== "pink") return t("ui.config.orca_color.light", {c: name(key)});
      return name(key);
    }
    // Every slot of every ACE as one target list: "ACE 1 / 2: PLA red".
    function toolTargetOptions(vt) {
      const out = [];
      const count = Math.max(1, Math.min(8, Number(state.device_count || 1)));
      for (let a = 0; a < count; a++) {
        const ace = (state.aces || [])[a];
        for (let sl = 0; sl < 4; sl++) {
          const slot = ace && ace.slots ? ace.slots[sl] : null;
          const empty = !slot || slot.state === "empty";
          const mat = slot ? (slot.material || slot.type || "") : "";
          const color = !empty && slot.color ? slot.color : null;
          const desc = empty ? t("ui.common.empty")
            : [mat, color ? colorName(color) : ""].filter(Boolean).join(" ") || "-";
          out.push({value: a + ":" + sl, color,
                    label: `MMU ${a + 1} / ${sl + 1}: ${desc}`});
        }
      }
      if (vt && !out.some(o => o.value === vt.ace + ":" + vt.slot)) {
        out.push({value: vt.ace + ":" + vt.slot, color: null,
                  label: `MMU ${vt.ace + 1} / ${vt.slot + 1}`});
      }
      return out;
    }
    function setToolMapTarget(tool, value) {
      const [ace, slot] = String(value).split(":").map(Number);
      return setToolMap(tool, ace, slot);
    }
    // Both go through headSet: a refusal (e.g. while printing) lands in
    // the macro log instead of vanishing, and the state reloads after.
    function setToolMap(tool, ace, slot) {
      return headSet("macro",
        {name: "ACE_SET_TOOL", args: {TOOL: tool, ACE: Number(ace), SLOT: Number(slot)}},
        `T${tool}`);
    }
    function resetToolMap(tool) {
      return headSet("macro", {name: "ACE_SET_TOOL", args: {TOOL: tool, RESET: 1}},
                     `T${tool}`);
    }
    async function spoolImport(fileList) {
      const f = fileList && fileList[0];
      if (spoolFileInput.value) spoolFileInput.value.value = "";
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const r = await fetch(`${API}/spools/import?mode=${spoolImportMode.value}`,
                              {method: "POST", body: fd});
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
      } catch (e) {
        confirm({title: t('ui.spools.title'), message: escapeHtml(e),
                 dismissOnly: true, okLabel: "OK", onOk: () => {}});
      }
      reloadState();
    }
    async function setQuadReplenish(enable) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_QUAD_REPLENISH",
                                args: {ENABLE: enable ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Order vs stock's head-twin replenish: off = stock switches heads
    // first (instant), on = the printing head drains its own lane first
    // (reaches every spool, one reload pause each). ENABLE is re-sent
    // unchanged - the command takes both in one call.
    // The CHECKBOX is worded as the deviation ("switch heads first"), the
    // engine flag as quad_first - so the template inverts, not this. Keeping
    // the stored name means no config/save-variable migration: an [ace]
    // quad_first line that is no longer read would halt Klipper.
    // D11 Devices: assignment of a unit (by its key) to a place, removal
    // ("none") and forgetting a disconnected key. The engine applies the
    // table at the next firmware restart and says so (units_restart_needed).
    // A refused command reaches the alerts strip through Klipper's own
    // error path; the tab shows nothing extra (it used to duplicate it).
    async function assignUnit(key, place) {
      if (!key) return;
      const p = (place === "" || place === null || place === undefined)
        ? "NONE" : String(Number(place) + 1);
      await unitMacro("ACE_UNIT_ASSIGN", {KEY: key, PLACE: p});
    }
    function forgetUnit(dev) {
      confirm({
        title: t("ui.config.dev_forget"),
        message: tHtml("ui.config.dev_forget_confirm", {key: dev.key}),
        okLabel: t("ui.config.dev_forget"),
        onOk: () => unitMacro("ACE_UNIT_FORGET", {KEY: dev.key}),
      });
    }
    // Rename a unit: the name belongs to its key, the engine takes the
    // place index. Klipper splits gcode parameters on blanks, so blanks
    // become underscores.
    // The name field is bound to POLLED printer state (devices arrive every
    // second), so the typed text needs an edit buffer or the next poll
    // overwrites it. Buffer wins while editing; released when the printer
    // reports the committed name back (applyState), on an unchanged commit,
    // or when the rename failed (the error box explains, the old name shows).
    const devNameEdit = reactive({});
    function devNameClean(name) {
      return String(name || "").trim().replace(/\s+/g, "_").slice(0, 32);
    }
    function devNameValue(dev) {
      const k = dev && dev.key;
      return (k in devNameEdit) ? devNameEdit[k] : (dev.name || "");
    }
    function devNameInput(dev, ev) {
      if (dev && dev.key) devNameEdit[dev.key] = ev.target.value;
    }
    async function devNameCommit(dev, ev) {
      if (!dev || !dev.key) return;
      const clean = devNameClean(ev.target.value);
      if (clean === (dev.name || "")) { delete devNameEdit[dev.key]; return; }
      devNameEdit[dev.key] = clean;
      const ok = await renameUnit(dev, clean);
      if (!ok) delete devNameEdit[dev.key];
    }
    function devNameSettle(devices) {
      for (const d of devices || []) {
        if (d && d.key in devNameEdit && (d.name || "") === devNameEdit[d.key]) {
          delete devNameEdit[d.key];
        }
      }
    }
    // Locate: the engine blinks an ACE 2 or jogs a slot on an ACE Pro.
    function locateUnit(dev) {
      if (!dev || dev.place === null || dev.place === undefined) return;
      return unitMacro("ACE_LOCATE", {ACE: dev.place});
    }
    async function renameUnit(dev, name) {
      if (!dev || dev.place === null || dev.place === undefined) return false;
      return unitMacro("ACE_UNIT_NAME", {ACE: dev.place, NAME: devNameClean(name)});
    }
    async function unitMacro(name, args) {
      let ok = true;
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, args}),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(macroErrorText(j.detail || `${r.status}`));
      } catch (e) {
        console.warn(`${name}: ${macroErrorText(e.message || String(e))}`);
        ok = false;
      }
      reloadState();
      return ok;
    }
    // Driver switches: the engine refuses the last active driver and a
    // driver whose unit feeds a loaded head (reason in the alerts strip);
    // the checkbox only shows what the printer reports back.
    function driverOnlyActive(drv) {
      return drv.enabled
        && !(state.driver_switches || []).some(d => d.name !== drv.name && d.enabled);
    }
    function driverToggle(drv, ev) {
      ev.target.checked = !!drv.enabled;
      return unitMacro("ACE_SET_DRIVER", {NAME: drv.name, ENABLE: drv.enabled ? 0 : 1});
    }
    function driverNote(drv) {
      const parts = [];
      if (drv.enabled !== drv.active) {
        parts.push(t(drv.enabled ? "ui.config.driver_pending_on" : "ui.config.driver_pending_off"));
      }
      if (drv.places && drv.places.length) {
        parts.push(t("ui.config.driver_serves",
                     {list: drv.places.map(p => dispIdx(p)).join(", ")}));
      }
      if (!drv.tested) parts.push(t("ui.config.driver_untested"));
      return parts.join(" · ");
    }
    const driversRestartNeeded = computed(() =>
      (state.driver_switches || []).some(d => d.enabled !== d.active));
    function deviceStatus(dev) {
      if (dev.removed) return t("ui.config.dev_removed");
      if (!dev.present) return t("ui.config.dev_not_connected");
      return dev.connected ? t("ui.config.dev_connected") : t("ui.config.dev_present");
    }
    function deviceDriver(dev) {
      return unitDriverLabel({kind: dev.kind});
    }
    // Places in the unit table (for the per-place [ace N] cards): the
    // table decides, the old ace_device_count only on an engine without it.
    function perAceCount() {
      const places = (state.devices || []).filter(d => d.place !== null && d.place !== undefined);
      if (places.length) return Math.min(4, Math.max(...places.map(d => d.place)) + 1);
      return Math.max(1, Math.min(4, configForm.ace_device_count | 0));
    }
    async function setQuadFirst(first) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_QUAD_REPLENISH",
                                args: {ENABLE: state.quad_replenish ? 1 : 0,
                                       FIRST: first ? 1 : 0}}),
        });
      } catch (_) {}
      reloadState();
    }
    // Per-pair purge stamps of the preflight: off = the engine ignores the
    // LENGTH stamps and purges the fixed length again, also on files that
    // already carry them. Write-through setter - live, no Save/restart.
    async function setPurgeMatrix(enable) {
      // Reports the outcome: the engine writes the ace.cfg line itself and
      // says so loudly when that write fails ("applied for this session
      // only"). Swallowing it left the user with a setting that looked
      // saved and was gone after the restart.
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_PURGE",
                                args: {MATRIX: enable ? 1 : 0}}),
        });
        const j = await r.json().catch(() => null);
        if (!r.ok) throw new Error(j?.detail || `HTTP ${r.status}`);
        const res = String(j?.result || "");
        if (/WARNING|failed/i.test(res)) setMacroLog(res);
      } catch (e) {
        setMacroLog(String(e?.message || e));
      }
      await reloadState();
    }
    async function setHeadAce(idx, ace) {
      await headSet("head-ace", {head: idx, ace: Number(ace)},
                    t("ui.dashboard.head_ace"));
    }
    // head mode: connected ACEs as {value,label} for the per-head ACE dropdown.
    const aceOptions = computed(() =>
      (state.aces || []).map(a => ({
        value: a.idx,
        label: "MMU " + dispIdx(a.idx) + (unitDriverLabel(a) ? " (" + unitDriverLabel(a) + ")" : ""),
      })));
    // head mode: the ACE currently wired to a head (head_ace), defaulting to the
    // head index.
    // Tooltip of the "(V2)" marker on an ACE card: model and firmware
    // version, whichever the unit reported. Keeping the version out of the
    // visible text is what gives the header room -
    // 'Unknown' is the handshake's placeholder and is not worth showing.
    // Community-firmware badge tooltip ('ace2_open' | 'gen1_cfw').
    function aceCfwTitle(ace) {
      const key = ace.community_fw === "gen1_cfw" ? "ui.common.cfw_gen1" : "ui.common.cfw_ace2_open";
      return [t(key), ace.firmware].filter(Boolean).join(" · ");
    }
    // Driver label behind the place number (D11): the registry name of the
    // codec that drives this unit, never the place. 'v1'/'v2' are the codec
    // names ace.py reports as kind/protocol.
    const DRIVER_LABELS = {v1: "AceV1", v2: "AceV2", cfs: "CFS Nano", ace2k: "Ace2k"};
    function unitDriverLabel(ace) {
      const k = String((ace && (ace.kind || ace.protocol)) || "");
      if (!k) return "";
      return DRIVER_LABELS[k] || k;
    }
    function aceProtoTitle(ace) {
      return [ace.model,
              (ace.firmware && ace.firmware !== "Unknown") ? ace.firmware : ""]
        .filter(Boolean).join(" · ");
    }
    function headAceOf(idx) {
      const ha = state.head_ace || {};
      const a = ha[idx] ?? ha[String(idx)];
      return (a === undefined || a === null) ? idx : Number(a);
    }
    // head mode: ACE options for one head's dropdown. Every connected ACE is
    // listed; one wired to ANOTHER ACE head stays selectable and says so
    // (ACE_SET_HEAD_ACE swaps the two wirings), unless that head is loaded,
    // then the option is disabled (the engine refuses that case).
    function aceOptionsForHead(idx) {
      const mine = headAceOf(idx);
      const holder = {};
      for (const h of (state.ace_heads || [])) {
        if (Number(h) === Number(idx)) continue;
        holder[headAceOf(h)] = Number(h);
      }
      return aceOptions.value.map(o => {
        if (o.value === mine || holder[o.value] === undefined) return o;
        const h = holder[o.value];
        const th = (state.toolheads || []).find(tt => tt.idx === h);
        const loaded = !!(th && (th.head_source_known || th.filament_at_extruder));
        const note = loaded
          ? t("ui.dashboard.head_ace_holder_loaded", {head: dispIdx(h)})
          : t("ui.dashboard.head_ace_swap_with", {head: dispIdx(h)});
        return {value: o.value, label: `${o.label} - ${note}`, disabled: loaded};
      });
    }
    // Select twin of headToggle: put the DOM back on the current value at
    // once; the state moves it when the engine accepted the change.
    function headSelect(ev, current, fn) {
      const wanted = ev.target.value;
      ev.target.value = String(current);
      return fn(wanted);
    }
    // head mode: true when every wired ACE head is a right-side head (internal
    // index >= 2, display 3/4) -> right-align the ACE grid so the cards start
    // from the right, lining up with the right toolheads.
    const aceHeadsRightSide = computed(() => {
      const h = state.ace_heads || [];
      return h.length > 0 && h.every(x => Number(x) >= 2);
    });
    // The ACE cards to render. In head mode: only ACEs wired to an ACE head
    // (unused ones hidden), ordered by their head index so each ACE card lines
    // up with its toolhead (T0..T3 left to right). Other modes: all ACEs as-is.
    const visibleAces = computed(() => {
      const aces = state.aces || [];
      if (state.mode !== "head") return aces;
      const byIdx = {};
      for (const a of aces) byIdx[a.idx] = a;
      const out = [];
      const seen = new Set();
      for (const h of [...(state.ace_heads || [])].sort((a, b) => a - b)) {
        const ai = headAceOf(h);
        if (byIdx[ai] && !seen.has(ai)) { out.push(byIdx[ai]); seen.add(ai); }
      }
      return out;
    });
    // Head mode hides ACEs no head is wired to. A connected one would then
    // simply not exist on the dashboard, so it is shown after the wired
    // ones, dimmed and inert (_unwired).
    const unwiredAces = computed(() => {
      if (state.mode !== "head") return [];
      const shown = new Set(visibleAces.value.map(a => a.idx));
      return (state.aces || []).filter(a => a.connected !== false
                                           && !shown.has(a.idx));
    });
    const gridAces = computed(() => visibleAces.value.concat(
      unwiredAces.value.map(a => Object.assign({}, a, {_unwired: true}))));
    function loadSlot(aceIdx, slotIdx) {
      if (_blockIfPrinting()) return;
      // Before anything is enqueued: an empty source slot cannot load, and
      // the unload half would otherwise run and strand the head.
      if (slotIsEmpty(aceIdx, slotIdx)) {
        setMacroLog(t("ui.dashboard.slot_empty_hint",
                      {ace: dispIdx(aceIdx), slot: dispIdx(slotIdx)}));
        return;
      }
      if (state.mode === "head") {
        // head mode: each ACE head is wired to exactly one ACE (head_ace), so
        // this ACE's slots all feed the head whose head_ace points here. Loading
        // a slot loads that head from this slot (swap if already loaded).
        const h = aceHeadForAce(aceIdx);
        if (h === null) return;
        // Confirm AFTER resolving the head, so the question names the head
        // that will actually move - in head mode that is not the slot index.
        if (!_confirmCmd("ui.confirm.load_slot", {head: dispIdx(h),
                ace: dispIdx(aceIdx), slot: dispIdx(slotIdx)})) return;
        const th = state.toolheads.find(tt => tt.idx === h);
        // SAME ace/slot with the toolhead sensor CLEAR needs NO unload
        // cycle - nothing is at the head, ACE_LOAD_HEAD just (re)runs the
        // feed (its already-loaded guard reads this same eN sensor). That
        // covers BOTH: a failed load (head_source kept with
        // load_failed=true) and the mid-print runout reload (head_source
        // kept for the FA-rearm on resume). A
        // different slot still unloads first (two filaments must not share
        // the path), and a head whose sensor reads filament (no-flow
        // class / really loaded) still unloads first too.
        const directLoad = !!(th && !th.filament_at_extruder
          && th.ace === aceIdx && th.slot === slotIdx);
        if (th && th.head_source_known && !directLoad) {
          enqueue("ACE_UNLOAD_HEAD", {HEAD: h});
        }
        enqueue("ACE_LOAD_HEAD", {HEAD: h, ACE: aceIdx, SLOT: slotIdx});
        return;
      }
      // multi: slot N feeds head N.
      if (slotHeadHasNoAce(slotIdx)) {
        setMacroLog(t("ui.dashboard.slot_head_no_ace",
                      {head: dispIdx(slotIdx)}));
        return;
      }
      if (!_confirmCmd("ui.confirm.load_slot", {head: dispIdx(slotIdx),
              ace: dispIdx(aceIdx), slot: dispIdx(slotIdx)})) return;
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (th && th.head_source_known && th.ace !== aceIdx) {
        enqueue("ACE_UNLOAD_HEAD", {HEAD: slotIdx});
        enqueue("ACE_LOAD_HEAD",   {HEAD: slotIdx, ACE: aceIdx});
        return;
      }
      enqueue("ACE_LOAD_HEAD", {HEAD: slotIdx, ACE: aceIdx});
    }
    // Load a head without ACE (feeder, or manual in multi) via its native
    // stock side feeder.
    function loadFeederHead(h) {
      if (_blockIfPrinting()) return;
      if (!_confirmCmd("ui.confirm.load_head", {head: dispIdx(h)})) return;
      enqueue("ACE_LOAD_HEAD", {HEAD: h});
    }
    // head mode: the ACE head wired to this ACE (head_ace reverse lookup), or
    // null if no ACE head uses it.
    // RC522 tag read/write need the ACE2-Open firmware on THAT unit; the
    // picker hides both otherwise.
    function aceOpenFw(aceIdx) {
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      return !!(a && a.open_fw);
    }
    // Tag READ on that unit: ACE2-Open or an ACE Pro with the Gen-1 tunnel.
    function aceTagRead(aceIdx) {
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      return !!(a && (a.open_fw || a.tag_read));
    }
    function aceHeadForAce(aceIdx) {
      const heads = state.ace_heads || [];
      const ha = state.head_ace || {};
      for (const h of heads) {
        const a = Number(ha[h] ?? ha[String(h)] ?? h);
        if (a === aceIdx) return h;
      }
      return null;
    }
    // head mode: is this ACE slot the one currently loaded into its ACE head?
    // (used to disable that slot's Load button; other slots stay loadable=swap).
    function slotLoadedInHead(aceIdx, slotIdx) {
      if (state.mode !== "head") return false;
      const h = aceHeadForAce(aceIdx);
      if (h === null) return false;
      const th = state.toolheads.find(tt => tt.idx === h);
      // load_failed: the slot is NOT actually loaded (failed load keeps
      // head_source for the retry) - keep its Load button usable as the
      // one-click retry. Same for a head whose toolhead sensor reads CLEAR
      // (manual extraction: bowden off, lever, pull, cut - a no-flow
      // recovery): head_source alone is bookkeeping, not filament. Only an
      // explicit sensor False counts (None/undefined = module offline ->
      // stay conservative, keep disabled). toolheadOps keeps the button
      // disabled while a load/unload is still RUNNING on that head (the
      // sensor clears ~30s before an unload finishes - without this the
      // button re-enabled mid-op).
      return !!(th && th.head_source_known && !th.load_failed
                && (th.filament_at_extruder !== false
                    || !!toolheadOps.value[th.idx])
                && th.ace === aceIdx && th.slot === slotIdx);
    }
    // ---- per-material tip forming ([ace_tipform]) editor ---------------
    // Server truth = the cfg section; state.tipform = what Klipper RUNS.
    // The two differ until a restart -> tipformRestartPending banner.
    const tipform = reactive({
      supported: false, mode: "stock", rows: [],
      cfgMode: "stock", cfgNames: [],
      error: "", savedMsg: "",
    });
    // A section key is '<material>' or '<vendor>_<material>' (join '_', see
    // ace_tipform.table_for). Split back for the editor: the material is the
    // LAST '_'-segment (DB materials are single tokens / hyphenated, never
    // underscored - pla, petg, pla-cf), the vendor is everything before,
    // with '_' shown as spaces for readability. 'default'/'soft' and any
    // plain material have no '_' -> vendor ''.
    function tipformSplitKey(key) {
      const k = String(key || "");
      const i = k.lastIndexOf("_");
      if (i < 0) return {material: k, vendor: ""};
      return {vendor: k.slice(0, i).replace(/_/g, " "), material: k.slice(i + 1)};
    }
    // Compose the section key from a row: vendor collapsed to lower_snake,
    // '<vendor>_<material>' or '<material>'. Mirrors ace_tipform.table_for
    // so the editor's key and the engine's constructed lookup key match.
    function tipformComposeKey(material, vendor) {
      const mat = (material || "").trim().toLowerCase();
      const ven = (vendor || "").trim().toLowerCase().split(/\s+/).filter(Boolean).join("_");
      if (!mat) return "";
      return ven ? `${ven}_${mat}` : mat;
    }
    // --- Visual token builder (web-only convenience; the raw string stays
    // the source of truth, this just parses/serializes it). Step = one token
    // {type, a, b}: move -> a@b, others -> type:a. Mirrors the token grammar
    // in ace_tipform.parse_table; the backend parse_table remains the strict
    // gate on save, so a half-built table can never reach Klipper.
    const TIPFORM_STEP_TYPES = ["move", "pause", "temp", "waittemp", "fan", "unloadtemp", "loadtemp", "bgtemp", "prime", "primetemp"];
    function tipformParseSteps(tableStr) {
      const steps = [];
      for (let part of String(tableStr || "").split(",")) {
        part = part.trim();
        if (!part) continue;
        const low = part.toLowerCase();
        let m;
        if ((m = /^(pause|waittemp|unloadtemp|loadtemp|bgtemp|primetemp|prime|temp|fan)\s*:\s*(.*)$/.exec(low))) {
          steps.push({type: m[1], a: m[2].trim(), b: ""});
        } else if (part.includes("@")) {
          const [mm, f] = part.split("@");
          steps.push({type: "move", a: mm.trim(), b: (f || "").trim()});
        } else {
          steps.push({type: "move", a: part, b: ""});
        }
      }
      return steps;
    }
    function tipformStepToToken(s) {
      return s.type === "move" ? `${s.a}@${s.b}` : `${s.type}:${s.a}`;
    }
    function tipformStepsToTable(row) {
      row.table = (row._steps || []).map(tipformStepToToken).join(", ");
    }
    function tipformToggleBuilder(row) {
      row._builderOpen = !row._builderOpen;
      if (row._builderOpen) row._steps = tipformParseSteps(row.table);
    }
    function tipformAddStep(row) {
      if (!row._steps) row._steps = [];
      row._steps.push({type: "move", a: "", b: ""});
      tipformStepsToTable(row);
    }
    function tipformRemoveStep(row, i) {
      row._steps.splice(i, 1);
      tipformStepsToTable(row);
    }
    function tipformStepPlaceholder(step) {
      if (step.type === "move") return "mm";
      if (step.type === "pause") return "ms";
      if (step.type === "fan") return "0-255";
      return "°C";
    }
    // The firmware's stock pull (CONTROL_RETRACT_ACTION), as a starting point
    // to tune from - matches the reference in the [ace_tipform] cfg comment.
    const TIPFORM_STOCK = "57@400, 3@1500, -27@2700, -5.5@40, -37.5@1500";
    function tipformInsertStock(row) {
      row.table = TIPFORM_STOCK;
      if (row._builderOpen) row._steps = tipformParseSteps(row.table);
    }
    function _tipformNewRow(material, vendor, table) {
      return {material: material || "", vendor: vendor || "", table: table || "",
              _builderOpen: false, _steps: []};
    }
    async function loadTipform() {
      try {
        const r = await fetch(`${API}/tipform`);
        if (!r.ok) { tipform.supported = false; return; }
        const j = await r.json();
        tipform.supported = !!j.supported;
        tipform.mode = j.mode || "stock";
        tipform.rows = Object.entries(j.tables || {})
          .map(([name, table]) => {
            const s = tipformSplitKey(name);
            return _tipformNewRow(s.material, s.vendor, table);
          });
        tipform.cfgMode = tipform.mode;
        tipform.cfgNames = Object.keys(j.tables || {});
        tipform.error = "";
      } catch (_) { tipform.supported = false; }
    }
    function tipformAddRow() { tipform.rows.push(_tipformNewRow()); }
    function tipformRemoveRow(i) { tipform.rows.splice(i, 1); }
    async function saveTipform() {
      tipform.error = ""; tipform.savedMsg = "";
      const tables = {};
      for (const row of tipform.rows) {
        const tbl = (row.table || "").trim();
        const key = tipformComposeKey(row.material, row.vendor);
        if (!key && !tbl) continue;
        if (!key) { tipform.error = t("ui.config.tipform_err_name"); return; }
        tables[key] = tbl;
      }
      try {
        const resp = await fetch(`${API}/tipform`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          // Never restarts Klipper - the "restart pending" line tells the
        // user what is still missing, and Fluidd can do the restart
        // (same policy as the config save).
        body: JSON.stringify({mode: tipform.mode, tables,
                                restart_klipper: false}),
        });
        const j = await resp.json();
        if (!resp.ok || j.detail) {
          tipform.error = String(j.detail || `HTTP ${resp.status}`);
          return;
        }
        // Same feedback shape as the config save above - two identical
        // buttons must answer the same way (path+backup).
        tipform.savedMsg = `✓ ${j.path || ""}\nBackup: ${j.backup || ""}\n`
          + (j.reloaded ? t("ui.config.tipform_applied")
                        : t("ui.config.tipform_saved"));
        await loadTipform();
      } catch (e) { tipform.error = String(e); }
    }
    // Name options for a table row: the special keys + the firmware
    // filament DB (same list the slot picker offers, /api/materials),
    // lowercased to the lookup key. A row whose saved name is not in the
    // list (hand-edited cfg) keeps its own entry so the select never
    // blanks an existing table.
    function tipformNameOptions(row) {
      const opts = ['default', 'soft'];
      for (const m of pickerMaterials.value) {
        const k = String(m).toLowerCase();
        if (!opts.includes(k)) opts.push(k);
      }
      const own = (row && row.material || '').trim().toLowerCase();
      if (own && !opts.includes(own)) opts.push(own);
      return opts;
    }

    // saved cfg vs live Klipper state (NOT the unsaved editor rows)
    const tipformRestartPending = computed(() => {
      if (!tipform.supported) return false;
      const live = state.tipform || {};
      const liveMode = live.available ? (live.mode || "stock") : "stock";
      if (liveMode !== (tipform.cfgMode || "stock")) return true;
      if (liveMode === "stock") return false;
      const a = [...(live.tables || [])].map(String).sort().join(",");
      const b = [...(tipform.cfgNames || [])].sort().join(",");
      return a !== b;
    });

    // Default/fallback list; the live list + per-type subtypes are loaded
    // from /api/materials, which sources them from the firmware filament DB
    // (filament_parameters.py) - same materials the printer's display offers.
    const pickerMaterials = ref([
      "PLA", "PLA-CF",
      "PETG", "PETG-CF", "PETG-HF",
      "ABS", "ASA",
      "TPU",
      "PA", "PA-CF", "PA-GF", "PA6-CF", "PA6-GF",
      "PC", "PC-ABS",
      "PVA",
    ]);
    // Full { type: { vendor: [subtype, ...] } } hierarchy from the firmware DB.
    const pickerDb = ref({});
    async function loadMaterials() {
      try {
        const r = await fetch(`${API}/materials`);
        if (r.ok) {
          const j = await r.json();
          if (Array.isArray(j.materials) && j.materials.length) {
            pickerMaterials.value = j.materials;
          }
          if (j.db && typeof j.db === "object") {
            pickerDb.value = j.db;
          }
        }
      } catch (_) {}
    }
    // Vendors declared in [ace_tipform] keys ('<vendor>_<material>'), per
    // material - offered in the filament picker so the declared identity can
    // hit a vendor table/unloadtemp exactly (table_for matching is
    // case-insensitive, so the title-cased display value matches). Sources:
    // the LIVE Klipper tables plus the editor rows (saved or in-progress).
    function _titleCase(s) {
      return String(s || "").split(/\s+/).filter(Boolean)
        .map(w => w[0].toUpperCase() + w.slice(1)).join(" ");
    }
    const tipformVendorsByMaterial = computed(() => {
      const map = {};
      const add = (material, vendor) => {
        const m = String(material || "").trim().toUpperCase();
        const v = _titleCase(vendor);
        if (!m || !v) return;
        if (!(map[m] = map[m] || []).includes(v)) map[m].push(v);
      };
      for (const k of (state.tipform && state.tipform.tables) || []) {
        const s = tipformSplitKey(String(k).toLowerCase());
        if (s.vendor) add(s.material, s.vendor);
      }
      for (const row of (tipform.rows || [])) {
        if (row && row.vendor) add(row.material, row.vendor);
      }
      return map;
    });
    // Vendors for the chosen material (Generic always first) straight from the
    // firmware DB, PLUS the [ace_tipform] vendors for that material (dedup,
    // case-insensitive). Tipform vendors are in the VALIDATION list on
    // purpose: the material-change cascade must not snap them away when the
    // same vendor is declared for the new material too.
    const pickerDbVendors = computed(() => {
      const v = Object.keys(pickerDb.value[picker.material] || { Generic: [] });
      const base = v.includes("Generic")
        ? ["Generic", ...v.filter(x => x !== "Generic")] : [...v];
      const low = new Set(base.map(x => x.toLowerCase()));
      const mat = String(picker.material || "").toUpperCase();
      for (const tv of tipformVendorsByMaterial.value[mat] || []) {
        if (!low.has(tv.toLowerCase())) { base.push(tv); low.add(tv.toLowerCase()); }
      }
      return base;
    });
    // Display list for the <select>: the DB vendors PLUS the slot's current
    // vendor when the printer doesn't ship it (e.g. an RFID-set brand). Without
    // this the <select> shows blank for an unknown vendor, so it can't be
    // changed or cleared. Display-only - the cascade still validates against the
    // DB list, so a user material change resets a non-DB vendor.
    const pickerVendors = computed(() => {
      const v = pickerDbVendors.value;
      return (picker.vendor && !v.includes(picker.vendor)) ? [...v, picker.vendor] : v;
    });
    // Subtypes for the chosen material + vendor from the firmware DB; "Basic" =
    // firmware 'generic'.
    const pickerDbSubtypes = computed(() => {
      const byVendor = pickerDb.value[picker.material] || {};
      return ["Basic", ...(byVendor[picker.vendor] || [])];
    });
    // Display list: DB subtypes PLUS the slot's current subtype when the printer
    // doesn't know it (e.g. an RFID-set subtype) - same display-only rationale
    // as pickerVendors (this is the RFID 'Transparent' that couldn't be cleared).
    const currentSubtypes = computed(() => {
      const s = pickerDbSubtypes.value;
      return (picker.subtype && !s.includes(picker.subtype)) ? [...s, picker.subtype] : s;
    });
    const picker = reactive({
      show: false,
      ace: 0,
      slot: 0,
      spool: "",      // bound spool id of this slot ("" = none)
      tagFormat: "",      // ACE_TAG_WRITE FORMAT= ("" = the config default)
      tagUidSku: null,    // anycubic: UID as sku (null = the config default)
      // Grams field of the spool row. Reads as "the weight of the spool in
      // this slot": with a spool picked it carries THAT spool's weight and
      // the button corrects it, without one it seeds the spool the button
      // creates. Prefilled on open and whenever the pick changes, so a
      // correction always starts from the current value instead of blank.
      weight: "",
      head: null,     // head mode: set when editing a feeder head (no ACE slot)
      material: "PLA",
      subtype: "Basic",
      vendor: "Generic",
      color: "#ffffff",
    });
    // Suppress the cascade snap while openPicker is programmatically setting the
    // fields, so an RFID-set vendor/subtype the printer doesn't know is NOT
    // snapped away on open (it's preserved + shown via the augmented lists). A
    // real user material/vendor change (flag clear) still snaps to a DB-valid
    // value. Validate against the DB lists, not the augmented display lists.
    let _pickerOpening = false;
    watch(() => picker.material, () => {
      if (_pickerOpening) return;
      if (!pickerDbVendors.value.includes(picker.vendor)) {
        picker.vendor = pickerDbVendors.value[0] || "Generic";
      }
      if (!pickerDbSubtypes.value.includes(picker.subtype)) {
        picker.subtype = "Basic";
      }
    });
    watch(() => picker.vendor, () => {
      if (_pickerOpening) return;
      if (!pickerDbSubtypes.value.includes(picker.subtype)) {
        picker.subtype = "Basic";
      }
    });
    // Choosing a spool ADOPTS its identity - the spool knows what it is, so
    // the user should not retype colour/material/vendor/subtype. Set
    // under the _pickerOpening guard: the material/vendor cascade would
    // otherwise snap away a vendor or subtype the firmware DB does not list
    // (exactly the case for a third-party spool).
    watch(() => picker.spool, (id, prev) => {
      if (_pickerOpening || id === prev) return;
      // Cleared the pick -> the field is a seed for a NEW spool again, and a
      // leftover weight of the spool just released must not become its
      // start weight.
      if (!id) {
        picker.weight = "";
        // Unpicking a spool must not leave ITS identity behind as the
        // slot's own (it would be saved as an override). Back to what
        // the slot showed when the picker opened.
        _pickerOpening = true;
        picker.material = _pickerOrig.material;
        picker.subtype = _pickerOrig.subtype;
        picker.vendor = _pickerOrig.vendor;
        picker.color = _pickerOrig.color;
        nextTick(() => { _pickerOpening = false; });
        return;
      }
      const sp = (state.spools || {})[id];
      if (!sp) return;
      picker.weight = _spoolWeightField(sp);
      _pickerOpening = true;
      if (sp.material) picker.material = sp.material;
      if (sp.vendor) picker.vendor = sp.vendor;
      if (sp.subtype) picker.subtype = sp.subtype;
      if (sp.color) picker.color = "#" + String(sp.color).replace("#", "");
      nextTick(() => { _pickerOpening = false; });
    });
    let _pickerOrig = {material: "", subtype: "", vendor: "", color: ""};
    // Set by the identity controls' own input events (template pickerTouch),
    // never by programmatic assignment. Needed because the picker opens a
    // slot WITHOUT a known identity on the display defaults (PLA / Basic /
    // Generic / #ffffff) and snapshots exactly those as the "original" -
    // so choosing white, or PLA, on such a slot compared equal and Save
    // took the "only the binding changed" exit without writing anything.
    // A user touch is a change by definition; the value comparison stays
    // for the programmatic paths (spool adopt, tag adopt).
    let _pickerTouched = false;
    function pickerTouch() { _pickerTouched = true; }
    function _pickerIdentityChanged() {
      if (_pickerTouched) return true;
      const norm = (v) => String(v || "").trim().toLowerCase();
      return (norm(picker.material) !== norm(_pickerOrig.material)
              || norm(picker.subtype) !== norm(_pickerOrig.subtype)
              || norm(picker.vendor) !== norm(_pickerOrig.vendor)
              || norm(picker.color).replace("#", "")
                 !== norm(_pickerOrig.color).replace("#", ""));
    }
    function openPicker(ace, slot) {
      _pickerOpening = true;
      picker.head = null;
      picker.ace = ace.idx;
      picker.slot = slot.idx;
      picker.material = (slot.material || "PLA");
      picker.subtype = slot.subtype || "Basic";
      picker.vendor = slot.brand || "Generic";
      picker.color = slot.color || "#ffffff";
      // Snapshot of the identity fields at open: savePicker writes an
      // override only when the user actually changed one of them, and
      // unpicking a spool restores them (see the picker.spool watcher).
      _pickerOrig = {material: picker.material, subtype: picker.subtype,
                     vendor: picker.vendor, color: picker.color};
      _pickerTouched = false;
      // Which spool of the table sits in this slot ("" = none). Applied
      // together with the identity in savePicker.
      picker.spool = spoolIdForSlot(ace.idx, slot.idx) || "";
      picker.weight = _spoolWeightField((state.spools || {})[picker.spool]);
      picker.show = true;
      // Let the watchers' snap run again only after this open settles.
      nextTick(() => { _pickerOpening = false; });
    }
    // head mode: edit a feeder head's filament identity (color/material). It has
    // no ACE slot - the values go straight to the head's print_task_config via
    // SET_PRINT_FILAMENT_CONFIG (same path the touchscreen uses; the heartbeat
    // leaves feeder heads untouched). The RFID/load-after buttons hide because
    // _pickerSlot() is null without an ACE slot.
    function openHeadPicker(th) {
      _pickerOpening = true;
      headRfidNote.value = "";
      picker.ace = null;
      picker.slot = null;
      picker.head = th.idx;
      picker.material = (th.material || "PLA");
      picker.subtype = th.subtype || "Basic";
      picker.vendor = th.brand || "Generic";
      picker.color = th.color || "#ffffff";
      // Own snapshot: without it _pickerIdentityChanged compared the head
      // picker against the LAST SLOT picker's original.
      _pickerOrig = {material: picker.material, subtype: picker.subtype,
                     vendor: picker.vendor, color: picker.color};
      _pickerTouched = false;
      // Head binding ('h<n>') - the same init the slot picker does, so the
      // weight field and the generic picker.spool watcher work unchanged.
      picker.spool = (state.spool_binding || {})[`h${th.idx}`] || "";
      picker.weight = _spoolWeightField((state.spools || {})[picker.spool]);
      picker.show = true;
      nextTick(() => { _pickerOpening = false; });
    }
    function closePicker() {
      picker.show = false;
      // The search dropdown must not survive the dialog: reopened later it
      // would show stale rows over a different slot/head context.
      smOpen.value = false;
      smQuery.value = "";
      smRows.value = [];
    }
    function _pickerSlot() {
      const a = state.aces.find(x => x.idx === picker.ace);
      if (!a) return null;
      return (a.slots || []).find(s => s.idx === picker.slot) || null;
    }
    // Tag data only: an identity Klipper filled from the bound spool (the
    // card itself carries none, ident_src 'spool') is table data - no
    // "read from tag" button for it, and material/colour edits still go
    // back to the spool.
    function _slotTagData(s) {
      return (s && s.rfid === 2 && s.ident_src !== "spool") ? s.rfid_data : null;
    }
    const pickerHasRfid = computed(() => {
      if (!picker.show) return false;
      return !!_slotTagData(_pickerSlot());
    });
    // The SKU written on the tag, shown next to the RFID button. Comes from
    // rfid_data, which the backend only fills on a LIVE read (rfid == 2), so
    // it can never be a stale value from an earlier spool.
    const pickerRfidSku = computed(() => {
      if (!picker.show) return "";
      const s = _pickerSlot();
      // Tag sku; without an identity the host-read card UID (MIFARE etc.),
      // shown the same way.
      const v = (s && s.rfid_data && s.rfid_data.sku)
        ? s.rfid_data.sku : ((s && s.uid) ? s.uid : "");
      return v ? String(v).trim() : "";
    });
    // Tag format of the picked slot's last read (anycubic / openspool /
    // mifare / unknown), shown above the code.
    const pickerTagFormat = computed(() => {
      if (!picker.show) return "";
      const s = _pickerSlot();
      return tagFormatLabel(s && s.tag_format);
    });
    // "UID" when the shown code is the card UID, else "SKU" (Anycubic
    // article/app code) - one line above the header with the format.
    const pickerCodeKind = computed(() => {
      const s = _pickerSlot();
      const code = String(pickerRfidSku.value || "").replace(/[: ]/g, "").toUpperCase();
      const uid = String((s && s.uid) || "").replace(/[: ]/g, "").toUpperCase();
      return (code && uid && code === uid) ? t("ui.dialog.tag_uid") : t("ui.dialog.tag_sku");
    });
    // The card UID when it is NOT the shown code already (Anycubic tags:
    // SKU on the tag + chip UID) - shown behind it.
    const pickerUidExtra = computed(() => {
      const s = _pickerSlot();
      const uid = String((s && s.uid) || "");
      const code = String(pickerRfidSku.value || "").replace(/[: ]/g, "").toUpperCase();
      if (!uid || uid.replace(/[: ]/g, "").toUpperCase() === code) return "";
      return uid;
    });
    function tagFormatLabel(f) {
      if (!f) return "";
      const key = "ui.common.tag_fmt_" + String(f);
      const v = t(key);
      return (v && v !== key) ? v : String(f);
    }
    // Head picker twin of pickerRfidSku: the last code the STOCK feeder
    // reader delivered for this head (card UID hex, else the M1 layout's
    // numeric SKU) - state.head_tag_seen, filled by the Klipper capture.
    const pickerHeadTag = computed(() => {
      if (!picker.show) return "";
      if (picker.head === null || picker.head === undefined) return "";
      return String((state.head_tag_seen || {})[String(picker.head)] || "");
    });
    // New design: Feeder / Manual tag in the head picker's title.
    // New design: custom spool dropdown in the picker (colour chip per row).
    const spoolDdOpen = ref(false);
    function spoolDdPick(id) { spoolDdOpen.value = false; picker.spool = id; }
    const spoolDdCurrent = computed(() =>
      spoolPickerOptions.value.find(o => o.id === picker.spool) || null);
    const pickerHeadKind = computed(() => {
      if (!picker.show || picker.head === null || picker.head === undefined) return "";
      const th = (state.toolheads || []).find(x => x.idx === picker.head);
      if (!th) return "";
      return th.feeder ? "feeder" : (th.manual ? "manual" : "");
    });
    const headRfidBusy = ref(false);
    const headRfidNote = ref("");
    async function readHeadRfid() {
      // On-demand feeder-reader read: FILAMENT_DT_UPDATE CHANNEL=<head> is
      // the stock command behind the insert-event read. The result arrives
      // asynchronously (~0.7-1.5s start-to-parse), so wait, refresh, then
      // report honestly - a silent button hides a failed read.
      if (picker.head === null || picker.head === undefined) return;
      if (headRfidBusy.value) return;
      headRfidBusy.value = true;
      headRfidNote.value = "";
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "FILAMENT_DT_UPDATE",
                                args: {CHANNEL: picker.head}}),
        });
        await new Promise(r => setTimeout(r, 2500));
        await reloadState();
        const tag = (state.head_tag_seen || {})[String(picker.head)] || "";
        if (!tag) {
          headRfidNote.value = t("ui.dialog.head_no_tag");
        } else if (state.spool_mode === "spoolman") {
          // A freshly registered card_uid should adopt NOW, not on the
          // next sweep tick (the Klipper-side capture dedupes an
          // unchanged code, so the kick is the immediate path).
          try {
            await fetch(`${API}/spoolman/adopt_by_tags`, {method: "POST"});
          } catch (e) { /* sweep tick covers it */ }
          await reloadState();
        }
      } catch (e) {
        headRfidNote.value = String(e);
      } finally {
        headRfidBusy.value = false;
      }
    }
    const pickerRfidStyle = computed(() => {
      if (!pickerHasRfid.value) return {};
      const c = (_pickerSlot()?.rfid_data?.color || "").trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(c)) return {};
      const r = parseInt(c.slice(1, 3), 16);
      const g = parseInt(c.slice(3, 5), 16);
      const b = parseInt(c.slice(5, 7), 16);
      const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      return {
        background: c,
        borderColor: c,
        color: lum > 0.55 ? "#001619" : "#ffffff",
      };
    });
    // Slots without an RFID tag get a "Clear" button instead of "Read
    // RFID"; shown only when the slot's identity actually comes from a
    // manual override (source === "override"), so RFID/derived/empty
    // slots offer nothing to clear.
    const pickerHasOverride = computed(() => {
      if (!picker.show) return false;
      const s = _pickerSlot();
      return !!(s && s.source === "override");
    });
    function readPickerRfid() {
      const s = _pickerSlot();
      const r = s && s.rfid_data;
      if (!r) return;
      // Reset the WHOLE identity to the tag's values unconditionally. Guarding
      // each field on a truthy RFID value left a manually-changed vendor/subtype
      // stuck when the tag had none (empty brand/subtype) - then it never
      // matched _pickerMatchesRfid and save kept a shadow override. Empty tag
      // fields fall back to the same Generic/Basic placeholders openPicker uses,
      // so save then matches the tag and drops the override. _pickerOpening
      // suppresses the cascade snap while we set the fields (as in openPicker),
      // so a tag vendor/subtype the printer doesn't ship isn't snapped away.
      _pickerOpening = true;
      picker.material = r.material || "PLA";
      picker.subtype  = r.subtype  || "Basic";
      picker.vendor   = r.brand    || "Generic";
      picker.color    = r.color    || "#ffffff";
      nextTick(() => { _pickerOpening = false; });
    }
    function _ptcGcodeFor(aceIdx, slotIdx, mat, brand, sub, colorHex) {
      const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
      const hex = (colorHex || "#ffffff").replace("#", "");
      const colorRGBA = hex.toUpperCase() + "FF";
      return {
        CONFIG_EXTRUDER: slotIdx,
        FILAMENT_TYPE:   dq(mat || "PLA"),
        FILAMENT_COLOR_RGBA: colorRGBA,
        VENDOR:          dq(brand || "Generic"),
        FILAMENT_SUBTYPE: dq(sub || ""),
      };
    }
    // Bug 1: saving an RFID slot unchanged must not create an override
    // that masks the tag. openPicker prefills Generic/Basic placeholders
    // for empty vendor/subtype, so normalise those to "" when comparing
    // the form against the tag's rfid_data.
    function _ovNorm(s) { return String(s || "").trim().toLowerCase(); }
    function _ovVendor(s) { const v = _ovNorm(s); return v === "generic" ? "" : v; }
    function _ovSub(s) { const v = _ovNorm(s); return (v === "basic" || v === "generic") ? "" : v; }
    function _ovColor(s) { return _ovNorm(s).replace(/^#/, ""); }
    // Save can carry the edit into the SPOOL, not just the slot - otherwise a
    // correction is lost the moment the picker re-reads the spool's values.
    //
    // WHAT may be written back mirrors the enrichment exactly: only fields
    // the TABLE is authoritative for.
    //   sub-type - the tag has no field for it at all
    //   weight   - likewise never on a tag
    //   vendor   - the tag HAS a brand field, so the table only owns it while
    //              that field is empty (which is what writer apps leave)
    //   material, colour - the TAG owns these; changing them permanently is a
    //              tag job, so an RFID slot does not offer them here.
    // Without a tag the table is authoritative for everything.
    //
    // Asks before changing the IDENTITY - that record is read by other slots
    // and future prints, so it must not change as a side effect of saving a
    // slot. The WEIGHT is exempt: the field only exists while a spool is
    // picked and can only ever land on that spool, so there is nothing to
    // ask about. Declining the identity question still saves it.
    async function _spoolWriteBackFromPicker() {
      const id = picker.spool;
      const sp = id ? (state.spools || {})[id] : null;
      if (!sp) return;
      const rfid = _slotTagData(_pickerSlot());
      const args = {}, names = [];
      const silent = {};
      const add = (key, label, val) => { args[key] = val; names.push(t(label)); };
      if (!rfid) {
        if (_ovNorm(picker.material) !== _ovNorm(sp.material) && picker.material)
          add("MATERIAL", "ui.spools.material", picker.material);
        if (_ovColor(picker.color) !== _ovColor(sp.color) && picker.color)
          add("COLOR", "ui.spools.color", (picker.color || "").replace("#", ""));
      }
      if ((!rfid || !_ovVendor(rfid.brand))
          && _ovVendor(picker.vendor) !== _ovVendor(sp.vendor))
        add("VENDOR", "ui.spools.vendor", picker.vendor || "");
      if (_ovSub(picker.subtype) !== _ovSub(sp.subtype))
        add("SUBTYPE", "ui.spools.subtype", picker.subtype || "");
      if (picker.weight !== "" && String(picker.weight) !== _spoolWeightField(sp))
        silent.WEIGHT = picker.weight;
      const ask = names.length
        && window.confirm(t("ui.spools.write_back", {
             spool: spoolTitle(sp), fields: names.join(", ")}));
      const payload = Object.assign({}, silent, ask ? args : {});
      if (!Object.keys(payload).length) return;
      await spoolMacro("ACE_SPOOL_SET", Object.assign({ID: id}, payload));
    }
    // null = nothing to learn (no UID at the slot, or the spool already
    // lists it); true/false = the user's answer.
    function _askLearnUid(spoolId) {
      const s = _pickerSlot();
      const sp = (state.spools || {})[spoolId];
      const uid = String((s && s.uid) || "").replace(/[: ]/g, "").toUpperCase();
      if (!uid || !sp) return null;
      const codes = String(sp.sku || "").split(",")
        .map(c => c.trim().replace(/^#/, "").replace(/[: ]/g, "").toUpperCase())
        .filter(Boolean);
      if (codes.includes(uid)) return null;
      return window.confirm(t("ui.spools.learn_uid", {
        uid, spool: spoolTitle(sp),
        where: (state.spool_mode && state.spool_mode !== "local")
          ? t("ui.spools.learn_uid_where_sm") : t("ui.spools.learn_uid_where_local")}));
    }
    function _pickerMatchesRfid() {
      const s = _pickerSlot();
      const r = (s && s.rfid === 2) ? s.rfid_data : null;
      if (!r) return false;
      return _ovNorm(picker.material) === _ovNorm(r.material)
          && _ovVendor(picker.vendor) === _ovVendor(r.brand)
          && _ovSub(picker.subtype) === _ovSub(r.subtype)
          && _ovColor(picker.color) === _ovColor(r.color);
    }
    async function savePicker(loadAfter) {
      // "Save + load" is a load like any other; plain "Save" only writes the
      // identity and is not gated.
      if (loadAfter && picker.ace !== null && picker.ace !== undefined
          && !_confirmCmd("ui.confirm.load_slot", {
                head: dispIdx(state.mode === "head"
                                ? (aceHeadForAce(picker.ace) ?? picker.slot)
                                : picker.slot),
                ace: dispIdx(picker.ace), slot: dispIdx(picker.slot)})) return;
      // Feeder head (no ACE slot): push the identity straight to the head's
      // print_task_config via SET_PRINT_FILAMENT_CONFIG (same path the
      // touchscreen uses). The heartbeat leaves feeder/manual heads untouched,
      // so this sticks until the user changes it.
      if (picker.head !== null && picker.head !== undefined) {
        // Spool binding travels with the identity here too - the head
        // picker must reach the binding like the slot picker does.
        // Only sent on a real change, like the slot flow.
        const hb = (state.spool_binding || {})[`h${picker.head}`] || "";
        if ((picker.spool || "") !== hb) {
          if (picker.spool) {
            enqueue("ACE_SPOOL_ASSIGN", {HEAD: picker.head, ID: picker.spool});
          } else {
            enqueue("ACE_SPOOL_ASSIGN", {HEAD: picker.head});
          }
        }
        if (picker.spool && picker.weight !== "") {
          const sp = (state.spools || {})[picker.spool];
          if (sp && String(picker.weight) !== _spoolWeightField(sp)) {
            enqueue("ACE_SPOOL_SET", {ID: picker.spool, WEIGHT: picker.weight});
          }
        }
        const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
        const hex = (picker.color || "#ffffff").replace("#", "");
        enqueue("SET_PRINT_FILAMENT_CONFIG", {
          CONFIG_EXTRUDER:     picker.head,
          FILAMENT_TYPE:       dq(picker.material || "PLA"),
          FILAMENT_COLOR_RGBA: hex.toUpperCase() + "FF",
          VENDOR:              dq(picker.vendor || "Generic"),
          FILAMENT_SUBTYPE:    dq(picker.subtype || ""),
        });
        closePicker();
        reloadState();
        return;
      }
      const aceIdx = picker.ace;
      const slotIdx = picker.slot;
      // Spool binding travels with the identity: the picker IS the place
      // where the user says what sits in this slot. Only sent when it
      // changed, so a plain colour edit does not touch the table.
      const spoolBefore = spoolIdForSlot(aceIdx, slotIdx) || "";
      if ((picker.spool || "") !== spoolBefore) {
        if (picker.spool) {
          // The assignment can LEARN the slot's read card UID into the
          // spool's code list (and from there into Spoolman's card_uids).
          // Ask first - it is permanent and travels.
          const learn = _askLearnUid(picker.spool);
          const args = {ACE: aceIdx, SLOT: slotIdx, ID: picker.spool};
          if (learn !== null) args.LEARN_UID = learn ? 1 : 0;
          enqueue("ACE_SPOOL_ASSIGN", args);
        } else {
          enqueue("ACE_SPOOL_ASSIGN", {ACE: aceIdx, SLOT: slotIdx});
        }
      }
      await _spoolWriteBackFromPicker();
      if (!_pickerIdentityChanged()) {
        // Only the binding/weight changed: the slot's identity stays
        // whatever it was (RFID, an existing override, or none) - no
        // override is invented from the picker's defaults.
        closePicker();
        if (loadAfter) {
          loadSlot(aceIdx, slotIdx);
        }
        reloadState();
        return;
      }
      if (_pickerMatchesRfid()) {
        // Values equal the RFID tag -> drop any existing override so the
        // RFID identity stays the source of truth (no shadow override).
        try {
          await fetch(`${API}/slot-override/${aceIdx}/${slotIdx}`, {method: "DELETE"});
        } catch (e) {
          setMacroLog(`${t("ui.common.error")}: ${e}`);
        }
        closePicker();
        enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
        if (loadAfter) {
          loadSlot(aceIdx, slotIdx);
        }
        reloadState();
        return;
      }
      try {
        await fetch(`${API}/slot-override`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            ace: aceIdx,
            slot: slotIdx,
            material: picker.material || "",
            brand:    picker.vendor || "",
            subtype:  picker.subtype || "",
            color:    picker.color || "",
          }),
        });
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
      }
      closePicker();
      enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
      if (loadAfter) {
        loadSlot(aceIdx, slotIdx);
      }
      reloadState();
    }
    async function clearPickerOverride() {
      const aceIdx = picker.ace;
      const slotIdx = picker.slot;
      try {
        await fetch(`${API}/slot-override/${aceIdx}/${slotIdx}`, {method: "DELETE"});
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
      }
      closePicker();
      enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
      reloadState();
    }
    let _lastActive = null;
    watch(() => state.active_device, (newAce) => {
      _lastActive = newAce;
    });
    // A SET, not one index: each ACE has its own thresholds, and comparing
    // or setting two of them meant the second card closing the first
    // (then only one card's dry menu could be open).
    const dryOpenAces = reactive(new Set());
    function dryPanelOpen(aceIdx) { return dryOpenAces.has(aceIdx); }
    function toggleDryPanel(aceIdx) {
      if (dryOpenAces.has(aceIdx)) dryOpenAces.delete(aceIdx);
      else dryOpenAces.add(aceIdx);
    }
    function aceDrying(ace) {
      const d = ace && ace.dryer;
      return !!(d && d.status && d.status !== 'stop');
    }
    function dryStart(aceIdx) {
      const cfg = dryerCfg[aceIdx] || {duration: 240};
      // Temperature comes from the unit's stored setting - the same one
      // auto-dry uses, and the same one the field above edits. Only the
      // duration is a per-click choice (auto-dry has no duration: it runs
      // until the humidity target is met).
      const a = (state.aces || []).find(x => x.idx === aceIdx);
      const temp = (a && a.auto_dry && a.auto_dry.temp) || 50;
      run("ACE_DRY", {ACE: aceIdx, TEMP: temp, DURATION: cfg.duration});
    }
    function dryStop(aceIdx) {
      run("ACE_STOP_DRYING", {ACE: aceIdx});
    }
    const snapshots = ref([]);
    const selectedSnapshot = ref("");
    const snapshotPreview = computed(() => snapshots.value.find(s => s.name === selectedSnapshot.value));
    // Head-mode snapshots are stored separately from multi - tag every snapshot
    // call with the current mode so each shows/saves its own set.
    function _snapMode() { return state.mode === "head" ? "head" : ""; }
    function _snapQS() { return state.mode === "head" ? "?mode=head" : ""; }
    async function reloadSnapshots() {
      try {
        const r = await fetch(`${API}/snapshots${_snapQS()}`);
        if (!r.ok) return;
        const j = await r.json();
        snapshots.value = j.snapshots || [];
      } catch (_) {}
    }
    // Reload the right snapshot set (and drop a stale selection) on mode switch.
    watch(() => state.mode, () => { selectedSnapshot.value = ""; reloadSnapshots(); });
    async function _doSaveSnapshot(name) {
      try {
        const r = await fetch(`${API}/snapshots`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, mode: _snapMode()}),
        });
        if (!r.ok) {
          setMacroLog(t("ui.log.snapshot_save_failed", {error: await r.text()}));
          return;
        }
        setMacroLog(t("ui.log.snapshot_saved", {name}));
        await reloadSnapshots();
        selectedSnapshot.value = name;
      } catch (e) { setMacroLog(`${t("ui.common.error")}: ${e}`); }
    }
    async function saveSnapshot() {
      if (selectedSnapshot.value) {
        const name = selectedSnapshot.value;
        confirm({
          title: t("ui.dialog.overwrite_snapshot_title", {name}),
          message: tHtml("ui.dialog.overwrite_snapshot_msg", {name}),
          okLabel: t("ui.common.save"),
          onOk: () => _doSaveSnapshot(name),
        });
        return;
      }
      const name = prompt(t("ui.dashboard.snapshot_name_prompt"));
      if (!name) return;
      await _doSaveSnapshot(name);
    }
    async function deleteSnapshot() {
      if (!selectedSnapshot.value) return;
      if (!confirmSync(t("ui.dialog.delete_snapshot", {name: selectedSnapshot.value}))) return;
      try {
        await fetch(`${API}/snapshots/${encodeURIComponent(selectedSnapshot.value)}${_snapQS()}`, {method: "DELETE"});
        selectedSnapshot.value = "";
        await reloadSnapshots();
      } catch (e) { setMacroLog(`${t("ui.common.error")}: ${e}`); }
    }
    async function loadSnapshot() {
      if (!selectedSnapshot.value) return;
      const name = selectedSnapshot.value;
      let plan;
      try {
        const r = await fetch(`${API}/snapshots/${encodeURIComponent(name)}/apply${_snapQS()}`, {method: "POST"});
        plan = await r.json();
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
        return;
      }
      const errs = plan.errors || [];
      const warns = plan.warnings || [];
      const actions = plan.actions || [];
      if (errs.length) {
        confirm({
          title: t("ui.dialog.snapshot_errors_title"),
          message: errs.map(e => "• " + escapeHtml(e.message)).join("<br>"),
          okLabel: "OK",
          dismissOnly: true,
          onOk: () => {},
        });
        return;
      }
      const proposals = plan.override_proposals || [];
      const writeOverridesAndEnqueue = async (writeOverrides) => {
        if (writeOverrides && proposals.length) {
          for (const o of proposals) {
            try {
              await fetch(`${API}/slot-override`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(o),
              });
            } catch (e) {
              setMacroLog(`${t("ui.common.error")}: ${e}`);
            }
          }
          enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
        }
        for (const a of actions) {
          enqueue(a.name, a.args || {});
        }
      };
      if (warns.length) {
        confirm({
          title: t("ui.dialog.snapshot_warnings_title"),
          message: warns.map(w => "• " + escapeHtml(w.message)).join("<br>")
                   + "<br><br>" + t("ui.dialog.snapshot_warnings_hint"),
          okLabel: t("ui.dialog.apply_anyway"),
          checkboxLabel: proposals.length
            ? t("ui.dialog.set_filaments_per_snapshot")
            : null,
          checkboxDefault: false,
          onOk: ({checked}) => { writeOverridesAndEnqueue(checked); },
        });
        return;
      }
      confirm({
        title: t("ui.dialog.apply_snapshot_title", {name}),
        message: t("ui.dialog.apply_snapshot_msg"),
        okLabel: t("ui.common.apply"),
        onOk: () => { writeOverridesAndEnqueue(false); },
      });
    }
    const confirmDialog = reactive({
      show: false, title: "", message: "",
      okLabel: "OK",  _onOk:  null,
      altLabel: null, _onAlt: null,
      dismissOnly: false,
      checkboxLabel: null, checkboxChecked: false,
      // Optional single input. `validate` returns an error string that BLOCKS
      // OK (not a warning) - the one caller so far, the SKU collision, must
      // never be able to hand back a code another spool already holds.
      inputLabel: null, inputValue: "", inputHint: null, _validate: null,
    });
    const confirmInputError = computed(() =>
      confirmDialog._validate ? (confirmDialog._validate(confirmDialog.inputValue) || "") : "");
    function confirm(opts) {
      confirmDialog.show = true;
      confirmDialog.title = opts.title || t("ui.common.confirm");
      confirmDialog.message = opts.message || "";
      confirmDialog.okLabel = opts.okLabel || "OK";
      confirmDialog._onOk   = opts.onOk || (()=>{});
      confirmDialog.altLabel = opts.altLabel || null;
      confirmDialog._onAlt   = opts.onAlt || null;
      // The built-in Cancel closes the dialog; a caller that also has to tear
      // something down behind it (an open preflight modal) hooks it here
      // instead of passing a SECOND button that also says "Cancel".
      confirmDialog._onCancel = opts.onCancel || null;
      confirmDialog.dismissOnly = !!opts.dismissOnly;
      confirmDialog.checkboxLabel = opts.checkboxLabel || null;
      confirmDialog.checkboxChecked = !!opts.checkboxDefault;
      confirmDialog.inputLabel = opts.inputLabel || null;
      confirmDialog.inputValue = opts.inputValue == null ? "" : String(opts.inputValue);
      confirmDialog.inputHint = opts.inputHint || null;
      confirmDialog._validate = opts.validate || null;
    }
    function okConfirm() {
      // A blocking validation error must survive Enter as well as the button,
      // so the guard sits here and not only on :disabled.
      if (confirmInputError.value) return;
      const cb = confirmDialog._onOk;
      const checked = confirmDialog.checkboxChecked;
      const value = confirmDialog.inputValue;
      confirmDialog.show = false;
      if (cb) cb({checked, value});
    }
    function altConfirm() {
      const cb = confirmDialog._onAlt;
      confirmDialog.show = false;
      if (cb) cb();
    }
    function cancelConfirm() {
      const cb = confirmDialog._onCancel;
      confirmDialog.show = false;
      if (cb) cb();
    }
    function confirmSync(msg) { return window.confirm(msg); }
    const config = reactive({path: "", content: "", params: {},
                             sha1: ""});
    const configLog = ref("");
    const configLoadError = ref("");
    const showRawConfig = ref(false);
    // The button sits in the save bar, which sticks to the bottom of the
    // window in the new design: open the editor and scroll to it.
    function toggleRawConfig() {
      showRawConfig.value = !showRawConfig.value;
      if (!showRawConfig.value) return;
      nextTick(() => {
        const el = document.getElementById("raw-config");
        if (!el) return;
        el.open = true;
        el.scrollIntoView({behavior: "smooth", block: "start"});
      });
    }
    const configForm = reactive({
      ace_device_count: 1,
      feed_speed: 80,
      retract_speed: 80,
      load_length: 2100,
      retract_length: 1950,
      seat_overshoot_length: '',
      swap_retract_length: '',
      swap_purge_length: '',
      dryer_temp: '',
      dryer_duration: '',
      display_index_base: 0,
      v2_order: 'first',
      load_retry: '',
      extrusion_retry: '',
      unload_retry: '',
      state_debug: false,
      usb_debug: false,
      fa_debug: false,
      perAce: [],
    });
    function _makePerAceEntry() {
      const perSlot = [];
      for (let s = 0; s < 4; s++) {
        perSlot.push({load_length: '', retract_length: '', swap_retract_length: ''});
      }
      return {
        dryer_temp: '', dryer_duration: '',
        feed_speed: '', retract_speed: '',
        load_length: '', retract_length: '', swap_retract_length: '',
        perSlot,
      };
    }
    function _ensurePerAceLength() {
      const n = Math.max(0, Math.min(4, perAceCount()));
      while (configForm.perAce.length < n) {
        configForm.perAce.push(_makePerAceEntry());
      }
      while (configForm.perAce.length > n) {
        configForm.perAce.pop();
      }
    }
    watch(() => [configForm.ace_device_count, (state.devices || []).length], _ensurePerAceLength, {immediate: true});
    watch([() => tab.value, calibrationRouteKnown], ([activePage, routeKnown]) => {
      if (activePage !== 'calibration' || calibrationUi.panelTouched
          || state.calibration?.state !== 'idle') return;
      calibrationSetPanel(routeKnown ? 'verify' : 'setup', false);
    }, {immediate: true});
    // A new route (or the config arriving) starts the scope from the
    // route's badge: own values -> this slot, otherwise the whole ACE. The
    // undo bar of a clear belongs to the route it was made on.
    watch(() => [calibrationUi.ace, calibrationUi.slot, calibrationUi.head, config.path],
      (next, previous) => {
        calibrationUi.scope = calibrationSavedKind.value === 'own' ? 'slot' : 'ace';
        if (previous && next.slice(0, 3).some((v, i) => Number(v) !== Number(previous[i]))) {
          _calibrationClearBarClose();
        }
      }, {immediate: true});
    // Any other config write (or a reload of a changed file) ends the undo.
    watch(() => config.sha1, (sha1) => {
      if ((calibrationClear.undo || calibrationClear.restored)
          && sha1 !== calibrationClear.sha1) _calibrationClearBarClose();
    });
    // True after a config save, which needs a full printer restart to take
    // effect (a bare Klipper restart misses USB/serial + PAXX boot-script
    // changes). Drives the prominent top reboot banner; cleared once a restart
    // actually starts (klippy down). Mode changes do NOT use this - crossing
    // 'normal' raises a backend reboot error that reaches the display too.
    const rebootNeeded = ref(false);
    // An update that could not restart the Klipper service itself: the new
    // code only loads after a reboot. Same banner, other text.
    const printerRebootNeeded = ref(false);
    // Restart straight from a "please restart" hint: same endpoint and
    // confirm dialog as the Control tab. Not offered while a print runs or
    // when the platform manages the install (the server refuses both).
    const restartNotice = ref("");
    let _restartNoticeTimer = null;
    // "restart requested" is cleared once the restart has visibly happened
    // (klippy or the websocket went down and a state came back); the timer
    // below is only the fallback.
    let _restartAwaitDown = false, _restartSeenDown = false;
    function restartNoticeClear() {
      restartNotice.value = "";
      _restartAwaitDown = _restartSeenDown = false;
      if (_restartNoticeTimer) { clearTimeout(_restartNoticeTimer); _restartNoticeTimer = null; }
    }
    function restartNoteDown() { if (_restartAwaitDown) _restartSeenDown = true; }
    function restartNoteBack() { if (_restartSeenDown) restartNoticeClear(); }
    const restartOffered = computed(() => !updateState.managed
      && !["printing", "paused"].includes(state.printer_state));
    function restartNow(behavior) {
      confirm({
        title: t(`ui.restart.${behavior}_title`),
        message: t(`ui.restart.${behavior}_msg`),
        okLabel: t(`ui.restart.${behavior}_btn`),
        onOk: async () => {
          try {
            const r = await fetch(`${API}/restart`, {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({behavior}),
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || `${r.status}`);
            rebootNeeded.value = false;
            printerRebootNeeded.value = false;
            restartNotice.value = t(`ui.restart.${behavior}_sent`);
            _restartAwaitDown = true; _restartSeenDown = false;
          } catch (e) {
            restartNotice.value = `${t("ui.restart.failed")}: ${e.message || e}`;
            _restartAwaitDown = _restartSeenDown = false;
          }
          if (_restartNoticeTimer) clearTimeout(_restartNoticeTimer);
          _restartNoticeTimer = setTimeout(() => {
            restartNotice.value = "";
            _restartNoticeTimer = null;
            _restartAwaitDown = _restartSeenDown = false;
          }, 60000);
        },
      });
    }
    // Klipper's own "please reboot" (SET_ACE_MODE across 'normal' swaps the
    // feed modules) reaches the web as a notification.
    function notifWantsReboot(n) {
      return /please reboot/i.test((n && n.msg) || "");
    }
    function paramsToForm(params, perAceParams) {
      if (!params) return;
      const num  = (k) => params[k] != null ? Number(params[k]) : configForm[k];
      const bool = (k) => params[k] != null ? params[k] === 'true' : configForm[k];
      const numOrEmpty = (v) => (v != null && v !== '') ? Number(v) : '';
      configForm.ace_device_count = num('ace_device_count');
      configForm.feed_speed     = num('feed_speed');
      configForm.retract_speed  = num('retract_speed');
      configForm.load_length    = num('load_length');
      configForm.retract_length = num('retract_length');
      configForm.seat_overshoot_length = numOrEmpty(params.seat_overshoot_length);
      configForm.swap_retract_length = numOrEmpty(params.swap_retract_length);
      configForm.swap_purge_length = numOrEmpty(params.swap_purge_length);
      configForm.dryer_temp        = numOrEmpty(params.dryer_temp);
      configForm.dryer_duration    = numOrEmpty(params.dryer_duration);
      configForm.display_index_base = numOrEmpty(params.display_index_base);
      configForm.v2_order = (params.v2_order === 'last') ? 'last' : 'first';
      configForm.load_retry        = numOrEmpty(params.load_retry);
      configForm.extrusion_retry   = numOrEmpty(params.extrusion_retry);
      configForm.unload_retry      = numOrEmpty(params.unload_retry);
      configForm.state_debug    = bool('state_debug');
      configForm.usb_debug      = bool('usb_debug');
      configForm.fa_debug       = bool('fa_debug');
      _ensurePerAceLength();
      const pa = perAceParams || {};
      for (let i = 0; i < configForm.perAce.length; i++) {
        const t = params[`dryer_temp_${i}`];
        const d = params[`dryer_duration_${i}`];
        configForm.perAce[i].dryer_temp     = numOrEmpty(t);
        configForm.perAce[i].dryer_duration = numOrEmpty(d);
        const aceSec = pa[i] || pa[String(i)] || {};
        configForm.perAce[i].feed_speed     = numOrEmpty(aceSec.feed_speed);
        configForm.perAce[i].retract_speed  = numOrEmpty(aceSec.retract_speed);
        configForm.perAce[i].load_length    = numOrEmpty(aceSec.load_length);
        configForm.perAce[i].retract_length = numOrEmpty(aceSec.retract_length);
        configForm.perAce[i].swap_retract_length = numOrEmpty(aceSec.swap_retract_length);
        for (let s = 0; s < 4; s++) {
          configForm.perAce[i].perSlot[s].load_length    = numOrEmpty(aceSec[`load_length_${s}`]);
          configForm.perAce[i].perSlot[s].retract_length = numOrEmpty(aceSec[`retract_length_${s}`]);
          configForm.perAce[i].perSlot[s].swap_retract_length = numOrEmpty(aceSec[`swap_retract_length_${s}`]);
        }
      }
    }
    function formToCfgContent(content) {
      const lines = content.split('\n');
      const numStr = (v) => (v === '' || v == null) ? '' : String(v);
      const mainRepl = {
        ace_device_count:   numStr(configForm.ace_device_count),
        feed_speed:         numStr(configForm.feed_speed),
        retract_speed:      numStr(configForm.retract_speed),
        load_length:        numStr(configForm.load_length),
        retract_length:     numStr(configForm.retract_length),
        seat_overshoot_length: numStr(configForm.seat_overshoot_length),
        swap_retract_length: numStr(configForm.swap_retract_length),
        swap_purge_length:   numStr(configForm.swap_purge_length),
        dryer_temp:         numStr(configForm.dryer_temp),
        dryer_duration:     numStr(configForm.dryer_duration),
        display_index_base: numStr(configForm.display_index_base),
        v2_order:           configForm.v2_order === 'last' ? 'last' : 'first',
        load_retry:         numStr(configForm.load_retry),
        extrusion_retry:    numStr(configForm.extrusion_retry),
        unload_retry:       numStr(configForm.unload_retry),
        state_debug:        configForm.state_debug ? 'true' : 'false',
        usb_debug:          configForm.usb_debug   ? 'true' : 'false',
        fa_debug:           configForm.fa_debug    ? 'true' : 'false',
      };
      for (let i = 0; i < configForm.perAce.length; i++) {
        const p = configForm.perAce[i];
        mainRepl[`dryer_temp_${i}`]     = numStr(p.dryer_temp);
        mainRepl[`dryer_duration_${i}`] = numStr(p.dryer_duration);
      }
      const perAceRepl = {};
      for (let i = 0; i < configForm.perAce.length; i++) {
        const p = configForm.perAce[i];
        const sec = {};
        sec.feed_speed     = numStr(p.feed_speed);
        sec.retract_speed  = numStr(p.retract_speed);
        sec.load_length    = numStr(p.load_length);
        sec.retract_length = numStr(p.retract_length);
        sec.swap_retract_length = numStr(p.swap_retract_length);
        for (let s = 0; s < 4; s++) {
          sec[`load_length_${s}`]    = numStr(p.perSlot[s].load_length);
          sec[`retract_length_${s}`] = numStr(p.perSlot[s].retract_length);
          sec[`swap_retract_length_${s}`] = numStr(p.perSlot[s].swap_retract_length);
        }
        perAceRepl[i] = sec;
      }
      const keyRegex = /^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/;
      const sectionRegex = /^\s*\[(.+?)\]\s*$/;
      const out = [];
      let curSection = null;
      const sectionEnd = {};
      const seenInSection = {};
      const seenSet = (sec) => {
        const k = sec === 'ace' ? 'ace' : `ace${sec}`;
        if (!seenInSection[k]) seenInSection[k] = new Set();
        return seenInSection[k];
      };
      const closeSection = () => {
        if (curSection === null) return;
        const k = curSection === 'ace' ? 'ace' : `ace${curSection}`;
        sectionEnd[k] = out.length;
      };
      for (const raw of lines) {
        const sm = raw.match(sectionRegex);
        if (sm) {
          closeSection();
          const head = sm[1].trim();
          if (head === 'ace') {
            curSection = 'ace';
          } else if (head.startsWith('ace ') || head.startsWith('ace\t')) {
            const idx = parseInt(head.split(/\s+/, 2)[1], 10);
            curSection = isNaN(idx) ? null : idx;
          } else {
            curSection = null;
          }
          out.push(raw);
          continue;
        }
        if (curSection === 'ace') {
          const m = raw.match(keyRegex);
          if (m && (m[1] in mainRepl)) {
            const key = m[1];
            const val = mainRepl[key];
            seenSet('ace').add(key);
            if (val === '' || val == null) continue;
            out.push(`${key}: ${val}`);
            continue;
          }
        } else if (typeof curSection === 'number') {
          const sec = perAceRepl[curSection];
          if (sec) {
            const m = raw.match(keyRegex);
            if (m && (m[1] in sec)) {
              const key = m[1];
              const val = sec[key];
              seenSet(curSection).add(key);
              if (val === '' || val == null) continue;
              out.push(`${key}: ${val}`);
              continue;
            }
          }
        }
        out.push(raw);
      }
      closeSection();
      const insertMissing = (sectionLabel, repl, seen) => {
        const missing = Object.keys(repl)
          .filter(k => !seen.has(k))
          .filter(k => repl[k] !== '' && repl[k] != null);
        if (!missing.length) return;
        const sectionKey = sectionLabel === '[ace]' ? 'ace'
          : `ace${sectionLabel.match(/\[ace (\d+)\]/)[1]}`;
        const endIdx = sectionEnd[sectionKey];
        const block = missing.map(k => `${k}: ${repl[k]}`);
        if (endIdx != null) {
          out.splice(endIdx, 0, ...block);
          for (const k of Object.keys(sectionEnd)) {
            if (sectionEnd[k] > endIdx) sectionEnd[k] += block.length;
          }
        } else {
          out.push('', sectionLabel, ...block);
        }
      };
      insertMissing('[ace]', mainRepl, seenSet('ace'));
      for (let i = 0; i < configForm.perAce.length; i++) {
        insertMissing(`[ace ${i}]`, perAceRepl[i], seenSet(i));
      }
      // Drop any [ace N] section header that ends up with no content.
      // When the user clears all per-ACE overrides, the keyed lines get
      // filtered out by the value=='' rule above, leaving a bare header.
      // Klipper refuses to load an empty section, so strip it here.
      const cleaned = [];
      for (let i = 0; i < out.length; i++) {
        const m = out[i].match(/^\s*\[ace\s+\d+\]\s*$/);
        if (!m) { cleaned.push(out[i]); continue; }
        let j = i + 1;
        let hasContent = false;
        while (j < out.length && !/^\s*\[.+\]\s*$/.test(out[j])) {
          const s = out[j].trim();
          if (s !== '' && !s.startsWith('#') && !s.startsWith(';')) {
            hasContent = true;
          }
          j++;
        }
        if (hasContent) {
          cleaned.push(out[i]);
          continue;
        }
        // Drop header + intervening lines; also strip one trailing blank
        // line from cleaned so we don't pile up separators.
        if (cleaned.length && cleaned[cleaned.length - 1].trim() === '') {
          cleaned.pop();
        }
        i = j - 1;
      }
      return cleaned.join('\n');
    }
    const updateState = reactive({
      current: "",
      latest: "",
      statusText: "",
      canApply: false,
      managed: false,
      busy: null,
      log: "",
    });
    const debugState = reactive({
      enabled: false,
      busy: false,
      rebootPrompt: false,
    });
    async function refreshDebugState() {
      try {
        const r = await fetch(`${API}/debug-mode`);
        const j = await r.json();
        if (r.ok) debugState.enabled = !!j.enabled;
      } catch (e) {
      }
    }
    async function refreshUpdateStatus() {
      try {
        const r = await fetch(`${API}/update/status`);
        const j = await r.json();
        if (r.ok) updateState.managed = !!j.managed;
      } catch (e) {
      }
    }
    async function debugEnable() {
      if (debugState.busy) return;
      debugState.busy = true;
      try {
        const r = await fetch(`${API}/debug-mode/enable`, {method: "POST"});
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        debugState.enabled = !!j.enabled;
        debugState.rebootPrompt = debugState.enabled;
      } catch (e) {
        setMacroLog(`${t("ui.config.debug_enable_failed")}: ${e.message || e}`);
      } finally {
        debugState.busy = false;
      }
    }
    async function debugDisable() {
      if (debugState.busy) return;
      confirm({
        title: t("ui.config.debug_disable_title"),
        message: t("ui.config.debug_disable_msg"),
        okLabel: t("ui.config.debug_disable_btn"),
        onOk: async () => {
          debugState.busy = true;
          try {
            const r = await fetch(`${API}/debug-mode/disable`, {method: "POST"});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            debugState.enabled = !!j.enabled;
            debugState.rebootPrompt = false;
          } catch (e) {
            setMacroLog(`${t("ui.config.debug_disable_failed")}: ${e.message || e}`);
          } finally {
            debugState.busy = false;
          }
        },
      });
    }
    function _parseUpdateResult(r) {
      const lines = r.status_lines || [];
      let cur = updateState.current, lat = updateState.latest;
      let canApply = false, statusText = "";
      for (const line of lines) {
        const mCur = line.match(/current=(\S+)/);
        if (mCur) cur = mCur[1];
        const mLat = line.match(/latest=(\S+)/);
        if (mLat) lat = mLat[1];
        const mTo = line.match(/to=(\S+)/);
        if (mTo) lat = mTo[1];
        if (line.startsWith("update_available")) canApply = true;
        if (line.startsWith("up_to_date") || line.startsWith("done")
            || line.startsWith("refusing_downgrade")) canApply = false;
        statusText = line;
      }
      updateState.current = cur || updateState.current;
      updateState.latest = lat || updateState.latest;
      updateState.canApply = canApply;
      updateState.statusText = statusText;
      updateState.log = r.stdout || "";
    }
    async function updateCheck() {
      if (updateState.busy) return;
      updateState.busy = "check";
      try {
        const r = await fetch(`${API}/update/check`);
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        _parseUpdateResult(j);
      } catch (e) {
        updateState.statusText = `${t("ui.config.update_failed")}: ${e.message || e}`;
        setMacroLog(`${t("ui.config.update_failed")}: ${e.message || e}`);
      } finally {
        updateState.busy = "";
      }
    }
    async function updateApply() {
      if (updateState.busy) return;
      confirm({
        title: t("ui.config.update_apply_title"),
        message: tHtml("ui.config.update_apply_msg", {
          from: updateState.current || "?",
          to:   updateState.latest  || "latest",
        }),
        okLabel: t("ui.config.update_apply_btn"),
        onOk: async () => {
          updateState.busy = "apply";
          try {
            const r = await fetch(`${API}/update/apply`, {method: "POST"});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            _parseUpdateResult(j);
            if (j.ok) {
              const reboot = (j.status_lines || []).some(l => String(l).startsWith("please_reboot"));
              setMacroLog(t(reboot ? "ui.config.update_done_reboot" : "ui.config.update_done"));
              if (reboot) printerRebootNeeded.value = true;
            }
          } catch (e) {
            updateState.statusText = `${t("ui.config.update_failed")}: ${e.message || e}`;
            setMacroLog(`${t("ui.config.update_failed")}: ${e.message || e}`);
          } finally {
            updateState.busy = "";
          }
        },
      });
    }
    async function loadConfig() {
      configLoadError.value = "";
      try {
        const r = await fetch(`${API}/config`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        config.path = j.path || "";
        config.content = j.content || "";
        config.params = j.params || {};
        // Revision token for the lost-update guard (see saveConfigForm).
        config.sha1 = j.sha1 || "";
        paramsToForm(j.params, j.per_ace_params || {});
      } catch (e) {
        configLoadError.value = t("ui.log.config_load_failed", {error: e});
      }
    }
    async function saveConfigForm() {
      configLog.value = t("ui.common.saving");
      try {
        // LOST-UPDATE GUARD: the form PATCHES the browser's cached copy of
        // the file, so a tab that loaded an older revision would silently
        // write it back (e.g. revert a cfg repaired via SSH). We send the
        // sha1 we loaded; on 409 the server
        // returns the CURRENT content and we re-apply the form values on top
        // of it and retry once, so the user's edit lands without clobbering
        // whatever else changed on disk meanwhile.
        let base = config.content;
        let sha1 = config.sha1;
        let newContent = formToCfgContent(base);
        let r = await fetch(`${API}/config`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          // Do NOT auto-restart Klipper: a bare Klipper restart applies most
          // [ace] scalars but NOT changes that need a full reboot (USB/serial
          // re-enumeration, PAXX boot-script settings), and it caused a scary
          // "503 Klippy Host not connected" mid-restart. Save the file and
          // tell the user to restart the printer so every change takes effect.
          body: JSON.stringify({content: newContent, restart_klipper: false,
                                base_sha1: sha1}),
        });
        if (r.status === 409) {
          let fresh = null;
          try { fresh = JSON.parse((await r.json()).detail); } catch (e) { fresh = null; }
          if (!fresh || typeof fresh.content !== "string") {
            throw new Error(`HTTP 409 ${t("ui.log.config_conflict_failed")}`);
          }
          base = fresh.content;
          sha1 = fresh.sha1 || "";
          newContent = formToCfgContent(base);
          r = await fetch(`${API}/config`, {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({content: newContent, restart_klipper: false,
                                  base_sha1: sha1}),
          });
          configLog.value = t("ui.log.config_conflict_merged");
        }
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
        const j = await r.json();
        config.content = newContent;
        config.sha1 = j.sha1 || "";
        rebootNeeded.value = true;
        configLog.value = `✓ ${j.path}\nBackup: ${j.backup}\n${t("ui.common.please_restart")}`;
      } catch (e) { configLog.value = `${t("ui.common.error")}: ${e}`; }
    }
    async function saveConfigRaw() {
      configLog.value = t("ui.log.saving_raw");
      try {
        // Same guard as the form save, but NO auto-retry: the raw editor's
        // content IS the user's text - silently rebasing it would discard
        // either their edit or the on-disk change. Report the conflict and
        // let them reload.
        const r = await fetch(`${API}/config`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          // Never restart Klipper from here - same policy as the form save.
          // A bare Klipper restart applies most [ace] scalars but misses
          // USB/serial re-enumeration and PAXX boot scripts, so it produces
          // a half-applied config.
          body: JSON.stringify({content: config.content,
                                restart_klipper: false,
                                base_sha1: config.sha1}),
        });
        if (r.status === 409) {
          configLog.value = t("ui.log.config_conflict_raw");
          return;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
        const j = await r.json();
        config.sha1 = j.sha1 || "";
        rebootNeeded.value = true;
        configLog.value = `✓ ${j.path}\nBackup: ${j.backup}\n${t("ui.common.please_restart")}`;
      } catch (e) { configLog.value = `${t("ui.common.error")}: ${e}`; }
    }
    async function setMode(m) {
      // multi<->head is a runtime flip (no reboot); only transitions crossing
      // 'normal' need a Klipper restart. In head mode each head is toggled to
      // feeder individually (per-head feeder checkbox), no single ACE head.
      if (state.mode === m) return;
      // Mode changes that cross 'normal' (stock<->ACE file swap) require all
      // toolheads unloaded - mirror the SET_ACE_MODE macro guard client-side so
      // the "unload first" rejection is visible in the web, not only in Fluidd's
      // console (action_respond_info). filament_at_extruder is the same toolhead
      // motion-sensor signal the macro checks (eN_filament.filament_detected).
      const cur = state.mode || "normal";
      if ((cur === "normal") !== (m === "normal")) {
        const loaded = (state.toolheads || []).filter(th => th.filament_at_extruder);
        if (loaded.length) {
          const heads = loaded.map(th => th.name || ("T" + th.idx)).join(", ");
          confirm({
            title: t("ui.config.mode_locked_title"),
            message: tHtml("ui.config.mode_locked_msg", {heads}),
            okLabel: "OK",
            dismissOnly: true,
          });
          return;
        }
      }
      const args = {MODE: m};
      confirm({
        title: t("ui.dialog.switch_mode_title", {mode: m}),
        message: tHtml("ui.dialog.switch_mode_msg", {mode: m}),
        okLabel: t("ui.dialog.switch"),
        onOk: async () => {
          // No web reboot banner for a mode change: a transition crossing
          // 'normal' makes ACE_RUN_MODE_SWITCH raise a reboot error, which
          // shows on the touchscreen popup AND Fluidd AND the web - for both
          // directions and any trigger (web or Fluidd SET_ACE_MODE), unlike a
          // web-only banner. multi<->head is a runtime flip and raises nothing.
          await run("SET_ACE_MODE", args);
        },
      });
    }
    const screenCanvas = ref(null);
    const floatScreenCanvas = ref(null);
    const screenPopout = ref(false);
    const screenFps = ref(0);
    const screenEtag = ref("");
    let frameCount = 0;
    let lastFpsTs = performance.now();
    let pollScreenBusy = false;
    function _liveScreenCanvases() {
      return [screenCanvas.value, floatScreenCanvas.value].filter(Boolean);
    }
    async function pollScreen() {
      if (pollScreenBusy) return;
      const targets = _liveScreenCanvases();
      if (!targets.length) return;
      pollScreenBusy = true;
      try {
        const headers = {};
        if (screenEtag.value) headers["If-None-Match"] = `"${screenEtag.value}"`;
        const r = await fetch(`${SCREEN}/snapshot`, {headers, cache: "no-store"});
        if (r.status === 304) {  }
        else if (r.ok) {
          screenEtag.value = (r.headers.get("ETag") || "").replace(/"/g, "");
          const blob = await r.blob();
          const img = await createImageBitmap(blob);
          for (const c of targets) {
            if (img.width !== c.width || img.height !== c.height) {
              c.width = img.width;
              c.height = img.height;
            }
            c.getContext("2d").drawImage(img, 0, 0);
          }
          frameCount += 1;
          const now = performance.now();
          if (now - lastFpsTs >= 1000) {
            screenFps.value = (frameCount * 1000) / (now - lastFpsTs);
            frameCount = 0;
            lastFpsTs = now;
          }
        }
      } catch (_) {  }
      finally { pollScreenBusy = false; }
    }
    function screenCoords(ev) {
      const c = ev.currentTarget;
      const rect = c.getBoundingClientRect();
      return {
        x: Math.round((ev.clientX - rect.left) * c.width / rect.width),
        y: Math.round((ev.clientY - rect.top) * c.height / rect.height),
      };
    }
    async function sendTouch(action, x, y) {
      try { await fetch(`${SCREEN}/touch?a=${action}&x=${x}&y=${y}`, {method: "POST"}); } catch (_) {}
    }
    function screenDown(ev) {
      ev.currentTarget?.setPointerCapture?.(ev.pointerId);
      const {x, y} = screenCoords(ev); sendTouch("down", x, y);
    }
    function screenMove(ev) {
      if (ev.buttons === 0) return;
      const {x, y} = screenCoords(ev); sendTouch("move", x, y);
    }
    function screenUp(ev) {
      const {x, y} = screenCoords(ev); sendTouch("up", x, y);
    }
    function toggleScreenPopout() {
      screenPopout.value = !screenPopout.value;
    }
    const popoutPos = reactive({
      x: parseFloat(localStorage.getItem("multiace.popout.x")) || null,
      y: parseFloat(localStorage.getItem("multiace.popout.y")) || null,
    });
    const popoutStyle = computed(() => {
      if (popoutPos.x == null || popoutPos.y == null) return {};
      return {
        left: popoutPos.x + "px",
        top:  popoutPos.y + "px",
        right: "auto",
        bottom: "auto",
      };
    });
    let _popoutDrag = null;
    function popoutDragStart(ev) {
      if (ev.target.closest(".screen-popout-close")) return;
      const panel = ev.currentTarget.parentElement;
      const rect = panel.getBoundingClientRect();
      _popoutDrag = {
        offX: ev.clientX - rect.left,
        offY: ev.clientY - rect.top,
        panel,
      };
      ev.currentTarget.setPointerCapture?.(ev.pointerId);
      ev.preventDefault();
    }
    function popoutDragMove(ev) {
      if (!_popoutDrag) return;
      const p = _popoutDrag;
      const w = p.panel.offsetWidth;
      const h = p.panel.offsetHeight;
      const maxX = window.innerWidth - w;
      const maxY = window.innerHeight - h;
      popoutPos.x = Math.max(0, Math.min(maxX, ev.clientX - p.offX));
      popoutPos.y = Math.max(0, Math.min(maxY, ev.clientY - p.offY));
    }
    function popoutDragEnd(ev) {
      if (!_popoutDrag) return;
      _popoutDrag = null;
      ev.currentTarget?.releasePointerCapture?.(ev.pointerId);
      if (popoutPos.x != null) localStorage.setItem("multiace.popout.x", String(popoutPos.x));
      if (popoutPos.y != null) localStorage.setItem("multiace.popout.y", String(popoutPos.y));
    }
    let ws = null;
    let wsReconnectTimer = null;
    function wsConnect() {
      try { ws = new WebSocket(WS_URL); }
      catch (e) { conn.value = {state: "err", text: `WS: ${e}`}; scheduleReconnect(); return; }
      ws.onopen = () => { conn.value = {state: "ok", text: t("ui.header.live")}; };
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "state") queueState(m);
          else if (m.type === "gcode_error") onGcodeError(m);
          else if (m.type === "error") conn.value = {state: "warn", text: m.error || t("ui.header.ws_error")};
        } catch (_) {}
      };
      ws.onclose = () => { conn.value = {state: "warn", text: t("ui.header.offline")}; restartNoteDown(); scheduleReconnect(); };
      ws.onerror = () => { conn.value = {state: "err", text: t("ui.header.ws_error")}; };
    }
    function scheduleReconnect() {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = setTimeout(wsConnect, 3000);
    }
    let screenTimer = null;
    function _updateScreenTimer() {
      clearInterval(screenTimer);
      const wantPoll = screenAvailable.value && screenPopout.value;
      if (wantPoll) screenTimer = setInterval(pollScreen, 200);
    }
    watch([screenPopout, screenAvailable], _updateScreenTimer, {immediate: true});
    const uploading = ref(false);
    const uploadInput = ref(null);
    const preflight = reactive({
      open:    false,
      busy:    false,
      sending: "",
      // Non-empty while "apply loadout" is writing slot-overrides / feeder
      // identities (the plan key being applied).
      applying: "",
      report:  null,
      error:   "",
      progress: null,
      // Manual slot reassignment for the slicer plan only: {origT: "ace-slot"}.
      // slicerSwaps holds the recomputed swap count (null = use the plan's
      // server value); recomputed automatically on every dropdown change.
      slicerOverrides: {},
      slicerSwaps: null,
      // Head mode: same idea for the single colour->target table. headOverrides
      // is {origT: target_id} ("feeder-N" / "slot-A-S"); headSwaps the recomputed
      // ACE-head swap count (null = use the server plan value).
      headOverrides: {},
      headSwaps: null,
      // Colour copies (Multifilament-Optimierung): {origT: [target_id,...]}
      // the loadout print may use (null = the server report's copies) and
      // the recomputed suggestion (null = the server report's copy_plan).
      headCopies: null,
      copyPlan: null,
      // Live copies of the three proposal plans ({hp: plan} like copyPlan;
      // null = the server report's copy rows), so a changed max-copies
      // setting re-plans them without a new upload.
      proposalCopies: null,
      // Print-preference toggles (default off): inject SET_PRINT_PREFERENCES so
      // an upload/SD start runs bed mesh / timelapse camera (stock only does
      // these on the official start).
      bedMesh: false,
      camera:  false,
      // Stock PA calibration ("flow calibration") behind the auto-load block;
      // off = the slicer's SET_PRESSURE_ADVANCE placeholder prints (issue #115).
      flowCal: false,
      // true when this report was produced in-browser (Pyodide worker) rather
      // than by the printer backend - selects the local rewrite+upload path.
      local: false,
    });
    function triggerUpload() { uploadInput.value && uploadInput.value.click(); }
    function tierLabel(tier) {
      const t_map = {
        exact_hex:        t("ui.preflight.tier_exact"),
        name_exact:       t("ui.preflight.tier_name"),
        name_base:        t("ui.preflight.tier_name_base"),
        name_canon:       t("ui.preflight.tier_name_canon"),
        fuzzy:            t("ui.preflight.tier_fuzzy"),
        fallback:         t("ui.preflight.tier_fallback"),
        duplicate:        t("ui.preflight.tier_duplicate"),
        no_slot:          t("ui.preflight.tier_no_slot"),
      };
      return t_map[tier] || tier;
    }
    function tierWarn(tier) {
      return tier && (tier === "fallback"
                      || tier === "duplicate"
                      || tier === "no_slot");
    }
    function rgbDec(hex) {
      const s = (hex || "").replace(/^#/, "");
      if (s.length < 6) return "";
      const r = parseInt(s.slice(0, 2), 16);
      const g = parseInt(s.slice(2, 4), 16);
      const b = parseInt(s.slice(4, 6), 16);
      return `${r},${g},${b}`;
    }
    function sortedMapping(plan) {
      const rows = (plan && plan.mapping) || [];
      return rows.slice().sort((a, b) => {
        const sa = a.slot, sb = b.slot;
        if (!sa && !sb) return a.t - b.t;
        if (!sa) return  1;
        if (!sb) return -1;
        if (sa.ace !== sb.ace)   return sa.ace  - sb.ace;
        if (sa.slot !== sb.slot) return sa.slot - sb.slot;
        return a.t - b.t;
      });
    }
    // --- FOrca mixed-nozzle view -----------------------------------------
    // A mixed-nozzle file is NOT an assignment problem: the slicer baked each
    // tool's own line width into that tool's extrusions, so a colour may only
    // print on a head carrying the diameter it was sliced for (the backend's
    // nozzle gate enforces it). What is left to decide is which ACE a spool
    // sits in - not which slot index. So this view replaces the normal
    // assignment table with a per-DIAMETER loading instruction.
    function forcaMixed() {
      const r = preflight.report;
      return !!(r && r.forca && r.nozzles_mixed);
    }
    function forcaNozzleList() {
      // [{head, dia}] in head order - the PRINTER's nozzles, not the file's.
      // The file's `nozzle_diameter` is indexed per FILAMENT (demand); which
      // head carries which nozzle is the machine's business (supply). Falls
      // back to reading the file's first four entries as heads when the
      // printer could not be asked.
      const r = preflight.report || {};
      const hn = r.head_nozzles || {};
      const src = Object.keys(hn).length ? hn : (r.nozzles || {});
      return Object.keys(src)
        .map(k => ({head: parseInt(k, 10), dia: Number(src[k])}))
        .filter(e => !isNaN(e.head) && e.dia > 0 && e.head < 4)
        .sort((a, b) => a.head - b.head);
    }
    function forcaToolDia(tt) {
      // Which nozzle diameter tool `tt` was sliced for, or null if the file
      // does not say (an unassigned filament - the list can be shorter than
      // the filament count, seen on a file whose 5th filament had no nozzle
      // wired yet).
      const nz = (preflight.report && preflight.report.nozzles) || {};
      const d = nz[String(tt)];
      return (d === undefined || d === null) ? null : Number(d);
    }
    // Swap count for the FOrca view. That view REPLACES the normal plan
    // tables, and with them the only place the count was rendered - so a
    // mixed-nozzle file would never tell the user how many tool changes its
    // assignment costs. No new arithmetic: the FOrca
    // rows edit the very plan the existing counters already track
    // (loadout in head mode, slicer in multi), including the live recalc
    // after a dropdown change.
    function forcaSwapsDisplay() {
      const r = preflight.report;
      if (!r) return null;
      return r.head_mode ? headSwapsDisplay() : slicerSwapsDisplay();
    }
    // Which plan the FOrca rows edit - the same one forcaGroups() reads.
    function forcaPlanKey() {
      const r = preflight.report;
      return (r && r.head_mode) ? "loadout" : "slicer";
    }
    // The rest of the plan header's info line, reusing the plan readers
    // verbatim (display only, no optimisation).
    // bg-swaps only in head mode - background swaps REQUIRE the 1:1
    // head<->ACE wiring, in multi one ACE feeds several heads and the
    // number would be meaningless (a hardware limit, not policy).
    function forcaBgLabel() {
      const r = preflight.report;
      if (!r || !r.head_mode) return "";
      return headPlanBgLabel(forcaPlanKey());
    }
    function forcaFlushG() {
      return headPlanFlushG(forcaPlanKey());
    }
    function forcaGroups() {
      // One section per nozzle DIAMETER, NAMED after the head(s) that carry
      // it. The two possible groupings answer different questions - per size
      // is the CONSTRAINT ("these colours need a 0.4 head"), per head is the
      // ACTION ("these spools go into ACE 3") - and which one is right
      // depends on whether the size is shared:
      //   one head per size  -> the section has no freedom in it, so naming
      //                         it by the size names an abstraction the user
      //                         then has to translate back
      //   two heads per size -> the freedom is real, and splitting them into
      //                         two head sections would present our pick as
      //                         if it were fixed
      // Grouping by size keeps the pool visible; the heading says "Head 3"
      // or "Head 1 + 2", so the common case reads as the action it is.
      const r = preflight.report;
      if (!r) return [];
      const nz = forcaNozzleList();
      const cols = (r.slicer_colors || []);
      // Current assignment, whichever mode produced it.
      const plan = r.head_mode ? (r.plans && r.plans.loadout)
                               : (r.plans && r.plans.slicer);
      const byT = {};
      ((plan && plan.mapping) || []).forEach(m => { byT[m.t] = m; });
      // One group per nozzle diameter the MACHINE offers, plus a catch-all for
      // colours whose nozzle the file does not declare. Grouping by the
      // machine's supply (not the file's per-filament list) is what makes
      // "several colours share one head" render correctly - and it is the
      // normal case for a feature split.
      const groups = {};
      nz.forEach(e => {
        const key = String(e.dia);
        if (!groups[key]) groups[key] = {dia: e.dia, heads: [], colors: []};
        groups[key].heads.push(e.head);
      });
      Object.keys(groups).forEach(k => {
        groups[k].heads.sort((a, b) => a - b);
        groups[k].head = groups[k].heads[0];   // sort key + section key
      });
      const unknown = {head: null, dia: null, heads: [], colors: []};
      cols.forEach(c => {
        const want = forcaToolDia(c.t);
        // A colour with no declared nozzle must be SHOWN, not dropped: it
        // used to vanish from the view entirely (a file with more filaments
        // than nozzle entries). It is also unconstrained by the gate, so the
        // user has to see that it needs a decision.
        const g = (want === null) ? unknown : groups[String(want)];
        if (!g) {
          // Declared a diameter no head on this machine carries.
          unknown.colors.push({
            t: c.t, hex: c.hex, name: c.name, material: c.material,
            slot: null, tier: "no_slot", ok: false, foundAt: null,
            wantDia: want, noHead: true,
          });
          return;
        }
        const m = byT[c.t] || {};
        // Unassigned does NOT mean "absent". The usual case is that the
        // spool IS loaded, just in a lane feeding a different nozzle size -
        // telling the user "not loaded" would send them hunting for a spool
        // sitting right in front of them. So look the colour up among the
        // live slots and report where it actually is.
        let foundAt = null;
        if (!m.slot) {
          const wantHex = (c.hex || "").toLowerCase().replace(/^#/, "");
          const wantMat = (c.material || "").toLowerCase();
          foundAt = (r.live_slots || []).find(s => {
            const have = (s.color || "").toLowerCase().replace(/^#/, "");
            const haveMat = (s.material || "").toLowerCase();
            return wantHex && have === wantHex
              && (!wantMat || !haveMat || haveMat === wantMat);
          }) || null;
        }
        g.colors.push({
          t: c.t, hex: c.hex, name: c.name, material: c.material,
          slot: m.slot || null, tier: m.tier || "no_slot",
          ok: !!(m.slot), foundAt: foundAt,
        });
      });
      const out = Object.keys(groups)
        .map(k => groups[k])
        .sort((a, b) => a.head - b.head);
      if (unknown.colors.length) out.push(unknown);
      return out;
    }
    function forcaGroupAces(g) {
      // HEAD MODE only: each head is wired 1:1 to one ACE, so a section that
      // names its head can name the unit the spools physically go into - the
      // whole point of the view. In MULTI the ACE is the free axis (slot N
      // feeds head N across all units), so naming one there would be a lie;
      // returns null and the heading stays head + size.
      const r = preflight.report;
      if (!r || !r.head_mode) return null;
      const ha = state.head_ace || {};
      const out = (g.heads || []).map(h => {
        const a = ha[h] ?? ha[String(h)];
        return dispIdx((a === undefined || a === null) ? h : Number(a));
      });
      return out.length ? out : null;
    }
    function forcaTargetHint(g, c) {
      // What the user physically has to do - and that is the OPPOSITE axis in
      // the two modes, so a single wording is wrong in one of them:
      //   MULTI     head == slot index -> the SLOT is fixed, the ACE is free
      //   HEAD MODE the ACE is wired to the head -> the ACE is fixed, any of
      //             its slots will do
      // Saying "head N" in multi is technically true but useless: spools go
      // into slots, not heads.
      const r = preflight.report;
      const heads = g.heads || [];
      if (!heads.length) {
        // Catch-all group: no head to name, and the REASON is per colour, not
        // per group - it mixes "the file names no nozzle" with "it names one
        // this machine does not have", and the latter differs by diameter.
        // Reading it off g.colors[0] would make all rows show the first
        // row's diameter.
        const cc = c || {};
        return cc.noHead
          ? t("ui.preflight.forca_dia_absent", {dia: cc.wantDia})
          : t("ui.preflight.forca_undeclared");
      }
      if (r && r.head_mode) {
        const aces = heads.map(h => {
          const ha = state.head_ace || {};
          const a = ha[h] ?? ha[String(h)];
          return dispIdx((a === undefined || a === null) ? h : Number(a));
        });
        return t("ui.preflight.forca_target_head",
                 {ace: aces.join("/"), head: heads.map(h => dispIdx(h)).join("/")});
      }
      return t("ui.preflight.forca_target_multi",
               {slot: heads.map(h => dispIdx(h)).join("/")});
    }
    function forcaFeasible() {
      // Judge the EFFECTIVE choice (base plan + the user's dropdown), not the
      // backend's initial assignment - otherwise picking a substitute spool
      // would leave the print button dead.
      const head = !!(preflight.report && preflight.report.head_mode);
      return forcaGroups().every(g => g.colors.every(c => head
        ? !!headEffectiveTargetId(c.t)
        : !!slicerEffectiveSlot(c.t)));
    }
    function slicerColorsInPrintOrder() {
      // The "Slicer colors" list ordered by FIRST use in the print.
      // First-appearance index from report.events
      // (the toolchange sequence); colours the body never prints keep
      // their T order after the used ones. No events -> original order.
      const cols = (preflight.report && preflight.report.slicer_colors) || [];
      const evs = (preflight.report && preflight.report.events) || [];
      const first = {};
      evs.forEach((t, i) => { if (!(t in first)) first[t] = i; });
      return cols.slice().sort((a, b) => {
        const fa = (a.t in first) ? first[a.t] : Infinity;
        const fb = (b.t in first) ? first[b.t] : Infinity;
        if (fa !== fb) return fa - fb;
        return a.t - b.t;
      });
    }
    // --- slicer-plan manual slot reassignment ---------------------------
    function slotKey(slot) { return slot ? (slot.ace + "-" + slot.slot) : ""; }
    function textOn(hex) {
      // Readable text colour (dark/light) for a coloured background, by
      // perceived luminance. Unknown/short colour -> neutral light text.
      const s = (hex || "").replace(/^#/, "");
      if (s.length < 6) return "#e8e8e8";
      const r = parseInt(s.slice(0, 2), 16);
      const g = parseInt(s.slice(2, 4), 16);
      const b = parseInt(s.slice(4, 6), 16);
      return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#111" : "#fff";
    }
    function _liveSlotByKey(key) {
      const [a, s] = (key || "").split("-").map(Number);
      return (preflight.report?.live_slots || [])
        .find(ls => ls.ace === a && ls.slot === s) || null;
    }
    function _slicerColorMat(tt) {
      const c = (preflight.report?.slicer_colors || []).find(x => x.t === tt);
      return ((c && c.material) || "").trim().toLowerCase();
    }
    function slicerSlotOptions(tt) {
      // Only loaded slots whose material matches the slicer-T (material-strict,
      // mirrors the auto-matcher).
      //
      // Under a mixed-nozzle file the option list is additionally restricted
      // to slots feeding a head of the RIGHT nozzle size - but it stays a
      // free choice within that (an unloaded colour can be swapped for
      // another one). The nozzle fixes which HEAD a tool
      // prints on, never which COLOUR the user wants there; picking a
      // different spool is legitimate and the auto-load block loads it.
      const mat = _slicerColorMat(tt);
      const allowed = forcaAllowedHeads(tt);
      return (preflight.report?.live_slots || []).filter(ls => {
        const m = (ls.material || "").trim().toLowerCase();
        if (mat && m && m !== mat) return false;
        if (allowed && !allowed.includes(headOfSlot(ls))) return false;
        return true;
      });
    }
    function multiTopology() {
      // The multi assignment report (copies / manual heads): slot N of any
      // ACE feeds head N. Everything else in the head-mode code is the
      // wired 1:1 topology.
      return !!(preflight.report && preflight.report.topology === "multi");
    }
    function slotHead(ace, slot) {
      // The head an ACE slot feeds in the report's topology (null = none).
      if (multiTopology()) {
        const heads = (preflight.report && preflight.report.ace_heads) || [];
        return heads.includes(Number(slot)) ? Number(slot) : null;
      }
      return aceHeadForAce(Number(ace));
    }
    function headOfSlot(ls) {
      // Which head a loaded slot feeds - the same modus split the backend
      // gate makes (_head_of). MULTI: head == slot index. HEAD MODE: the
      // head wired to that ACE, via the EXISTING reverse lookup
      // (head_ace maps head -> ace, not ace -> head; reading it the other
      // way round would silently return the wrong head).
      const r = preflight.report;
      if (r && r.head_mode) return slotHead(Number(ls.ace), Number(ls.slot));
      return ls.slot;
    }
    function forcaAllowedHeads(tt) {
      // The heads that CARRY the diameter this tool needs - demand from the
      // file, supply from the machine. null = no restriction (not FOrca,
      // uniform machine, or the file does not declare this tool's nozzle).
      if (!forcaMixed()) return null;
      const want = forcaToolDia(tt);
      if (want === null) return null;
      return forcaNozzleList()
        .filter(e => Math.abs(e.dia - want) < 0.001)
        .map(e => e.head);
    }
    function unsetSlotsForT(tt) {
      // Occupied slots the preflight may NOT use because nobody declared what
      // is in them (no tag, no override - the card shows them as "Job" or
      // blank). They used to be omitted silently, which reads as "the slot
      // does not exist"; listing them greyed out says WHY they are unusable.
      // Read straight from the dashboard state - live_slots has already
      // dropped them, and it must keep doing so (an undeclared slot must
      // never become a match target).
      const out = [];
      const allowed = forcaAllowedHeads(tt);
      (state.aces || []).forEach(a => {
        (a.slots || []).forEach(s => {
          if (preflight.report && preflight.report.head_mode) {
            const head = slotHead(a.idx, s.idx);
            if (head === null) return;               // no head can use this slot
            if (allowed && !allowed.includes(head)) return;
          }
          if (s.state === "empty") return;
          if (s.source === "rfid" || s.source === "override") return;
          out.push({ace: a.idx, slot: s.idx});
        });
      });
      return out;
    }
    function forcaHeadsForMaterial(mat) {
      // Which heads a MISSING material would have to be loaded on: union of
      // the allowed heads of every slicer colour asking for it. Naming the
      // head turns "PETG missing" into an instruction - under mixed nozzles
      // the material can only go on the head that carries the right size, so
      // "load PETG somewhere" is not actionable on its own.
      // [] = the file declares no nozzle for those colours (no constraint),
      // null = not a mixed-nozzle file at all.
      if (!forcaMixed()) return null;
      const want = (mat || "").trim().toLowerCase();
      const heads = new Set();
      (((preflight.report || {}).slicer_colors) || []).forEach(c => {
        if ((c.material || "").trim().toLowerCase() !== want) return;
        (forcaAllowedHeads(c.t) || []).forEach(h => heads.add(h));
      });
      return Array.from(heads).sort((a, b) => a - b);
    }
    function _slicerBaseSlot(tt) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      const row = ((plan && plan.mapping) || []).find(m => m.t === tt);
      return (row && row.slot) || null;
    }
    function slicerEffectiveSlot(tt) {
      const ov = preflight.slicerOverrides[tt];
      return ov ? _liveSlotByKey(ov) : _slicerBaseSlot(tt);
    }
    function onSlicerSlotChange(tt, key) {
      const base = _slicerBaseSlot(tt);
      if (base && slotKey(base) === key) delete preflight.slicerOverrides[tt];
      else preflight.slicerOverrides[tt] = key;
      recalcSlicer();
    }
    function _slicerEffectiveMapping() {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      return ((plan && plan.mapping) || [])
        .map(m => ({t: m.t, slot: slicerEffectiveSlot(m.t)}));
    }
    function realSwapCount(events, mapping) {
      // Port of backend _real_swap_count: replay the toolchange T-sequence,
      // initial loadout = slot==head per head, count head re-(ace,slot) changes.
      const byT = {};
      for (const m of mapping) if (m.slot) byT[m.t] = m.slot;
      const head = {0: [0, 0], 1: [0, 1], 2: [0, 2], 3: [0, 3]};
      let swaps = 0;
      for (const tt of (events || [])) {
        const slot = byT[tt];
        if (!slot) continue;
        const cur = head[slot.slot];
        if (!cur || cur[0] !== slot.ace || cur[1] !== slot.slot) {
          swaps++;
          head[slot.slot] = [slot.ace, slot.slot];
        }
      }
      return swaps;
    }
    function recalcSlicer() {
      preflight.slicerSwaps = realSwapCount(
        (preflight.report && preflight.report.events) || [],
        _slicerEffectiveMapping());
      scheduleEstimate("slicer");
    }
    function slicerSwapsDisplay() {
      if (preflight.slicerSwaps !== null) return preflight.slicerSwaps;
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      return (plan && plan.swaps) || 0;
    }
    // --- head-mode colour -> target (feeder pin / ACE slot) assignment -----
    function headTargets() {
      return (preflight.report && preflight.report.targets) || [];
    }
    function _headTargetById(id) {
      return headTargets().find(tg => tg.id === id) || null;
    }
    function headTargetOptions(tt) {
      // Only targets whose material matches the slicer-T (material-strict,
      // mirrors compute_head_mode_layout's pre-filter). Empty material on
      // either side is treated as a wildcard.
      // Under a mixed-nozzle file also restricted to targets on a head of
      // the right nozzle size - a pin target IS a head, an ace target's head
      // comes from the wiring (see headOfSlot). The choice stays free within
      // that set, exactly like the multi dropdown.
      const mat = _slicerColorMat(tt);
      const allowed = forcaAllowedHeads(tt);
      return headTargets().filter(tg => {
        const m = (tg.material || "").trim().toLowerCase();
        if (mat && m && m !== mat) return false;
        if (allowed) {
          const h = (tg.kind === "pin") ? tg.head : slotHead(tg.ace, tg.slot);
          if (h === null || !allowed.includes(h)) return false;
        }
        return true;
      });
    }
    function _headBaseTargetId(tt) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.loadout;
      const row = ((plan && plan.mapping) || []).find(m => m.t === tt);
      return (row && row.target_id) || "";
    }
    function headEffectiveTargetId(tt) {
      const ov = preflight.headOverrides[tt];
      return ov !== undefined ? ov : _headBaseTargetId(tt);
    }
    function pinLabel(head) {
      // A pinned target: a feeder head in head mode, a hand-fed head in multi.
      return (multiTopology() ? t("ui.preflight.manual_head") : t("ui.preflight.feeder"))
        + " " + dispIdx(head);
    }
    function headTargetLabel(tg) {
      if (!tg) return "";
      const mat = tg.material || "?";
      if (tg.kind === "pin") return pinLabel(tg.head) + " · " + mat;
      return "MMU " + dispIdx(tg.ace) + " Slot " + dispIdx(tg.slot) + " · " + mat;
    }
    // Background for a colour chip or select: the "no colour" placeholder
    // goes through a CSS variable (fallback #444), so the light shade can
    // draw it as an empty outline instead of a black square. Filament
    // colours are 6-digit hex, the placeholder is exactly "#444".
    function chipBg(c) {
      return c && c !== "#444" ? c : "var(--nt-nocolor, #444)";
    }
    function headTargetColor(id) {
      const tg = _headTargetById(id);
      return (tg && tg.color) || "#444";
    }
    function headTargetLabelById(id) {
      const tg = _headTargetById(id);
      return tg ? headTargetLabel(tg) : "";
    }
    // Custom dropdown for the head-mode target picker: native <option>s
    // cannot render a colour chip NEXT to a label (only full-background
    // fills, which were loud/uneven), so the open list is
    // a small custom popup with chip + label per entry. One open at a time,
    // keyed by the slicer-T; items pick on mousedown (fires before the
    // button's blur closes the list).
    const hmDropOpen = ref(null);
    function hmDdToggle(tt) {
      hmDropOpen.value = (hmDropOpen.value === tt) ? null : tt;
    }
    function hmDdClose() { hmDropOpen.value = null; }
    function hmDdPick(tt, id) {
      hmDropOpen.value = null;
      onHeadTargetChange(tt, id);
    }
    function onHeadTargetChange(tt, id) {
      const base = _headBaseTargetId(tt);
      if (id === base) delete preflight.headOverrides[tt];
      else preflight.headOverrides[tt] = id;
      recalcHead();
    }
    function _headEffectiveAssignment() {
      // {origT: target_id} across every slicer colour (base plan + overrides).
      const out = {};
      for (const c of ((preflight.report && preflight.report.slicer_colors) || [])) {
        out[c.t] = headEffectiveTargetId(c.t);
      }
      return out;
    }
    function headSwapCount(events, assignment, copies) {
      // Port of backend head_mode_swap_count: only ACE-target (ace,slot)
      // changes count, PER ACE head (each ACE head swaps independently);
      // pinned feeder colours never swap. With colour copies the count is
      // the per-event choice of simulateDynamicSwaps (same as the backend).
      if (copies && Object.keys(copies).length) {
        return simulateDynamicSwaps(events, _targetsWithCopies(assignment, copies),
                                    headBgHeads()).swaps;
      }
      const cur = {};
      let swaps = 0;
      for (const tt of (events || [])) {
        const tg = _headTargetById(assignment[tt]);
        if (!tg || tg.kind !== "ace") continue;
        const key = tg.ace + "-" + tg.slot;
        if (cur[tg.head] !== key) { swaps++; cur[tg.head] = key; }
      }
      return swaps;
    }
    // --- colour copies (Multifilament-Optimierung) ---------------------------
    // One slicer colour may print from SEVERAL heads when a second spool of
    // it is loaded elsewhere; per toolchange the head that already holds the
    // colour is taken (Belady otherwise). JS mirror of the post-processor's
    // simulate_dynamic_swaps / detect_color_copies / plan_color_copies so the
    // row updates live on loadout edits and on the max-copies setting; the
    // print sends the copies verbatim (head_copies), so preview == print.
    function headBgHeads() {
      const bg = preflight.report && preflight.report.bg_swap;
      return (bg && bg.enabled_heads) || [];
    }
    function headMaxCopies() {
      return Math.max(1, Math.min(4, Number(state.preflight_max_copies || 1)));
    }
    function headCopiesEffective() {
      // {origT: [target_id...]} actually usable: capped by the setting,
      // never the colour's own target nor a target on its head.
      const src = preflight.headCopies !== null
                ? preflight.headCopies
                : ((preflight.report && preflight.report.copies) || {});
      const limit = headMaxCopies();
      const out = {};
      if (limit <= 1) return out;
      const asn = _headEffectiveAssignment();
      for (const k of Object.keys(src)) {
        const prim = _headTargetById(asn[k]);
        if (!prim) continue;
        const heads = new Set([prim.head]);
        const lst = [];
        for (const id of (src[k] || [])) {
          const tg = _headTargetById(id);
          if (!tg || tg.kind !== "ace" || heads.has(tg.head)) continue;
          lst.push(id); heads.add(tg.head);
          if (lst.length >= limit - 1) break;
        }
        if (lst.length) out[k] = lst;
      }
      return out;
    }
    function _targetsWithCopies(assignment, copies) {
      const out = {};
      for (const k of Object.keys(assignment)) {
        const prim = _headTargetById(assignment[k]);
        if (!prim) continue;
        out[k] = [prim].concat(((copies && copies[k]) || [])
                    .map(id => _headTargetById(id)).filter(x => !!x));
      }
      return out;
    }
    function simulateDynamicSwaps(events, targetsOf, bgHeads) {
      const n = (events || []).length;
      const pos = {};
      events.forEach((tt, k) => { (pos[tt] = pos[tt] || []).push(k); });
      const nextUse = (tt, k) => {
        const p = pos[tt];
        if (!p) return n;
        let lo = 0, hi = p.length;
        while (lo < hi) { const m = (lo + hi) >> 1; if (p[m] <= k) lo = m + 1; else hi = m; }
        return lo < p.length ? p[lo] : n;
      };
      // Per head: positions of events only that head can print (mirror of
      // the post-processor's forced-event tie-break between empty heads).
      const forced = {};
      events.forEach((tt, k) => {
        const heads = new Set((targetsOf[tt] || targetsOf[String(tt)] || [])
          .filter(e => e.kind === "ace").map(e => e.head));
        if (heads.size === 1) { const h = [...heads][0]; (forced[h] = forced[h] || []).push(k); }
      });
      const nextForced = (h, k) => {
        const p = forced[h];
        if (!p) return n;
        let lo = 0, hi = p.length;
        while (lo < hi) { const m = (lo + hi) >> 1; if (p[m] <= k) lo = m + 1; else hi = m; }
        return lo < p.length ? p[lo] : n;
      };
      const bg = new Set((bgHeads || []).map(Number));
      const cur = {};           // head -> {key, tool}
      const swapsBy = {};
      let swaps = 0;
      const plan = [];
      events.forEach((tt, k) => {
        const cands = targetsOf[tt] || targetsOf[String(tt)] || [];
        let chosen = null;
        for (const e of cands) {
          if (e.kind === "pin") { chosen = e; break; }
          const c = cur[e.head];
          if (c && c.key === e.ace + "-" + e.slot) { chosen = e; break; }
        }
        if (!chosen) {
          let bestKey = null;
          cands.forEach((e, i) => {
            if (e.kind !== "ace") return;
            const c = cur[e.head];
            const key = c ? [-nextUse(c.tool, k), 0, bg.has(Number(e.head)) ? 0 : 1, swapsBy[e.head] || 0, i]
                          : [-(n + 1), -nextForced(e.head, k), i, 0, i];
            if (bestKey === null || _keyLess(key, bestKey)) { bestKey = key; chosen = e; }
          });
          if (!chosen) { plan.push(null); return; }
          swaps++;
          swapsBy[chosen.head] = (swapsBy[chosen.head] || 0) + 1;
          cur[chosen.head] = {key: chosen.ace + "-" + chosen.slot, tool: tt};
        }
        plan.push(chosen);
      });
      return {swaps, plan};
    }
    function _keyLess(a, b) {
      for (let i = 0; i < a.length; i++) { if (a[i] !== b[i]) return a[i] < b[i]; }
      return false;
    }
    function headCopiesStrict() {
      return !!state.preflight_copies_strict;
    }
    const _COLOR_QUALIFIERS = ["Dark", "Light"];
    const _COLOR_SYNONYMS = {Silver: "Gray", Gold: "Yellow"};
    function _colorBase(name) {
      for (const q of _COLOR_QUALIFIERS) {
        if (name.startsWith(q) && name.length > q.length) return name.slice(q.length);
      }
      return name;
    }
    function _copyMatches(tg, c) {
      // Mirror of the post-processor's copy match: same material (empty on
      // either side = wildcard) and the matcher's tiers 1-4 on the colour:
      // exact hex, same approx name, same base name (Dark/Light stripped),
      // same synonym canon. Never fuzzy. Strict = tier 1 only.
      const mat = (c.material || "").trim().toLowerCase();
      const m = (tg.material || "").trim().toLowerCase();
      if (mat && m && m !== mat) return false;
      const hex = (c.hex || "").replace("#", "").toLowerCase();
      const th = (tg.color || "").replace("#", "").toLowerCase();
      if (!hex) return false;
      if (th && hex === th) return true;
      if (headCopiesStrict()) return false;
      const cn = c.name || "", tn = tg.name || "";
      if (!cn || cn === "?" || !tn || tn === "?") return false;
      if (cn === tn) return true;
      const cb = _colorBase(cn), tb = _colorBase(tn);
      if (cb === tb) return true;
      return (_COLOR_SYNONYMS[cb] || cb) === (_COLOR_SYNONYMS[tb] || tb);
    }
    function detectHeadCopies() {
      // Loaded, unclaimed slots holding a slicer colour on another head.
      const asn = _headEffectiveAssignment();
      const claimed = new Set(Object.values(asn).filter(x => !!x));
      const out = {};
      for (const c of ((preflight.report && preflight.report.slicer_colors) || [])) {
        const prim = _headTargetById(asn[c.t]);
        if (!prim) continue;
        const allowed = forcaAllowedHeads(c.t);
        const heads = new Set([prim.head]);
        for (const tg of headTargets()) {
          if (tg.kind !== "ace" || claimed.has(tg.id) || heads.has(tg.head)) continue;
          if (allowed && !allowed.includes(tg.head)) continue;
          if (!_copyMatches(tg, c)) continue;
          (out[c.t] = out[c.t] || []).push(tg.id);
          heads.add(tg.head);
        }
      }
      return out;
    }
    function _planCopiesGreedy(events, targets, free, limit, bg, order) {
      // Greedy: the (colour, empty slot) that saves most swaps, repeated.
      // Shared by the loadout suggestion and the proposal plans (JS mirror
      // of the post-processor's plan_color_copies). `order` = the colour
      // iteration order for tie-breaks (Python walks the assignment in
      // insertion order; integer keys here would always iterate ascending).
      const before = simulateDynamicSwaps(events, targets, bg).swaps;
      const res = {max_copies: limit, suggestions: [], swaps_before: before, swaps_after: before};
      if (limit <= 1) return res;
      let cur = before;
      const keys = (order || Object.keys(targets)).map(String).filter(k => targets[k]);
      while (free.length) {
        let best = null;
        for (const k of keys) {
          const lst = targets[k];
          if (lst.length >= limit) continue;
          const allowed = forcaAllowedHeads(Number(k));
          const heads = new Set(lst.map(x => x.head));
          for (const f of free) {
            if (heads.has(f.head)) continue;
            if (allowed && !allowed.includes(f.head)) continue;
            const trial = Object.assign({}, targets);
            trial[k] = lst.concat([{kind: "ace", head: f.head, ace: f.ace, slot: f.slot}]);
            const sw = simulateDynamicSwaps(events, trial, bg).swaps;
            if (sw < cur && (best === null || sw < best.sw)) best = {sw, k, f};
          }
        }
        if (!best) break;
        targets[best.k] = targets[best.k].concat([{kind: "ace", head: best.f.head, ace: best.f.ace, slot: best.f.slot}]);
        free = free.filter(f => !(f.ace === best.f.ace && f.slot === best.f.slot));
        res.suggestions.push({t: Number(best.k), ace: best.f.ace, slot: best.f.slot, head: best.f.head, swaps: best.sw});
        cur = best.sw;
      }
      res.swaps_after = cur;
      res.targets = targets;
      return res;
    }
    function planHeadCopies() {
      const events = (preflight.report && preflight.report.events) || [];
      const asn = _headEffectiveAssignment();
      const targets = _targetsWithCopies(asn, headCopiesEffective());
      const free = ((preflight.report && preflight.report.empty_slots) || [])
        .map(e => ({ace: Number(e.ace), slot: Number(e.slot), head: slotHead(e.ace, e.slot)}))
        .filter(e => e.head !== null);
      return _planCopiesGreedy(events, targets, free, headMaxCopies(), headBgHeads());
    }
    function _wiredAces() {
      // [ace index] of every ACE an ACE head is wired to (head mode); in
      // the multi topology every attached ACE feeds every head.
      if (multiTopology()) return (state.aces || []).map(a => Number(a.idx));
      const out = [];
      for (const h of (state.ace_heads || [])) {
        const ha = state.head_ace || {};
        out.push(Number(ha[h] ?? ha[String(h)] ?? h));
      }
      return out;
    }
    function planProposalCopies(hp) {
      // Copies for a PROPOSED loadout: every slot of an ACE head the
      // proposal leaves free is a candidate (mirror of proposal_copies).
      const plan = preflight.report && preflight.report.plans && preflight.report.plans[hp];
      if (!plan || !plan.feasible || !plan.mapping) return null;
      const events = (preflight.report && preflight.report.events) || [];
      const targets = {};
      const used = new Set();
      for (const m of plan.mapping) {
        if (!m || m.kind === "none" || m.tier === "copy") continue;
        targets[m.t] = [{kind: m.kind, head: m.head, ace: m.ace, slot: m.slot}];
        if (m.kind === "ace") used.add(m.ace + "-" + m.slot);
      }
      const free = [];
      for (const a of _wiredAces()) {
        for (let si = 0; si < 4; si++) {
          const h = slotHead(a, si);
          if (h === null) continue;
          if (!used.has(a + "-" + si)) free.push({ace: a, slot: si, head: h});
        }
      }
      // The proposal's assignment is built in first-use order of the
      // events - mirror that so equal-saving ties resolve like the server.
      const order = [];
      for (const tt of events) if (!order.includes(tt)) order.push(tt);
      return _planCopiesGreedy(events, targets, free, headMaxCopies(), headBgHeads(), order);
    }
    function proposalCopyPlan(hp) {
      const pc = preflight.proposalCopies;
      return (pc && pc[hp]) || null;
    }
    function headPlanMapping(hp) {
      // The proposal table: the server's rows without its copy rows, plus
      // the live-planned copies, in the server's re-stick order.
      const plan = preflight.report && preflight.report.plans && preflight.report.plans[hp];
      if (!plan || !plan.mapping) return [];
      const live = proposalCopyPlan(hp);
      if (!live) return plan.mapping;
      const rows = plan.mapping.filter(m => m.tier !== "copy");
      for (const sg of live.suggestions) {
        rows.push({t: sg.t, kind: "ace", head: sg.head, ace: sg.ace, slot: sg.slot, tier: "copy"});
      }
      const rank = {pin: 0, ace: 1};
      const nz = v => (v === null || v === undefined) ? 99 : v;
      rows.sort((a, b) => {
        const ka = [rank[a.kind] ?? 2, nz(a.ace), nz(a.slot), nz(a.head), a.t || 0];
        const kb = [rank[b.kind] ?? 2, nz(b.ace), nz(b.slot), nz(b.head), b.t || 0];
        for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
        return 0;
      });
      return rows;
    }
    function proposalCopyIds(hp) {
      // {t: ["slot-A-S", ...]} of the proposal's live copies (print payload).
      const live = proposalCopyPlan(hp);
      const out = {};
      if (!live) return out;
      for (const sg of live.suggestions) {
        (out[String(sg.t)] = out[String(sg.t)] || []).push("slot-" + sg.ace + "-" + sg.slot);
      }
      return out;
    }
    function recalcCopies() {
      preflight.headCopies = detectHeadCopies();
      preflight.copyPlan = planHeadCopies();
      const pc = {};
      for (const hp of ["optimize", "layer", "color"]) {
        const p = planProposalCopies(hp);
        if (p) pc[hp] = p;
      }
      preflight.proposalCopies = pc;
    }
    function headCopyPlan() {
      if (preflight.copyPlan !== null) return preflight.copyPlan;
      const cp = preflight.report && preflight.report.copy_plan;
      return cp || {max_copies: 1, suggestions: [], swaps_before: null, swaps_after: null};
    }
    function headCopyList() {
      // [{t, id}] of the copies in use, for the row's "active" line.
      const eff = headCopiesEffective();
      const out = [];
      for (const k of Object.keys(eff)) for (const id of eff[k]) out.push({t: Number(k), id});
      return out;
    }
    function copySlotLabel(ace, slot) {
      return "MMU " + dispIdx(ace) + " Slot " + dispIdx(slot);
    }
    async function _setPreflightCopiesArgs(args) {
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "ACE_SET_PREFLIGHT_COPIES", args}),
        });
      } catch (_) {}
      await reloadState();
      if (preflight.report && preflight.report.head_mode) recalcHead();
    }
    async function setPreflightCopies(v) {
      const n = Math.max(1, Math.min(4, Number(v) || 1));
      await _setPreflightCopiesArgs({MAX: n});
    }
    async function setPreflightCopiesStrict(on) {
      await _setPreflightCopiesArgs({STRICT: on ? 1 : 0});
    }
    function recalcHead() {
      if (preflight.report && preflight.report.head_mode) recalcCopies();
      preflight.headSwaps = headSwapCount(
        (preflight.report && preflight.report.events) || [],
        _headEffectiveAssignment(), headCopiesEffective());
      scheduleEstimate("loadout");
    }
    function headSwapsDisplay() {
      if (preflight.headSwaps !== null) return preflight.headSwaps;
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.loadout;
      return (plan && plan.swaps) || 0;
    }
    function headFeasible() {
      // Every slicer colour must resolve to a real target (no unassigned row).
      const asn = _headEffectiveAssignment();
      return Object.values(asn).every(id => !!_headTargetById(id));
    }
    // --- the three head plans (loadout editable, optimize/layer proposed) -----
    function headPlanFeasible(hp) {
      if (hp === "loadout") return headFeasible();
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return !!(p && p.feasible);
    }
    function headPlanSwaps(hp) {
      if (hp === "loadout") return headSwapsDisplay();
      const live = proposalCopyPlan(hp);
      if (live) return live.swaps_after;
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && p.swaps) || 0;
    }
    function _bgStatsFor(targets) {
      // Port of head_mode_bg_stats over the per-event plan: every unload
      // the rewrite would stamp (a released ACE head whose next arrival
      // needs a different slot), rated by its parked window.
      const rep = preflight.report;
      const events = (rep && rep.events) || [];
      const n = events.length;
      const times = rep && rep.event_times;
      const haveT = Array.isArray(times) && times.length === n;
      const bgs = rep && rep.bg_swap;
      const minW = Number((bgs && bgs.min_window_min) || 3);
      const saving = Number((bgs && bgs.inline_saving_s) || 180);
      const bgSet = new Set(headBgHeads().map(Number));
      const plan = simulateDynamicSwaps(events, targets, headBgHeads()).plan;
      const st = {unloads: 0, bg_ok: 0, bg_small: 0, bg_unknown: 0, bg_disabled: 0, saved_s: 0};
      const same = (a, b) => a.kind === b.kind && a.head === b.head && a.ace === b.ace && a.slot === b.slot;
      for (let i = 1; i < n; i++) {
        const cur = plan[i], rel = plan[i - 1];
        if (!cur || (cur.kind !== "pin" && cur.kind !== "ace")) continue;
        if (!rel || rel.kind !== "ace") continue;
        if ((events[i - 1] === events[i] && same(rel, cur)) || rel.head === cur.head) continue;
        let nxt = null, nxtJ = null;
        for (let j = i + 1; j < n; j++) {
          const e2 = plan[j];
          if (e2 && e2.kind === "ace" && e2.head === rel.head) { nxt = e2; nxtJ = j; break; }
        }
        if (!nxt || (nxt.ace === rel.ace && nxt.slot === rel.slot)) continue;
        st.unloads++;
        const rNow = haveT ? times[i] : null;
        const rNxt = haveT ? times[nxtJ] : null;
        const window = (rNow !== null && rNow !== undefined && rNxt !== null && rNxt !== undefined)
                     ? (rNow - rNxt) : null;
        if (!bgSet.has(Number(rel.head))) st.bg_disabled++;
        else if (window === null) st.bg_unknown++;
        else if (window < minW) st.bg_small++;
        else st.bg_ok++;
      }
      st.saved_s = st.bg_ok * saving;
      return st;
    }
    // Background-unload balance of a head-mode plan: live from the plan the
    // table shows once a recalc ran (copies, edits), else the server's.
    function headPlanBg(hp) {
      if (multiTopology()) return null;          // no background swaps in multi
      const live = (hp === "loadout") ? preflight.copyPlan : proposalCopyPlan(hp);
      if (live && live.targets) {
        const st = _bgStatsFor(live.targets);
        return st.unloads > 0 ? st : null;
      }
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && p.bg && p.bg.unloads > 0) ? p.bg : null;
    }
    // Same-nozzle contamination volume of a head plan, as grams (server-
    // computed from the slicer flush matrix; null = no matrix in the file).
    function headPlanFlushG(hp) {
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && typeof p.flush_g === "number") ? p.flush_g : null;
    }
    function headPlanBgLabel(hp) {
      const bg = headPlanBg(hp);
      if (!bg) return "";
      let s = t('ui.preflight.bg_label') + " " + bg.bg_ok + "/" + bg.unloads;
      const min = Math.round((bg.saved_s || 0) / 60);
      if (bg.bg_ok > 0 && min > 0) {
        s += " (~" + min + " min " + t('ui.preflight.bg_saved') + ")";
      }
      // Why the rest does NOT qualify (short windows vs chain on a
      // non-BG head vs missing M73 look different here).
      const parts = [];
      if (bg.bg_small)    parts.push(bg.bg_small + " " + t('ui.preflight.bg_too_short'));
      if (bg.bg_disabled) parts.push(bg.bg_disabled + " " + t('ui.preflight.bg_not_enabled'));
      if (bg.bg_unknown)  parts.push(bg.bg_unknown + " " + t('ui.preflight.bg_no_m73'));
      if (parts.length) s += " · " + parts.join(", ");
      return s;
    }
    function _headSlicerColor(tt) {
      return ((preflight.report && preflight.report.slicer_colors) || [])
        .find(c => c.t === tt) || null;
    }
    function headSlicerHex(tt) {
      const c = _headSlicerColor(tt);
      return (c && c.hex) || "#444";
    }
    function headSlicerMat(tt) {
      const c = _headSlicerColor(tt);
      return (c && c.material) || "?";
    }
    function headProposalLabel(m) {
      // The proposed destination for a slicer colour (load that colour here).
      if (!m || m.kind === "none") return "";
      if (m.kind === "pin") return pinLabel(m.head);
      return "MMU " + dispIdx(m.ace) + " Slot " + dispIdx(m.slot);
    }
    // ---- Send-to-multiACE inbox -----------------------------------------
    // A slicer pushed a raw gcode to /api/preflight/inbox (store-only). The
    // pickup fetches it INTO THE BROWSER and runs the normal Pyodide
    // preflight - analysis on this PC, against the slot state of right now,
    // never on the U1's CPU. One-shot delivery: the inbox is cleared at
    // pickup, so cancelling the preview means re-sending from the slicer.
    // Auto-open fires once per delivery and only while the printer is not
    // printing; a delivery during a print waits as a banner.
    let _inboxAutoTried = "";
    const inboxBusy = ref(false);
    function _inboxKey() {
      const ib = state.preflight_inbox || {};
      return ib.pending ? (ib.ts + ":" + (ib.name || "")) : "";
    }
    const inboxCanStart = computed(() => {
      const ps = state.printer_state;
      return !!(state.preflight_inbox && state.preflight_inbox.pending)
        && !inboxBusy.value
        && ps !== 'printing' && ps !== 'paused' && ps !== 'busy';
    });
    function _maybeAutoOpenInbox() {
      if (panelMode) return;
      if (!inboxCanStart.value) return;
      if (preflight.open || preflight.busy) return;
      const key = _inboxKey();
      if (!key || key === _inboxAutoTried) return;
      _inboxAutoTried = key;
      startInboxPreflight();
    }
    async function startInboxPreflight() {
      if (inboxBusy.value) return;
      const ib = state.preflight_inbox;
      if (!ib || !ib.pending) return;
      inboxBusy.value = true;
      try {
        const r = await fetch(`${API}/preflight/inbox/file`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const blob = await r.blob();
        const f = new File([blob], ib.name || "upload.gcode",
                           {type: "text/plain"});
        await fetch(`${API}/preflight/inbox`, {method: "DELETE"});
        state.preflight_inbox = {pending: false, name: null, size: 0, ts: 0};
        _runPreflight(f);
      } catch (e) {
        setMacroLog(t('ui.inbox.fetch_failed', {error: String(e)}));
      } finally {
        inboxBusy.value = false;
      }
    }
    function dismissInbox() {
      fetch(`${API}/preflight/inbox`, {method: "DELETE"}).catch(() => {});
      state.preflight_inbox = {pending: false, name: null, size: 0, ts: 0};
    }
    function onUploadGcode(fileList) {
      const f = fileList && fileList[0];
      if (uploadInput.value) uploadInput.value.value = "";
      if (!f) return;
      const lower = f.name.toLowerCase();
      if (!(lower.endsWith(".gcode") || lower.endsWith(".gco") || lower.endsWith(".g"))) {
        confirm({
          title: t("ui.upload.title"),
          message: t("ui.upload.bad_ext"),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
        return;
      }
      _runPreflight(f);
    }
    // ---- in-browser (Pyodide) preflight ----------------------------------
    // The heavy parse/rewrite runs in a Web Worker via Pyodide, executing the
    // UNMODIFIED post-processor + preflight_core (served by /api/preflight/pysrc)
    // - the same Python the backend runs, so no JS re-port / drift. Falls back
    // to the server /api/preflight path if the browser can't do it (no Worker,
    // offline CDN, etc.).
    const PYODIDE_INDEX_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/";
    let preflightWorker = null;
    let preflightWorkerReady = null;
    let preflightFile = null;
    let preflightJobId = "";
    let preflightJobSeq = 0;

    function ensurePreflightWorker() {
      if (!window.Worker) throw new Error(t("ui.preflight.local_worker_missing"));
      if (preflightWorker && preflightWorkerReady) return preflightWorkerReady;
      // Content hash from the served index.html; the fixed tag is the
      // fallback for a shell that predates it.
      const workerV = (window.MA_ASSET_V && window.MA_ASSET_V.worker)
        || "pyodide-20260624";
      preflightWorker = new Worker("preflight_pyodide_worker.js?v="
        + encodeURIComponent(workerV));
      preflightWorkerReady = (async () => {
        const r = await fetch(`${API}/preflight/pysrc`);
        if (!r.ok) throw new Error("pysrc " + r.status);
        const src = await r.json();
        await new Promise((resolve, reject) => {
          const onMsg = (ev) => {
            const m = ev.data || {};
            if (m.type === "ready") {
              preflightWorker.removeEventListener("message", onMsg); resolve();
            } else if (m.type === "error") {
              preflightWorker.removeEventListener("message", onMsg);
              reject(new Error(m.message || "worker init failed"));
            }
          };
          preflightWorker.addEventListener("message", onMsg);
          preflightWorker.postMessage({
            type: "init",
            pyodideIndexURL: PYODIDE_INDEX_URL,
            postprocessSrc: src.postprocess,
            coreSrc: src.core,
            swapCostSrc: src.swap_cost || "",
          });
        });
      })();
      // On a failed bring-up, drop the worker so the next attempt re-inits.
      preflightWorkerReady.catch(() => {
        try { preflightWorker.terminate(); } catch (_) {}
        preflightWorker = null; preflightWorkerReady = null;
      });
      return preflightWorkerReady;
    }

    function runPreflightWorker(type, payload, onProgress) {
      return new Promise((resolve, reject) => {
        const worker = preflightWorker;
        const jobId = payload.jobId;
        const onMsg = (ev) => {
          const msg = ev.data || {};
          if (msg.jobId && msg.jobId !== jobId) return;
          if (msg.type === "progress") { if (onProgress) onProgress(msg); return; }
          if (msg.type === "error") {
            worker.removeEventListener("message", onMsg);
            reject(new Error(msg.message || "worker error"));
            return;
          }
          if ((type === "analyze" && msg.type === "analyze-done")
              || (type === "rewrite" && msg.type === "rewrite-done")
              || (type === "estimate" && msg.type === "estimate-done")
              || (type === "estimate-all" && msg.type === "estimate-all-done")) {
            worker.removeEventListener("message", onMsg);
            resolve(msg);
          }
        };
        worker.addEventListener("message", onMsg);
        worker.postMessage(Object.assign({type}, payload));
      });
    }

    // Live ACE/slot identity + head-mode context, in the exact shape
    // preflight_core expects. Fetched from the backend (single source) rather
    // than re-derived in JS.
    async function loadLiveSlotsForPreflight() {
      const r = await fetch(`${API}/preflight/livedata`);
      if (!r.ok) {
        let d = `${r.status}`;
        try { const j = await r.json(); if (j.detail) d = j.detail; } catch (_) {}
        throw new Error(d);
      }
      return await r.json();   // {live_slots, head_ctx}
    }

    function clearLocalPreflightJob() {
      if (preflightWorker && preflightJobId) {
        try { preflightWorker.postMessage({type: "clear", jobId: preflightJobId}); } catch (_) {}
      }
      preflightJobId = "";
      preflightFile = null;
    }

    // A refusal by preflight_core.PreflightRejected is about the FILE, so it
    // reads as a sentence, not as a Python traceback - and it must not offer
    // the in-printer fallback, which runs the same check and refuses too.
    // Pyodide hands us the whole traceback, so pull the final line out of it.
    function _preflightRejection(msg) {
      const m = /PreflightRejected:\s*([^\n]+)/.exec(msg || "");
      return m ? m[1].trim() : null;
    }
    // Entry point: try the browser path, offer the server fallback on failure.
    async function _runPreflight(f) {
      try {
        await _runLocalPreflight(f);
      } catch (e) {
        const msg = e && e.message ? e.message : String(e);
        const rejected = _preflightRejection(msg);
        if (rejected) {
          confirm({
            title: t("ui.preflight.rejected_title"),
            message: escapeHtml(rejected),
            okLabel: t("ui.common.ok"),
            dismissOnly: true,      // one button: there is nothing to choose
            onOk: () => { closePreflight(); },
          });
          return;
        }
        confirm({
          title: t("ui.preflight.local_failed_title"),
          message: tHtml("ui.preflight.local_failed_msg", {error: msg}),
          okLabel: t("ui.preflight.local_fallback_ok"),
          // No altLabel: the dialog already renders its own Cancel, and
          // passing one labelled "Cancel" produced TWO identical buttons.
          onCancel: () => { closePreflight(); },
          onOk: () => { _runServerPreflight(f); },
        });
      }
    }

    async function _runLocalPreflight(f) {
      preflight.open    = true;
      preflight.busy    = true;
      preflight.sending = "";
      preflight.report  = null;
      preflight.error   = "";
      preflight.local   = true;
      preflight.progress = {percent: 0, stage: "queued", running: true};
      uploading.value   = true;
      clearLocalPreflightJob();
      preflightFile  = f;
      preflightJobId = `local-${Date.now()}-${++preflightJobSeq}`;
      try {
        await ensurePreflightWorker();
        const live = await loadLiveSlotsForPreflight();
        const j = await runPreflightWorker("analyze", {
          jobId: preflightJobId, file: f,
          liveSlots: live.live_slots, headCtx: live.head_ctx,
          costParams: live.cost_params,
        }, msg => {
          preflight.progress = {
            percent: Number(msg.percent || 0),
            stage:   String(msg.stage || ""), running: true};
        });
        preflight.report = Object.assign({local: true}, j.report || {});
        pfPlanSel.value = null;
        preflight.slicerOverrides = {};
        preflight.slicerSwaps = null;
        preflight.headOverrides = {};
        preflight.headSwaps = null;
        preflight.headCopies = null;
        preflight.copyPlan = null;
        preflight.proposalCopies = null;
      } finally {
        uploading.value = false;
        preflight.busy  = false;
        // In the finally, not at the end of the try: on an exception the bar
        // kept its last value with running:true and sat there frozen behind
        // the dialog (seen on the re-processing refusal).
        preflight.progress = null;
      }
    }

    async function _runServerPreflight(f) {
      preflight.open    = true;
      preflight.busy    = true;
      preflight.sending = "";
      preflight.report  = null;
      preflight.error   = "";
      preflight.local   = false;
      preflight.progress = null;
      uploading.value   = true;
      try {
        const fd = new FormData();
        fd.append("file", f, f.name);
        const r = await fetch(`${API}/preflight`, {method: "POST", body: fd});
        if (!r.ok) {
          let msg = `${r.status} ${r.statusText}`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        preflight.report = await r.json();
        pfPlanSel.value = null;
        preflight.slicerOverrides = {};
        preflight.slicerSwaps = null;
        preflight.headOverrides = {};
        preflight.headSwaps = null;
        preflight.headCopies = null;
        preflight.copyPlan = null;
        preflight.proposalCopies = null;
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        uploading.value = false;
        preflight.busy  = false;
      }
    }
    function closePreflight() {
      preflight.open    = false;
      preflight.report  = null;
      preflight.error   = "";
      preflight.sending = "";
      preflight.progress = null;
      preflight.local   = false;
      clearLocalPreflightJob();
    }
    function stageLabel(stage) {
      const map = {
        queued:            t("ui.preflight.stage_queued"),
        analyze:           t("ui.preflight.stage_analyze"),
        apply_remap:       t("ui.preflight.stage_apply_remap"),
        optimize:          t("ui.preflight.stage_optimize"),
        layer:             t("ui.preflight.stage_layer"),
        print_prefs:       t("ui.preflight.stage_print_prefs"),
        rewrite:           t("ui.preflight.stage_rewrite"),
        inject_auto_load:  t("ui.preflight.stage_inject_auto_load"),
        upload:            t("ui.preflight.stage_upload"),
        done:              t("ui.preflight.stage_done"),
      };
      return map[stage] || stage || "";
    }
    // New design: the plans become comparison cards; one is selected, only
    // its table shows and a single footer button prints it. Default is the
    // first plan (as loaded), the same one the classic view lists first.
    const pfPlanSel = ref(null);
    function pfPlanList() {
      const r = preflight.report;
      if (!r || !r.plans || forcaMixed()) return [];
      const order = r.head_mode ? ["loadout", "optimize", "layer", "color"]
                                : ["slicer", "optimize", "layer"];
      return order.filter(p => r.plans[p]);
    }
    function pfPlanFeasible(p) {
      const r = preflight.report;
      if (!r || !r.plans || !r.plans[p]) return false;
      if (r.head_mode) return headPlanFeasible(p);
      return !!r.plans[p].feasible && !(r.missing_materials || []).length;
    }
    function pfPlanSwaps(p) {
      const r = preflight.report;
      if (r.head_mode) return headPlanSwaps(p);
      return p === "slicer" ? slicerSwapsDisplay() : (r.plans[p].swaps || 0);
    }
    function pfPlanFlush(p) {
      return preflight.report && preflight.report.head_mode ? headPlanFlushG(p) : null;
    }
    // Background swaps of a head-mode plan (bg_ok / unloads), null without.
    function pfPlanBg(p) {
      return preflight.report && preflight.report.head_mode ? headPlanBg(p) : null;
    }
    function pfSelected() {
      const list = pfPlanList();
      return list.includes(pfPlanSel.value) ? pfPlanSel.value : (list[0] || null);
    }
    // Tagged when a feasible plan saves swaps against the first one.
    function pfRecommended() {
      const list = pfPlanList().filter(pfPlanFeasible);
      if (list.length < 2) return null;
      let best = list[0];
      for (const p of list) if (pfPlanSwaps(p) < pfPlanSwaps(best)) best = p;
      return best === list[0] ? null : best;
    }
    // ---- estimate card ----------------------------------------------------
    // Every number is a model estimate (never measured). A plan's estimate
    // comes with the report; after a mapping edit the browser worker
    // recomputes it for the plan being edited (pfEstLive), without a re-parse.
    const pfEstLive = reactive({});
    let pfEstTimer = null;
    let pfEstSeq = 0;
    function pfEst(p) {
      const live = pfEstLive[p];
      if (live && live.estimate) return live.estimate;
      const pl = preflight.report && preflight.report.plans && preflight.report.plans[p];
      return (pl && pl.estimate) || null;
    }
    function pfTimeline(p) {
      const live = pfEstLive[p];
      if (live && live.timeline) return live.timeline;
      const pl = preflight.report && preflight.report.plans && preflight.report.plans[p];
      return (pl && pl.timeline) || [];
    }
    function pfEstDelta(e) {
      return (e && e.added_s !== null && e.added_s !== undefined) ? "+" + fmtDuration(e.added_s) : "";
    }
    // Per-kind share of the added time, for the timeline strip.
    function pfTimelineSegs(p) {
      const evs = pfTimeline(p).filter(ev => ev.kind !== "first_load" && Number(ev.seconds) > 0);
      const total = evs.reduce((a, ev) => a + (Number(ev.seconds) || 0), 0) || 1;
      return evs.map((ev, i) => ({
        key: i, kind: ev.kind, t: ev.t,
        pct: Math.max(0.4, 100 * (Number(ev.seconds) || 0) / total),
        tip: "T" + ev.t + " - " + swapKindLabel(ev.kind)
             + " - " + fmtDuration(ev.seconds)
             + (ev.purge_mm ? " - " + Math.round(ev.purge_mm) + " mm" : ""),
      }));
    }
    function pfEstCounts(e) {
      const parts = [];
      if (e.inline_swaps)  parts.push(swapKindLabel("cross_ace_inline") + " x " + e.inline_swaps);
      if (e.bg_swaps)      parts.push(swapKindLabel("cross_ace_bg") + " x " + e.bg_swaps);
      if (e.head_changes)  parts.push(swapKindLabel("head_change") + " x " + e.head_changes);
      return parts.join(" · ");
    }
    // Live recompute after a mapping/target edit. Only the in-browser path
    // can do it (the server has no endpoint for it); otherwise the numbers
    // keep showing the analysed assignment, flagged stale.
    const pfEstStale = ref(false);
    function scheduleEstimate(plan) {
      if (!preflight.local || !preflightJobId) return;
      pfEstStale.value = true;
      clearTimeout(pfEstTimer);
      pfEstTimer = setTimeout(async () => {
        const seq = ++pfEstSeq;
        const payload = {jobId: preflightJobId, plan};
        if (plan === "slicer") {
          payload.mapping = _slicerEffectiveMapping();
        } else {
          payload.targetIds = _headEffectiveAssignment();
          payload.copyIds = headCopiesEffective();
        }
        try {
          const j = await runPreflightWorker("estimate", payload);
          if (seq !== pfEstSeq) return;
          if (j.result) pfEstLive[plan] = j.result;
          pfEstStale.value = false;
        } catch (_) { /* keep the analysed numbers */ }
      }, 200);
    }
    watch(() => preflight.report, () => {
      for (const k of Object.keys(pfEstLive)) delete pfEstLive[k];
      pfEstStale.value = false;
    });
    function pfPlanTitle(p) {
      return t(preflight.report && preflight.report.head_mode
               ? "ui.preflight.head_plan_" + p : "ui.preflight.plan_" + p);
    }
    function pfGoLabel() {
      const p = pfSelected();
      if (!p) return "";
      if (preflight.sending === p) return t("ui.preflight.sending");
      return t(preflight.report.head_mode ? "ui.preflight.head_go_" + p
                                         : "ui.preflight.go_" + p);
    }
    function pfPrintSelected() {
      const p = pfSelected();
      if (!p || !pfPlanFeasible(p)) return;
      if (preflight.report.head_mode) startPreflightPrint("head", p);
      else startPreflightPrint(p);
    }
    async function startPreflightPrint(mode, headPlan) {
      if (preflight.busy || preflight.sending) return;
      const rep = preflight.report;
      if (!rep) return;
      if (rep.local) { await _startLocalPreflightPrint(mode, headPlan); return; }
      await _startServerPreflightPrint(mode, headPlan);
    }

    // Browser path: rewrite in the worker, upload straight to Moonraker.
    async function _startLocalPreflightPrint(mode, headPlan) {
      const rep = preflight.report;
      if (!rep || !preflightFile || !preflightJobId) return;
      preflight.sending = (mode === "head") ? (headPlan || "loadout") : mode;
      preflight.error   = "";
      preflight.progress = {percent: 0, stage: "queued", running: true};
      const startedAt = Date.now();
      const MIN_VISIBLE_MS = 1500;
      try {
        const payload = {jobId: preflightJobId, file: preflightFile, mode};
        if (mode === "slicer") {
          // Same (possibly user-edited) remap the server path sends.
          const remap = {};
          for (const m of _slicerEffectiveMapping()) {
            if (!m.slot) continue;
            const synth = m.slot.ace * 4 + m.slot.slot;
            if (synth !== m.t) remap[String(m.t)] = synth;
          }
          payload.remapOverride = remap;
        } else if (mode === "head") {
          const hp = headPlan || "loadout";
          payload.headPlan = hp;
          if (hp === "loadout") {
            const asn = {};
            const eff = _headEffectiveAssignment();
            for (const k of Object.keys(eff)) { if (eff[k]) asn[String(k)] = eff[k]; }
            payload.headAssignment = asn;
            payload.headCopies = headCopiesEffective();
          }
        }
        const live = await loadLiveSlotsForPreflight();
        payload.liveSlots = live.live_slots;
        payload.headCtx   = live.head_ctx;
        // The worker writes the print-preference line (and moves the flow
        // calibration) with the same preflight_core pass the printer uses.
        payload.printPrefs = {bedMesh: !!preflight.bedMesh,
                              camera: !!preflight.camera,
                              flowCal: !!preflight.flowCal};
        const j = await runPreflightWorker("rewrite", payload, msg => {
          preflight.progress = {
            percent: Number(msg.percent || 0),
            stage:   String(msg.stage || ""), running: true};
        });
        preflight.progress = {percent: 90, stage: "upload", running: true};
        const fd = new FormData();
        fd.append("root", "gcodes");
        fd.append("print", "true");
        fd.append("file", j.blob, rep.filename || preflightFile.name);
        const r = await fetch("/server/files/upload", {method: "POST", body: fd});
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          const detail = body.error || body.detail || body.message
            || `${r.status} ${r.statusText}`;
          throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        }
        preflight.progress = {percent: 100, stage: "done", running: true};
        const elapsed = Date.now() - startedAt;
        const wait = Math.max(0, MIN_VISIBLE_MS - elapsed);
        if (wait > 0) await new Promise(res => setTimeout(res, wait));
        setMacroLog(t("ui.upload.started", {name: rep.filename || preflightFile.name}));
        closePreflight();
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        preflight.sending = "";
        if (preflight.progress) preflight.progress.running = false;
      }
    }

    async function _startServerPreflightPrint(mode, headPlan) {
      const rep = preflight.report;
      if (!rep || !rep.token) return;
      // For head mode the button identity is the head plan (loadout/optimize/
      // layer); for multi it is the mode.
      preflight.sending = (mode === "head") ? (headPlan || "loadout") : mode;
      preflight.error   = "";
      preflight.progress = {percent: 0, stage: "queued", running: true};
      const startedAt = Date.now();
      const MIN_VISIBLE_MS = 1500;
      const FIRST_POLL_MS  = 250;
      const POLL_MS        = 500;
      try {
        const body = {token: rep.token, mode,
                      bed_mesh: !!preflight.bedMesh, camera: !!preflight.camera,
                      flow_cal: !!preflight.flowCal};
        if (mode === "slicer") {
          // Send the (possibly user-edited) slot assignment verbatim so the
          // print matches the preview exactly. Only entries differing from
          // slot==head go into the remap.
          const remap = {};
          for (const m of _slicerEffectiveMapping()) {
            if (!m.slot) continue;
            const synth = m.slot.ace * 4 + m.slot.slot;
            if (synth !== m.t) remap[String(m.t)] = synth;
          }
          body.remap = remap;
        } else if (mode === "head") {
          const hp = headPlan || "loadout";
          body.head_plan = hp;
          if (hp === "loadout") {
            // Send the (possibly user-edited) colour->target assignment verbatim
            // so the print matches the preview exactly. Keys are slicer-T strings.
            const asn = {};
            const eff = _headEffectiveAssignment();
            for (const k of Object.keys(eff)) {
              if (eff[k]) asn[String(k)] = eff[k];
            }
            body.head_assignment = asn;
            // The colour copies the preview counted with, verbatim (an
            // empty object = none; the pipeline then rewrites statically).
            body.head_copies = headCopiesEffective();
          }
          else {
            // optimize / layer / color: the server recomputes the proposed
            // loadout (the user has arranged spools to match it); the copies
            // go verbatim so the print uses the ones the preview showed.
            body.head_copies = proposalCopyIds(hp);
          }
        }
        const r = await fetch(`${API}/preflight/print`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          throw new Error(j.detail || `${r.status} ${r.statusText}`);
        }
        const jobId = j.job_id;
        let last;
        let pollDelay = FIRST_POLL_MS;
        for (;;) {
          await new Promise(res => setTimeout(res, pollDelay));
          pollDelay = POLL_MS;
          let sr;
          try {
            sr = await fetch(`${API}/preflight/print/status?job_id=${encodeURIComponent(jobId)}`);
          } catch (_) {
            continue;
          }
          if (!sr.ok) {
            const sj = await sr.json().catch(() => ({}));
            throw new Error(sj.detail || `${sr.status} ${sr.statusText}`);
          }
          last = await sr.json();
          preflight.progress = {
            percent: Number(last.percent || 0),
            stage:   String(last.stage || ""),
            running: !last.done,
          };
          if (last.done) break;
        }
        if (last.error) throw new Error(last.error);
        preflight.progress = {percent: 100, stage: "done", running: true};
        const elapsed = Date.now() - startedAt;
        const wait = Math.max(0, MIN_VISIBLE_MS - elapsed);
        if (wait > 0) {
          await new Promise(res => setTimeout(res, wait));
        }
        setMacroLog(t("ui.upload.started", {name: rep.filename}));
        closePreflight();
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        preflight.sending = "";
        if (preflight.progress) preflight.progress.running = false;
      }
    }

    // "Loadout übernehmen": the user has physically rearranged the spools to
    // match a proposed (optimize/layer) plan; write those identities onto the
    // ACE slots (slot-override) and, in head mode, onto the pinned feeder heads
    // (print_task_config). This only SETS filaments/colours - it does NOT start
    // a print.
    function _hex6(c) {
      let s = String(c || "").trim().toLowerCase();
      if (!s) return "";
      if (s[0] !== "#") s = "#" + s;
      if (s.length === 9) s = s.slice(0, 7); // #rrggbbaa -> #rrggbb
      return s;
    }
    function _loadoutOps(mode, headPlan) {
      // -> {overrides:[{ace,slot,material,color}], feeders:[{head,material,color}]}
      const overrides = [], feeders = [];
      const rep = preflight.report;
      if (!rep) return {overrides, feeders};
      if (mode === "head" && headPlan === "loadout") {
        // The loadout plan applies only its SUGGESTED copies: the empty
        // slots get the colour's identity, the user loads the spool there.
        for (const sg of headCopyPlan().suggestions) {
          const mat = headSlicerMat(sg.t);
          overrides.push({ace: sg.ace, slot: sg.slot,
                          material: (mat === "?") ? "" : mat,
                          color: _hex6(headSlicerHex(sg.t))});
        }
      } else if (mode === "head") {
        for (const m of headPlanMapping(headPlan)) {
          if (!m || m.kind === "none") continue;
          const color = _hex6(headSlicerHex(m.t));
          const mat = headSlicerMat(m.t);
          const material = (mat === "?") ? "" : mat;
          if (m.kind === "pin" && m.head !== null && m.head !== undefined) {
            feeders.push({head: m.head, material, color});
          } else if (m.kind === "ace" && m.ace !== null && m.ace !== undefined) {
            overrides.push({ace: m.ace, slot: m.slot, material, color});
          }
        }
      } else {
        const plan = rep.plans[mode];
        if (!plan || !plan.mapping) return {overrides, feeders};
        for (const m of plan.mapping) {
          if (!m || !m.slot) continue;
          overrides.push({
            ace: m.slot.ace, slot: m.slot.slot,
            material: m.slot.material || "", color: _hex6(m.slot.color),
          });
        }
      }
      return {overrides, feeders};
    }
    async function applyLoadout(mode, headPlan) {
      if (preflight.applying || preflight.sending) return;
      const ops = _loadoutOps(mode, headPlan);
      const total = ops.overrides.length + ops.feeders.length;
      if (!total) return;
      confirm({
        title:   t("ui.preflight.apply_loadout"),
        message: tHtml("ui.preflight.apply_loadout_confirm", {count: total}),
        okLabel: t("ui.preflight.apply_loadout"),
        onOk: async () => {
          preflight.applying = (mode === "head") ? (headPlan || "loadout") : mode;
          try {
            for (const o of ops.overrides) {
              await fetch(`${API}/slot-override`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                  ace: o.ace, slot: o.slot, material: o.material,
                  brand: "", subtype: "", color: o.color,
                }),
              });
            }
            for (const f of ops.feeders) {
              const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
              const hex = (f.color || "#ffffff").replace("#", "");
              enqueue("SET_PRINT_FILAMENT_CONFIG", {
                CONFIG_EXTRUDER:     f.head,
                FILAMENT_TYPE:       dq(f.material || "PLA"),
                FILAMENT_COLOR_RGBA: hex.toUpperCase() + "FF",
                VENDOR:              dq("Generic"),
                FILAMENT_SUBTYPE:    dq(""),
              });
            }
            if (ops.overrides.length) {
              enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
            }
            setMacroLog(t("ui.preflight.apply_loadout_done", {count: total}));
            reloadState();
          } catch (e) {
            setMacroLog(`${t("ui.common.error")}: ${e}`);
          } finally {
            preflight.applying = "";
          }
        },
      });
    }
    // Control tab: the printer's camera. MJPEG comes through the backend as
    // an <img>; a WebRTC camera (camera-streamer) is a <video> whose media
    // travels browser-to-camera, with only the handshake going through the
    // backend. The stream is only opened while the tab is showing, so a
    // hidden tab does not keep a connection to the camera.
    const webcam = reactive({
      loaded: false, available: false, reason: "", name: "",
      webrtc: false, proxy: "", signal: "", aspect: "16 / 9",
      connFailed: false,
      // Cache-buster: an MJPEG <img> that was torn down keeps its old
      // connection cached, so re-opening the tab shows a frozen frame
      // until the src actually changes.
      nonce: 0,
    });
    const controlShown = computed(() => !panelMode && tab.value === "control");
    const webcamVideo = ref(null);
    const webcamSrc = computed(() =>
      webcam.available && !webcam.webrtc && controlShown.value
        ? `${BASE}${webcam.proxy}?n=${webcam.nonce}` : "");
    let webcamPc = null;
    let webcamRetry = null;
    function stopWebrtc() {
      clearTimeout(webcamRetry);
      if (webcamPc) { webcamPc.close(); webcamPc = null; }
      if (webcamVideo.value) webcamVideo.value.srcObject = null;
    }
    // camera-streamer's handshake is reversed from a typical WHIP/WHEP
    // flow: the SERVER makes the offer (it knows the video track) and the
    // browser answers, over two plain POSTs of JSON-wrapped SDP.
    async function startWebrtc() {
      stopWebrtc();
      webcam.connFailed = false;
      webcam.reason = "";
      await nextTick();
      const video = webcamVideo.value;
      if (!video || !webcam.signal) return;
      const post = (body) => fetch(`${BASE}${webcam.signal}`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      const pc = new RTCPeerConnection();
      webcamPc = pc;
      const fail = (why) => {
        if (pc !== webcamPc) return;
        webcam.reason = why;
        webcam.connFailed = true;
        // Retry while the tab is open, but not in a tight loop.
        if (controlShown.value) {
          webcamRetry = setTimeout(() => { if (pc === webcamPc) startWebrtc(); }, 5000);
        }
      };
      pc.addTransceiver("video", {direction: "recvonly"});
      pc.addEventListener("track", (evt) => {
        if (pc === webcamPc && evt.track.kind === "video") video.srcObject = evt.streams[0];
      });
      // "track" fires as soon as the transceiver is negotiated, before any
      // media arrives, so it proves nothing. Some LAN cameras never resolve
      // Chrome's mDNS-obfuscated host candidates, leaving the connection in
      // "checking" with no "failed" event: a flat timeout catches that.
      // The same check covers a connection that comes up but delivers no
      // video (a stale session on the camera): it fails visibly and retries.
      const connectTimeout = setTimeout(() => {
        if (pc !== webcamPc) return;
        if (pc.connectionState !== "connected") fail("connection timed out");
        else if (!video.videoWidth) fail("no video received");
      }, 10000);
      pc.addEventListener("connectionstatechange", () => {
        if (pc !== webcamPc) return;
        if (pc.connectionState === "connected") {
          webcam.connFailed = false;
        } else if (["failed", "disconnected"].includes(pc.connectionState)) {
          clearTimeout(connectTimeout);
          fail(pc.connectionState);
        }
      });
      try {
        const offerRes = await post({type: "request"});
        if (!offerRes.ok) throw new Error(`camera: HTTP ${offerRes.status}`);
        const offer = await offerRes.json();
        await pc.setRemoteDescription(offer);
        await pc.setLocalDescription(await pc.createAnswer());
        await new Promise((resolve) => {
          if (pc.iceGatheringState === "complete") { resolve(); return; }
          const check = () => {
            if (pc.iceGatheringState === "complete") {
              pc.removeEventListener("icegatheringstatechange", check);
              resolve();
            }
          };
          pc.addEventListener("icegatheringstatechange", check);
        });
        if (pc !== webcamPc) return;
        const ans = await post({type: pc.localDescription.type, id: offer.id,
                                sdp: pc.localDescription.sdp});
        if (!ans.ok) throw new Error(`camera: HTTP ${ans.status}`);
      } catch (e) {
        clearTimeout(connectTimeout);
        fail(e.message || String(e));
      }
    }
    function reloadWebcam() {
      if (webcam.available && webcam.webrtc) startWebrtc();
      else loadWebcam();
    }
    let webcamLoading = false;
    async function loadWebcam() {
      webcamLoading = true;
      try {
        const r = await fetch(`${API}/webcam/info`);
        const j = r.ok ? await r.json() : {available: false, reason: `HTTP ${r.status}`};
        webcam.webrtc = !!j.webrtc;
        webcam.proxy = j.proxy_path || "";
        webcam.signal = j.signal_path || "";
        webcam.available = !!(j.available && (webcam.proxy || webcam.signal));
        webcam.reason = j.reason || "";
        webcam.name = j.name || "";
        webcam.aspect = String(j.aspect_ratio || "16:9").replace(":", " / ");
      } catch (e) {
        webcam.available = false;
        webcam.reason = String(e);
      } finally {
        webcamLoading = false;
        webcam.loaded = true;
        webcam.nonce++;
        if (controlShown.value && webcam.available && webcam.webrtc) startWebrtc();
        else stopWebrtc();
      }
    }
    watch(controlShown, (shown) => {
      if (!shown) { stopWebrtc(); return; }
      if (!webcam.loaded) loadWebcam();
      else if (webcam.webrtc) startWebrtc();
      else webcam.nonce++;
    }, {immediate: true});
    onUnmounted(stopWebrtc);

    // Print panel: speed, flow, fan, temperatures, Z offset and the job
    // verbs. Every value shown is READ BACK from the printer (polled while
    // the tab is open), never remembered locally: a change made on the
    // printer's own display has to show up here too.
    const printCtl = reactive({
      speed_factor: 100, extrude_factor: 100, fan: 0, z_offset: 0, light: null, detection: null, enclosure: null,
      nozzle: {temp: null, target: null}, bed: {temp: null, target: null},
      progress: 0, message: "", state: "", filename: "",
    });
    const printCtlLimits = reactive({
      ranges: {speed: {min: 25, max: 300}, flow: {min: 75, max: 125},
               fan: {min: 0, max: 100}},
      nozzle: {min: 0, max: 300}, bed: {min: 0, max: 120},
      babystep: {step: 0.05, total: 0.5, used: 0},
    });
    // Local slider positions. They follow the readback except while the
    // user is holding one.
    const printCtlDraft = reactive({speed: 100, flow: 100, fan: 0, exhaust: 0, inner: 0});
    const printCtlDragging = ref("");
    const printCtlBusy = ref("");
    const printCtlError = ref("");
    // A command that timed out on its way back is QUEUED, not failed: a
    // multiACE swap holds Klipper's queue for minutes and the command runs
    // afterwards. Until then the same control stays disabled, so a second
    // press cannot send it twice.
    const printCtlNotice = ref("");
    const printCtlQueued = reactive({});
    const PRINT_CTL_QUEUED_MS = 180000;
    const printCtlCan = computed(() => ({
      pause: printCtl.state === "printing",
      resume: printCtl.state === "paused",
      cancel: printCtl.state === "printing" || printCtl.state === "paused",
    }));
    // Outside a print the fans may be driven directly only while no mode
    // runs; the server enforces it, this only explains it. During a print
    // they are always allowed (stock accepts it too); a running mode may
    // override the exhaust value, which is only a hint.
    // Stock's defect detection watches the print with the camera, which needs
    // the chamber light: while it is on during a print the light stays on.
    // The server refuses it too; this only disables the switch and explains.
    const lightLocked = computed(() =>
      printCtl.light > 0 && !!printCtl.detection
      && (printCtl.state === "printing" || printCtl.state === "paused"));
    const enclosureLock = computed(() => {
      const e = printCtl.enclosure;
      if (printCtl.state === "printing" || printCtl.state === "paused") return "";
      if (e && e.mode) return t("ui.control.enc_locked_mode");
      return "";
    });
    const enclosureHint = computed(() => {
      const e = printCtl.enclosure;
      return (printCtl.state === "printing" || printCtl.state === "paused")
        && e && e.mode ? t("ui.control.enc_hint_print") : "";
    });
    const enclosureModeText = computed(() => {
      const e = printCtl.enclosure;
      if (!e || e.mode === null || e.mode === undefined) return "";
      const name = t("ui.control.mode_" + e.mode);
      return e.desired_temp && (e.mode === 1 || e.mode === 2)
        ? `${name} · ${Math.round(e.desired_temp)} °C` : name;
    });
    async function loadPrintControl() {
      try {
        const r = await fetch(`${API}/print-control`);
        if (!r.ok) {
          if (printCtlRestarting.value) restartSawFail = true;
          else printCtlError.value = `HTTP ${r.status}`;
          return;
        }
        const pc = await r.json();
        printCtlError.value = "";
        if (printCtlRestarting.value && restartSawFail) {
          // The printer has answered again after the restart.
          printCtlRestarting.value = false;
          printCtlNotice.value = "";
        }
        Object.assign(printCtl, pc);
        if (printCtlDragging.value !== "speed") printCtlDraft.speed = pc.speed_factor;
        if (printCtlDragging.value !== "flow") printCtlDraft.flow = pc.extrude_factor;
        if (printCtlDragging.value !== "fan") printCtlDraft.fan = pc.fan;
        for (const f of ["exhaust", "inner"]) {
          if (pc.enclosure && pc.enclosure[f] !== null && printCtlDragging.value !== f) {
            printCtlDraft[f] = pc.enclosure[f];
          }
        }
      } catch (e) {
        if (printCtlRestarting.value) restartSawFail = true;
        else printCtlError.value = e.message || String(e);
      }
    }
    async function loadPrintControlLimits() {
      try {
        const r = await fetch(`${API}/print-control/limits`);
        if (r.ok) Object.assign(printCtlLimits, await r.json());
      } catch (_) {}
    }
    async function sendPrintControl(verb, value) {
      printCtlBusy.value = verb;
      printCtlError.value = "";
      try {
        const r = await fetch(`${API}/print-control`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(value === undefined ? {verb} : {verb, value}),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        // Snap the slider to what the server ACTUALLY applied: it clamps,
        // and a control showing a value the printer refused lies about the
        // machine.
        if (j.applied !== null && j.applied !== undefined) {
          if (verb === "speed") printCtlDraft.speed = j.applied;
          if (verb === "flow") printCtlDraft.flow = j.applied;
          if (verb === "fan") printCtlDraft.fan = j.applied;
          if (verb === "exhaust" || verb === "inner") printCtlDraft[verb] = j.applied;
        }
        if (j.ok === false && j.detail) printCtlError.value = j.detail;
        if (j.queued) {
          printCtlNotice.value = t("ui.control.queued");
          printCtlQueued[verb] = true;
          setTimeout(() => { delete printCtlQueued[verb]; printCtlNotice.value = ""; },
                     PRINT_CTL_QUEUED_MS);
        }
        if (verb === "babystep" && j.total_mm !== undefined) {
          printCtlLimits.babystep.used = j.total_mm;
        }
        await loadPrintControl();
      } catch (e) {
        printCtlError.value = e.message || String(e);
      } finally {
        printCtlBusy.value = "";
      }
    }
    // One command per ~250 ms of dragging, and ALWAYS a final one on
    // release: an undelivered last event is how a slider lies.
    let _ctlDebounce = null;
    function printCtlSlide(verb, value) {
      printCtlDragging.value = verb;
      if (_ctlDebounce) clearTimeout(_ctlDebounce);
      _ctlDebounce = setTimeout(() => sendPrintControl(verb, Number(value)), 250);
    }
    function printCtlRelease(verb, value) {
      if (_ctlDebounce) { clearTimeout(_ctlDebounce); _ctlDebounce = null; }
      printCtlDragging.value = "";
      sendPrintControl(verb, Number(value));
    }
    function printCtlReset(verb) {
      printCtlRelease(verb, verb === "fan" ? 0 : 100);
    }
    // The target boxes show the current target, so a spinner arrow steps from
    // the real value instead of from an empty box. While the user is in a
    // box the typed value wins over the readback; leaving the box (blur)
    // goes back to showing the target.
    const printCtlTempDraft = reactive({nozzle: null, bed: null});
    function printCtlTempShown(verb) {
      if (printCtlTempDraft[verb] !== null) return printCtlTempDraft[verb];
      const target = printCtl[verb].target;
      return target === null ? "" : Math.round(target);
    }
    // An empty box is "nothing typed", not 0: sending 0 would switch the
    // heater off.
    function printCtlHeat(verb, ev) {
      const raw = String(ev.target.value).trim();
      if (raw === "") return;
      const v = Number(raw);
      if (Number.isFinite(v)) sendPrintControl(verb, v);
    }
    // Restart Firmware / Restart Printer. Refused by the server while a
    // print runs and under a managed install; the buttons mirror that. After
    // a restart the printer is unreachable for a while, so poll errors are
    // expected and are not shown until it has answered again.
    const printCtlRestarting = ref(false);
    let restartSawFail = false;
    const printCtlRestartBlocked = computed(() =>
      printCtl.state === "printing" || printCtl.state === "paused" || printCtlBusy.value !== "");
    function printCtlRestart(behavior) {
      confirm({
        title: t(`ui.restart.${behavior}_title`),
        message: t(`ui.restart.${behavior}_msg`),
        okLabel: t(`ui.restart.${behavior}_btn`),
        onOk: async () => {
          printCtlBusy.value = "restart";
          printCtlError.value = "";
          try {
            const r = await fetch(`${API}/restart`, {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({behavior}),
            });
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.detail || `${r.status}`);
            printCtlNotice.value = t(`ui.restart.${behavior}_sent`);
            printCtlRestarting.value = true;
            restartSawFail = false;
            setTimeout(() => {
              printCtlRestarting.value = false;
              printCtlNotice.value = "";
            }, 120000);
          } catch (e) {
            printCtlError.value = e.message || String(e);
          } finally {
            printCtlBusy.value = "";
          }
        },
      });
    }
    function printCtlCancel() {
      confirm({
        title: t("ui.control.cancel_title"),
        message: t("ui.control.cancel_msg"),
        okLabel: t("ui.control.cancel_ok"),
        onOk: () => sendPrintControl("cancel"),
      });
    }

    // Console, read only. Polled incrementally while the tab is open.
    const CONSOLE_MAX = 400;
    const consoleLines = ref([]);
    const consoleFollow = ref(localStorage.getItem("multiace.console.follow") !== "0");
    const consoleEl = ref(null);
    let lastConsoleId = 0;
    watch(consoleFollow,
          v => localStorage.setItem("multiace.console.follow", v ? "1" : "0"));
    async function loadConsole() {
      try {
        const r = await fetch(`${API}/console-logs?lines=200&since_id=${lastConsoleId}`);
        if (!r.ok) return;
        const lines = (await r.json()).lines || [];
        if (!lines.length) return;
        for (const ln of lines) {
          if (ln.id <= lastConsoleId) continue;
          lastConsoleId = ln.id;
          consoleLines.value.push(ln);
        }
        if (consoleLines.value.length > CONSOLE_MAX) {
          consoleLines.value.splice(0, consoleLines.value.length - CONSOLE_MAX);
        }
        if (consoleFollow.value) {
          nextTick(() => {
            const el = consoleEl.value;
            if (el) el.scrollTop = el.scrollHeight;
          });
        }
      } catch (_) {}
    }
    function consoleTime(ts) {
      if (!ts) return "";
      try { return new Date(ts * 1000).toLocaleTimeString(); }
      catch (_) { return ""; }
    }

    // Poll the printer only while the Control tab is showing.
    const CONTROL_POLL_MS = 2000;
    let controlTimer = null;
    let webcamRetryTick = 0;
    function controlPoll() {
      loadPrintControl();
      loadConsole();
      // No camera yet (Moonraker was slow, or it was added later): look again
      // every ~10 s while the tab is open instead of waiting for a click.
      if (webcam.loaded && !webcam.available && !webcamLoading && ++webcamRetryTick % 5 === 0) {
        loadWebcam();
      }
    }
    watch(controlShown, (shown) => {
      clearInterval(controlTimer);
      controlTimer = null;
      if (!shown) return;
      loadPrintControlLimits();
      controlPoll();
      controlTimer = setInterval(controlPoll, CONTROL_POLL_MS);
    }, {immediate: true});
    onUnmounted(() => clearInterval(controlTimer));

    let resizeObserver = null;
    onMounted(async () => {
      // Page loaded straight into the spools tab (stored tab) - the watcher
      // never fires for that, so trigger the idle push here too.
      if (tab.value === "spools") spoolmanIdlePush();
      acefwLoadVersions();
      await loadLanguageList();
      // No explicit browser choice yet -> follow the printer's persisted
      // language (ace__language), so a fresh browser opens in the same
      // language as the printer instead of defaulting to English.
      if (!localStorage.getItem("multiace.lang")) {
        try {
          const r = await fetch(`${API}/state`);
          if (r.ok) {
            const s = await r.json();
            if (s && s.language) language.value = s.language;
          }
        } catch (_) {}
      }
      await loadCatalog(language.value);
      try {
        const r = await fetch(`${API}/version`);
        if (r.ok) {
          const j = await r.json();
          version.value = `v${j.web}`;
          const p = j.printer || {};
          printerName.value = p.device_name || "";
          printerFw.value   = p.firmware_version || "";
        }
      } catch (_) {}
      try { const r = await fetch(`${API}/screen-available`); if (r.ok) screenAvailable.value = (await r.json()).available; } catch (_) {}
      await reloadState();
      await reloadSnapshots();
      await loadConfig();
      await loadTipform();
      await loadMaterials();
      await loadNotifications();
      await refreshDebugState();
      await refreshUpdateStatus();
      await refreshPlugins();
      if (state.mode === "normal" && ["dashboard", "calibration"].includes(tab.value)) {
        tab.value = "config";
      }
      wsConnect();
      if (window.ResizeObserver && wiringContainerEl.value) {
        resizeObserver = new ResizeObserver(() => recomputeWiring());
        resizeObserver.observe(wiringContainerEl.value);
      } else {
        window.addEventListener("resize", recomputeWiring);
      }
      scheduleWiringRecompute();
      document.addEventListener("pointerdown", _smDocPointerDown, true);
      document.addEventListener("keydown", _smDocKeydown);
      // Never in the panel. The guard is there to stop you navigating the
      // MAIN UI away mid-operation; embedded as a Fluidd camera card we have
      // no say over the parent's navigation anyway, and there is nothing to
      // lose in a tile - the commands run on the printer, not in the browser.
      // Worse, beforeunload fires every time OUR document is torn down, and
      // Fluidd drops the card's iframe src whenever it pauses its streams -
      // which a browser tab switch does. With anything left in the queue that
      // was a "leave site?" prompt on every single tab switch.
      if (!panelMode) window.addEventListener("beforeunload", _onBeforeUnload);
    });
    function _onBeforeUnload(ev) {
      const pending = cmdQueue.value.some(
        it => it.status === 'queued' || it.status === 'running');
      if (!pending) return;
      ev.preventDefault();
      ev.returnValue = '';
      return '';
    }
    // =================================================================
    // Print history
    //
    // Moonraker owns duration and result; multiACE owns the plan, the
    // assignment and the swaps. The table shows the join, not a copy.
    // =================================================================
    const history = reactive({
      loaded: false, busy: false, error: "", jobs: [], detail: null,
      reprinting: "", stats: [], minSamples: 5, info: null,
    });
    async function loadHistory() {
      history.busy = true;
      history.error = "";
      try {
        const r = await fetch(`${API}/history?limit=50`);
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        history.jobs = j.jobs || [];
        history.loaded = true;
        loadHistoryStats();
      } catch (e) {
        history.error = e.message || String(e);
      } finally {
        history.busy = false;
      }
    }
    // Measured swap time per kind. Secondary to the table: a failure here
    // just leaves the row out.
    async function loadHistoryStats() {
      try {
        const r = await fetch(`${API}/history/stats/swaps`);
        if (!r.ok) return;
        const j = await r.json();
        history.minSamples = j.min_samples || history.minSamples;
        history.stats = Object.entries(j.stats || {}).map(
          ([kind, s]) => ({kind, ...s}));
      } catch (e) {}
    }
    function swapKindLabel(kind) {
      const key = "ui.history.swap_kind." + kind;
      const v = t(key);
      return v === key ? kind : v;
    }
    function historyRowKey(row) {
      return (row && (row.id || row.job_id || row.filename)) || "";
    }
    function openHistoryDetail(row) {
      const key = historyRowKey(row);
      history.detail = history.detail === key ? null : key;
    }
    // A head's entry is one slot (older records) or a list of every slot that
    // fed it during the print; both come out as a list.
    function historyLoadout(row) {
      const asn = (row && row.multiace && row.multiace.assignment) || {};
      return Object.keys(asn).sort((a, b) => Number(a) - Number(b)).map(h => {
        const v = asn[h];
        return {head: h, slots: (Array.isArray(v) ? v : [v]).filter(Boolean).map(e => ({
          ace: e.ace_index, slot: e.slot,
          color: e.color ? ("#" + String(e.color).replace(/^#/, "")) : "#666",
        }))};
      });
    }
    function historyColors(row) {
      return historyLoadout(row).flatMap(l => l.slots.map((s, i) => ({
        head: l.head + "-" + i, th: l.head, ace: s.ace, slot: s.slot, color: s.color,
      })));
    }
    function fmtDuration(sec) {
      if (!sec || sec < 0) return "-";
      if (sec < 120) return `${Math.round(sec)} s`;
      const m = Math.round(sec / 60);
      return m < 60 ? `${m} min`
                    : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
    }
    // Reprint: start the file exactly as it already sits on the printer, with
    // no preflight and no rewrite. The swaps baked in from its first run
    // still apply, so the user gets to compare the loadout of that run with
    // what sits on the machine now before confirming.
    function reprintLoadoutHtml(row) {
      const loadout = historyLoadout(row).filter(l => l.slots.length);
      if (!loadout.length) {
        return `<p class="muted small">${escapeHtml(t("ui.history.reprint_no_record"))}</p>`;
      }
      const items = loadout.map(l => {
        const slots = l.slots.map(e => {
          const hasAce = e.ace != null && e.slot != null;
          const where = hasAce
            ? `MMU ${dispIdx(e.ace)} / ${dispIdx(e.slot)}`
            : t("ui.history.reprint_feeder");
          const sp = hasAce ? spoolForSlot(e.ace, e.slot) : spoolForHead(Number(l.head));
          const now = sp ? t("ui.history.reprint_now", {spool: spoolTitle(sp)})
                         : t("ui.history.reprint_now_empty");
          return `<span class="swatch" style="background:${escapeHtml(e.color)}"></span> ` +
                 `${escapeHtml(where)} <span class="muted small">(${escapeHtml(now)})</span>`;
        });
        return `<li><b>T${escapeHtml(dispIdx(Number(l.head)))}</b> · ${slots.join(" → ")}</li>`;
      });
      return `<ul class="reprint-loadout">${items.join("")}</ul>`;
    }
    function reprintJob(row) {
      if (!row || !row.filename || history.reprinting) return;
      confirm({
        title: t("ui.history.reprint_title"),
        message: `<p><b>${escapeHtml(row.filename)}</b></p>` +
                 `<p>${escapeHtml(t("ui.history.reprint_msg"))}</p>` +
                 reprintLoadoutHtml(row) +
                 `<p class="muted small">${escapeHtml(t("ui.history.reprint_note"))}</p>` +
                 `<p>${escapeHtml(t("ui.history.reprint_confirm_q"))}</p>`,
        okLabel: t("ui.history.reprint_ok"),
        onOk: () => doReprint(row),
      });
    }
    // Column header "i" button: a banner next to the header. Click toggles
    // it (hover tooltips do not exist on touch); a click elsewhere, Escape or
    // scrolling closes it. Fixed-position so the table's scroll box cannot
    // clip it.
    function historyColumnInfo(col, ev) {
      if (history.info && history.info.col === col) { history.info = null; return; }
      const r = ev.currentTarget.getBoundingClientRect();
      const w = 260;
      history.info = {
        col,
        left: Math.max(8, Math.min(r.left, window.innerWidth - w - 8)),
        top: r.bottom + 6,
      };
    }
    const closeHistoryInfo = () => { history.info = null; };
    document.addEventListener("click", closeHistoryInfo);
    const historyInfoKeydown = (e) => { if (e.key === "Escape") closeHistoryInfo(); };
    document.addEventListener("keydown", historyInfoKeydown);
    window.addEventListener("scroll", closeHistoryInfo, true);
    async function doReprint(row) {
      history.reprinting = historyRowKey(row);
      history.error = "";
      try {
        const r = await fetch(`${API}/history/reprint`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({filename: row.filename}),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `${r.status}`);
        setMacroLog(t("ui.history.reprint_started", {name: row.filename}));
      } catch (e) {
        history.error = e.message || String(e);
      } finally {
        history.reprinting = "";
      }
    }
    watch(tab, (v) => { if (v === "history" && !history.loaded) loadHistory(); });
    if (tab.value === "history") loadHistory();

    onUnmounted(() => {
      document.removeEventListener("click", closeHistoryInfo);
      document.removeEventListener("keydown", historyInfoKeydown);
      window.removeEventListener("scroll", closeHistoryInfo, true);
      clearTimeout(wsReconnectTimer);
      clearInterval(screenTimer);
      try { ws?.close(); } catch (_) {}
      try { resizeObserver?.disconnect(); } catch (_) {}
      window.removeEventListener("resize", recomputeWiring);
      window.removeEventListener("beforeunload", _onBeforeUnload);
    });
    return {
      history, loadHistory, openHistoryDetail, historyRowKey, historyColors,
      fmtDuration, reprintJob, historyColumnInfo, swapKindLabel,
      subText,
      sourceLabel,
      tab, version, printerName, printerFw, connClass, connText, screenAvailable,
      state, loadError, run, macroLog,
      calibrationUi, connectedCalibrationAces, calibrationSelectedAce,
      calibrationSelectedSlot,
      calibrationSelectedToolhead, calibrationMoving, calibrationRouteLocked,
      calibrationVerifyMoving, calibrationVerifyActive, calibrationRouteKnown,
      calibrationRouteAnchors, calibrationRouteOccupied,
      calibrationVerifyBlockedReason, calibrationStartBlockedReason,
      calibrationTabStatus, CALIBRATION_TAB, ORCA_SECTION,
      calibrationStatus, calibrationRecovery, calibrationMeasureStep,
      calibrationVerifyStep, calibrationResultCase, calibrationNothingToSave,
      calibrationVerifyOutsideTolerance,
      calibrationOwnValueSlots, calibrationSlotBadge,
      calibrationTipPositionMm, calibrationTipSegment,
      calibrationTipSegmentPercent, calibrationTipLabel,
      calibrationHubMm, calibrationRouteLengthMm, calibrationRouteGrid,
      calibrationSelectedHeadSensorKnown, calibrationSelectedHeadClear,
      calibrationCanPrepare,
      calibrationCanStart, calibrationCanVerifyConfigured, calibrationProposed,
      calibrationTipEstimated,
      calibrationSetPanel, calibrationContinueRoute,
      calibrationBeginRecalibration, calibrationKeepSaved,
      calibrationSetScope, calibrationPrepare,
      calibrationCancelPrepare,
      calibrationSelectAce, calibrationSelectSlot, calibrationSelectHead,
      calibrationStart, calibrationMarkAndReturn, calibrationUndoMark,
      calibrationDrag, calibrationTipNowMm, calibrationTrackHubMm,
      calibrationTrackPercent, calibrationDragLabel, calibrationDragStart,
      calibrationDragMove, calibrationDragEnd, calibrationDragCancel, calibrationJog,
      calibrationNextRoute,
      calibrationClear, calibrationSavedKind, calibrationSavedLevels,
      calibrationClearPlan, calibrationClearBlocked, calibrationClearSession,
      calibrationClearLead, calibrationClearOpen, calibrationClearConfirm,
      calibrationClearUndo,
      calibrationVerifyConfigured, calibrationAction, calibrationApply,
      calibrationVerifyValues,
      panelMode, panelAce, panelAceIdx, panelSlotHead, panelPages, panelPage, panelPageId, panelFeederHeads, setPanelPage,
      panelSlotHeadLoaded, panelSlotActive, panelSlotLabel, panelSlotOp,
      panelMini, fullUiHref, uiTheme, uiThemeChoice, uiLight, setUiTheme, spoolPct, gaugeColor, thSettingsOpen, slotGaugePct, slotGaugeLabel, headGaugePct, headGaugeText,
      slotTitle, switchAce, loadSlot, slotIsEmpty, aceFlashEngine, aceHasDryer, aceHasDecoder, loadFeederHead, slotLoadedInHead, loadAll, unloadHead, unloadAll, cancelUnloadAll, anyUnloading, setHeadManual, setHeadFeeder, setHeadAce, headToggle, headSelect, aceOptionsForHead, headAceOf, aceProtoTitle, unitDriverLabel, assignUnit, forgetUnit, renameUnit, locateUnit, devNameValue, devNameInput, devNameCommit, deviceStatus, deviceDriver, driverOnlyActive, driverToggle, driverNote, driversRestartNeeded, perAceCount, aceCfwTitle, visibleAces, unwiredAces, gridAces, openHeadPicker, isToolheadOccupied, slotHeadHasNoAce, needsReload, toolheadOps, bgEnabledFor, setBgHead, setPickupCleaning, setConfirmCommands, setPaSync, setAutoDry, autoDryValue, autoDryInput, autoDryCommit, autoDryPairInvalid, autoDryFieldError, autoDryEnable, autoDrySetMaster, autoDryMasters, spoolmanUrl, spoolmanBusy, spoolmanStatusText, saveSpoolmanUrl, setSpoolmanAuto, spoolmanSync,
      spoolmanConnected, spoolmanUrlSet, setSpoolMode, smQuery, smRows, smBusy, smOpen, smSearchDebounced, smAdopt,
      smPing, smPingInfo, spoolmanPing, spoolQuery, smPick, smPickTarget, smAdoptStaged,
      spoolBadgeCls, spoolBadgeLabel, headTileEmpty, orcaProfile, virtualTools, virtualToolGroups, toolTargetOptions, setToolMapTarget, colorName, setToolMap, resetToolMap, setAirprintDetection, setQuadReplenish, setQuadFirst, setPurgeMatrix, FILAMENT_SWATCHES, knownColors, knownColorChips, pickerHexInput, pickerGaugePct, pickerSubline, sameSwatch, pickerTouch, pickerRfidSku, pickerTagFormat, pickerCodeKind, pickerUidExtra, tagFormatLabel, pickerHeadTag, pickerHeadKind, spoolDdOpen, spoolDdPick, spoolDdCurrent, headRfidBusy, headRfidNote, readHeadRfid,
      addFluiddCamera, fluiddCamBusy, fluiddCamMsg,
      spoolForm, spoolImportMode, spoolFileInput,
      spoolMaterials, spoolVendors, spoolSubtypes,
      spoolListShown, spoolFilterActive, spoolFormClear,
      spoolList, spoolForSlot, spoolForHead, spoolSlotLabel, spoolWeightLabel, spoolTitle,
      spoolAdd, spoolSave, spoolEdit, spoolEditCancel, spoolEditId, spoolAddBusy,
      spoolUnassign, spoolDelete, spoolDetails,
      spoolSlotOptions, spoolSlotKey, spoolMoveLocked, spoolAssignTo, spoolPickerOptions,
      smRowTaken, smRowWhere, spoolWeightDialog, spoolSort, spoolNewTitle,
      tagWrite,      tagWriteFormat, tagWriteUidSku, setTagWrite, slotOccupied,
      tagWriteOutcome, dismissTagWriteOutcome, tagRead, aceOpenFw, aceTagRead,
      paDlg, paClip, spoolPaDialog, paRowDel, paAddRow, paCopy, paPaste,
      paCalibrate, paCanCalibrate, paCalibrating, paLoading, paSpoolHead, paSpoolLoadPlan,
      paSave, paDirty, paKeyValid, paValueValid, paKeyOptions, pickerPaDialog,
      spoolCreating, spoolNewForm,
      acefw, acefwInput, acefwCandidates, acefwPickFile, acefwUpload,
      acefwCanTest, acefwReady, acefwTest, acefwFlash, acefwStatusText,
      acefwStatusBad,
      acefwPatchTarget, acefwPatchTargets, acefwTargetLabel, acefwCanPatch,
      acefwVersions, acefwGen1Versions, acefwIsV1, acefwVersionOptions,
      acefwGen1Unknown,
      spoolCreateFromPicker,
      spoolExport, spoolImport, triggerSpoolImport,
      isPrinting,
      dryerCfg, dryStart, dryStop, dryPanelOpen, toggleDryPanel, aceDrying,
      snapshots, selectedSnapshot, snapshotPreview, saveSnapshot, loadSnapshot, deleteSnapshot,
      config, configLog, configLoadError, showRawConfig, toggleRawConfig, configForm, rebootNeeded,
      printerRebootNeeded, restartNotice, restartOffered, restartNow, notifWantsReboot,
      aceHeadsRightSide,
      loadConfig, saveConfigForm, saveConfigRaw, setMode,
      tipform, loadTipform, tipformAddRow, tipformRemoveRow, saveTipform, tipformRestartPending, tipformNameOptions,
      TIPFORM_STEP_TYPES, tipformToggleBuilder, tipformAddStep, tipformRemoveStep, tipformStepsToTable, tipformStepPlaceholder, tipformInsertStock,
      preflight, closePreflight, startPreflightPrint, applyLoadout, stageLabel,
      tierLabel, tierWarn, rgbDec, sortedMapping, slicerColorsInPrintOrder,
      forcaMixed, forcaNozzleList, forcaGroups, forcaFeasible, forcaTargetHint,
      forcaSwapsDisplay, forcaBgLabel, forcaFlushG,
      forcaGroupAces,
      headOfSlot, forcaAllowedHeads, forcaHeadsForMaterial, unsetSlotsForT,
      slotKey, textOn, slicerSlotOptions, slicerEffectiveSlot, onSlicerSlotChange,
      slicerSwapsDisplay,
      headTargets, headTargetOptions, headEffectiveTargetId, headTargetLabel,
      headTargetColor, chipBg, headTargetLabelById, onHeadTargetChange, headSwapsDisplay,
      hmDropOpen, hmDdToggle, hmDdClose, hmDdPick,
      pfPlanSel, pfEst, pfTimelineSegs, pfEstCounts, pfEstDelta, pfEstStale, pfPlanList, pfPlanFeasible, pfPlanSwaps, pfPlanFlush, pfPlanBg, pfSelected, pfRecommended, pfPlanTitle, pfGoLabel, pfPrintSelected,
      headFeasible, headPlanFeasible, headPlanSwaps, headPlanBg, headPlanFlushG, headPlanBgLabel, headSlicerHex,
      headSlicerMat, headProposalLabel,
      headMaxCopies, headCopiesEffective, headCopyPlan, headCopyList, copySlotLabel, setPreflightCopies,
      headCopiesStrict, setPreflightCopiesStrict,
      headPlanMapping, multiTopology, slotHead, pinLabel,
      updateState, updateCheck, updateApply,
      debugState, debugEnable, debugDisable,
      navItems, topItems, subItems, topActive, openNav,
      webcam, webcamSrc, webcamVideo, reloadWebcam,
      printCtl, printCtlLimits, printCtlDraft, printCtlBusy, printCtlError, printCtlCan,
      printCtlNotice, printCtlQueued, printCtlRestart, printCtlRestartBlocked,
      printCtlTempDraft, printCtlTempShown,
      enclosureLock, enclosureHint, enclosureModeText, lightLocked,
      printCtlSlide, printCtlRelease, printCtlReset, printCtlHeat, printCtlCancel,
      sendPrintControl,
      consoleLines, consoleFollow, consoleEl, loadConsole, consoleTime,
      plugins, refreshPlugins, pluginIframeSrc,
      notifications, notifWarnOnly, notifTime, dismissNotification, dismissAllNotifications,
      confirmDialog, okConfirm, altConfirm, cancelConfirm, confirmInputError,
      screenCanvas, floatScreenCanvas, screenPopout, toggleScreenPopout,
      popoutStyle, popoutDragStart, popoutDragMove, popoutDragEnd,
      screenFps, screenEtag,
      screenDown, screenMove, screenUp,
      wiringContainerEl, setSlotEl, setThEl, wiringPaths, wiringViewBox,
      t, dispIdx, language, languages, setLanguage,
      picker, openPicker, closePicker, savePicker, clearPickerOverride, pickerMaterials,
      pickerDb, pickerVendors, currentSubtypes,
      pickerHasRfid, pickerHasOverride, pickerRfidStyle, readPickerRfid,
      cmdQueue, visibleQueue, cmdPaused, removeFromQueue, pauseQueue, resumeQueue, clearAllErrors,
      sendingAll, sendAllToPrinter,
      fmtArgs, cmdLabel,
      uploading, uploadInput, triggerUpload, onUploadGcode,
      startInboxPreflight, dismissInbox, inboxCanStart,
    };
  },
}).mount("#app");

// @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
// optional fourth operating mode; original MultiACE modes must not depend on it.
/* Original Flexi configuration presentation. No hardware or persistence calls. */
(function(root) {
  const sections = [
    ['initial','IN','Initial setup'],
    ['devices','','Devices'], ['routing','RT','Paths','routing'], ['pathchek','PC','PathCheck','run'],
    ['ptfe','PT','PTFE Connections','connections'], ['calibration','CA','Guided Calibration','calibration'],
    ['sources','SS','Filament Sources','sources'], ['filament','FM','Filament Sources','filament'],
    ['general','GE','General'], ['overrides','AC','ACE and slot overrides'],
    ['advanced','AD','Advanced','advanced'], ['diagnostics','DX','Diagnostics','troubleshooting'],
    ['firmware','','Firmware'], ['help','?','MultiACE Help']
  ];
  const terminal = ['complete','reconciled'];
  function status(row, job, key, pending=false, history=[]) {
    if (job?.source && job.source.join(':') === key && !terminal.includes(job.phase))
      return job.phase === 'recovery_required' ? 'Failed' : 'Testing';
    if (row?.verification === 'Conflict' || ['Blocked','Fail'].includes(row?.test_result)) return 'Failed';
    const latest = history.findLast(result => result.source?.join(':') === key && result.current_context_matches);
    if (latest?.phase === 'reconciled' && latest?.reason) return 'Failed';
    if (!pending && row?.verification === 'Pass' && row?.test_result === 'Pass' && row.evidence?.verified === true &&
        row.evidence?.physically_reconciled === true && Number.isInteger(row.observed_head) &&
        row.observed_head === row.configured_head) return 'Verified';
    return 'Not Tested';
  }

  const readinessLabels = {
    ready:'Ready', needs_recipe:'Settings needed', recovery_required:'Recovery needed',
    blocked:'Blocked', unavailable:'Source unavailable', disconnected:'Not connected',
    adapter_unsupported:'Adapter unsupported', unmapped:'Not mapped', draft:'Unsaved changes'
  };
  const readinessActions = {
    map:'Map route', save:'Review changes', set_up_recipe:'Review settings',
    open_recovery:'Open recovery', open_operations:'Open operations',
    review_source:'Review availability', open_devices:'Open devices', review_path:'Review path'
  };

  function routeReadiness(row, options = {}) {
    const key = row?.key;
    if (!key) return {state:'unmapped', label:readinessLabels.unmapped,
      reason:'Choose a slot before configuring a route.', action:'map', actionLabel:readinessActions.map, missing:[]};
    const inventory = options.inventory || [];
    const saved = options.savedRoutes || {};
    const draft = options.draft || {};
    const slotStatus = options.slotStatus || {};
    const slotDraft = options.slotDraft || {};
    const savedHead = Number.isInteger(saved[key]) ? saved[key] : -1;
    const draftHead = Object.hasOwn(draft, key)
      ? (Number.isInteger(draft[key]) ? draft[key] : -1) : savedHead;
    const savedStatus = slotStatus[key] || 'assigned';
    const draftStatus = slotDraft[key] || savedStatus;
    if (draftHead !== savedHead || draftStatus !== savedStatus)
      return {state:'draft', label:readinessLabels.draft,
        reason:'Save routes to update readiness. No saved route or recipe was removed.',
        action:'save', actionLabel:readinessActions.save, missing:[]};
    if (row.connected === false)
      return {state:'disconnected', label:readinessLabels.disconnected,
        reason:'ACE communication is unavailable. Reconnect the unit and refresh before loading.',
        action:'open_devices', actionLabel:readinessActions.open_devices, missing:[]};
    if (!Number.isInteger(saved[key]))
      return {state:'unmapped', label:readinessLabels.unmapped,
        reason:'Choose the physical toolhead for this slot and save routes.',
        action:'map', actionLabel:readinessActions.map, missing:[]};
    const blocked = (options.recovery || []).find(record =>
      record.head === savedHead || (record.ace === row.ace && record.slot === row.slot));
    if (blocked)
      return {state:'recovery_required', label:readinessLabels.recovery_required,
        reason:`T${savedHead} has an interrupted operation. Inspect the path and record the recovery result before loading.`,
        action:'open_recovery', actionLabel:readinessActions.open_recovery, missing:[]};
    const item = inventory.find(entry => entry.key === key);
    if (!item)
      return {state:'unavailable', label:readinessLabels.unavailable,
        reason:'Route readiness is not available from the printer. Save routes or reload the saved routing before loading.',
        action:'review_path', actionLabel:readinessActions.review_path, missing:[]};
    // Older engines only expose recipe_complete, which also covers unload
    // fields (retract_mm/tip_moves). Do not tell the operator a load recipe is
    // incomplete when the print/load actions are explicitly complete.
    const completeActions = item.profile_complete_actions || item.validated_actions || [];
    const loadReady = item.load_recipe_complete === true || item.recipe_complete === true ||
      (Array.isArray(completeActions) && completeActions.includes('load'));
    const explicitlyIncomplete = item.load_recipe_complete === false ||
      item.recipe_complete === false || (item.load_missing || []).length > 0;
    const state = readinessLabels[item.route_state] ? item.route_state
      : (loadReady ? 'ready' : (explicitlyIncomplete ? 'needs_recipe' : 'ready'));
    const missing = item.load_missing || item.route_missing || [];
    const reason = item.route_reason || (state === 'needs_recipe'
      ? 'Required route settings are missing. The saved route is kept; review or customize its settings, or use the standard MultiACE settings.'
      : state === 'ready' ? 'Route settings are complete. Starting a load still uses the normal live checks.'
      : 'Review this route before loading.');
    const action = item.route_action || (state === 'needs_recipe' ? 'set_up_recipe' : 'review_path');
    return {state, label:readinessLabels[state] || 'Check route', reason,
      action, actionLabel:readinessActions[action] || '', missing:
      Array.isArray(missing) ? missing.slice() : []};
  }

  function discoveryRows(devices, flexi, pending = {}) {
    const keys = new Set([...Object.keys(flexi.routes || {}), ...Object.keys(flexi.slot_status || {})]);
    for (const device of devices) {
      if (!Number.isInteger(device.idx) || device.idx < 0 || device.idx > 3) continue;
      const reported = Number(device.slot_count ?? device.slots?.length ?? 4);
      const slotCount = Math.max(1, Math.min(4, Number.isInteger(reported) && reported > 0 ? reported : 4));
      for (let slot = 0; slot < slotCount; slot++) keys.add(device.idx + ':' + slot);
    }
    const pathchek = flexi.pathchek || {};
    return [...keys].sort().map(key => {
      const ace = Number(key.split(':')[0]);
      const route = flexi.route_verification?.[key];
      const readiness = pathchek.routes?.find(row => row.key === key);
      const connected = devices.some(device => device.idx === ace && device.connected === true);
      const availability = flexi.slot_status?.[key] || 'assigned';
      const configured = flexi.routes?.[key];
      const job = pathchek.job;
      const isBusy = job && !terminal.includes(job.phase);
      const result = status(route, job, key, Object.hasOwn(pending, key), pathchek.results);
      let reason = readiness?.reason;
      if (!Number.isInteger(configured)) reason = 'Confirm the tubing and save a provisional destination first. A sensor can identify arrival but cannot establish safe travel or shared-path clearance for unknown tubing.';
      if (availability !== 'assigned') reason = 'This slot is ' + availability + '. New feeding is unavailable.';
      if (!connected) reason = 'ACE communication unavailable. Reconnect the unit and review its identity.';
      if (isBusy) reason = job.source?.join(':') === key ? (job.reason || 'Continue or reconcile the current PathCheck operation.') : 'Finish or reconcile the current PathCheck operation before selecting another slot.';
      return {key, configured, observed:route?.observed_head, result, reason,
        canSelect:!isBusy && connected && availability === 'assigned' && Number.isInteger(configured)};
    });
  }

  function observedTopology(rows) {
    const heads = [...new Set(rows.map(row => row.observed).filter(Number.isInteger))].sort((a,b) => a-b);
    return heads.flatMap(head => {
      const keys = rows.filter(row => row.result === 'Verified' && row.observed === head).map(row => row.key);
      if (!keys.length) return [];
      return [{id:'observed-'+head, head, keys, logical:true, joiner:keys.length>1 ? {name:keys.length+'-to-1 logical grouping'} : null}];
    });
  }

  // Offline subset of official references. Extend only with verified code/source pairs.
  const errorCatalog = {
    revision:'2026-11-09.1',
    'Snapmaker U1':{url:'https://wiki.snapmaker.com/en/snapmaker_u1/troubleshooting/u1_error_codes', codes:{
      '0002-0525-0000-0003':'The toolhead 1 feeder did not detect filament entering.',
      '0002-0525-0000-0004':'Filament is blocked at the toolhead 1 drive gear.'
    }},
    'Anycubic ACE Pro':{url:'https://wiki.anycubic.com/en/error-codes', codes:{
      '11503':'The printer reported an ACE Pro connection problem.',
      '11513':'The printer did not receive an ACE Pro response in time.'
    }},
    'Anycubic ACE 2 Pro':{url:'https://wiki.anycubic.com/en/error-codes', codes:{}}
  };

  function lookupError(source, code) {
    const reference = Object.hasOwn(errorCatalog, source) ? errorCatalog[source] : null;
    return {source, code, revision:errorCatalog.revision, url:reference?.url,
      description:code === undefined || code === null || code === '' ? 'No error code reported' :
        (reference?.codes && Object.hasOwn(reference.codes, code) ? reference.codes[code] : 'Unknown error code for this reporting source')};
  }

  function diagnostic(row, pathchek, route, sensors) {
    const current = pathchek.job?.source?.join(':') === row.key ? pathchek.job : null;
    const last = (pathchek.results || []).findLast(job => job.source?.join(':') === row.key && job.current_context_matches);
    const job = current || last;
    const observation = job?.discovery || route?.evidence;
    const reason = job?.reason || observation?.reason || row.reason || 'Verification did not complete.';
    const unexpected = observation?.observed_heads?.length > 1 ||
      (Number.isInteger(observation?.observed_head) && observation.observed_head !== row.configured);
    let title = 'Filament route could not be verified';
    let guidance = 'Review the route, sensor readiness and recorded evidence before starting another test.';
    if (unexpected) {
      title = 'Filament reached an unexpected or ambiguous destination';
      guidance = 'Trace the selected tube and inspect all configured and observed branches. Reconcile physical position before considering a route correction.';
    } else if (job?.feed_diagnostic?.reason === 'transport_rejected') {
      title = 'The ACE rejected a feed or stop request';
      guidance = 'Check the recorded command and raw ACE reply. Confirm the tip position before retrying. A rejection alone does not identify a jam.';
    } else if (job?.direction === 'retract') {
      title = 'Filament retraction could not be verified';
      guidance = 'Inspect the tip position and shared branches. Use the recovery controls only after confirming the filament is stopped.';
    } else if (job?.feed_diagnostic?.reason === 'physical_progress_unverified') {
      title = 'Filament movement could not be confirmed';
      guidance = 'Inspect the spool, visible filament mark and tube before retrying. A stopped motor alone does not identify an obstruction.';
    } else if (/timeout|timed out|communication|heartbeat/i.test(reason)) {
      title = 'PathCheck lost a response or exceeded a time limit';
      guidance = 'Check the indicated device or browser connection and inspect the filament position before retrying.';
    }
    return {schema:1, source:row.key, configured:row.configured, observed:observation?.observed_head,
      title, reason, guidance, positionUncertain:job?.phase === 'recovery_required' || Boolean(job && !job.physically_reconciled),
      phase:job?.phase, direction:job?.direction, observedAt:observation?.observed_at_utc,
      code:lookupError(job?.reported_error?.source, job?.reported_error?.code),
      reportedError:job?.reported_error, evidence:{job, observation, currentSensorDiagnostics:sensors}};
  }

  function topology(routes={}, joiners={}, verification={}, manualOverrides={}) {
    const groups=[];
    const heads = [...new Set(Object.values(routes).filter(head => Number.isInteger(head) && head >= 0))].sort((a,b) => a-b);
    const counts={};
    Object.values(routes).forEach(head=>{counts[head]=(counts[head]||0)+1;});
    function connection(key, head, joiner, missing) {
      if (joiner) return {type:'shared', configured:true, head, fitting_required:true,
        joiner:{id:joiner.id, name:joiner.name, branch:joiner.branches?.[key]}};
      if (manualOverrides[key] === true) return {type:'other', configured:true, head,
        fitting_required:true, joiner:null};
      if (missing || (counts[head] || 0) > 1) return {type:'shared', configured:false,
        head, fitting_required:true, joiner:null};
      return {type:'direct', configured:true, head, fitting_required:false, joiner:null};
    }
    for(const head of heads) {
      const keys=Object.keys(routes).filter(key=>routes[key]===head).sort();
      if(!keys.length)continue;
      const used=new Set();
      for(const [id,j] of Object.entries(joiners)) {
        if(j.head!==head)continue;
        const branch=keys.filter(key=>Object.hasOwn(j.branches||{},key));
        if(!branch.length)continue;
        branch.forEach(key=>used.add(key));
        const withId={...j,id};
        groups.push({id,head,keys:branch,joiner:withId,verification,
          connection_type:'shared', connection:connection(branch[0],head,withId,false)});
      }
      const loose=keys.filter(key=>!used.has(key));
      for(const key of loose) {
        const missing=keys.length>1;
        const conn=connection(key,head,null,missing);
        groups.push({id:key,head,keys:[key],verification,missing,
          connection_type:conn.type, connection:conn});
      }
    }
    return groups;
  }
  const exampleRoutes={'0:0':0,'0:1':1,'0:2':2,'0:3':2,'1:0':3,'1:1':3,'1:2':3,'1:3':3};
  const examples=[
    {title:'Direct connection',groups:topology({'0:0':0})},
    {title:'Two inputs, one output',groups:topology({'0:2':2,'0:3':2},{pair:{name:'2-to-1',head:2,branches:{'0:2':'Input 1','0:3':'Input 2'}}})},
    {title:'Four inputs, one output',groups:topology({'1:0':3,'1:1':3,'1:2':3,'1:3':3},{four:{name:'4-to-1',head:3,branches:{'1:0':'Input 1','1:1':'Input 2','1:2':'Input 3','1:3':'Input 4'}}})},
    {title:'Two ACE units, four toolheads',groups:topology(exampleRoutes,{
      pair:{name:'2-to-1',head:2,branches:{'0:2':'Input 1','0:3':'Input 2'}},
      four:{name:'4-to-1',head:3,branches:{'1:0':'Input 1','1:1':'Input 2','1:2':'Input 3','1:3':'Input 4'}}
    })},
    {title:'Custom example: two units share T1',groups:topology({'0:1':1,'1:2':1},{custom:{name:'Cross-unit joiner',head:1,branches:{'0:1':'A','1:2':'B'}}})}
  ];
  const chapters=[
    ["modes","MultiACE operating modes","general","Normal, Multi, Head and Flexi are chosen under Config → Initial setup.",[
      "Normal uses the stock Snapmaker setup. Multi and Head are the original MultiACE modes and work as they always have.",
      "Flexi lets you say which ACE slot feeds which toolhead (Config → Paths), including several slots sharing one toolhead through a splitter.",
      "Before switching mode, let any load, unload or print finish and unload the toolheads if the switch asks you to. Klipper restarts once to apply the new mode."
    ]],
    ["spools","Spools and RFID","devices","The Spools tab manages what is loaded in each ACE slot: material, colour, brand, remaining weight and Spoolman links.",[
      "Pick the ACE and slot first, then assign or read a spool.",
      "ACE Pro (Gen 1) units read RFID tags in place, without moving the spool. Writing tags needs an ACE Pro 2.",
      "On an ACE Pro 2 a tag read turns the spool, so in Flexi mode that read is switched off."
    ]],
    ["dryers","Dryer operation","overrides","Start and stop ACE drying from the Dashboard; default temperature and time are under Config → General.",[
      "Starting a dryer never changes filament routes.",
      "Stop ends drying the normal MultiACE way; wait until the ACE reports that it stopped.",
      "While drying in Flexi mode the ACE does not turn the spools automatically."
    ]],
    ["settings","Saving MultiACE settings","general","General settings, routes and lengths each have their own Save button.",[
      "Save only what you meant to change. If the file was changed elsewhere in the meantime, your edits are kept on screen: reload, then apply them again.",
      "Lengths and most General settings are written to ace.cfg and take effect after a Klipper restart; the page tells you when one is needed.",
      "Install updates with the Flexi release package (check.sh, then install.sh). It keeps your configuration, routes, spools and recovery records and can be undone with rollback.sh."
    ]],
    ["welcome","Welcome to MultiACE","routing","MultiACE is developed by decay71 (github.com/decay71/multiACE). Flexi adds configurable filament paths: you tell MultiACE which ACE slot feeds which toolhead, either through its own tube or through a splitter shared with other slots.",[
      "Mode is chosen under Config → Initial setup. The Dashboard is for daily loading and unloading, Spools for filament identity and RFID, and Config for setup and preferences.",
      "Start with the tubing already on your printer: trace each tube from its ACE slot to its toolhead, or let Config → Paths → Find routes automatically work it out.",
      "Saving routes only describes your tubing; it never moves filament."
    ]],
    ["hardware","Hardware requirements","ptfe","Snapmaker U1 with the PAXX12 firmware and MultiACE, with one to four Anycubic ACE Pro or ACE Pro 2 units (up to 16 slots).",[
      "ACE Pro (Gen 1) units are tested with Flexi. ACE Pro 2 routes are supported but have not been tested on hardware yet.",
      "You need PTFE tubes seated firmly at both ends and a splitter wherever several slots share one toolhead. Flexi does not need the U1 stock feeders."
    ]],
    ["joiners","Understanding splitters (PTFE joiners)","ptfe","A splitter, also called a joiner, combines several inlet tubes into one outlet tube. A slot with its own tube to a toolhead needs none.",[
      "Trace each inlet back to its ACE slot and the outlet to its toolhead, and label the inlets so they match the settings.",
      "The 2-into-1 and 4-into-1 drawings are examples of the layout, not exact parts."
    ]],
    ["routing","Setting up filament routing","routing","Config → Paths has one row per ACE slot. ACE 0 slot 0 is the first slot of the first ACE.",[
      "Assigned means the slot can be used. Empty means no filament is expected there. Unused leaves the slot out on purpose.",
      "Pick T0–T3 to match where the tube really goes, or Disabled to remove the route, then press Save routes (this also saves availability and splitters). Discard edits goes back to the saved state.",
      "Not sure which slot reaches which toolhead? Find routes automatically at the top of Paths finds out and fills this table when you press Use these routes."
    ]],
    ["connections","Configuring splitters","ptfe","Config → Paths → PTFE diagram → Edit tube splitters: give each splitter a name, the toolhead its outlet goes to, and the slots that enter it.",[
      "Tick the slots whose tubes enter the splitter and press Save routes. Then trace the tubes at the printer, tick \"I checked that the tubes on the printer match this layout\", add a short note and save again.",
      "Changing which slots enter a splitter clears that check. The toolhead sensor cannot tell which splitter inlet a filament came through."
    ]],
    ["discovery","Finding routes automatically","routing","Config → Paths → Find routes automatically works out which toolhead each ACE slot feeds, how long each tube is and which slots share a splitter.",[
      "Before you start: unload every toolhead and make sure every tube is recorded clear (the panel lists anything still missing). The slots you want to probe must hold filament. Stay near the printer the first time.",
      "For one slot at a time it feeds slowly (25 mm/s by default, at most 3000 mm) until a toolhead sensor sees the filament, then pulls it back by the same distance minus 6 % and 20 mm, so the tip ends just in front of where it started: inside the ACE, behind every splitter. No heating.",
      "Find routes probes every slot that holds filament. Probe / Probe again on a row tests just that slot after you confirm. Stop pulls the current slot back and ends the run.",
      "Results: T0–T3 in green means the slot reaches that toolhead. Not reached means no sensor saw filament within the limit (the tube is not connected, or the filament did not move). Did not advance means the filament slipped and its end is now at the ACE inlet: re-seat that spool. A failure stops the run until you press I inspected it.",
      "Use these routes copies the results into the routing table and the PTFE diagram; nothing is saved until you press Save routes. Use all suggested and Save lengths write each slot's load and pull-back length to ace.cfg (applied after a Klipper restart)."
    ]],
    ["sources","Filament Sources","sources","Config → Filament Sources sets what feeds each toolhead: the ACE slots routed to it in Paths, the U1 stock feeder, or a manual spool (for example from a dryer).",[
      "An ACE toolhead can have several slots routed to it, for colour changes and runout backup.",
      "Choosing U1 stock feeder or Manual spool stops ACE loads, swaps and runout backup into that toolhead and keeps your ACE routes. In Flexi those toolheads are loaded and unloaded at the printer (touchscreen or by hand), not from the dashboard. Changing a source never moves filament.",
      "A temporary manual source lasts until the next restart. Material and colour fields describe a manual spool only."
    ]],
    ["filament","Loading, unloading and runout backup","filament","How Flexi loads, unloads and replaces a spool that runs out.",[
      "Load, Unload and tool changes use the original MultiACE loader with the load length, pull-back length and speeds from ace.cfg. Flexi first checks the saved route, the tube record and any open recovery, and if something is wrong it refuses with the reason instead of guessing.",
      "When a spool runs out, the toolhead is refilled from a spare slot routed to the same toolhead with the same material and brand (and colour, unless you allow another colour for this print). Set the order under Config → General → Backup spool order.",
      "Flexible filament (TPU/TPE) can buckle in long tubes and splitters, so Flexi only loads it on a path where you have allowed it."
    ]],
    ["clearance","Shared tubes and splitters","ptfe","Two filaments can never be in the same tube after a splitter at the same time.",[
      "Before a different slot feeds through a splitter, the previous filament must be pulled back behind the splitter. An unload does this when the slot's pull-back length is long enough (set it in Paths); otherwise Flexi asks you to check the tube first.",
      "The tube record for each toolhead (clear, loaded or uncertain) is what Flexi uses for this. Uncertain means it does not know: look at the tube and record what you find in the Dashboard recovery panel."
    ]],
    ["advanced","Advanced configuration","advanced","Advanced view adds tip forming, Diagnostics and Firmware to Config.",[
      "General and Advanced settings edit ace.cfg; they do not save routes. Reload replaces unsaved edits with the saved file.",
      "Diagnostics shows the live toolhead sensors (e0–e3 are their Klipper names for T0–T3) and the ACE connections. Firmware holds the ACE firmware tools.",
      "Do not use \"multiACE updates\" on the Firmware page with this Flexi build: it installs the stock MultiACE release over it. Update Flexi with its own release package."
    ]],
    ["troubleshooting","Troubleshooting","diagnostics","Find the symptom before changing settings.",[
      "A slot shows Empty although a spool is on it: check that the filament actually reaches the ACE inlet.",
      "A button is greyed out: hover over it. Every disabled control says why (for example a loaded toolhead, a tube not recorded clear, a print running, or nothing changed yet).",
      "Filament reached a different toolhead than expected: stop, trace the tubes, then fix the route in Paths or run Find routes.",
      "A toolhead sensor shows Not reporting on Diagnostics: do not load into that toolhead until it reads Yes or No again.",
      "Jam, broken filament or a splitter that is not clear: stop and look before doing anything else. Do not force a second filament through.",
      "The Dashboard asks for recovery after an interrupted load or unload: look at the tube, then record what you found (clear, or filament still in the tube). Refreshing the page does not clear it.",
      "Routes will not save or a splitter shows a conflict: every slot in a splitter must be routed to the splitter's toolhead; reload if someone else saved in the meantime."
    ]],
    ["usb","ACE connection alerts","diagnostics","Why an alert such as \"Only 0 of 2 ACE units found on USB at startup\" or \"ACE 1 reset its USB connection\" can appear right after Klipper restarts, and when you need to act.",[
      "An ACE Pro (Gen 1) drops off USB and comes back by itself whenever nothing has talked to it for a few seconds (about every 3.5 s), and again when its port is opened. Every Klipper restart (installing an update, changing mode, FIRMWARE RESTART) leaves such a gap.",
      "If MultiACE opens the port at that moment it sees a short USB error, or at startup no unit at all. One unit: \"ACE n reset its USB connection while Klipper was starting and was reconnected\", nothing to do. Units missing at startup: MultiACE keeps checking and restarts Klipper by itself once they are back (only while the printer is idle), normally within a minute.",
      "An ACE connected through more USB hubs (for example a second hub in a chain) is more likely to be caught in the middle of such a reset.",
      "Act only if it repeats or does not clear: check the power and USB cable of that ACE and the hub it is on (a powered hub helps), then press FIRMWARE RESTART. During a print the warning appears at once; a reconnect within a few seconds lets the print continue, otherwise the print pauses so you can fix the connection and resume.",
      "In Flexi mode the automatic restart waits for you if a different ACE unit appears in a saved place, a Flexi load or unload was interrupted, or route discovery was running. The alert then says which, so you can check the units and press FIRMWARE RESTART."
    ]],
    ["glossary","Glossary","routing","ACE: Anycubic filament feeder with 4 slots. Slot: one spool position in an ACE. Toolhead (T0–T3): the print head the filament goes to.",[
      "PTFE tube: the low-friction tube from an ACE slot to a toolhead. Splitter (joiner): a fitting that merges several tubes into one. Route: which toolhead a slot feeds.",
      "Load length: how far a load may feed before giving up (the toolhead sensor stops it on arrival). Pull-back length: how far an unload pulls the filament back toward the ACE.",
      "Tube record: what Flexi remembers about each toolhead's tube (clear, loaded or uncertain). Recovery: recording what is really in a tube after an interrupted load or unload.",
      "Route discovery: feeding one slot until a toolhead sensor sees it, then pulling it back, to learn its route and tube length."
    ]]
  ];
  // Tutorial step strips illustrate actions; they never represent live progress.
  const flows={
    modes:['Review the mode','Finish active operations','Confirm the transition'],
    spools:['Choose ACE and slot','Review filament identity','Use a supported action'],
    dryers:['Choose the ACE','Review temperature and duration','Observe device status'],
    settings:['Edit the intended settings','Save the matching scope','Resolve any conflict'],
    welcome:['Trace the tubing','Find or assign each route','Save routes and lengths'],
    hardware:['ACE slot','PTFE guide tube','U1 toolhead'],
    joiners:['Label the inlets','Inspect the joiner','Trace its outlet'],
    routing:['Pick a slot','Match the toolhead','Save Routes'],
    connections:['Name the joiner','Assign branches','Inspect and confirm'],
    discovery:['Unload every toolhead','Probe the slots','Use the routes and lengths'],
    sources:['Choose a toolhead','Choose its source','Keep the saved ACE route'],
    filament:['Select a source','Check its recipe','Review session limits'],
    clearance:['Stop the previous strand','Inspect every branch','Confirm clearance'],
    advanced:['Review current settings','Use the correct Save','Check diagnostics'],
    usb:['Klipper restarts','The ACE resets its USB','MultiACE reconnects it'],
    troubleshooting:['Identify the symptom','Inspect the evidence','Recover before retrying'],
    glossary:['Route: planned path','Qualification: prerequisites','Verification: observed result']
  };
  function search(query) {
    const words=query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return chapters.filter(ch=>words.every(word=>ch.flat(2).join(' ').toLowerCase().includes(word)));
  }
  const diagram={
    props:['group','example'],emits:['adapter'],methods:{routeStatus:status},
    computed:{
      middle(){return this.group.keys.length*22;},
      height(){return this.group.keys.length*44;},
      portCount(){return this.group.keys.length;}
    },
    template: /* html */ '<div class="fx-diagram"><div class="fx-graphic"><svg :viewBox="\'0 0 440 \'+height" role="img" :aria-label="group.keys.join(\', \')+\' to toolhead T\'+group.head">'+
      '<g v-for="(key,i) in group.keys" :key="key" :transform="\'translate(0 \'+(i*44)+\')\'"><rect x="6" y="5" width="118" height="34" rx="9"/><text x="65" y="27" text-anchor="middle">ACE {{key.split(\':\')[0]}} · slot {{key.split(\':\')[1]}}</text><path :d="\'M124 22 H157 L\'+(group.joiner ? 208 : 335)+\' \'+(middle-i*44)" :class="{uncertain:group.missing}"/><text v-if="group.joiner" class="fx-port" x="143" y="16">{{i+1}}</text></g>'+
      '<g v-if="group.joiner"><rect class="fx-combiner" x="205" :y="middle-17" width="65" height="34" rx="9"/><text x="237" :y="middle+5" text-anchor="middle">{{portCount}} → 1</text><path :d="\'M270 \'+middle+\' H335\'"/></g>'+
      '<path :d="\'M325 \'+(middle-6)+\' L335 \'+middle+\' L325 \'+(middle+6)"/><rect x="335" :y="middle-17" width="97" height="34" rx="9"/><text x="383" :y="middle+5" text-anchor="middle">T{{group.head}}</text></svg>'+
      '<button v-if="group.joiner && !group.logical" class="fx-adapter-hit" type="button" :aria-label="\'About \'+group.joiner.name" @click="$emit(\'adapter\',group,$event.currentTarget)"><span class="fx-sr">About {{group.joiner.name}}</span></button></div>'+
      '<p v-if="group.connection_type === \'direct\' && !example" class="muted small">Direct PTFE connection · no fitting required.</p>'+
      '<p v-if="group.missing" class="fx-warning">Shared T{{group.head}}: no joiner is recorded for this branch. Dashed line shows a saved assignment, not a direct tube.</p>'+
      '<p v-if="!example && group.joiner">{{group.joiner.name}} · {{group.joiner.confirmed ? \'Topology inspected, clearance not established\' : \'Physical topology unconfirmed\'}}</p>'+
      '<ul v-if="!example"><li v-for="key in group.keys" :key="key">{{key}}: saved T{{group.head}} · observed {{Number.isInteger(group.verification[key]?.observed_head) ? \'T\'+group.verification[key].observed_head : \'not recorded\'}} · {{routeStatus(group.verification[key])}}</li></ul></div>'
  };
  const pathSetupHelp = {
    source:'The first number is the ACE unit and the second is its slot. T0 through T3 are toolheads. This list comes from saved Routing. Choose the slot whose tube and measurements you are documenting.',
    readiness:'This is the status of the measurements for the selected route and its current setup. The saved destination comes from Routing. Neither the status nor the saved destination tells you whether filament is in the tube now.',
    notes:'Write down what you observed at the printer, the point you used as zero when measuring the tip position, and what is still unknown. These notes are kept in the browser draft. The evidence reference below is submitted separately.',
    step_mm:'The longest single move that your test evidence supports, in millimetres of filament, in either direction. This caps each feed or pullback command. Use the tested movement limit, with physical evidence that the feeder stayed within it.',
    speed:'The speed setting used in your supported test, in millimetres per second. PathCheck sends it to the ACE for both feeding and pulling back. Get it from the test record. A speed command alone does not measure how fast the filament moved.',
    max_feed_mm:'The forward feed cutoff, measured in millimetres of tip position from your documented zero point along the route. PathCheck checks that the current upper tip position plus the next feed step and possible extra travel after Stop stay within this cutoff. Use the supported test evidence.',
    safe_limit_mm:'The farthest position the filament tip can safely reach, in millimetres from the same zero point. This is the outer safety limit. The feed cutoff plus extra travel after Stop must fit inside it. Use your documented safe position limit.',
    timeout_s:'The maximum time, in seconds from test start, for a PathCheck session. It includes time to confirm the feeder has stopped. Use the time limit supported by the test evidence.',
    stop_settle_s:'The time in seconds from sending Stop until the motor has settled at rest, from your stop test. PathCheck uses it to decide how soon it can accept a reported stop observation.',
    stop_timeout_s:'The longest time, in seconds from sending Stop, to allow for physical stop confirmation. It must exceed settling time. If confirmation does not arrive in time, PathCheck requires recovery. Use the supported stop test limit.',
    ack_timeout_s:'The longest wait, in seconds from sending a movement command, for the ACE to reply that it accepted the request. Use the supported communication test limit. A reply does not prove movement or a physical stop.',
    overshoot_mm:'The greatest measured extra filament travel, in millimetres, from the Stop request until physical rest. Use the stop test record. PathCheck allows for this extra travel when checking feed and pullback limits.',
    retention_min_mm:'The lowest tip position, in millimetres from the same zero point, at which the ACE still holds the filament. Use the observed retention limit. Pulling back farther could release the filament from the feeder.',
    parking_mm:'The measured tip position where you leave filament between tests, in millimetres from the same zero point. Use the position recorded during parking. It must be within the range that keeps the filament held and the path clear.',
    shared_clear_max_mm:'The highest tip position, in millimetres from the same zero point, that still leaves the path clear for the required parking condition. Use your physical clearance evidence, including every branch when routes share a splitter. A clear toolhead sensor alone cannot establish this.',
    max_sensor_age_s:'The oldest toolhead sensor reading PathCheck may use, in seconds since the recorded reading. Older readings are rejected. Use the freshness limit supported by the sensor observations and their timestamps.',
    max_device_age_s:'The oldest ACE status reading PathCheck may use, in seconds since the recorded reading. Older readings are rejected. Use the freshness limit supported by the ACE status observations and their timestamps.',
    polarity:'Choose the raw On or Off reading that this toolhead showed with filament physically present. Compare it with the reading when filament was absent. PathCheck uses this to tell which toolhead detected arrival. Leave Unknown if you have not verified both conditions.',
    bounded_motion_qualified:'Check this only if the recorded test showed the feeder stayed within the movement limit. An accepted command is not evidence of how far the filament moved.',
    sensors_qualified:'Check this only after comparing all four live sensor readings against physically observed filament presence and absence. The readings must change reliably so PathCheck can detect arrival at either the expected or a different toolhead.',
    stop_qualified:'Check this only if you observed physical rest and recorded how far the filament traveled after Stop was sent. An ACE reply or a Ready status alone is not proof.',
    parking_qualified:'Check this only if you observed that the ACE still held the filament at the measured parked position. This shows the filament was not pulled out of the feeder.',
    shared_clearance_qualified:'For shared routes, check this only if you physically inspected the splitter and confirmed all shared branches are clear at the recorded parked position, including branches marked Empty or Unused. The toolhead sensor cannot confirm clearance inside the tubes.',
    reference:'Identify the log, measurement sheet, or witnessed test that supports the values. Include enough detail to find the evidence and its position reference. This reference goes with the submitted measurements.',
    save:'Keeps a draft for this route and setup in this browser. Use it to keep incomplete observations. It does not send them to the printer or grant permission to move.',
    submit:'Sends the measured values and confirmations for the current route to the PathCheck verifier. The verifier checks the setup, required evidence fields, and safety limits. Submission does not start a test or approve movement.',
    recovery:'An interrupted filament operation still needs to be resolved in Recovery. Until then, measurement submission is blocked. You can still save a draft in this browser.',
    status:'This message reports whether the browser draft was saved or restored, or why a submission could not proceed. A saved browser draft does not qualify the route.'
  };
  const pathInfo = {
    props:['topic','label','instance'],
    computed:{
      helpId(){return 'fx-path-info-'+this.topic+(this.instance == null ? '' : '-'+this.instance);},
      helpText(){return pathSetupHelp[this.topic];}
    },
    methods:{
      show(){const tip=this.$refs.tip;if(tip && !tip.matches(':popover-open')) tip.showPopover();},
      closeOnBlur(event){
        if(this.$el.contains(event.relatedTarget)) return;
        const tip=this.$refs.tip;
        if(tip?.matches(':popover-open')) tip.hidePopover();
      }
    },
    template: /* html */ '<span class="fx-path-info"><button type="button" class="fx-path-info-button" :aria-label="\'Help: \'+label" :aria-describedby="helpId" @pointerenter="show" @focus="show" @blur="closeOnBlur" @click.stop.prevent="show">?</button><span ref="tip" :id="helpId" popover="auto" role="tooltip" class="fx-path-info-popover"><strong>{{label}}</strong><span>{{helpText}}</span></span></span>'
  };
  const pathMeasurements = [
    ['step_mm','Largest tested feed/retract step (mm)'], ['speed','Tested feed speed (mm/s)'],
    ['max_feed_mm','Maximum allowed feed from the reference (mm)'], ['safe_limit_mm','Farthest safe tip position (mm)'],
    ['timeout_s','Maximum session duration (s)'], ['stop_settle_s','Observed motor settling time (s)'],
    ['stop_timeout_s','Maximum wait for a physical stop (s)'], ['ack_timeout_s','Maximum wait for command response (s)'],
    ['overshoot_mm','Maximum measured travel after stop (mm)'], ['retention_min_mm','Closest position that retains filament (mm)'],
    ['parking_mm','Measured parked tip position (mm)'], ['shared_clear_max_mm','Farthest position with the path clear (mm)'],
    ['max_sensor_age_s','Maximum reliable sensor reading age (s)'], ['max_device_age_s','Maximum reliable ACE reading age (s)']
  ];
  const pathWitnesses = [
    ['bounded_motion_qualified','Verified the feeder stays within the recorded movement limit.'],
    ['sensors_qualified','Verified each toolhead sensor changes reliably with filament presence.'],
    ['stop_qualified','Observed motor rest and measured travel after the stop request.'],
    ['parking_qualified','Observed filament retained at the measured parked position.'],
    ['shared_clearance_qualified','Inspected the junction and verified every shared branch clears at the parked position.']
  ];
  function pathStartBlock({route, printing, busy, recovery, job, confirmed, branches, low, high, record}) {
    if (job && !['complete','reconciled'].includes(job.phase)) return 'Finish or recover the existing PathCheck test first.';
    if (recovery) return 'Resolve the interrupted filament operation in Load, Unload and Recover first.';
    if (printing || busy) return 'Wait until the printer and filament controls are idle.';
    if (!route) return 'Choose a slot with a saved toolhead assignment.';
    if (route.availability && route.availability !== 'assigned') return 'This slot is unavailable. Review its availability in Routing.';
    if (!route.qualified) return 'This filament path hasn’t been verified yet. Open Initial Path Setup.';
    if (!route.ready) return 'Check that the ACE and sensors are ready and the filament paths are clear.';
    if (!confirmed || !branches) return 'Confirm the actual route, stopped feeder and branch clearance below.';
    if (!record || [low, high].some(value=>value === '' || value == null || !Number.isFinite(Number(value)))
        || !(record.retention_min_mm <= Number(low) && Number(low) <= Number(high) && Number(high) <= record.shared_clear_max_mm))
      return 'Enter a measured starting range inside the verified parking limits.';
    return '';
  }
  function measuredPathRecord(draft, context, shared) {
    if (!context || !draft.reference?.trim()) throw Error('A current route and an observation reference are required.');
    const record = {context, reference:draft.reference.trim(), present_polarity:[]};
    for (const [key,label] of pathMeasurements) {
      const value = Number(draft[key]);
      if (!Number.isFinite(value) || value <= 0) throw Error('Enter a measured positive value for: '+label);
      record[key] = value;
    }
    for (const value of draft.present_polarity || []) {
      if (value !== true && value !== false) throw Error('Record the observed presence reading for all four sensors.');
      record.present_polarity.push(value);
    }
    if (record.present_polarity.length !== 4) throw Error('Record the observed presence reading for all four sensors.');
    for (const [key,label] of pathWitnesses) {
      record[key] = draft[key] === true;
      if (!record[key] && (key !== 'shared_clearance_qualified' || shared)) throw Error(label);
    }
    return record;
  }
  function productionRecovery(position, receipt, resumeBlock) {
    switch (position) {
      case 'empty': return {action:'inspect_clear', note:'Trace the full ACE-to-toolhead path and every shared branch. Confirm the tube and toolhead are empty and the feeder is stopped. Filament parked in the ACE slot does not block this action and does not mean the toolhead is loaded.'};
      case 'partial':
        if (resumeBlock === '') return {action:'resume_loading', note:'This load reached the toolhead sensor and stopped before seating. Confirm the filament has stayed in place. Resume uses the normal heated load from seating onward; it will not repeat the ACE feed.'};
        if (resumeBlock) return {note:resumeBlock};
        return {note:'The tube is occupied. This build has no bounded resume or withdraw action for an interrupted feed without a recorded arrival. Keep the feeder stopped and inspect where the tip is. If the path is later physically emptied, use Confirm empty path.'};
      case 'loaded': return receipt
        ? {action:'trace_loaded', note:'Trace the filament back to the recorded ACE slot and verify the feeder is stopped. The printer checks live presence before restoring the load. After it is restored, use the normal Unload control if the unload recipe and session are authorized.'}
        : {note:'No matching arrival record exists for the current ACE connection, spool and route. Keep recovery in place while you inspect the filament position.'};
      default: return {note:'Keep the feeder stopped. Trace the tube and locate the filament before choosing a recovery action. If you have confirmed the entire ACE-to-toolhead tube and every shared branch are empty, choose Entire path empty. Filament present in the ACE slot alone is not evidence that the toolhead is loaded.'};
    }
  }
  function recoveryContext(row, flexi={}, production={}) {
    const source = row ? row.ace+':'+row.slot : '';
    const journal = flexi.interlocks || {};
    const inventory = (production.inventory || []).find(item => item.key === source);
    const sensor = (flexi.diagnostics?.sensors || []).find(item => item.head === row?.head);
    return JSON.stringify({
      row, routes: flexi.routes, topology: flexi.topology, slots: flexi.slot_status,
      routeFingerprint: journal.routes_sha256,
      device: journal.devices?.[row?.ace]?.identity,
      nozzle: journal.nozzle_recovery?.[row?.head],
      spool: inventory?.identity, actions: inventory?.profile_complete_actions || inventory?.validated_actions,
      profiles: production.profiles,
      sensor: sensor && {fresh:sensor.poll_fresh, communication:sensor.communication, present:sensor.raw_present}
    });
  }
  const api={recoveryContext,pathMeasurements,pathWitnesses,pathStartBlock,measuredPathRecord,productionRecovery,routeReadiness,sections,topology,status,discoveryRows,observedTopology,lookupError,diagnostic,examples,chapters,flows,search,components:{'flexi-diagram':diagram,'path-info':pathInfo}};
  if(typeof module==='object' && module.exports)module.exports=api;else root.FlexiConfigUX=api;
})(typeof window==='undefined'?globalThis:window);

# tests/units - H0-Harness und Golden Traces (M0)

Schritt M0 aus `docs/UNIT_DRIVER_DESIGN.md` (§10 H0, §11). Das echte
`multiace/klipper/extras/ace.py` laeuft gegen gefaelschte Einheiten auf
einer virtuellen Uhr; jedes Szenario erzeugt einen Trace, der mit dem
eingefrorenen Golden verglichen wird. Kein Code in `multiace/` wird dafuer
angefasst.

Dateien:

- `harness.py` - Lader (synthetisches Paket), FakeConfig (protokolliert
  jeden Optionszugriff), FakeReactor mit virtueller Uhr, FakeSerial mit
  Fehlerinjektion, Thread-/Queue-Shims fuer deterministische V2-Threads,
  Trace und Golden-Vergleich.
- `fake_v1.py` / `fake_v2.py` - Geraetemodelle ACE Pro (JSON) und ACE 2
  (Protobuf), Bytes rein, Bytes raus, mit virtueller Latenz.
- `scenarios.py` - 33 Szenarien: Connect V1/V2/Mischflotte (v2_order
  first/usb/last), 60 s Heartbeat, USB-Zug mit und ohne Rueckkehr (V1 und
  V2), Lese-/Schreib-EIO, Stille-Watchdog, head mode, Startup-Wartezeit,
  Startup-Soft-Fail, Id-Wrap, Callback-Ausnahme, ACE_FEED/RETRACT, V1 stuck
  busy, Late-Join, Insert idle; Druck: FA-Arm V2/V1 (FORBIDDEN),
  verschluckter Arm, Komm-Verlust mit/ohne Pause, USB-Blip mit FA-Re-Arm.
  Druckszenarien laufen mit `fa_debug: true` (nur Log-Level). M1:
  Portausschluss per `[mcu]`-serial und per `exclude_ports`, Shim-
  (`V2.0.0-shim`) und OEM-IAP-Einheit (`1.0.2`) gehalten.
- `contract.py` - M2: der Vertrag ace.py <-> Codec (Design §5) als
  Docstring und als `ContractChecker` (Regeln R1-R4 Anfrage, D1-D5
  Zustellung, A1-A3 Antwort, S1-S9 Status-Dict). Der Harness legt ihn um
  `send_request_to`; eine Verletzung wird als Trace-Eintrag `contract`
  festgehalten, `test_goldens` bricht dann. `test_contract.py` prueft,
  dass der Pruefer anschlaegt (Einheitstests + ein absichtlich falsches
  FakeV1). Ein neuer Codec (M6a) laeuft gegen dieselben Regeln.
- `goldens/*.jsonl` - ein Trace-Eintrag pro Zeile, `[t, art, ...]`.
- `goldens/predicates.json` - das Praedikat-Inventar (§4), per AST aus den
  Klipper-Modulen.
- `tools/predicate_inventory.py` - erzeugt/prueft das Inventar.
- `tools/` R0-Werkzeuge (Design §10 "R0 Werkzeuge"), Tests in
  `test_r0_tools.py`:
  - `modules.py` - DIE Modulliste (aus `multiace/managed/manifest.json`),
    `core_files()`, `core_sha()` = der Repo-Wert fuer die Bannerzeile `core=`.
  - `module_lists.py` - prueft Installer, Uninstaller, post-commit,
    Bake-Globs, release.yml, Platte und ace.py `CORE_FILES` gegen die Liste.
  - `globals_check.py` - symtable: kein undefinierter Modulglobal in einer
    Funktion (die NameError einer verschobenen Methode).
  - `surface_check.py` - alle `ace.X` der Nachbarmodule existieren auf
    MultiAce; Golden `goldens/surface.json` (`--update`).
  - `move_check.py` - zwei Baeume per `ast.dump` pro Definition vergleichen:
    unchanged/moved/changed/added/removed; Mixin-Gruppen aus
    `modules.MIXIN_GROUPS` (+ `--group Mixin=MultiAce` ad hoc),
    `--allow-changed/-added/-removed`, `--ignore-docstrings`. Nachweis eines
    R1-Schritts: `move_check <alt> --allow-changed MultiAce:bases
    --allow-changed CORE_FILES` zeigt nur moved.

Laufen lassen:

    python3 -m pytest tests/units -q
    python3 -m tests.units.scenarios connect_mixed 80      # Trace ansehen
    UPDATE_GOLDENS=1 python3 -m pytest tests/units -q      # Goldens neu
    python3 -m tests.units.tools.predicate_inventory --update
    python3 -m tests.units.tools.modules                   # Repo core-SHA
    python3 -m tests.units.tools.module_lists
    python3 -m tests.units.tools.globals_check --all
    python3 -m tests.units.tools.surface_check --check
    python3 -m tests.units.tools.move_check HEAD           # HEAD vs Arbeitskopie

Ein Golden aendert sich NUR, wenn sich das Verhalten aendern soll; das
neue Golden gehoert in denselben Commit wie die Codeaenderung.

Trace-Arten: `tx`/`rx` (Draht, hex), `timer` (register/update/unregister
mit relativer Weckzeit), `fd`, `thread`, `serial`, `log` (erste Zeile,
Zeitstempel/Build/Pfade normalisiert), `gcode` (Skriptzeilen an den
Drucker), `gcode_error` (Kommandofehler mit action/id/code), `respond`,
`webhook`, `contract` (Vertragsverletzung, Regel + Text, nur im Fehlerfall),
`event` (Drucker-Ereignisse aus dem Szenario: print_stats,
Toolwechsel, Extrusion), `status` (get_status()-Schnappschuss, nur
str-Schluessel), `cfg_read` (alle gelesenen Optionen), `fault`, `poke`
(direkter Zustandseingriff, z. B. Sequenzzaehler vor dem Wrap).

Szenario-Hooks am Harness: `at(delay, fn, *args)` (virtuelle Zeit),
`unplug/replug/mute`, `inject_read_error/inject_write_error`,
`print_start/print_end/tool_change(head)/extruding(mm_s)/gcode_line`.
Geraete-Knoepfe: FakeV1 `busy_for(s)`, `stuck`; FakeV2 `drop_next(cmd)`,
`ignore_next_assist`, `stick_slot(slot)`; beide `insert/remove`.

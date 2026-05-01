# mUlt1ACE

# DIESE VERSION HAT EIN GRÖSSERES FEED ASSIST PROBLEM, LEIDER ZU SPÄT BEMERKT, ABSCHALTEN ODER ÄLTERE VERSION BENUTZEN, HOTFIX KOMMT!

## Was ist neu in multiACE 0.90b "Vibrant Fungi"

**Das ist KEINE AMS-Lösung mit tausenden zuverlässigen Swaps, und ich glaube auch nicht, dass sie das jemals sein wird — aber sie pausiert, wenn etwas schief geht, sodass du das Problem lösen und fortfahren kannst.**

**Farbwechsel im Druck bis zu 16 Farben** - Swaps an Layer-Grenzen und mitten im Layer sind jetzt stabil genug für echte Drucke statt nur für Tests. Der USB-Rewrite, der gehärtete Load/Unload-Pfad und die FA/Load-Toggles schließen zusammen die Fehlerbilder, die Mid-Print-Swaps bisher fragil gemacht haben.

**Swaptimizer** - `--optimize` im Post-Processing-Script weist die T-Indizes neu zu, um Mid-Print-Swaps zu reduzieren, und gibt eine nach ACE/Slot sortierte Loadout-Liste aus, nach der du die Kartuschen einlegst. Typische Ersparnis bei 5+ Farben: 20–30 %.

Der eigentliche Star ist **`--layer`**: erkennt, ob jedes einzelne Druck-Layer mit ≤4 Farben auskommt, und wenn ja, schreibt es den gcode so um, dass Swaps ausschließlich an Layer-Grenzen passieren (Belady-optimal). Das bedeutet typischerweise eine Größenordnung weniger Swaps als bei naiver Zuweisung - und gar keine Mid-Layer-Toolchanges mehr. Beispiel: Ein 7-Farben-Toad-Druck von MakerWorld fällt von 120 Mid-Print-Swaps auf 3 Layer-Boundary-Swaps - bei ~3:48 pro Swap sind das ~7,4 h Druckzeit zurück.

**Auto-Load Spools** - das Post-Processing-Script lädt die Spulen über alle ACEs hinweg und entlädt dort, wo nötig. Vollautomatisch.

**Neugeschriebene USB-Engine** - Cross-ACE-Toolchanges laufen jetzt in Stock-Geschwindigkeit *mit* feed_assist auf jedem Head. Die Start-ACE-only-Einschränkung aus 0.81b ist weg: alle angeschlossenen ACEs bleiben während des Drucks voll verfügbar, und die Reset-Zyklus-Edge-Cases, die den 0.81b-Workaround nötig gemacht haben, werden jetzt direkt in der Engine abgefangen.

**Gehärtetes Load/Unload** - mehrere Fehlerbilder, die Drucke bisher zum Stillstand gebracht haben, werden jetzt abgefangen statt nur gemeldet. Zusätzliche Extrude-Retries ziehen zwischendurch zurück, damit die Extruder-Zahnräder loslassen und neu greifen. Fehler snapshotten vor dem Raise den Pausen-Zustand (aktiver Extruder, Heiztemperaturen pro Head), und Resume wurde mit einer multiACE-Sicherheitsschicht überschrieben.

Viele Farbwechsel ohne U1-Load/Unload-Fehler - alle von der Sensor-/Retry-Logik abgefangen. Falls mal einer durchrutscht: beheben und resume.

**FA / Load Handling on / off** - feed_assist lässt sich jetzt pro ACE togglen, getrennt für Druckzeit und Ladezeit (`fa_print_disable` / `fa_load_disable`). Nützlich bei ACEs, auf denen FA mit der Lademechanik eines bestimmten Materials kollidiert.

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/K3K610R4F9)

## multiACE v0.90b "Vibrant Fungi"

**Multi-ACE-Pro-Support für Snapmaker U1 mit Klipper**

> ⚠️ **Beta-Software** - ein Community-Projekt, um mehrere Anycubic ACE Pro Filamentwechsler an Snapmaker-Drucker anzubinden. Ist sorgfältig getestet, reift aber über Community-Feedback und Testing weiter. Nutzung auf eigenes Risiko - bitte Issues melden und Erfahrungen teilen, damit multiACE für alle besser wird.

> **Wichtig:** Snapmaker U1 und Anycubic ACE Pro haben beide ihre Eigenheiten beim Filament-Load/Unload, bei der RFID-Erkennung (manchmal abhängig von der Position des Tags) und gelegentlich auch mechanisch. Nicht jedes Problem ist ein multiACE-Bug - vieles liegt einfach an der Hardware. Das hier ist ein Beta-Release, keine produktionsreife Lösung. Ob diese U1- und ACE-Pro-Grenzen irgendwann wegfallen, wird sich zeigen.

## Was ist multiACE?

multiACE erweitert [SnapACE](https://github.com/BlackFrogKok/SnapACE) um Support für **mehrere ACE-Pro-Einheiten** an einem einzelnen Snapmaker U1. Du schaltest einfach zwischen den ACEs um und nutzt unterschiedliche Filament-Sets - zum Beispiel PLA auf ACE 0 und PETG auf ACE 1 - ohne Spulen wechseln zu müssen.

## Typischer Workflow

### Farbwechsel im Druck (Layer / Mid-Layer)

Es gibt zwei Wege, Farbwechsel mitten im Druck auszulösen:

1. **`ACE_SWAP_HEAD` an einem Layer einfügen** über das "Change Filament" / "Insert Custom G-code at Layer"-Feature deines Slicers - gut für ein paar Akzent-Swaps, ohne neu zu slicen.
2. **Slicer-gcode durch das mitgelieferte Post-Processing-Script schicken** - das schreibt die vom Slicer ausgegebenen virtuellen Toolheads `T4..T15` automatisch in die passenden `ACE_SWAP_HEAD`-Commands um.

Beide Wege nutzen dieselbe gehärtete Load/Unload-Logik wie ein normaler Toolchange. Details zu Command-Format und Post-Processing stehen unter [Toolswaps einrichten](#toolswaps-einrichten).

### Einzelmaterial (z.B. PLA auf ACE 0)

1. Spulen in ACE 0 einlegen
2. **ACEB__Load_0** drücken → lädt alle belegten Slots
3. Normal drucken

### Mehrere Materialien (z.B. PLA auf ACE 0, PETG auf ACE 1)

1. PLA-Spulen in ACE 0, PETG-Spulen in ACE 1 einlegen
2. PLA-Toolheads (T0–T2) übers Display laden
3. **ACEA__Switch_1** drücken → auf ACE 1 umschalten
4. PETG in den gewünschten Toolhead (z.B. T3) laden - übers Display oder mit **ACEC__Load_T3**
5. Toolchanges im Druck schalten automatisch zwischen den ACEs um

### Komplettes Filament-Set wechseln

1. **ACEC__Unload_All** → entlädt alles
2. **ACEB__Load_1** → auf ACE 1 umschalten und alles laden

### ACE-Einheiten wechseln

Dafür gibt es die Fluidd-Makros **ACEA__Switch_0..3**.

> **Hinweis zu den Makronamen:** Die Buchstaben-Präfixe (ACEA, ACEB, ACEC…) sorgen dafür, dass die Makros in Fluidds alphabetischer Liste in sinnvoller Reihenfolge auftauchen. Wer andere Namen will, kann sie jederzeit in `config/extended/ace.cfg` ändern.

## Features

- **In-Print-Farbwechsel** - Layer- und Mid-Layer-Farbwechsel während eines laufenden Drucks, aus dem Slicer-gcode oder per Post-Processing-Script getriggert
- **Full Cross-ACE Feed_Assist** - Alle angeschlossenen ACEs bleiben während eines Drucks voll verfügbar, Feed_Assist auf jedem Head, mit Stock-Toolchange-Geschwindigkeit (neu geschriebene USB-Engine)
- **Gehärteter Load/Unload** - Retract-Recovery zwischen Extrude-Retries, Pause-State-Snapshot vor Fehler, sichererer Resume-Pfad (Pre-Heat vor Travel, Z-Hop vor XY)
- **FA-Toggle pro ACE** - Feed_Assist pro ACE deaktivierbar und separat für Print-Time / Load-Time (`fa_print_disable` / `fa_load_disable`)
- **Load Off für TPU** - No-Load-Modus für weiche Filamente; manuelles Laden, während multiACE Slot, RFID und Swap-State trackt
- **Multi-ACE-Support** - Bis zu 4 ACE-Pro-Einheiten gleichzeitig anschließbar
- **ACE-Switching** - Umschalten zwischen ACE-Einheiten über Fluidd-Makros oder Konsole
- **Auto-Load** - Alle belegten Slots der gewählten ACE mit einem Command laden
- **Unload All** - Alle Toolheads entladen, schaltet automatisch zur korrekten ACE für den Retract
- **RFID-Handling** - Automatische RFID-Erkennung und Anzeige über ACE-Wechsel hinweg
- **Manuelle Filament-Unterstützung** - Funktioniert mit RFID- und Non-RFID-Spulen
- **Per-ACE-Trockner-Einstellungen** - Konfigurierbare Temperatur und Dauer pro ACE
- **Normal Mode** - Jederzeit zurück auf Stock-Snapmaker-Betrieb (nur Originaldateien aktiv, kein ACE-Code läuft). Nützlich für Filamente, die der ACE Pro nicht verarbeiten kann, etwa TPU/TPE
- **Auto-Feed-Control** - Automatisch während des Drucks, außerhalb deaktiviert, um ungewollte Preloads zu verhindern
- **Print-Start-Safety-Check** - Warnt, wenn eine benötigte ACE offline ist
- **PAXX-Firmware-kompatibel** - Funktioniert mit PAXX-Firmware, die Display-Mirroring bietet und volle Load/Unload-Kontrolle vom Rechner aus erlaubt
- **Sauberer Install/Uninstall** - One-Command-Scripts mit automatischem Backup und Restore

## Voraussetzungen

- Snapmaker U1
- Snapmaker- oder PAXX-Firmware (getestet mit Snapmaker 1.2 und PAXX 12-14)
- 1-4 Anycubic ACE Pro über USB (mit 3 getestet)
- SSH-Zugang zum Drucker
- Fluidd-Weboberfläche
- PTFE-Splitter (1-zu-N pro Toolhead) - ermöglicht auch das Umschalten in Normal Mode ohne Umkabeln

## Q & A

**Warum zurück zum "Poop"-Drucken?**
Muss man nicht. Der Hauptzweck dieser Software bleibt sauberes Filament-Handling, nicht Farbwechsel-Druck. Keiner muss 1000-Farben-Drucke fahren, bei denen mehr Filament im Abfall landet als im Teil (und multiACE ist dafür nicht gemacht) - aber 1-12 zusätzliche Farben oben auf den normalen Multi-Material-Workflow sind nett, ohne großen Mehrabfall. Oder einfach Gold und Silber für einen CMYKW-Full-Spectrum-Druck dazupacken.

**Du nutzt immer noch Tip-Forming - warum keinen Cutter?**
Tip-Forming ist nur eine von mehreren ACE/U1-Load-Unload-Eigenheiten. Der erste Ansatz ist hier, sie per Software zu lösen: Sensor-Read-Retries, gehärtete Recovery-Pfade etc. Wer einen physischen Cutter bauen will - ich schreibe gerne die Routinen dafür, einfach melden. Und falls doch mal was schiefgeht, kann man immer noch zum Drucker gehen, von Hand sortieren und Resume drücken.
Tip-Forming produziert weniger Poop als ein Cutter - das ist ein Vorteil.

**Ist es schnell?**
Nein. Durch Tip-Forming statt Cutter und die Bowden-Länge dauert ein Colorswap bis zu 4 Minuten. Da nicht in der Parkposition gewechselt wird, addiert sich jeder Wechsel direkt auf die Druckzeit. Snapmaker-typische Per-Layer-Farbwechsel sind im Stock allerdings auch nicht schneller und brauchen jedes Mal manuelles Eingreifen - hier läuft es immerhin automatisiert. Park-Position-Swaps sind eine Option für die Zukunft.

**Funktioniert es mit ACE 2, AnkerMake Vivid oder anderen Changern?**
Nein. Aber die ACE 2 ist mittlerweile reverse-engineered, ich schaue es mir an. Falls jemand einen anderen Changer kennt, der *nachweislich* zuverlässiger als der ACE Pro und dazu Klipper-kompatibel ist - bitte melden. Zur Vivid habe ich keine belastbaren Tests gesehen, und Eigenbauten sind deutlich teurer. multiACE soll etwas sein, das jeder aufsetzen kann.

**Kann ich multiACE mit nur einer ACE Pro nutzen?**
Ja. Mit einer einzigen ACE managt multiACE trotzdem Loads, Unloads und Auto-Feed sauber und fügt den gehärteten Retry/Resume-Pfad hinzu. `ace_device_count` defaultet auf `1`, keine Extra-Config nötig.

**Funktioniert es mit/ohne RFID-Tags?**
Ja. Anycubic-RFID- (oder selbstgeschriebene) Spulen funktionieren problemlos - oder Filamenttyp und Farbe manuell über das Snapmaker-Display setzen. RFID- und Non-RFID-Spulen können über Slots und ACEs hinweg gemischt werden.

**Kann ich trotzdem TPU/TPE nutzen?**
Ja, auf zwei Wegen: **Normal Mode** (Stock-Feeder, keine ACE) für eine komplette TPU-Session, oder **Load Off** pro Toolhead, während die anderen Heads weiter die ACE nutzen. Beide halten Swap/Unload-State konsistent.

**Brauche ich PAXX-Firmware?**
Nein. Stock-Snapmaker-Firmware 1.2+ reicht. PAXX ergänzt Display-Mirroring, sodass Load/Unload komplett vom Rechner aus steuerbar ist - bequem, aber nicht erforderlich.

**Was passiert, wenn eine ACE beim Start aus/getrennt ist?**
multiACE wartet bis zu 20s auf alle erwarteten Geräte (entsprechend `ace_device_count`), bevor das Path-to-Index-Mapping gelockt wird. Ein zu diesem Zeitpunkt fehlendes Gerät wird markiert, und ein Pre-Print-Safety-Check warnt, wenn eine benötigte ACE offline ist.

**Ruiniert ein fehlgeschlagener Load während des Drucks den ganzen Druck?**
Nicht automatisch. Load-Failures triggern eine Pause (kein Full-Abort), snapshotten den Pause-State (aktiver Extruder, Target-Temps) und laufen über einen gehärteten Resume-Pfad. In vielen Fällen - loses Filament, Gear-Slip - behebt die Retract-between-Retries-Recovery das Problem, bevor die Pause überhaupt ausgelöst wird.

**Kann ich zurück zum Stock-Betrieb?**
Ja. `ACEF__Mode_Normal` schaltet auf Stock-Snapmaker-Betrieb um (kein ACE-Code läuft). Das Uninstall-Script setzt alles mit einem Command zurück.

**Läuft das zuverlässig?**
Nein. Vielleicht doch. Das ist Beta-Software, Fehler werden auftauchen. Ich hab's ausgiebig getestet - aber jetzt bist du dran! Testen und Bugs melden, du bist Teil des Teams.

## Hardware-Setup

### Kabel bauen (lötfrei)

Der ACE Pro verbindet sich per USB über einen Molex-Micro-Fit-3.0-Stecker mit dem Snapmaker U1. Kein Löten nötig.

**Was du brauchst:**
- 1x Molex Micro-Fit 3.0 Male 2x3 Stecker mit vorgecrimpten Kabeln - [AliExpress](https://de.aliexpress.com/item/1005010370245711.html)
- 1x USB-Typ-A-Schraubklemmen-Adapter - [Amazon](https://www.amazon.com/dp/B0825TWRW7)

**Pinout:**

```
ACE Pro Molex (2x3) - Frontansicht        Verbindung
         ||  <- Clip
   ┌────────────┐
   │ [1] [2] [3] │                        Pin 2 (D-)  -> USB D-
   │ [4] [5] [6] │                        Pin 3 (D+)  -> USB D+
   └────────────┘                         Pin 5 (GND) -> USB GND
                                          Pin 6 (VCC) -> NICHT VERBINDEN
```

Das [SnapAce-Pinout-Diagramm](https://github.com/BlackFrogKok/SnapAce/blob/main/.github/img/pinout.png) zeigt die genauen Molex-Pin-Positionen.

> **Wichtig:** Pin 6 (VCC) **nicht** anschließen - der ACE Pro hat sein eigenes Netzteil, und VCC anzuschließen kann den Drucker beschädigen. Molex-Kabel haben keine standardisierte Farbcodierung - vor dem Anschließen immer Durchgang messen.

**Zusammenbau:**
1. D-, D+ und GND vom Molex-Stecker mit D-, D+ und GND am USB-Stecker verbinden
2. D+ und D- miteinander verdrillen (2-3 Verdrehungen pro cm), um EMV-Einstreuungen zu reduzieren
3. Bei aufgeschnittenem USB-Kabel: freiliegenden Abschnitt mit Alufolie umwickeln, überlappend zur Kabelschirmung
4. Zusätzliche ACE-Einheiten werden über das Daisy-Chain-Kabel (dem ACE Pro beiliegend) angeschlossen - keine zusätzlichen USB-Kabel ab Einheit 2 nötig

### ACE-Verbindungsübersicht

Jeder ACE Pro verbindet sich über **zwei Schnittstellen** mit dem Drucker:
- **USB** - Serielle Kommunikation (Commands, Status, RFID)
- **PTFE-Schläuche** - Filamentweg von den ACE-Slots zu den Toolheads

Alle ACE-Einheiten sind **parallel** verkabelt - jeder ACE-Slot speist denselben Toolhead wie der entsprechende Slot jeder anderen ACE. Damit lässt sich ein komplettes Filament-Set durch Umschalten der aktiven ACE wechseln.

```
                    Splitter
ACE 0  Slot 0 ──────┐
ACE 1  Slot 0 ──────┤──── Head 0 (T0)
ACE 2  Slot 0 ──────┘

ACE 0  Slot 1 ──────┐
ACE 1  Slot 1 ──────┤──── Head 1 (T1)
ACE 2  Slot 1 ──────┘

ACE 0  Slot 2 ──────┐
ACE 1  Slot 2 ──────┤──── Head 2 (T2)
ACE 2  Slot 2 ──────┘

ACE 0  Slot 3 ──────┐
ACE 1  Slot 3 ──────┤──── Head 3 (T3)
ACE 2  Slot 3 ──────┘
```

### USB-Verbindung

Jeder ACE Pro verbindet sich per USB mit dem Snapmaker U1 (nur Daten - jeder ACE hat sein eigenes Netzteil). Die ACE-Einheiten werden über die USB-Ports auf der Rückseite jeder ACE im Daisy-Chain verkettet:

```
Snapmaker U1 USB Port
        │
      ACE 0 ─── ACE 1 ─── ACE 2 ─── ACE 3
       (USB out → USB in, Daisy Chain)
```

> **Hinweis:** VCC (5V) ist im USB-Kabel nicht verbunden - nur die Datenleitungen. Jeder ACE Pro wird über sein eigenes externes Netzteil versorgt.

multiACE erkennt ACE-Einheiten automatisch per USB-Vendor/Product-ID (28e9:018a). Die Reihenfolge der Daisy Chain bestimmt den ACE-Index (0, 1, 2, 3).

### PTFE-Splitter

Jeder Toolhead braucht einen **Splitter**, der PTFE-Schläuche von mehreren ACE-Einheiten zu einem einzelnen Pfad zum Extruder zusammenführt. Mit Splittern lässt sich zwischen ACE-Einheiten **und** zurück zu Normal Mode (Stock-Feeder) ohne Umkabeln umschalten.

- **3D-Druck** eines Y-Splitters oder Multi-Way-Splitters
- **Kommerzielle** PTFE-Verbinder mit mehreren Eingängen

> **Tipp:** PTFE-Schlauchlängen zwischen ACE-Einheiten möglichst gleich halten. Bei Bedarf `load_length` pro Toolhead in `ace.cfg` anpassen.

### RFID-Spulen-Tags

Der ACE Pro liest RFID-Tags von Anycubic-Spulen, um Filamenttyp, Farbe und Marke automatisch zu erkennen. Für Drittanbieter-Spulen ohne RFID lassen sich kompatible Tags selbst schreiben:

- **Tags** - NFC NTAG 213 oder 215 Sticker
- **iPhone** - TagMySpool App
- **Android** - RFID ACE App

Spulen ohne RFID-Tags funktionieren problemlos - Filamenttyp und Farbe lassen sich manuell über das Snapmaker-Display setzen.

### Empfohlenes Setup

| ACE-Einheiten | Anwendungsfall | Setup |
|---------------|----------------|-------|
| 2 ACEs | Materialwechsel (z.B. PLA + PETG) | 2-Wege-Splitter, direktes USB |
| 2 ACEs | Erweiterte Farbpalette (8 Farben) | 2-Wege-Splitter, direktes USB |
| 3-4 ACEs | Multi-Material + Farben | N-Wege-Splitter, USB-Hub |

## Installation

> **Vor Snapmaker-Firmware-Updates:** zuerst `bash uninstall_multiace.sh` ausführen, dann das Firmware-Update einspielen, dann multiACE neu installieren. Snapmaker-Firmware-Updates überschreiben die multiACE-Klipper-Dateien (`filament_feed.py`, `extruder.py`, `filament_switch_sensor.py`); ohne vorheriges Uninstall bleibt ein halb-Stock-halb-multiACE-Mischzustand, in dem keiner von beiden Pfaden zuverlässig funktioniert.

### Vorbereitungen

Vor der Installation von multiACE Folgendes sicherstellen:

1. **Firmware** - Snapmaker-Firmware 1.2+ oder PAXX-Firmware 12-14 auf dem Snapmaker U1 installieren
2. **Root-Zugang aktivieren** - Auf dem Snapmaker-Display: Settings > About > 10x auf die Firmware-Version tippen, um den Advanced Mode freizuschalten, dann Root Access aktivieren
3. **SSH aktivieren** - Per SSH oder seriellem Konsolen-Zugang verbinden und ausführen:
   ```
   touch /oem/.debug
   ```
   Nach dem Reboot muss das WLAN-Passwort am Display neu eingegeben werden. SSH ist dann unter `root@<printer-ip>` erreichbar
4. **SSH prüfen** - Vom Rechner aus verbinden:
   ```
   ssh root@<printer-ip>
   ```

### Schnellinstallation (empfohlen)

1. Dieses Repository herunterladen oder klonen
2. Den `multiace/`-Ordner per SCP/SFTP auf den Drucker kopieren (z.B. WinSCP unter Windows, oder Kommandozeile):
   ```
   scp -r multiace/ root@<printer-ip>:/tmp/multiace/
   ```
3. Per SSH auf den Drucker verbinden und ausführen:
   ```
   bash /tmp/multiace/install_multiace.sh
   ```
4. Drucker rebooten
5. multiACE startet im **Multi mode** - alle angeschlossenen ACE-Einheiten werden automatisch erkannt

### Manuelle Installation

Für die manuelle Installation:

1. Klipper-Extras auf den Drucker kopieren:
   ```
   cp klipper/extras/ace.py /home/lava/klipper/klippy/extras/
   cp klipper/extras/filament_feed_ace.py /home/lava/klipper/klippy/extras/
   cp klipper/extras/filament_switch_sensor_ace.py /home/lava/klipper/klippy/extras/
   cp klipper/kinematics/extruder_ace.py /home/lava/klipper/klippy/kinematics/
   ```

2. Config-Dateien kopieren:
   ```
   cp config/extended/ace.cfg /home/lava/printer_data/config/extended/
   mkdir -p /home/lava/printer_data/config/extended/multiace
   cp config/extended/multiace/ace_vars.cfg /home/lava/printer_data/config/extended/multiace/
   cp config/extended/multiace/ace_mode_switch.sh /home/lava/printer_data/config/extended/multiace/
   chmod +x /home/lava/printer_data/config/extended/multiace/ace_mode_switch.sh
   ```

3. ACE-File-Swap aktivieren:
   ```
   bash /home/lava/printer_data/config/extended/multiace/ace_mode_switch.sh ace
   ```

4. Python-Cache löschen:
   ```
   rm -rf /home/lava/klipper/klippy/extras/__pycache__/
   rm -rf /home/lava/klipper/klippy/kinematics/__pycache__/
   ```

5. Drucker rebooten

### Deinstallation

Das Uninstall-Script ausführen (wird automatisch auf den Drucker installiert):
```
bash /home/lava/printer_data/config/extended/multiace/uninstall_multiace.sh
```

Oder aus dem Installations-Ordner:
```
bash /tmp/multiace/uninstall_multiace.sh
```

Danach rebooten. Der Drucker läuft wieder im Stock-Betrieb.

## Fluidd-Makros

Alle Operationen sind als Makro-Buttons in Fluidd verfügbar, alphabetisch sortiert:

| Makro | Beschreibung |
|-------|--------------|
| **ACEA__Switch_0..3** | Auf ACE 0-3 umschalten (kein Auto-Load) |
| **ACEB__Load_0..3** | Auf ACE umschalten und alle belegten Slots laden |
| **ACEC__Unload_All** | Alle Toolheads entladen |
| **ACEC__Unload_T0..T3** | Einzelnen Toolhead entladen |
| **ACEC__Load_T0..T3** | Einzelnen Toolhead aus aktiver ACE laden |
| **ACED__Dry_Start_0..3** | Trocknen auf ACE starten (nutzt Config-Settings) |
| **ACED__Dry_Stop** | Trocknen auf aktueller ACE stoppen |
| **ACEF__Mode_Normal** | Auf Stock-Modus umschalten (keine ACE) |
| **ACEF__Mode_Multi** | Auf Multi-ACE-Modus umschalten |
| **ACEG__Status** | Aktive ACE, erkannte Geräte, Head-Mapping, Build-Tag anzeigen |
| **ACEG__List** | Alle erkannten ACE-Geräte auflisten |

## Toolswaps einrichten

multiACE unterstützt bis zu **16 logische Filamente** (4 Toolheads × bis zu 4 ACE-Einheiten). Das zentrale In-Print-Swap-Command ist:

```
ACE_SWAP_HEAD HEAD=<0..3> ACE=<0..3>
```

Dieser Befehl wechselt das Filament am `HEAD` zum passenden Slot der angegebenen `ACE` und nutzt dabei den gehärteten Load/Unload-Pfad. Es gibt zwei Wege, diese Commands in den Print-gcode zu bekommen.

### Option 1 - Manuelle G-code-Einfügung (per Layer)

Für einen schnellen 1-2-Swap-Druck ohne komplettes Neuslicen das **"Insert Custom G-code at Layer"**-Feature des Slicers nutzen (Orca / Prusa: "Change Filament At Layer"; Bambu: "Pause / Custom G-code at layer").

Beispiel: auf Layer 42 das Filament an Head 0 zur Spule in ACE 1, Slot 0 wechseln:

```
; layer 42
ACE_SWAP_HEAD HEAD=0 ACE=1
```

Wenn volle manuelle Kontrolle über Ort und Häufigkeit der Swaps nötig ist. Gut für Farbakzente, Signaturen oder Single-Layer-Labels.

### Option 2 - Automatisches Post-Processing (`post_process_virtual_toolheads.py`)

Für einen echten Multi-Color-Druck, bei dem der Slicer bereits in Toolchanges denkt, übernimmt das mitgelieferte Post-Processing-Script die Umwandlung. Das Script mappt die vom Slicer ausgegebenen **virtuellen Toolheads T4..T15** auf die richtigen `ACE_SWAP_HEAD`-Commands und räumt die Heater-/Pre-Extrude-Commands auf, damit nichts mit multiACEs eigenem Swap-Flow kollidiert.

**Filament-Reihenfolge im Slicer** - Projekt mit bis zu 16 Filamenten aufsetzen: **T0..T3** sind die vier "primären" Filamente, die physisch auf der aktiven ACE geladen sind (Head 0..3), und **T4..T15** sind die Swap-in-Filamente auf den anderen ACE-Slots. Das Mapping ist positionsbasiert: `T4` = ACE 1 / Head 0, `T5` = ACE 1 / Head 1, … `T7` = ACE 1 / Head 3, `T8..T11` = ACE 2 Heads 0..3, `T12..T15` = ACE 3 Heads 0..3. Slicer-Farben/-Materialien in dieser Reihenfolge zuweisen, damit das Post-Processing-Script jeden Toolchange in das richtige `ACE_SWAP_HEAD HEAD=X ACE=Y` übersetzen kann.

**Setup** - im Post-Processing-Feld des Slicers darauf verweisen:

```
python3 /path/to/multiace/tools/post_process_virtual_toolheads.py --aces 3 --layer
```

**Flags:**

- `--aces N` - Anzahl der physisch angeschlossenen ACE-Pro-Einheiten (1–8, Default 4). Bestimmt, wie viele ACE-Zeilen die Loadout-Analyse einplant und ob `--layer` für dein Setup überhaupt machbar ist.
- `--optimize` - Swaptimizer: weist T-Indizes neu zu, um Mid-Print-Swaps zu reduzieren, und gibt eine nach ACE/Slot sortierte Ladereihenfolge aus.
- `--layer` - Erweiterung von `--optimize`: wenn jedes Layer ≤4 Farben nutzt, wird der gcode so umgeschrieben, dass Swaps ausschließlich an Layer-Grenzen passieren. Fällt still auf `--optimize` zurück, wenn nicht machbar.

**Was es tut:**

- Schreibt bare `T4..T15` im Print-Body in `T<head%4>` + `ACE_SWAP_HEAD HEAD=X ACE=Y` um
- Fixt `M104 / M109 T4..T15` Heater-Commands auf den physischen Extruder
- Entfernt Stock-`SM_PRINT_PREEXTRUDE_FILAMENT INDEX=4..15`, sodass Pre-Extrude nur für physische Extruder läuft
- Überspringt redundante Swaps, wenn ein Head bereits die angeforderte Farbe hält
- Läuft einen **Optimizer**: gibt eine empfohlene ACE-Beladung aus (welche 4 Farben auf den "primären" Slots liegen sollten, damit weniger Swaps nötig sind) an den Post-Process-Dialog des Slicers und `multiace_postprocess.log`

**Verarbeiteten gcode über Fluidd hochladen** - nach dem Slicer-Export den resultierenden `.gcode` über Fluidd hochladen (Jobs → Upload) und den Druck von dort starten. Fluidd sendet die umgeschriebenen Commands direkt an Klipper, sodass der ACE-aware gcode exakt so bei multiACE ankommt, wie das Script ihn produziert hat.

Das ist der Pfad für vollständige Multi-Material-Drucke. Der Optimizer-Output ist auch dann nützlich, wenn die Beladung danach von Hand geändert wird - er zeigt, welche Farbwechsel die meisten Swaps kosten.

## Konfiguration

Alle Einstellungen liegen in `config/extended/ace.cfg` unter dem `[ace]`-Abschnitt (die Fluidd-Makros liegen in derselben Datei darunter). Für eine frische Multi-ACE-Installation muss nur `ace_device_count` angepasst werden - alles andere hat sinnvolle Defaults.

### Pflichtfeld

```ini
[ace]
ace_device_count: 1          # Anzahl physischer ACE-Pro-Geräte (1..8)
```

Beim Start wartet multiACE bis zu 20s auf alle erwarteten Geräte, bevor das Path-to-Index-Mapping gelockt wird, damit eine Einheit mitten im USB-Reset-Cycle beim Boot nie zu Index-Drift führt. **Pflicht für Multi-ACE** - ohne expliziten Count kann eine einzelne fehlende Einheit das falsche Mapping locken.

### Logging / Debug

```ini
state_debug: true            # Audit-Log pro Toolchange / pro Load
usb_debug: true              # Log der Serial-Layer pro Scan / pro Connect
fa_debug: true               # Feed_Assist-Trace (nützlich beim 0.90b-Bring-up)
# log_dir: /home/lava/printer_data/logs   # Default ist meist ausreichend
```

Separate Dateien unter `printer_data/logs/multiace_*.log`. Während der Beta-Phase anlassen - sie sind essentiell für Post-mortem-Analyse und kosten zur Laufzeit nichts.

### Serial / Feed / Retract

```ini
baud: 115200
feed_speed: 80               # mm/s
retract_speed: 80            # mm/s
load_length: 2100            # ACE-Feed-Distance in den Bowden (mm)
retract_length: 1950         # Sensor-zu-Splitter-Distance (mm)
```

`load_length` auf ca. **110 % der PTFE-Länge** setzen - die Phase ist sensor-gestoppt, Überschuss ist also sicher. `retract_length` = gemessene Extruder-Sensor-zu-Splitter-Distance minus ~100 mm; der Retract muss nur an der Splitter-Junction vorbei, nicht durch den gesamten Schlauch. Niedriger `retract_speed` hilft der ACE, die Spule straffer aufzuwickeln; ein Spulen-Guide-Upgrade wie [diese Roller-Führung](https://www.printables.com/model/1237589-20-anycubic-ace-pro-upgrade-kit-to-new-s1-version) verbessert die Aufwickelqualität weiter.

### Load / Unload Retry (multiACE-Hardening)

```ini
load_retry: 3                # FEED_AUTO LOAD Retries, wenn Sensor nicht erreicht

extrusion_retry: 7           # Outer-Retries, nachdem Wheel-Check fehlschlägt (0 = deaktiviert)

unload_retry: 3              # Unload Re-Heat / Re-Run-Versuche
```

### Trockner

```ini
dryer_temp: 55               # °C
dryer_duration: 240          # Minuten
max_dryer_temperature: 70    # Safety-Cap

# Per-ACE Overrides (optional):
# dryer_temp_0: 55
# dryer_temp_1: 45
# dryer_duration_0: 240
```

### Feed-Assist (FA) Gate

```ini
# Per-ACE FA-Ausschluss (kommaseparierte 0-basierte ACE-Indizes).
# fa_print_disable: kein FA während Druck - Extruder zieht Filament alleine
# fa_load_disable:  kein FA während Load - manuelles Einfädeln (z.B. TPU)
# fa_print_disable: 0,2
# fa_load_disable: 1
```

### Toolchange / Swap

```ini
extra_purge_length: 50       # zusätzliche mm nach Flush beim Toolchange
swap_default_temp: 250       # Fallback-Swap-Temp ohne Heater/RFID-Target

# swap_retract_length: 900   # Mid-Print-Swap-Retract (default = retract_length)
```

### Per-ACE-Längen-Overrides (optional)

Wenn die Bowden-Längen pro ACE unterschiedlich sind, in einem eigenen `[ace N]`-Abschnitt überschreiben:

```ini
[ace 0]
load_length: 2100
retract_length: 1950
load_length_2: 2200          # Slot-spezifischer Override (ACE 0, Slot 2)

[ace 1]
load_length: 2050
```

Lookup-Priorität: `[ace N] load_length_Y` → `[ace N] load_length` → `[ace] load_length`. Gleiches für `retract_length`. Speeds bleiben global.

## Bekannte Einschränkungen

- **Air Print Detection muss ausgeschaltet sein** - Wird im nächsten Hotfix behandelt.
- **Auto-Loading im Display nicht deaktivieren** - Wirft Fehler.
- **Purge-Finetuning** - Muss ich mir nochmal anschauen.
- **Unload vor Erstnutzung** - Nach einer frischen Installation oder beim Upgrade von einer vorherigen Version alle Toolheads entladen, bevor multiACE gestartet wird. Filament, das aus einer vorherigen Installation geladen ist, kann zu unerwartetem Verhalten führen, da multiACE keine Kenntnis vom vorherigen State hat. Vorher **ACEC__Unload_All** nutzen oder über das Display entladen.
- **Unload All löscht Display-Info** - Nach **ACEC__Unload_All** werden manuell gesetzte Filamenttypen und Farben gelöscht. So gewollt - nach dem Entladen Filament-Info neu setzen und neu laden.

## Tipps

Kleine Dinge, die in der Praxis viel ausmachen - meistens mechanisch, einiges Config:

- **Spulen nehmen, die der ACE mag.** Pappspulen sind der Klassiker: ziehen Feuchtigkeit, quellen auf, klemmen im ACE-Schacht, und das Feed-Wheel sieht trotzdem Bewegung, weil sich nur die Spule selbst dreht. Auf Plastikspulen umspulen oder einen Spulenadapter drucken. Zu große oder zu kleine Spulen verklemmen genauso - nah an Standard-1-kg-Plastikkernen bleiben.
- **Führungssystem-Upgrade (Kobra 3 / S1).** Beim neueren Anycubic-Rollenführungssystem springen die Originalteile oft - die aktualisierten Führungsteile drucken, dann läuft die Spule deutlich ruhiger. <!-- TODO: Link ergänzen --> *(Link folgt)*
- **Retry-Parameter fürs eigene Setup austesten.** `load_retry`, `extrusion_retry` und `unload_retry` sind zum Tunen da. Die Defaults fangen die meisten weichen Fehlerbilder ohne Eingriff ab; bei einer problematischen Spule oder einem schwierigen Setup lohnt es sich, sie weiter hochzudrehen.
- **Größeren Poop-Behälter nachrüsten.** Multi-Color-Drucke produzieren mehr Purge als Single-Material-Läufe. Ein größerer gekaufter oder gedruckter Poop-Container spart den Gang zum Drucker mitten im Druck.
- **Stabile, gut laufende Splitter-/PTFE-Verbindungen.** Ein Splitter, der sich unter Feed-Druck verschiebt, oder ein PTFE-Anschluss, der 1–2 mm zu kurz sitzt, kostet dich einen Load ohne erkennbaren Grund. Jeden Übergang vollständig einrasten lassen, Collets fest sichern, und den Splitter auf etwas Festes montieren.

## Troubleshooting

### Auf sauberen State zurücksetzen

Wenn Dinge aus dem Tritt kommen (falsches Filament angezeigt, unerwartetes Verhalten), alles zurücksetzen:

1. Alle Toolheads über Display entladen (sicherstellen, dass kein Filament in irgendeinem Head feststeckt)
2. In Fluidd-Konsole: `ACE_CLEAR_HEADS`
3. Drucker power-cyclen (komplett aus/an, nicht nur Klipper-Restart)
4. Nach dem Reboot frisch mit Laden aus ACE 0 starten

### Klipper startet nach Installation nicht
- Prüfen, ob `ace.cfg` inkludiert ist: `grep ace.cfg /home/lava/printer_data/config/printer.cfg`
- Prüfen, ob `multiace/ace_vars.cfg` existiert
- Uninstall ausführen und neu installieren

### ACE wird nicht erkannt
- USB-Verbindung prüfen: `ls /dev/serial/by-path/`
- ACE Pro sollte als Vendor `28e9`, Product `018a` erscheinen
- ACE power-cyclen

### Alter Code läuft trotz Update
- Python-Cache löschen: `rm -rf /home/lava/klipper/klippy/extras/__pycache__/`
- Datei-Timestamp in Konsole prüfen: `multiACE v0.90b (file: ...)`

### Serial-Fehler in der Konsole
- Serial-Fehler während ACE-Switch werden still geloggt. Bei persistenten Fehlern USB-Kabel prüfen.

### Issues melden

Beim Melden eines Problems bitte folgende Logs vom Drucker mitschicken. Sie sind essentiell für die Diagnose:

1. **multiACE-State-Log** - Audit-Trail pro Aktion (Toolchanges, Loads, Unloads, FA-Events):
   ```
   cat /home/lava/printer_data/logs/multiace_state.log
   ```
2. **multiACE-USB-Log** - Serial-Connect/Disconnect- und Scan-Events:
   ```
   cat /home/lava/printer_data/logs/multiace_usb.log
   ```
3. **Klipper-Log** - die letzten ~200 Zeilen rund um den Zeitpunkt des Issues:
   ```
   tail -200 /home/lava/printer_data/logs/klippy.log
   ```

Außerdem angeben:
- **Zeitpunkt des Fehlers** - exakter Timestamp, damit wir ihn in den Logs finden
- **Was du getan hast** - welcher Button / welches Makro / welcher gcode getriggert wurde
- **Was davor passiert ist** - war das mid-print, während eines Loads, nach einem Restart, etc.
- **Erwartetes Verhalten** - was stattdessen hätte passieren sollen
- Wie viele ACE-Einheiten angeschlossen sind
- Ob die Spulen RFID-Tags haben oder nicht
- Ob Developer Mode aktiviert ist (`ls /oem/.debug`)

## Lizenz

Dieses Projekt basiert auf [SnapACE](https://github.com/BlackFrogKok/SnapACE) und [Klipper](https://github.com/Klipper3d/klipper), beide unter GPL-3.0 lizenziert. multiACE steht daher ebenfalls unter GPL-3.0.

## Hinweis zur KI-unterstützten Entwicklung

Dieses Projekt enthält KI-unterstützte Content-Research-Dokumentation und Code-Teile.
Alle Inhalte werden vor der Aufnahme von Menschen geprüft.

## Credits

- **[SnapACE](https://github.com/BlackFrogKok/SnapACE)** von BlackFrogKok - Grundlage für die ACE-Pro-Klipper-Integration
- **[DuckACE](https://github.com/utkabobr/DuckACE)** - ACE-Pro-Reverse-Engineering und Protokoll-Dokumentation
- **[ACE Research](https://github.com/printers-for-people/ACEResearch)** von Printers for People - ACE-Pro-Protokollforschung
- **[3D Print Forum](https://forum.drucktipps3d.de/)** - Tipps, Tricks und Community-Wissen
- **Snapmaker** - Drucker-Hardware und -Firmware
- **Anycubic** - ACE-Pro-Filamentwechsler
- **Community** - Testing, Feedback und Bug-Reports (hoffentlich!)

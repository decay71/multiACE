# mUlt1ACE

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/K3K610R4F9)

## multiACE v0.80b "First Light"

**Multi-ACE Pro Unterstützung für Snapmaker U1 mit Klipper**

> ⚠️ **Beta-Software** - Dies ist ein Community-getriebenes Entwicklungsprojekt zur Unterstützung mehrerer Anycubic ACE Pro Filament-Wechsler an Snapmaker-Druckern. Obwohl sorgfältig getestet, lebt es vom Feedback und Testen der Community. Nutzung auf eigene Gefahr. Bitte meldet Probleme und teilt eure Erfahrungen, um multiACE für alle zu verbessern.

> **Wichtiger Hinweis:** Sowohl der Snapmaker U1 als auch das Anycubic ACE Pro haben eigene Eigenheiten beim Filament-Laden/Entladen, bei der RFID-Erkennung (möglicherweise abhängig von der Position der Tag-Aufkleber) und gelegentliche mechanische Probleme. Nicht jedes auftretende Problem ist ein multiACE-Problem - viele sind der zugrundeliegenden Hardware inhärent. Dies ist eine Beta-Version, keine produktionsreife Lösung. Ob sich die U1- und ACE Pro-Limitierungen in Zukunft lösen lassen, bleibt abzuwarten.

## Was ist multiACE?

multiACE erweitert die [SnapACE](https://github.com/BlackFrogKok/SnapACE) Software um die Unterstützung **mehrerer ACE Pro Einheiten** an einem einzelnen Snapmaker U1 Drucker. Wechsle zwischen ACE-Einheiten um verschiedene Filament-Sets zu verwenden - z.B. PLA auf ACE 0 und PETG auf ACE 1 - ohne Spulen physisch tauschen zu müssen.

## Typischer Arbeitsablauf

### Einzelnes Material (z.B. PLA auf ACE 0)

1. Spulen in ACE 0 einsetzen
2. **ACEB__Load_0** drücken → lädt alle bestückten Slots
3. Normal drucken

### Mehrere Materialien (z.B. PLA auf ACE 0, PETG auf ACE 1)

1. PLA-Spulen in ACE 0 einsetzen, PETG-Spulen in ACE 1
2. PLA-Toolheads (T0-T2) über Display laden
3. **ACEA__Switch_1** drücken → auf ACE 1 umschalten
4. PETG in gewünschten Toolhead (z.B. T3) laden über Display oder **ACEC__Load_T3**
5. Toolchanges im Druck schalten automatisch zwischen ACEs um

### Komplettes Filament-Set wechseln

1. **ACEC__Unload_All** drücken → entlädt alles
2. **ACEB__Load_1** drücken → auf ACE 1 umschalten und alles laden

### ACE-Einheiten umschalten

Die Fluidd-Makros **ACEA__Switch_0..3** verwenden um zwischen ACE-Einheiten umzuschalten.

> **Hinweis zu den Makronamen:** Die Makros verwenden Buchstaben-Präfixe (ACEA, ACEB, ACEC...) damit sie in Fluidds alphabetischer Makroliste in logischer Reihenfolge erscheinen. Wem die Namen nicht gefallen, kann sie jederzeit in `config/extended/ace.cfg` umbenennen.

## Funktionen

- **Multi-ACE Unterstützung** - Bis zu 4 ACE Pro Einheiten gleichzeitig anschließen
- **ACE Umschaltung** - Wechsel zwischen ACE-Einheiten über Fluidd-Makros oder Konsole
- **Auto-Load** - Alle bestückten Slots einer ausgewählten ACE mit einem Befehl laden
- **Alles Entladen** - Alle Toolheads entladen, automatisches Umschalten zur richtigen ACE für den Retract
- **RFID Handling** - Automatische RFID-Erkennung und Display-Aktualisierung beim ACE-Wechsel
- **Manuelle Filament-Unterstützung** - Funktioniert mit RFID- und nicht-RFID-Spulen
- **ACE-spezifische Trocknungseinstellungen** - Konfigurierbare Temperatur und Dauer pro ACE
- **Normal-Modus** - Jederzeit zurück zum Stock-Snapmaker-Betrieb (nur Originaldateien aktiv, kein ACE-Code). Nützlich für Filamente die das ACE Pro nicht verarbeiten kann, wie TPU/TPE
- **Auto-Feed Steuerung** - Automatisch während des Drucks, deaktiviert außerhalb um ungewolltes Vorladen zu verhindern
- **Druckstart-Sicherheitscheck** - Warnung wenn eine benötigte ACE offline ist
- **PAXX Firmware kompatibel** - Funktioniert mit PAXX-Firmware die Display-Spiegelung bietet, für vollständige Lade-/Entladesteuerung vom Computer
- **Saubere Installation/Deinstallation** - Ein-Befehl-Skripte mit automatischer Sicherung und Wiederherstellung

## Voraussetzungen

- Snapmaker U1 Drucker
- Snapmaker-Firmware oder PAXX-Firmware (getestet mit Snapmaker 1.2 und PAXX 12-14)
- 1-4 Anycubic ACE Pro Einheiten per USB angeschlossen (getestet mit 3)
- SSH-Zugang zum Drucker
- Fluidd Weboberfläche
- PTFE-Schlauch-Splitter (1-zu-N pro Toolhead) - ermöglichen auch den Wechsel zum Normal-Modus ohne Umverkabelung

## Hardware-Aufbau

### Kabelbau-Anleitung (Lötfrei)

Das ACE Pro wird über einen Molex Micro-Fit 3.0 Stecker per USB mit dem Snapmaker U1 verbunden. Kein Löten erforderlich.

**Was wird benötigt:**
- 1x Molex Micro-Fit 3.0 Male 2x3 Stecker mit vorgequetschten Kabeln - [AliExpress](https://de.aliexpress.com/item/1005010370245711.html)
- 1x USB Typ-A Schraubklemmen-Adapter - [Amazon](https://www.amazon.de/JZK-USB-Schraubklemmenblock-Adapter-Schraub-Abschirmungsklemmen-Daten%C3%BCbertragung-USB-Schnittstelle/dp/B0CQYSC4CN)

**Pinbelegung:**

```
ACE Pro Molex (2x3) - Ansicht von vorne    Verbindung
         ||  <- Clip
   ┌────────────┐
   │ [1] [2] [3] │                         Pin 2 (D-)  -> USB D-
   │ [4] [5] [6] │                         Pin 3 (D+)  -> USB D+
   └────────────┘                          Pin 5 (GND) -> USB GND
                                           Pin 6 (VCC) -> NICHT VERBUNDEN
```

Siehe [SnapAce Pinbelegungsdiagramm](https://github.com/BlackFrogKok/SnapAce/blob/main/.github/img/pinout.png) für die genauen Molex-Pin-Positionen.

> **Wichtig:** Pin 6 (VCC) wird nicht verbunden - das ACE Pro hat ein eigenes Netzteil.

**Zusammenbau:**
1. D-, D+ und GND vom Molex-Stecker mit D-, D+ und GND am USB-Stecker verbinden
2. D+ und D- Kabel miteinander verdrillen (2-3 Drehungen pro cm) um elektromagnetische Störungen zu reduzieren
3. Bei Verwendung eines aufgeschnittenen USB-Kabels: den freiliegenden Abschnitt mit Alufolie umwickeln, die die Kabelabschirmung überlappt
4. Weitere ACE-Einheiten werden über das Daisy-Chain-Kabel (im Lieferumfang des ACE Pro) angeschlossen - keine zusätzlichen USB-Kabel für Einheit 2+ nötig

### ACE Anschlussübersicht

Jedes ACE Pro ist über **zwei Schnittstellen** mit dem Drucker verbunden:
- **USB** - Serielle Kommunikation (Befehle, Status, RFID)
- **PTFE-Schläuche** - Filamentweg von ACE-Slots zu den Toolheads

Alle ACE-Einheiten sind **parallel** verdrahtet - jeder ACE-Slot führt zum **gleichen Toolhead** wie der entsprechende Slot an jeder anderen ACE. So kann man komplette Filament-Sets durch Umschalten der aktiven ACE wechseln.

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

Jedes ACE Pro wird per USB (nur Daten - jedes ACE hat ein eigenes Netzteil) mit dem Snapmaker U1 verbunden. Die ACE-Einheiten werden über die USB-Anschlüsse auf der Rückseite jedes ACE in Reihe geschaltet (Daisy-Chain):

```
Snapmaker U1 USB-Anschluss
        │
      ACE 0 ─── ACE 1 ─── ACE 2 ─── ACE 3
       (USB out → USB in, Daisy-Chain)
```

> **Hinweis:** VCC (5V) ist im USB-Kabel nicht verbunden - nur Datenleitungen. Jedes ACE Pro wird über ein eigenes externes Netzteil versorgt.

multiACE erkennt ACE-Einheiten automatisch anhand der USB Vendor/Product ID (28e9:018a). Die Reihenfolge der Daisy-Chain bestimmt den ACE-Index (0, 1, 2, 3).

### PTFE-Schlauch-Splitter

Jeder Toolhead benötigt einen **Splitter** der PTFE-Schläuche von mehreren ACE-Einheiten zu einem einzigen Weg zum Extruder zusammenführt. Mit eingebauten Splittern kann man zwischen ACE-Einheiten **und** zurück zum Normal-Modus (Stock-Feeder) wechseln, ohne umzuverkabeln.

- **3D-gedruckte** Y-Splitter oder Mehrweg-Splitter
- **Kommerzielle** PTFE-Schlauchverbinder mit mehreren Eingängen

> **Tipp:** Alle PTFE-Schlauchlängen zwischen den ACE-Einheiten möglichst gleich halten. Bei Bedarf `load_length` pro Toolhead in `ace.cfg` anpassen.

### RFID Spulen-Tags

Das ACE Pro liest RFID-Tags von Anycubic-Spulen um automatisch Filament-Typ, Farbe und Marke zu erkennen. Für Fremdspulen ohne RFID können kompatible Tags selbst beschrieben werden:

- **Tags** - NFC NTAG 213 oder 215 Aufkleber verwenden
- **iPhone** - TagMySpool App
- **Android** - RFID ACE App

Spulen ohne RFID-Tags funktionieren problemlos - Filament-Typ und Farbe können manuell über das Snapmaker-Display eingestellt werden.

### Empfohlenes Setup

| ACE-Einheiten | Anwendungsfall | Setup |
|---------------|----------------|-------|
| 2 ACEs | Materialwechsel (z.B. PLA + PETG) | 2-Wege-Splitter, direktes USB |
| 2 ACEs | Erweiterter Farbbereich (8 Farben) | 2-Wege-Splitter, direktes USB |
| 3-4 ACEs | Multi-Material + Farben | N-Wege-Splitter, USB-Hub |

## Installation

### Voraussetzungen

Vor der Installation von multiACE sicherstellen:

1. **Firmware** - Snapmaker Firmware 1.2+ oder PAXX Firmware 12-14 auf dem Snapmaker U1 installieren
2. **Root-Zugang aktivieren** - Am Snapmaker-Display unter Einstellungen > Über > Firmware-Version 10x antippen um den erweiterten Modus freizuschalten, dann Root-Zugang aktivieren
3. **SSH aktivieren** - Per SSH oder serieller Konsole verbinden und ausführen:
   ```
   touch /home/lava/.oem_debug
   ```
   Nach dem Neustart muss das WLAN-Passwort am Display neu eingegeben werden. SSH ist dann unter `root@<drucker-ip>` erreichbar
4. **SSH prüfen** - Vom Computer verbinden:
   ```
   ssh root@<drucker-ip>
   ```

### Schnellinstallation (Empfohlen)

1. Dieses Repository herunterladen oder klonen
2. Den `multiace/` Ordner per SCP/SFTP auf den Drucker kopieren (z.B. WinSCP unter Windows, oder Kommandozeile):
   ```
   scp -r multiace/ root@<drucker-ip>:/tmp/multiace/
   ```
3. Per SSH auf den Drucker verbinden und ausführen:
   ```
   bash /tmp/multiace/install_multiace.sh
   ```
4. Drucker neustarten
5. multiACE startet im **Multi-Modus** - alle angeschlossenen ACE-Einheiten werden automatisch erkannt

### Manuelle Installation

Für manuelle Installation:

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

3. ACE-Dateien aktivieren:
   ```
   bash /home/lava/printer_data/config/extended/multiace/ace_mode_switch.sh ace
   ```

4. Python-Cache löschen:
   ```
   rm -rf /home/lava/klipper/klippy/extras/__pycache__/
   rm -rf /home/lava/klipper/klippy/kinematics/__pycache__/
   ```

5. Drucker neustarten

### Deinstallation

Das Deinstallationsskript ausführen (wird automatisch auf den Drucker installiert):
```
bash /home/lava/printer_data/config/extended/multiace/uninstall_multiace.sh
```

Oder aus dem Installationsordner:
```
bash /tmp/multiace/uninstall_multiace.sh
```

Dann neustarten. Der Drucker kehrt zum Stock-Betrieb zurück.

## Fluidd-Makros

Alle Operationen sind als Makro-Buttons in Fluidd verfügbar, alphabetisch sortiert:

| Makro | Beschreibung |
|-------|--------------|
| **ACEA__Switch_0..3** | Auf ACE 0-3 umschalten (ohne Autoload) |
| **ACEB__Load_0..3** | Auf ACE umschalten und alle bestückten Slots laden |
| **ACEC__Unload_All** | Alle Toolheads entladen |
| **ACEC__Unload_T0..T3** | Einzelnen Toolhead entladen |
| **ACEC__Load_T0..T3** | Einzelnen Toolhead von aktiver ACE laden |
| **ACED__Dry_Start_0..3** | Trocknung auf ACE starten (nutzt Config-Einstellungen) |
| **ACED__Dry_Stop** | Trocknung auf aktueller ACE stoppen |
| **ACEE__Autofeed_Off/On** | Auto-Feed deaktivieren/aktivieren |
| **ACEF__Mode_Normal** | In Stock-Modus wechseln (kein ACE) |
| **ACEF__Mode_Multi** | In Multi-ACE-Modus wechseln |

## Konfiguration

Alle Einstellungen sind in `config/extended/ace.cfg` unter dem `[ace]` Abschnitt:

```ini
[ace]
# ACE Feed-/Retract-Einstellungen
feed_speed: 80          # Vorschubgeschwindigkeit (mm/s)
retract_speed: 30       # Rückzugsgeschwindigkeit (mm/s, niedriger = sauberere Wicklung)
retract_length: 1950    # Abstand vom Extruder-Sensor zum Splitter (mm)
load_length: 2100       # ACE-Vorschublänge beim Laden (mm)

# Wiederholungseinstellungen
load_retry: 1           # Anzahl der Ladeversuche
load_retry_retract: 50  # Mini-Rückzug vor Wiederholung (mm)

# Temperatur
swap_default_temp: 250  # Fallback-Temperatur wenn keine Config verfügbar
max_dryer_temperature: 70

# Purge (für Farbwechsel im Druck, zukünftiges Feature)
extra_purge_length: 50  # Extra-Extrusion nach Flush (mm), 0 = deaktiviert

# Trocknungs-Standardwerte (ACE-spezifische Überschreibungen möglich)
dryer_temp: 55          # Standard-Trocknungstemperatur
dryer_duration: 240     # Standard-Trocknungsdauer (Minuten)

# Optional: ACE-spezifische Trocknungs-Überschreibungen
# dryer_temp_0: 55
# dryer_temp_1: 45
# dryer_duration_0: 240
# dryer_duration_1: 180

# Optional: Toolhead-spezifische Überschreibungen
# load_length_0: 2100
# load_length_1: 2050
# retract_length_0: 1950
# retract_length_1: 1900
```

### Konfigurationsempfehlungen

**feed_length** - Auf `0` setzen (deaktiviert). Die Vorladephase kostet Zeit beim Bestücken der ACEs und führt zu inkonsistenten Filamentpositionen im PTFE-Schlauch.

**load_length** - Auf ca. **110% der tatsächlichen PTFE-Schlauchlänge** setzen (von ACE zum Splitter). Die Ladephase ist sensorgesteuert und stoppt wenn Filament erkannt wird, ein größerer Wert ist also sicher und gewährleistet zuverlässiges Laden.

**retract_speed** - Niedrig halten (Standard `30`). Das ACE Pro wickelt bei höheren Geschwindigkeiten manchmal lose, was zu Verhedderungen auf der Spule führt. Zusätzlich empfiehlt sich ein gedrucktes Spulenführungs-Upgrade wie z.B. [diese ACE Pro Rollenführung](https://www.printables.com/model/1237589-20-anycubic-ace-pro-upgrade-kit-to-new-s1-version) um die Wickelqualität zu verbessern.

**retract_length** - Den tatsächlichen Abstand vom Extruder-Sensor zum PTFE-Splitter messen und ~100mm abziehen. Der Retract muss das Filament nur bis hinter die Splitter-Kreuzung zurückziehen, nicht die volle Schlauchlänge.

## Bekannte Einschränkungen

- **Vor der ersten Nutzung entladen** - Nach einer Neuinstallation oder einem Upgrade von einer vorherigen Version alle Toolheads entladen, bevor multiACE verwendet wird. Filament aus einer vorherigen Installation kann unerwartetes Verhalten verursachen, da multiACE den vorherigen Zustand nicht kennt. **ACEC__Unload_All** oder Entladen über Display vor dem Start verwenden.
- **Toolchange-Dauer** - Wenn ein Toolchange im Druck einen Wechsel zu einer anderen ACE erfordert, verlängert sich der Toolchange um bis zu ~5 Sekunden (serielle Neuverbindung). Toolchanges innerhalb derselben ACE sind nicht betroffen.
- **ACE USB Reset** - Inaktive ACE-Einheiten setzen regelmäßig ihre USB-Verbindung zurück (~3s Zyklus). Dies ist normales ACE Pro Firmware-Verhalten und beeinträchtigt den Betrieb nicht. Sichtbar in `dmesg`, aber harmlos.
- **Display Attach Toolhead** - Das Anbringen eines Toolheads über das Snapmaker-Display löst Auto-Feed aus. Dies ist Standard-Snapmaker-Verhalten und kann nicht unterdrückt werden.
- **Unload All löscht Display-Info** - Nach **ACEC__Unload_All** werden manuell gesetzte Filament-Typen und Farben gelöscht. Das ist gewollt - nach dem Entladen neu laden und Filament-Info erneut setzen.

## Fehlerbehebung

### Auf Ausgangszustand zurücksetzen

Wenn etwas durcheinander geraten ist (falsches Filament angezeigt, unerwartetes Verhalten), alles zurücksetzen:

1. Alle Toolheads über Display entladen (sicherstellen dass kein Filament in einem Kopf steckt)
2. In der Fluidd-Konsole: `ACE_CLEAR_HEADS`
3. Drucker komplett aus- und einschalten (nicht nur Klipper-Neustart)
4. Nach dem Neustart frisch mit Laden von ACE 0 beginnen

### Klipper startet nicht nach der Installation
- Prüfen ob `ace.cfg` eingebunden ist: `grep ace.cfg /home/lava/printer_data/config/printer.cfg`
- Prüfen ob `multiace/ace_vars.cfg` existiert
- Deinstallation und Neuinstallation durchführen

### ACE wird nicht erkannt
- USB-Verbindung prüfen: `ls /dev/serial/by-path/`
- ACE Pro sollte als Vendor `28e9`, Product `018a` erscheinen
- ACE aus- und wieder einschalten

### Alter Code läuft trotz Update
- Python-Cache löschen: `rm -rf /home/lava/klipper/klippy/extras/__pycache__/`
- Datei-Zeitstempel in der Konsole prüfen: `multiACE v0.80b (file: ...)`

### Serielle Fehler auf der Konsole
- Serielle Fehler beim ACE-Wechsel werden nur ins Log geschrieben. Bei anhaltenden Fehlern USB-Kabel prüfen.

## Roadmap

### Nächste Version
- Fehlerbehebungen basierend auf Community-Feedback
- Eigenes Fluidd UI-Panel für ACE-Verwaltung
- Vielleicht eines Tages: [Die volle Vision](https://youtube.com/video/gJVQikjtDNs)

## Lizenz

Dieses Projekt basiert auf [SnapACE](https://github.com/BlackFrogKok/SnapACE) und [Klipper](https://github.com/Klipper3d/klipper), beide lizenziert unter GPL-3.0. multiACE ist daher ebenfalls GPL-3.0.

## Hinweis zur KI-gestützten Entwicklung

Dieses Projekt enthält KI-unterstützte Inhalte (Recherche, Dokumentation, Teile des Codes).
Alle Inhalte wurden vor der Aufnahme von Menschen überprüft.

## Credits

- **[SnapACE](https://github.com/BlackFrogKok/SnapACE)** von BlackFrogKok - Grundlage für die ACE Pro Klipper-Integration
- **[DuckACE](https://github.com/utkabobr/DuckACE)** - ACE Pro Reverse Engineering und Protokoll-Dokumentation
- **[ACE Research](https://github.com/printers-for-people/ACEResearch)** von Printers for People - ACE Pro Protokoll-Forschung
- **[3D Druck Forum](https://forum.drucktipps3d.de/)** - Tipps, Tricks und Community-Wissen
- **Snapmaker** - Drucker-Hardware und Firmware
- **Anycubic** - ACE Pro Filament-Wechsler
- **Community** - Testen, Feedback und Fehlermeldungen (hoffentlich!)

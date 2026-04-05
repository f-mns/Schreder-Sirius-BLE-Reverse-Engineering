#!/usr/bin/env python3
"""
Schréder/LGIT BLE Deep-Test v1
===============================
Basierend auf CSR uEnergy SDK 2.6.2 Quellcode-Analyse:

NEUE ERKENNTNISSE:
  Service 0x1016 = CSR OTA Update Service (BESTÄTIGT aus csr_ota_uuids.h)
    0x1011 = OTA Version       → liest 0x06 = OTA Protocol v6
    0x1013 = Current App       → liest 0x01 = normale App (WRITE = OTA MODE!)
    0x1014 = Data Transfer     → empfängt CS Block Daten per Notify
    0x1018 = Read CS Block     → NICHT Auth! Schreibt offset(2B)+length(2B)

  Service 0x5403 = Serial-over-GATT (UART Bridge zu Lampen-MCU)
    0x5404 = Serieller Datenkanal

TESTS:
  csdump    Configuration Store der CSR101x Firmware auslesen
  dali      DALI-Befehle über 0x5404 (Set DTR + Store Power On Level)
  auth2     Neue Auth-Versuche: GAIA-Format, erweiterte Sequenzen
  full      Alle Tests nacheinander

Verwendung:
  python3 schreder_deep_test.py csdump
  python3 schreder_deep_test.py dali
  python3 schreder_deep_test.py auth2
  python3 schreder_deep_test.py full
"""

import asyncio
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    print("FEHLER: pip install bleak")
    sys.exit(1)

MAC = "XX:XX:XX:XX:XX:XX"

BASE = "d102-11e1-9b23-00025b00a5a5"
# OTA Service (0x1016) Characteristics — aus csr_ota_uuids.h
CHAR_OTA_VERSION    = f"00001011-{BASE}"   # read: OTA protocol version
CHAR_OTA_CURRENT    = f"00001013-{BASE}"   # read/write: current app (WRITE=OTA!)
CHAR_OTA_DATA       = f"00001014-{BASE}"   # read/notify: CS block data transfer
CHAR_OTA_CS_BLOCK   = f"00001018-{BASE}"   # write: offset(2B) + length(2B)

# Serial Service (0x5403) Characteristics
CHAR_SERIAL         = f"00005404-{BASE}"   # write-no-resp/notify: UART bridge


def hex_str(data: bytes) -> str:
    if not data:
        return "(leer)"
    return " ".join(f"{b:02X}" for b in data)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def btctl(commands: list[str], timeout: float = 15.0) -> str:
    script = "\n".join(commands) + "\nexit\n"
    try:
        result = subprocess.run(
            ["bluetoothctl"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout + result.stderr
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        return "(timeout)"


def bt_remove_and_pair(mac: str, verbose: bool = True):
    if verbose:
        print(f"     🗑️  remove {mac}")
    btctl([f"remove {mac}"], timeout=8)
    time.sleep(1)
    if verbose:
        print(f"     🔍 scan + pair...")
    btctl(["agent NoInputNoOutput", "default-agent", "scan on"], timeout=5)
    time.sleep(3)
    btctl(["agent NoInputNoOutput", "default-agent", f"pair {mac}"], timeout=15)
    btctl([f"trust {mac}"], timeout=8)
    time.sleep(1)
    if verbose:
        print(f"     ✅ Pairing fertig")


async def fresh_connect(mac: str, verbose: bool = True) -> BleakClient:
    bt_remove_and_pair(mac, verbose=verbose)
    client = BleakClient(mac, timeout=10.0)
    for attempt in range(3):
        try:
            await client.connect()
            while not client.is_connected:
                await asyncio.sleep(0.1)
            if verbose:
                print(f"     🔌 Verbunden")
            await asyncio.sleep(0.3)
            return client
        except Exception as e:
            if attempt < 2:
                if verbose:
                    print(f"     ⚠️  Versuch {attempt+1}/3: {e}")
                await asyncio.sleep(2)
                bt_remove_and_pair(mac, verbose=False)
            else:
                raise BleakError(f"Verbindung zu {mac} fehlgeschlagen")
    raise BleakError(f"Verbindung zu {mac} fehlgeschlagen")


async def safe_disconnect(client: BleakClient):
    try:
        if client and client.is_connected:
            await client.disconnect()
    except Exception:
        pass


class NotifyCollector:
    """Sammelt Notify-Antworten von einem bestimmten Characteristic."""
    def __init__(self, label: str = ""):
        self.responses = []
        self.label = label

    def handler(self, sender, data):
        t = ts()
        raw = bytes(data)
        entry = {"time": t, "data": hex_str(raw), "raw": raw, "length": len(raw)}
        self.responses.append(entry)
        prefix = f"[{self.label}]" if self.label else ""
        print(f"     📡 {prefix}[{t}] {hex_str(raw)} ({len(raw)}B)")

    def clear(self):
        self.responses.clear()

    def get_last_raw(self) -> bytes:
        if self.responses:
            return self.responses[-1]["raw"]
        return b""


# =============================================================================
# TEST 1: CS BLOCK DUMP — Config Store der CSR101x auslesen
# =============================================================================

async def test_csdump(mac: str, log: list):
    """Liest den Configuration Store der CSR101x Firmware über BLE aus.

    Aus dem CSR OTA Quellcode (csr_ota_service.c):
      - Write auf 0x1018: offset (uint16, little-endian) + length (uint16, LE)
      - Subscribe auf 0x1014: CS Block Daten kommen als Notification
      - Maximale Leselänge: 20 Bytes pro Request (MAX_DATA_LENGTH)
    """
    print("\n" + "=" * 70)
    print("  📦 TEST: CS BLOCK DUMP — Configuration Store auslesen")
    print("=" * 70)
    print("  0x1018 = Read CS Block (offset + length)")
    print("  0x1014 = Data Transfer (empfängt CS-Daten per Notify)")
    print("  Lese die Firmware-Konfiguration in 20-Byte Blöcken\n")

    result = {"test": "csdump", "blocks": []}

    try:
        client = await fresh_connect(mac)
    except Exception as e:
        print(f"     ❌ Verbindung: {e}")
        result["error"] = "connect_failed"
        log.append(result)
        return

    try:
        # Collector für 0x1014 (Data Transfer)
        cs_collector = NotifyCollector("CS-DATA")

        # Auch 0x5404 überwachen falls es Seiteneffekte gibt
        serial_collector = NotifyCollector("SERIAL")

        # Notify auf 0x1014 starten
        await client.start_notify(CHAR_OTA_DATA, cs_collector.handler)
        await asyncio.sleep(0.3)
        print(f"     ✅ Notify auf 0x1014 (Data Transfer) aktiv")

        # Auch Notify auf 0x5404 starten
        try:
            await client.start_notify(CHAR_SERIAL, serial_collector.handler)
            print(f"     ✅ Notify auf 0x5404 (Serial) aktiv")
        except Exception:
            print(f"     ⚠️  Notify auf 0x5404 nicht möglich")

        await asyncio.sleep(0.3)

        # Zuerst OTA Version lesen (0x1011)
        try:
            ver = await client.read_gatt_char(CHAR_OTA_VERSION)
            print(f"     📋 OTA Version (0x1011): {hex_str(ver)}")
            result["ota_version"] = hex_str(ver)
        except Exception as e:
            print(f"     ⚠️  OTA Version lesen: {e}")

        # Current App lesen (0x1013) — NUR LESEN, NICHT SCHREIBEN!
        try:
            app = await client.read_gatt_char(CHAR_OTA_CURRENT)
            print(f"     📋 Current App (0x1013): {hex_str(app)}")
            result["current_app"] = hex_str(app)
        except Exception as e:
            print(f"     ⚠️  Current App lesen: {e}")

        # 0x1014 direkt lesen
        try:
            dt = await client.read_gatt_char(CHAR_OTA_DATA)
            print(f"     📋 Data Transfer (0x1014): {hex_str(dt)}")
            result["data_transfer_initial"] = hex_str(dt)
        except Exception as e:
            print(f"     ⚠️  Data Transfer lesen: {e}")

        print(f"\n     {'─' * 50}")
        print(f"     📦 CS Block Dump starten...")
        print(f"     {'─' * 50}")

        # CS Block in 20-Byte Schritten lesen (CSR101x CS ist typisch 512-1024 words)
        # Format: offset (uint16 LE) + length (uint16 LE)
        # Offset in WORDS (16-bit), Length in BYTES
        cs_data = {}
        max_offset = 64  # Start konservativ: 64 words = 128 bytes

        for offset in range(0, max_offset, 10):  # 10 words = 20 bytes pro Request
            length = 20  # Bytes

            # Pack as uint16 little-endian: offset + length
            payload = offset.to_bytes(2, 'little') + length.to_bytes(2, 'little')

            cs_collector.clear()
            print(f"\n     📤 CS Read: offset={offset} (0x{offset:04X}), length={length}B")
            print(f"        Write 0x1018: {hex_str(payload)}")

            try:
                await client.write_gatt_char(CHAR_OTA_CS_BLOCK, payload, response=True)
                print(f"        ✅ Write akzeptiert")
            except Exception as e:
                print(f"        ❌ Write abgelehnt: {e}")
                # Versuche ohne Response
                try:
                    await client.write_gatt_char(CHAR_OTA_CS_BLOCK, payload, response=False)
                    print(f"        ✅ Write (no-resp) akzeptiert")
                except Exception as e2:
                    print(f"        ❌ Auch no-resp: {e2}")
                    result["blocks"].append({
                        "offset": offset, "error": str(e2)
                    })
                    continue

            # Warte auf Notification
            await asyncio.sleep(1.0)

            if cs_collector.responses:
                raw = cs_collector.responses[-1]["raw"]
                print(f"        📡 CS Daten: {hex_str(raw)}")
                cs_data[offset] = raw
                result["blocks"].append({
                    "offset": offset,
                    "data": hex_str(raw),
                    "length": len(raw)
                })

                # Auch als Hex-Dump anzeigen
                for i in range(0, len(raw), 16):
                    chunk = raw[i:i+16]
                    hex_part = " ".join(f"{b:02X}" for b in chunk)
                    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                    print(f"        {offset*2+i:04X}: {hex_part:<48} |{ascii_part}|")
            else:
                print(f"        ⏳ Keine Notification erhalten")
                result["blocks"].append({
                    "offset": offset, "data": None, "no_response": True
                })
                # Wenn 3x keine Antwort → CS ist hier zu Ende
                recent_blocks = result["blocks"][-3:]
                if len(recent_blocks) >= 3 and all(b.get("no_response") for b in recent_blocks):
                    print(f"\n     ⚠️  3x keine Antwort → CS Block Ende bei offset {offset}")
                    break

            if not client.is_connected:
                print(f"     ⚡ Verbindung verloren!")
                break

        # Zusammenfassung
        total_bytes = sum(len(d) for d in cs_data.values())
        print(f"\n     {'═' * 50}")
        print(f"     📦 CS Dump Zusammenfassung:")
        print(f"        Blöcke gelesen: {len(cs_data)}")
        print(f"        Bytes gesamt:   {total_bytes}")
        print(f"     {'═' * 50}")

        # Kompletten Dump als Hex anzeigen
        if cs_data:
            print(f"\n     📋 Vollständiger CS Dump:")
            for offset in sorted(cs_data.keys()):
                raw = cs_data[offset]
                for i in range(0, len(raw), 16):
                    chunk = raw[i:i+16]
                    addr = offset * 2 + i
                    hex_part = " ".join(f"{b:02X}" for b in chunk)
                    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                    print(f"        {addr:04X}: {hex_part:<48} |{ascii_part}|")

    except Exception as e:
        print(f"     ❌ Fehler: {e}")
        result["error"] = str(e)

    finally:
        await safe_disconnect(client)

    log.append(result)
    print(f"\n     ✅ CS Dump Test abgeschlossen")


# =============================================================================
# TEST 2: DALI — DALI-Befehle über 0x5404
# =============================================================================

async def test_dali(mac: str, log: list):
    """Testet DALI-Befehle über den seriellen Kanal (0x5404).

    DALI Persistent Dimming:
      1. SET DTR (0xA3) + Wert → Data Transfer Register setzen
      2. STORE POWER ON LEVEL (0x2D) → DTR als Power-On Level speichern
      3. STORE SYSTEM FAILURE LEVEL (0x2C) → DTR als Ausfallpegel

    DALI Direct Arc Power Control (DAPC):
      Broadcast: 0xFE + Level (0-254)

    Die Befehle könnten direkt auf 0x5404 gehen, oder in das bestehende
    Protokollformat (01 XX) eingebettet sein.
    """
    print("\n" + "=" * 70)
    print("  🏭 TEST: DALI-Befehle über 0x5404")
    print("=" * 70)
    print("  Schréder nutzt DALI intern → Befehle könnten DALI-wrapped sein")
    print("  Teste DTR + STORE Sequenzen für persistentes Dimmen\n")

    result = {"test": "dali", "commands": []}

    DIM_LEVEL = 0x0A  # 10%
    DIM_LEVEL_DALI = 0x19  # ~10% in DALI logarithmic scale (25/254)

    # DALI-Befehle zum Testen
    dali_tests = [
        # --- Gruppe 1: Raw DALI Befehle ---
        {
            "name": "DALI DTR(10) + STORE POWER ON",
            "commands": [
                (bytes([0xA3, DIM_LEVEL]),       "SET DTR = 10%"),
                (bytes([0xA3, DIM_LEVEL]),       "SET DTR = 10% (repeat, DALI needs 2x)"),
                (bytes([0x2D]),                  "STORE DTR AS POWER ON LEVEL"),
                (bytes([0x2D]),                  "STORE DTR AS POWER ON LEVEL (2x)"),
            ]
        },
        {
            "name": "DALI DTR(10) + STORE SYSTEM FAILURE",
            "commands": [
                (bytes([0xA3, DIM_LEVEL]),       "SET DTR = 10%"),
                (bytes([0x2C]),                  "STORE DTR AS SYSTEM FAILURE LEVEL"),
                (bytes([0x2C]),                  "STORE DTR AS SYSTEM FAILURE LEVEL (2x)"),
            ]
        },
        {
            "name": "DALI Broadcast DAPC 10%",
            "commands": [
                (bytes([0xFE, DIM_LEVEL_DALI]),  "BROADCAST DAPC level 25"),
                (bytes([0xFF, DIM_LEVEL_DALI]),  "BROADCAST CMD level 25"),
            ]
        },
        # --- Gruppe 2: DALI commands mit unserem Protokoll-Format ---
        {
            "name": "Proto-Wrapped: DTR + STORE in 01-Format",
            "commands": [
                (bytes([0x01, DIM_LEVEL]),        "01 0A = Dim auf 10% (bekannt)"),
                (bytes([0xA3, DIM_LEVEL]),        "A3 0A = DALI SET DTR"),
                (bytes([0x2D]),                   "2D = DALI STORE POWER ON"),
            ]
        },
        # --- Gruppe 3: Erweiterte DALI Config-Befehle ---
        {
            "name": "DALI Store Max/Min Level",
            "commands": [
                (bytes([0xA3, DIM_LEVEL]),        "SET DTR = 10%"),
                (bytes([0x2A]),                   "STORE DTR AS MAX LEVEL"),
                (bytes([0x2B]),                   "STORE DTR AS MIN LEVEL"),
            ]
        },
        # --- Gruppe 4: DALI DTR als 3-Byte Paket ---
        {
            "name": "3-Byte DALI DTR+STORE combo",
            "commands": [
                (bytes([0xA3, DIM_LEVEL, 0x2D]),  "A3 0A 2D = DTR+STORE in einem Paket"),
                (bytes([0xA3, DIM_LEVEL, 0x2C]),  "A3 0A 2C = DTR+STORE-FAILURE"),
            ]
        },
        # --- Gruppe 5: Dim + DALI Store nacheinander ---
        {
            "name": "Dim(01) + DALI Store Sequence",
            "commands": [
                (bytes([0x01, DIM_LEVEL]),         "01 0A = Dim auf 10%"),
                (bytes([0x05]),                    "05 = Save/Apply?"),
                (bytes([0x06]),                    "06 = Save/Apply?"),
                (bytes([0x2D]),                    "2D = STORE POWER ON"),
                (bytes([0x2C]),                    "2C = STORE FAILURE LEVEL"),
                (bytes([0x2E]),                    "2E = STORE FADE TIME"),
            ]
        },
        # --- Gruppe 6: DALI-wrapped in Config-Format (03) ---
        {
            "name": "Config(03) mit DALI-Werten",
            "commands": [
                (bytes([0x03, 0xA3, DIM_LEVEL, 0x2D]),  "03 A3 0A 2D = Config+DTR+STORE"),
                (bytes([0x03, DIM_LEVEL, 0x2D, 0x00]),   "03 0A 2D 00 = Config+Level+Store"),
            ]
        },
        # --- Gruppe 7: RECALL und RESET Befehle ---
        {
            "name": "DALI RECALL + GOTO Befehle",
            "commands": [
                (bytes([0x10]),                    "10 = DALI RECALL MIN LEVEL (0x10=16)"),
                (bytes([0x11]),                    "11 = DALI RECALL MAX LEVEL"),
                (bytes([0x20]),                    "20 = DALI RESET (opcode 32)"),
                (bytes([0x05]),                    "05 = DALI CMD 'Up 200ms'"),
                (bytes([0x06]),                    "06 = DALI CMD 'Down 200ms'"),
                (bytes([0x08]),                    "08 = DALI CMD 'OFF'"),
            ]
        },
    ]

    for group_idx, group in enumerate(dali_tests):
        group_name = group["name"]
        commands = group["commands"]

        print(f"\n  {'━' * 60}")
        print(f"  🏭 Gruppe {group_idx+1}: {group_name}")
        print(f"  {'━' * 60}")

        group_result = {"group": group_name, "steps": []}
        collector = NotifyCollector("SERIAL")

        try:
            client = await fresh_connect(mac, verbose=True)
        except Exception as e:
            print(f"     ❌ Verbindung: {e}")
            group_result["error"] = "connect_failed"
            result["commands"].append(group_result)
            continue

        try:
            await client.start_notify(CHAR_SERIAL, collector.handler)
            await asyncio.sleep(0.3)

            for cmd_data, cmd_label in commands:
                collector.clear()
                print(f"\n     📤 {cmd_label}")
                print(f"        Write: {hex_str(cmd_data)}")

                try:
                    await client.write_gatt_char(CHAR_SERIAL, cmd_data, response=False)
                    print(f"        ✅ Gesendet")
                except Exception as e:
                    print(f"        ❌ Fehler: {e}")
                    group_result["steps"].append({
                        "cmd": hex_str(cmd_data), "label": cmd_label,
                        "error": str(e)
                    })
                    continue

                await asyncio.sleep(1.0)

                step = {
                    "cmd": hex_str(cmd_data),
                    "label": cmd_label,
                    "responses": []
                }

                if collector.responses:
                    for r in collector.responses:
                        step["responses"].append({
                            "time": r["time"], "data": r["data"]
                        })
                        print(f"        📡 Antwort: {r['data']}")
                else:
                    print(f"        ⏳ Keine Antwort")

                group_result["steps"].append(step)

                if not client.is_connected:
                    print(f"     ⚡ Verbindung verloren!")
                    break

            # Nach allen Befehlen: 5s warten
            if client.is_connected:
                print(f"\n     ⏱️  5s warten — LAMPE BEOBACHTEN!")
                await asyncio.sleep(5)

        except Exception as e:
            group_result["error"] = str(e)

        finally:
            await safe_disconnect(client)

        result["commands"].append(group_result)

        # Zwischen Gruppen: 10s Pause
        print(f"\n     ⏳ 10s Pause — Hat sich was an der Lampe geändert?")
        await asyncio.sleep(10)

    log.append(result)
    print(f"\n     ✅ DALI Test abgeschlossen")


# =============================================================================
# TEST 3: AUTH2 — Neue Auth-Versuche mit GAIA-Format und Sequenzen
# =============================================================================

async def test_auth2(mac: str, log: list):
    """Erweiterte Authentifizierungs-Versuche.

    Jetzt wo wir wissen, dass 0x1018 = CS Block Read ist (NICHT Auth),
    müssen wir Auth anders machen:

    1. Auth könnte IN dem seriellen Protokoll auf 0x5404 sein
    2. GAIA-Format: 00 0A 04 XX [payload] über 0x5404
    3. Spezielle Kommando-Sequenzen die "Commissioning Mode" aktivieren
    """
    print("\n" + "=" * 70)
    print("  🔐 TEST: AUTH2 — Erweiterte Authentifizierung")
    print("=" * 70)
    print("  0x1018 = CS Block Read (NICHT Auth!)")
    print("  Auth muss im seriellen Protokoll auf 0x5404 sein\n")

    result = {"test": "auth2", "attempts": []}

    DIM_LEVEL = 0x0A  # 10%

    # Verschiedene Auth-Sequenzen zum Testen
    auth_sequences = [
        # --- GAIA-Format Befehle ---
        {
            "name": "GAIA Login Command",
            "description": "CSR GAIA Vendor 0x000A format",
            "steps": [
                (bytes([0x00, 0x0A, 0x04, 0x00]),          "GAIA: Vendor 000A, CMD 0x400 (Set?)"),
                (bytes([0x01, DIM_LEVEL]),                   "Dim 10%"),
            ]
        },
        {
            "name": "GAIA Set Command",
            "description": "GAIA set with dim payload",
            "steps": [
                (bytes([0x00, 0x0A, 0x04, 0x02, DIM_LEVEL]), "GAIA CMD 0x402 + level"),
                (bytes([0x00, 0x0A, 0x04, 0x01, DIM_LEVEL]), "GAIA CMD 0x401 + level"),
            ]
        },
        # --- Protokoll-interne Auth ---
        {
            "name": "Config-Write als Auth",
            "description": "03-Befehl mit speziellem Payload als Auth",
            "steps": [
                (bytes([0x03, 0x00, 0x00, 0x00]),  "Config: 03 00 00 00 = Unlock?"),
                (bytes([0x01, DIM_LEVEL]),           "Dim 10%"),
                (bytes([0x03, DIM_LEVEL, 0x03, 0x00]),  "Config: 03 0A 03 00 = Set+Store?"),
            ]
        },
        {
            "name": "Config mit aktuellem Wert + Dim",
            "description": "Bekannte Config-Response war 0F 00 00 00 0F",
            "steps": [
                (bytes([0x03, 0x00, 0x00, 0x0F]),  "Config: letzter bekannter Wert"),
                (bytes([0x01, DIM_LEVEL]),           "Dim 10%"),
                (bytes([0x03, DIM_LEVEL, 0x00, 0x0F]),  "Config: Dim-Level eingebettet"),
            ]
        },
        # --- Erweiterte Befehle ---
        {
            "name": "Unbekannte Befehle als Save/Apply",
            "description": "Befehle die wir noch nicht mit Payload probiert haben",
            "steps": [
                (bytes([0x01, DIM_LEVEL]),           "Dim 10%"),
                (bytes([0x09, 0x01]),                "09 01 = Save mode 1?"),
                (bytes([0x0B, 0x01]),                "0B 01 = Apply mode 1?"),
                (bytes([0x0D, 0x01]),                "0D 01 = Commit mode 1?"),
                (bytes([0x11, 0x01]),                "11 01 = Store mode 1?"),
                (bytes([0x13, 0x01]),                "13 01 = Confirm mode 1?"),
                (bytes([0x17, 0x01]),                "17 01 = Lock mode 1?"),
                (bytes([0x19, 0x01]),                "19 01 = Persist mode 1?"),
                (bytes([0x1B, 0x01]),                "1B 01 = Finalize mode 1?"),
            ]
        },
        # --- Dim mit verschiedenen Payload-Längen ---
        {
            "name": "Erweiterte Dim-Payloads",
            "description": "Vielleicht braucht Persist-Dim mehr Bytes",
            "steps": [
                (bytes([0x01, DIM_LEVEL, 0x01]),            "01 0A 01 = Dim + persist flag?"),
                (bytes([0x01, DIM_LEVEL, 0xFF]),            "01 0A FF = Dim + store flag?"),
                (bytes([0x01, DIM_LEVEL, 0x00, 0x01]),      "01 0A 00 01 = Dim + persist mode?"),
                (bytes([0x01, DIM_LEVEL, 0x01, 0x00]),      "01 0A 01 00 = Dim + apply mode?"),
                (bytes([0x01, 0x00, DIM_LEVEL]),            "01 00 0A = Dim alt format?"),
                (bytes([0x01, DIM_LEVEL, 0x00, 0x00, 0x01]), "01 0A 00 00 01 = 5-byte dim?"),
            ]
        },
        # --- Session/Commissioning Mode ---
        {
            "name": "Commissioning Sequence",
            "description": "Kommissionierung: Handshake → Config → Dim → Store",
            "steps": [
                (bytes([0x07]),                       "Status query (07)"),
                (bytes([0x0E]),                       "Config read (0E)"),
                (bytes([0x15]),                       "Unknown read (15)"),
                (bytes([0x03, 0x00, 0x03, 0x00]),    "Config write: 03 00 03 00"),
                (bytes([0x01, DIM_LEVEL]),            "Dim 10%"),
                (bytes([0x03, DIM_LEVEL, 0x03, 0x00]), "Config write mit dim"),
                (bytes([0x07]),                       "Status query (confirm)"),
            ]
        },
    ]

    for seq_idx, seq in enumerate(auth_sequences):
        seq_name = seq["name"]
        seq_desc = seq.get("description", "")
        steps = seq["steps"]

        print(f"\n  {'━' * 60}")
        print(f"  🔐 Sequenz {seq_idx+1}: {seq_name}")
        if seq_desc:
            print(f"     {seq_desc}")
        print(f"  {'━' * 60}")

        seq_result = {"sequence": seq_name, "steps": []}
        collector = NotifyCollector("SERIAL")

        try:
            client = await fresh_connect(mac, verbose=True)
        except Exception as e:
            print(f"     ❌ Verbindung: {e}")
            seq_result["error"] = "connect_failed"
            result["attempts"].append(seq_result)
            continue

        try:
            await client.start_notify(CHAR_SERIAL, collector.handler)
            await asyncio.sleep(0.3)

            for cmd_data, cmd_label in steps:
                collector.clear()
                print(f"\n     📤 {cmd_label}")
                print(f"        Write: {hex_str(cmd_data)}")

                try:
                    await client.write_gatt_char(CHAR_SERIAL, cmd_data, response=False)
                    print(f"        ✅ Gesendet")
                except Exception as e:
                    print(f"        ❌ Fehler: {e}")
                    seq_result["steps"].append({
                        "cmd": hex_str(cmd_data), "error": str(e)
                    })
                    continue

                await asyncio.sleep(0.8)

                step = {"cmd": hex_str(cmd_data), "label": cmd_label, "responses": []}
                if collector.responses:
                    for r in collector.responses:
                        step["responses"].append(r["data"])
                        print(f"        📡 Antwort: {r['data']}")

                seq_result["steps"].append(step)

                if not client.is_connected:
                    print(f"     ⚡ Verbindung verloren!")
                    break

            # 5s beobachten
            if client.is_connected:
                print(f"\n     ⏱️  5s warten — LAMPE BEOBACHTEN!")
                await asyncio.sleep(5)

        except Exception as e:
            seq_result["error"] = str(e)

        finally:
            await safe_disconnect(client)

        result["attempts"].append(seq_result)

        # Pause zwischen Sequenzen
        print(f"\n     ⏳ 8s Pause...")
        await asyncio.sleep(8)

    log.append(result)
    print(f"\n     ✅ Auth2 Test abgeschlossen")


# =============================================================================
# Zusammenfassung
# =============================================================================

def print_summary(log: list):
    print("\n" + "=" * 70)
    print("  📊 ZUSAMMENFASSUNG")
    print("=" * 70)

    for entry in log:
        test = entry.get("test", "?")
        if test == "csdump":
            blocks = entry.get("blocks", [])
            ok = sum(1 for b in blocks if b.get("data"))
            fail = sum(1 for b in blocks if b.get("no_response") or b.get("error"))
            print(f"  📦 CS Dump: {ok} Blöcke gelesen, {fail} fehlgeschlagen")
            if entry.get("ota_version"):
                print(f"     OTA Version: {entry['ota_version']}")
        elif test == "dali":
            groups = entry.get("commands", [])
            for g in groups:
                responses = sum(
                    1 for s in g.get("steps", [])
                    if s.get("responses")
                )
                total = len(g.get("steps", []))
                print(f"  🏭 DALI {g['group']}: {responses}/{total} mit Antwort")
        elif test == "auth2":
            for a in entry.get("attempts", []):
                responses = sum(
                    1 for s in a.get("steps", [])
                    if s.get("responses")
                )
                total = len(a.get("steps", []))
                print(f"  🔐 {a['sequence']}: {responses}/{total} mit Antwort")


# =============================================================================
# Hauptprogramm
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(
        description="Schréder BLE Deep-Test v1 — CSR Firmware + DALI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tests:
  csdump    CS Block Dump — Config Store der CSR101x auslesen
  dali      DALI-Befehle über Serial (DTR + STORE)
  auth2     Erweiterte Auth (GAIA-Format, Sequenzen)
  full      Alle Tests nacheinander
        """,
    )
    parser.add_argument("test", choices=["csdump", "dali", "auth2", "full"],
                        help="Welcher Test soll laufen")
    parser.add_argument("--mac", default=MAC, help=f"BLE MAC (Standard: {MAC})")

    args = parser.parse_args()
    log = []

    print(f"\n  Schréder BLE Deep-Test v1")
    print(f"  MAC: {args.mac}")
    print(f"  Test: {args.test}")

    test_map = {
        "csdump": test_csdump,
        "dali": test_dali,
        "auth2": test_auth2,
    }

    if args.test == "full":
        for name in ["csdump", "dali", "auth2"]:
            await test_map[name](args.mac, log)
    else:
        await test_map[args.test](args.mac, log)

    print_summary(log)

    # Log speichern
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = f"schreder_deep_{args.test}_{timestamp}.json"

    # raw bytes entfernen für JSON
    for entry in log:
        for block in entry.get("blocks", []):
            block.pop("raw", None)
        for cmd in entry.get("commands", []):
            for step in cmd.get("steps", []):
                step.pop("raw", None)

    with open(logfile, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    print(f"\n  💾 Log: {logfile}")
    print(f"\n  ✅ Fertig!\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n  Abgebrochen (Strg+C).\n")
    except BleakError as e:
        print(f"\n  ❌ BLE-Fehler: {e}\n")

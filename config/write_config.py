#!/usr/bin/env python3
"""
Schréder CS-Config Writer
Sendet die komplette Config mit Fade-In = 8s (8000ms = 0x1F40)
Basierend auf CS-Dump vom 2026-04-05
"""

import asyncio
import subprocess
from bleak import BleakClient

ADDRESS = "76:8F:4D:38:36:28"
BASE    = "d102-11e1-9b23-00025b00a5a5"

CHAR_OTA_CURRENT  = f"00001013-{BASE}"  # write: aktuell app / OTA trigger
CHAR_OTA_DATA     = f"00001014-{BASE}"  # notify: CS block data transfer
CHAR_OTA_CS_BLOCK = f"00001018-{BASE}"  # write: offset(2B) + length(2B) → trigger CS read/write
CHAR_SERIAL       = f"00005404-{BASE}"  # UART bridge zur Lampen-MCU

# ─────────────────────────────────────────────────────────────
# Komplette Config aus CS-Dump, Block für Block (offset in words)
# Fade-In: Block 5, Bytes 6-7: D0 07 (2000ms) → 40 1F (8000ms)
# ─────────────────────────────────────────────────────────────

FADE_MS = 8000  # 8 Sekunden

CONFIG_BLOCKS = {
    0:  bytes.fromhex("540028363848F6B200000000000000000000000000000000".replace(" ","")),
    # Block 0: MAC-Adresse (unveränderт)
}

# Die vollständige Config als flaches Byte-Array (140 Bytes, offset 0–69 words)
# Aufgebaut aus dem Dump, mit geänderter Fade-Zeit in Block 5

def build_config() -> bytes:
    # Block 0  (offset word 0,  bytes 0x0000–0x0013)
    block0 = bytes.fromhex("54 00 XX XX XX XX XX XX 20 00 00 00 00 00 00 00 00 00 00 00".replace(" ",""))
    # Block 1  (offset word 10, bytes 0x0014–0x0027)
    block1 = bytes.fromhex("00 00 00 00 00 00 00 00 00 00 00 00 28 00 00 00 06 00 FF F7".replace(" ",""))
    # Block 2  (offset word 20, bytes 0x0028–0x003B)
    block2 = bytes.fromhex("E5 3F FF FF FF 3F 30 75 98 3A 00 00 9D 00 0F 00 09 00 0A 00".replace(" ",""))
    # Block 3  (offset word 30, bytes 0x003C–0x004F) — leer
    block3 = bytes(20)
    # Block 4  (offset word 40, bytes 0x0050–0x0063)
    block4 = bytes.fromhex("00 00 00 00 00 00 00 00 00 00 00 00 0A 00 0F 80 44 47 6D 09".replace(" ",""))
    # Block 5  (offset word 50, bytes 0x0064–0x0077)  ← FADE-ZEIT HIER
    fade_bytes = FADE_MS.to_bytes(2, 'little')  # 8000 → 40 1F
    print(f"  🕐 Fade-Zeit: {FADE_MS}ms → {' '.join(f'{b:02X}' for b in fade_bytes)}")
    # Original: 35 0A 34 08 00 41 40 00 00 10 01 00 [D0 07] 10 27 00 00 38 FF
    #                                                ^^^^^^ wird ersetzt
    block5 = (
        bytes.fromhex("35 0A 34 08 00 41 40 00 00 10 01 00".replace(" ",""))
        + fade_bytes                                          # bytes 12-13: Fade-In
        + bytes.fromhex("10 27 00 00 38 FF".replace(" ","")) # bytes 14-19: unverändert
    )
    # Block 6  (offset word 60, bytes 0x0078–0x008B)
    block6 = bytes.fromhex("E2 04 06 FF FD 0A 05 00 00 00 00 00 00 00 00 00 00 00 00 00".replace(" ",""))

    full = block0 + block1 + block2 + block3 + block4 + block5 + block6
    assert len(full) == 140, f"Config-Länge falsch: {len(full)}"
    return full


def hexdump(data: bytes, prefix: str = ""):
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part  = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04X}: {hex_part:<48} |{ascii_part}|")


async def remove_device(address: str):
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    proc.stdin.write(f"remove {address}\n".encode())
    proc.stdin.write(b"quit\n")
    await proc.stdin.drain()
    out, _ = await proc.communicate()
    print(f"  [bt] {out.decode(errors='replace').strip()[:80]}")


class NotifyCollector:
    def __init__(self, label=""):
        self.responses = []
        self.label = label

    def handler(self, sender, data):
        raw = bytes(data)
        hex_str = " ".join(f"{b:02X}" for b in raw)
        self.responses.append(raw)
        print(f"  📡 [{self.label}] {hex_str} ({len(raw)}B)")


async def main():
    config = build_config()

    print("\n" + "=" * 60)
    print("  Schréder CS-Config Writer")
    print("  Fade-In: 2s → 8s")
    print("=" * 60)
    print(f"\n  📋 Config ({len(config)} Bytes):")
    hexdump(config, prefix="     ")

    print(f"\n  🔗 Verbinde mit {ADDRESS}...")
    await remove_device(ADDRESS)
    await asyncio.sleep(1)

    async with BleakClient(ADDRESS, timeout=15.0) as client:
        for _ in range(50):
            if client.is_connected:
                break
            await asyncio.sleep(0.1)

        if not client.is_connected:
            print("  ❌ Verbindung fehlgeschlagen.")
            return

        print("  ✅ Verbunden!\n")
        await asyncio.sleep(0.3)

        # Notify auf 0x1014 starten um Antworten zu empfangen
        collector = NotifyCollector("CS-DATA")
        try:
            await client.start_notify(CHAR_OTA_DATA, collector.handler)
            print("  ✅ Notify auf 0x1014 aktiv")
        except Exception as e:
            print(f"  ⚠️  Notify 0x1014: {e}")

        await asyncio.sleep(0.3)

        # Config in 20-Byte Blöcken über 0x1018 schreiben
        # Format: offset (uint16 LE, in Words) + length (uint16 LE, in Bytes)
        print(f"\n  📤 Sende Config in 20-Byte Blöcken über 0x1018...\n")

        word_offset = 0
        success_count = 0
        error_count = 0

        for byte_pos in range(0, len(config), 20):
            chunk = config[byte_pos:byte_pos + 20]
            payload = word_offset.to_bytes(2, 'little') + len(chunk).to_bytes(2, 'little')

            hex_chunk = " ".join(f"{b:02X}" for b in chunk)
            print(f"  📦 offset={word_offset:3d} (0x{word_offset:04X}) | {hex_chunk}")
            print(f"     Write 0x1018: {' '.join(f'{b:02X}' for b in payload)}")

            collector.responses.clear()

            try:
                await client.write_gatt_char(CHAR_OTA_CS_BLOCK, payload, response=True)
                print(f"     ✅ Akzeptiert")
                success_count += 1
            except Exception as e:
                print(f"     ⚠️  Mit response fehlgeschlagen: {e}")
                try:
                    await client.write_gatt_char(CHAR_OTA_CS_BLOCK, payload, response=False)
                    print(f"     ✅ Akzeptiert (no-response)")
                    success_count += 1
                except Exception as e2:
                    print(f"     ❌ Fehlgeschlagen: {e2}")
                    error_count += 1
                    word_offset += 10
                    continue

            await asyncio.sleep(0.5)

            if collector.responses:
                print(f"     📡 Antwort erhalten ({len(collector.responses)} Pakete)")

            word_offset += 10  # 20 Bytes = 10 Words

        print(f"\n  {'═' * 50}")
        print(f"  ✅ {success_count} Blöcke gesendet, {error_count} Fehler")

        # Abschließend: Neustart/Apply über 0x5404 signalisieren
        print(f"\n  📤 Apply-Signal über 0x5404 senden...")
        for cmd, label in [
            (bytes([0x07]),       "Status query"),
            (bytes([0x05]),       "Apply/Save?"),
        ]:
            try:
                await client.write_gatt_char(CHAR_SERIAL, cmd, response=False)
                print(f"     ✅ {label}: {cmd.hex().upper()}")
            except Exception as e:
                print(f"     ⚠️  {label}: {e}")
            await asyncio.sleep(0.5)

        print(f"\n  ⏱️  5s beobachten...")
        await asyncio.sleep(5)

    print("\n  🔌 Verbindung getrennt")
    await remove_device(ADDRESS)
    print("  ✅ Fertig!")


if __name__ == "__main__":
    asyncio.run(main())

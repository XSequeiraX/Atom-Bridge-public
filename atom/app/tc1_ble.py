#!/usr/bin/env python3
"""Timecode dos Deity TC-1 por Bluetooth, sem cabo: alternativa ao ltc_audio.

Os TC-1 sao nos de uma rede Bluetooth Mesh (app Sidus). Enquanto a app esta
aberta com os timecodes a rolar, cada TC-1 poe o seu timecode no anuncio BLE,
~3 vezes por segundo. Com a app fechada o anuncio muda de formato e deixa de o
trazer -- por isso a mensagem de erro manda abrir a app.

Basta ouvir: nao se liga a nenhum TC-1 nem escreve nada. Cada anuncio da o
desvio TC - sistema no instante em que chegou; um anuncio isolado varia
~±0,07 s, mas a mediana de 15 s (~90 anuncios) fica a poucos ms.

Medido a 2026-10-08 contra o LTC pelo ADC: o Bluetooth chega ~+0,045 s a
frente do LTC que o mesmo TC-1 emite pelo cabo, constante (igual no Windows,
medido contra NTP). E o LTC que as camaras recebem, por isso desconta-se.

O scan corre num processo a parte (python3 tc1_ble.py --json): o bleak e o
D-Bus ficam isolados do bridge, que tem os seus proprios ciclos de BLE.

  python3 tc1_ble.py              uma leitura, legivel
  python3 tc1_ble.py --json       o que o bridge le

Ambiente (opcional): TC1_BLE_ADAPTER (hci0), TC1_BLE_SECONDS (15),
TC1_BLE_BIAS (0.045, segundos a descontar).
"""
import collections
import json
import os
import statistics
import subprocess
import sys
import time

MFR = 0x03F6                     # fabricante nos anuncios dos TC-1
ADAPTER = os.environ.get("TC1_BLE_ADAPTER", "hci0")
SECONDS = float(os.environ.get("TC1_BLE_SECONDS", "15"))
BIAS = float(os.environ.get("TC1_BLE_BIAS", "0.045"))
# Com a app aberta chegam ~3 anuncios/s por TC-1; abaixo disto algo esta mal.
MIN_SAMPLES = 20
# Os TC-1 acertados juntos pela app ficaram a 2 ms uns dos outros. Mais de um
# frame entre eles quer dizer que algum nao apanhou a sincronizacao.
MAX_SPREAD = 0.04


def decode(payload):
    """(h, m, s, f) do anuncio de um TC-1, ou None.

    Os cinco bytes a seguir a `54 ff ff` sao um fluxo de bits LSB-first: hora
    nos bits 17-21, minuto 22-27, segundo 28-33, frame 34-38.
    """
    i = payload.find(b"\x54\xff\xff")
    if i < 0 or len(payload) < i + 8:
        return None
    v = int.from_bytes(payload[i + 3:i + 8], "little")
    t = ((v >> 17) & 0x1F, (v >> 22) & 0x3F, (v >> 28) & 0x3F, (v >> 34) & 0x1F)
    return t if t[0] < 24 and t[1] < 60 and t[2] < 60 and t[3] < 30 else None


def decode_date(payload):
    """(aaaa, mm, dd) do anuncio, ou None. Tres bytes BCD dia-mes-ano, 9 bytes
    depois do timecode (visto `08 10 26` a 2026-10-08)."""
    i = payload.find(b"\x54\xff\xff")
    if i < 0 or len(payload) < i + 15:
        return None
    try:
        d, m, y = (int("%02x" % b) for b in payload[i + 12:i + 15])
    except ValueError:
        return None                       # nao e BCD
    return (2000 + y, m, d) if 1 <= m <= 12 and 1 <= d <= 31 else None


def tod(t):
    lt = time.localtime(t)
    return lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (t % 1)


def scan(seconds=SECONDS, adapter=ADAPTER):
    """[(endereco, instante, (h, m, s, f), data)] -- um por anuncio diferente."""
    import asyncio
    from bleak import BleakScanner
    am, vistos = [], set()

    def cb(dev, adv):
        t = time.time()
        data = (adv.manufacturer_data or {}).get(MFR)
        if not data:
            return
        data = bytes(data)
        tc = decode(data)
        # O BlueZ repete o mesmo anuncio; so conta a primeira vez que se ve.
        if tc and (dev.address, data) not in vistos:
            vistos.add((dev.address, data))
            am.append((dev.address, t, tc, decode_date(data)))

    async def run():
        async with BleakScanner(cb, adapter=adapter):
            await asyncio.sleep(seconds)
    asyncio.run(run())
    return am


def result(am, bias=BIAS):
    """O dict que o ltc_audio.read() devolve, a partir dos anuncios."""
    if not am:
        raise RuntimeError("no timecode from the TC-1s over Bluetooth — open the Sidus app "
                           "with the timecodes running (they only send it while it is open)")
    maxf = max(tc[3] for _, _, tc, _ in am)
    fps = next((f for f in (24, 25, 30) if maxf < f), None)
    if fps is None:
        raise RuntimeError(f"TC-1 frame number {maxf} makes no sense")
    if len(am) < MIN_SAMPLES:
        raise RuntimeError(f"only {len(am)} timecode messages in {SECONDS:.0f}s — too few to "
                           f"trust; is the Sidus app open with the timecodes running?")

    def desvio(t, tc):
        secs = tc[0] * 3600 + tc[1] * 60 + tc[2] + tc[3] / fps
        return (secs - tod(t) + 43200) % 86400 - 43200

    por = collections.defaultdict(list)
    for addr, t, tc, _ in am:
        por[addr].append(desvio(t, tc))
    medianas = {a: statistics.median(d) for a, d in por.items()}
    spread = max(medianas.values()) - min(medianas.values())
    if spread > MAX_SPREAD:
        raise RuntimeError(f"the TC-1s disagree by {spread * fps:.1f} frames — sync them "
                           f"again in the Sidus app")
    todos = [d for ds in por.values() for d in ds]
    med = statistics.median(todos)
    off = med - bias
    at = time.time()
    secs = (tod(at) + off) % 86400
    n = int(secs * fps + 1e-6)
    datas = [d for _, _, _, d in am if d]
    return {"secs": secs, "fps": fps, "at": at, "via": "ble",
            "tc": "%02d:%02d:%02d:%02d" % (n // (fps * 3600), n // (fps * 60) % 60,
                                          n // fps % 60, n % fps),
            "date": collections.Counter(datas).most_common(1)[0][0] if datas else None,
            "frames": len(todos), "devices": len(por), "spread": round(spread, 4),
            # Mesmo papel que no LTC: que parte das leituras concorda com a mediana.
            "continuity": round(sum(1 for d in todos if abs(d - med) <= 0.1) / len(todos), 3),
            # Pelo Bluetooth nao ha user bits: None em vez de "instaveis".
            "user_bits": None, "user_bits_stable": None}


def adaptador(pedido=ADAPTER):
    """O adaptador pedido se existir; senao o primeiro que houver. A numeracao muda quando um sai: com o
    Intel (Wi-Fi + Bluetooth) fora, o dongle passou a hci0, e um hci0 fixo deixava o GET LTC por Bluetooth
    sem adaptador."""
    base = "/sys/class/bluetooth"
    try:
        todos = sorted(x for x in os.listdir(base) if x.startswith("hci") and ":" not in x)
    except OSError:
        return pedido
    return pedido if pedido in todos or not todos else todos[0]


def read(seconds=SECONDS, adapter=ADAPTER):
    """Como ltc_audio.read(): dict ou RuntimeError com a causa. Bloqueia ~seconds+2."""
    adapter = adaptador(adapter)
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--json",
                            "--seconds", str(seconds), "--adapter", adapter],
                           capture_output=True, text=True, timeout=seconds + 30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Bluetooth scan did not finish") from None
    try:
        r = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        err = (p.stderr or "").strip().splitlines()
        raise RuntimeError("Bluetooth scan failed: " + (err[-1] if err else "no output")) from None
    if "error" in r:
        raise RuntimeError(r["error"])
    if r.get("date"):
        r["date"] = tuple(r["date"])
    return r


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seconds", type=float, default=SECONDS)
    ap.add_argument("--adapter", default=ADAPTER)
    o = ap.parse_args()
    try:
        r = result(scan(o.seconds, o.adapter))
    except Exception as e:
        r = {"error": str(e)}
    if o.json:
        print(json.dumps(r))
        return
    if "error" in r:
        sys.exit(r["error"])
    lt = time.localtime(r["at"])
    print("TC-1 by Bluetooth   %s @ %s fps   date %s" % (r["tc"], r["fps"], r["date"]))
    print("System              %s.%03d" % (time.strftime("%H:%M:%S", lt), int(r["at"] % 1 * 1000)))
    print("TC - system %+.3f s  (already corrected %.3f s)   %d adverts from %d TC-1, "
          "difference between them %.0f ms"
          % ((r["secs"] - tod(r["at"]) + 43200) % 86400 - 43200, BIAS, r["frames"],
             r["devices"], r["spread"] * 1000))


if __name__ == "__main__":
    main()

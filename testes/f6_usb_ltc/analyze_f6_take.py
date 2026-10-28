#!/usr/bin/env python3
"""Take do F6: o timecode que ele gravou bate com o dos TC-1?

O teste: o F6 em Ext Auto Rec recebe o LTC do Atom (ltc_gen.py --offset jam) e
grava, numa pista de audio, o LTC de um TC-1 (A-OUT numa entrada, phantom
DESLIGADO). No ficheiro ficam as duas horas, amostra a amostra:

  - o TC do F6, nos metadados BWF (bext TimeReference = amostras desde a
    meia-noite no primeiro sample do ficheiro);
  - o TC do TC-1, no audio dessa pista.

Para cada frame de LTC descodificado, compara a hora do F6 nessa amostra com a
hora do TC-1 nessa amostra. Perto de 0 = o F6 grava o mesmo TC que as camaras.

Pensado para o leitor SD do Atom: tira-se o cartao do F6, mete-se no leitor e

  sudo python3 analyze_f6_take.py
      procura o take mais recente no cartao. Se o cartao nao estiver montado,
      monta-o SO DE LEITURA em /mnt/f6sd e desmonta no fim -- nunca escreve no
      cartao do F6.
  sudo python3 analyze_f6_take.py --gen-start 10:40:12:05 --gen-stop 10:40:41:20
      com o primeiro e o ultimo frame que o ltc_gen.py diz ter emitido, mede
      tambem quanto o F6 demorou a arrancar e a parar.
  python3 analyze_f6_take.py /caminho/ZOOM0001            uma pasta de take
  python3 analyze_f6_take.py /caminho/ZOOM0001_Tr3.WAV    ou um ficheiro

Todas as pistas do take sao experimentadas; usa a que tiver LTC legivel.
So biblioteca padrao.
"""
import argparse
import array
import collections
import os
import re
import statistics
import struct
import subprocess
import sys

SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]
MOUNT = "/mnt/f6sd"
FS_CARTAO = ("vfat", "exfat", "msdos", "fat")


# --------------------------------------------------------------- cartao SD ---

def montagens():
    """[(ponto, fs)] dos sistemas de ficheiros montados que podem ser um cartao."""
    out = []
    try:
        with open("/proc/mounts") as f:
            for l in f:
                p = l.split()
                ponto = p[1].replace("\\040", " ")
                if p[2] in FS_CARTAO or ponto.startswith(("/media/", "/run/media/", "/mnt/")):
                    out.append(ponto)
    except OSError:
        pass
    return out


def wavs(raiz, prof=5):
    """Todos os .WAV debaixo de raiz, ate prof niveis."""
    base = raiz.rstrip("/").count("/")
    for d, dirs, fs in os.walk(raiz):
        if d.count("/") - base >= prof:
            dirs[:] = []
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in fs:
            if f.upper().endswith(".WAV") and not f.startswith("._"):
                yield os.path.join(d, f)


def montar_cartao():
    """Monta so de leitura a primeira particao FAT/exFAT por montar. Devolve o
    ponto de montagem (para desmontar no fim) ou None."""
    try:
        out = subprocess.run(["lsblk", "-rpno", "NAME,TYPE,FSTYPE,MOUNTPOINT"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    # Particao ou disco inteiro: ha cartoes formatados sem tabela de particoes,
    # com o sistema de ficheiros directamente no disco.
    for l in out.splitlines():
        p = l.split(" ")
        if len(p) >= 3 and p[1] in ("part", "disk") and p[2] in FS_CARTAO and (len(p) < 4 or not p[3]):
            os.makedirs(MOUNT, exist_ok=True)
            r = subprocess.run(["mount", "-o", "ro", p[0], MOUNT], capture_output=True, text=True)
            if r.returncode == 0:
                print("Card %s mounted read-only at %s" % (p[0], MOUNT))
                return MOUNT
            print("Could not mount %s: %s" % (p[0], r.stderr.strip()))
    return None


def take_mais_recente(raizes):
    todos = [w for r in raizes for w in wavs(r)]
    if not todos:
        return None, []
    ultimo = max(todos, key=os.path.getmtime)
    pasta = os.path.dirname(ultimo)
    return pasta, sorted(w for w in todos if os.path.dirname(w) == pasta)


# ------------------------------------------------------------------- WAV ---

def ler_wav(path):
    """(rate, canais, time_reference, ixml, amostras, formato) -- amostras num array."""
    with open(path, "rb") as f:
        cab = f.read(12)
        if cab[:4] == b"RF64":
            raise ValueError("RF64 (> 4 GB): record a short take for the test")
        if cab[:4] != b"RIFF" or cab[8:12] != b"WAVE":
            raise ValueError("not a WAV")
        fmt, tref, ixml, raw = None, None, "", None
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"data":
                raw = f.read(size)
            else:
                body = f.read(size)
                if cid == b"fmt ":
                    fmt = struct.unpack("<HHIIHH", body[:16])
                    if fmt[0] == 0xFFFE and len(body) >= 26:       # WAVE_FORMAT_EXTENSIBLE
                        fmt = (struct.unpack("<H", body[24:26])[0],) + fmt[1:]
                elif cid == b"bext" and len(body) >= 346:
                    lo, hi = struct.unpack("<II", body[338:346])
                    tref = lo + (hi << 32)
                elif cid == b"iXML":
                    ixml = body.decode("utf-8", "replace")
            if size & 1:
                f.read(1)
    if not fmt or raw is None:
        raise ValueError("WAV without fmt or data")
    tag, ch, rate, _, align, bits = fmt
    n = len(raw) // align
    if tag == 3 and bits == 32:
        a = array.array("f")
        a.frombytes(raw[:n * align])
    elif tag == 1 and bits == 16:
        a = array.array("h")
        a.frombytes(raw[:n * align])
    elif tag == 1 and bits == 24:
        # Para 32 bits com sinal, so com arrays: o byte baixo a zero e um shift.
        b = bytearray(n * ch * 4)
        b[1::4], b[2::4], b[3::4] = raw[0::3][:n * ch], raw[1::3][:n * ch], raw[2::3][:n * ch]
        a = array.array("i")
        a.frombytes(bytes(b))
    else:
        raise ValueError("unsupported format (tag %d, %d bits)" % (tag, bits))
    return rate, ch, tref, ixml, a


# ------------------------------------------------------------------- LTC ---

def decode_ltc(x, rate):
    """[(h, m, s, f, amostra_onde_o_frame_acaba)], fps -- o decoder do bridge
    (media movel, histerese relativa ao pico), com a amostra de fim de cada frame."""
    if len(x) < rate // 2:
        return [], 0
    w = max(1, 5 * rate // 48000)
    lp = array.array("f", bytes(4 * len(x)))
    s = 0.0
    for i in range(len(x)):
        s += x[i]
        if i >= w:
            s -= x[i - w]
        lp[i] = s / (w if i >= w else i + 1)
    pk = max(max(lp), -min(lp)) or 1
    hi, lo = pk * 0.15, -pk * 0.15
    tr, st = [], 0
    for i, v in enumerate(lp):
        if st <= 0 and v > hi:
            st = 1
            tr.append(i)
        elif st >= 0 and v < lo:
            st = -1
            tr.append(i)
    iv = [tr[i] - tr[i - 1] for i in range(1, len(tr))]
    if len(iv) < 100:
        return [], 0
    mode = collections.Counter(iv).most_common(1)[0][0]
    half = sum(1 for v in iv if mode * 0.35 <= v <= mode * 0.65)
    bitp = mode if half > len(iv) * 0.1 else mode * 2
    thr = bitp * 0.75
    bits, ends, k = [], [], 0
    while k < len(iv):
        if iv[k] >= thr:
            bits.append(0)
            ends.append(tr[k + 1])
            k += 1
        elif k + 1 < len(iv) and iv[k + 1] < thr:
            bits.append(1)
            ends.append(tr[k + 2])
            k += 2
        else:
            k += 1
    fps = min((24, 25, 30), key=lambda f: abs(f - rate / bitp / 80))
    out = []
    for i in range(64, len(bits) - 16):
        if bits[i:i + 16] == SYNC:
            f = bits[i - 64:i + 16]
            g = lambda s0, n, f=f: sum(f[s0 + j] << j for j in range(n))
            t = (g(48, 4) + g(56, 2) * 10, g(32, 4) + g(40, 3) * 10,
                 g(16, 4) + g(24, 3) * 10, g(0, 4) + g(8, 2) * 10)
            if t[0] < 24 and t[1] < 60 and t[2] < 60 and t[3] < fps:
                out.append(t + (ends[i + 15],))
    return out, fps


def tc_secs(t, fps):
    return t[0] * 3600 + t[1] * 60 + t[2] + t[3] / fps


def parse_tc(s, fps):
    h, m, sec, f = (int(x) for x in re.split(r"[:;.]", s))
    return tc_secs((h, m, sec, f), fps)


def fmt(secs, fps):
    n = int(round(secs * fps)) % (86400 * fps)
    return "%02d:%02d:%02d:%02d" % (n // (fps * 3600), n // (fps * 60) % 60, n // fps % 60, n % fps)


def wrap(x):
    return (x + 43200) % 86400 - 43200


# ------------------------------------------------------------------ main ---

def analisar(ficheiros, o):
    melhor = None             # (frames, fps, ficheiro, canal, rate, tref, dur, ixml)
    for w in ficheiros:
        try:
            rate, ch, tref, ixml, a = ler_wav(w)
        except (OSError, ValueError) as e:
            print("  %s: %s" % (os.path.basename(w), e))
            continue
        dur = len(a) / ch / rate
        maxn = int(o.max_seconds * rate)
        for c in ([o.channel - 1] if o.channel else range(ch)):
            if c >= ch:
                continue
            x = a[c::ch][:maxn]
            fr, fps = decode_ltc(x, rate)
            print("  %-22s pista %d: %s" % (os.path.basename(w), c + 1,
                                           "%d LTC frames" % len(fr) if fr else "no LTC"))
            if fr and (melhor is None or len(fr) > len(melhor[0])):
                melhor = (fr, fps, w, c, rate, tref, dur, ixml)
    return melhor


def takes_recentes(raizes, n):
    """[(pasta, [wavs])] das n pastas de take mais recentes, a mais recente primeiro."""
    por = collections.defaultdict(list)
    for r in raizes:
        for w in wavs(r):
            por[os.path.dirname(w)].append(w)
    pastas = sorted(por, key=lambda d: max(os.path.getmtime(w) for w in por[d]), reverse=True)
    return [(d, sorted(por[d])) for d in pastas[:n]]


def emissoes():
    """Historico do ltc_gen.py: [(inicio_s, fim_s, registo)] das ultimas 12 h."""
    import json
    import time
    out = []
    try:
        with open("/tmp/ltc_gen_runs.jsonl") as f:
            for l in f:
                try:
                    g = json.loads(l)
                except ValueError:
                    continue
                if time.time() - g.get("at", 0) < 12 * 3600:
                    out.append((parse_tc(g["start"], 25), parse_tc(g["stop"], 25), g))
    except OSError:
        pass
    return out


def medir(m):
    """Numeros de um take: dict com inicio do F6, desvio, inicio/fim do audio na hora do TC-1."""
    fr, fps, w, c, rate, tref, dur, ixml = m
    start = tref / rate
    # TC-1 no fim de cada frame = inicio do frame + um frame; F6 nessa amostra =
    # inicio do ficheiro + amostra / taxa.
    d = [wrap((start + e / rate) - (tc_secs(t, fps) + 1.0 / fps)) for (*t, e) in fr]
    med = statistics.median(d)
    # Inicio e fim na hora REAL (a do TC-1 gravado no audio), nao na que o F6
    # escreveu: essa ja traz o desvio.
    return {"start": start, "med": med, "d": d, "fps": fps, "ini": start - med,
            "fim": start + dur - med, "dur": dur}


def relatorio(m, r, gen_start, gen_stop):
    fr, fps, w, c, rate, tref, dur, ixml = m
    d, med = r["d"], r["med"]
    x = re.search(r"<TIMECODE_RATE>(\d+)/(\d+)</TIMECODE_RATE>", ixml)
    print("\nFicheiro %s, pista %d (%d Hz, %.1f s)" % (os.path.basename(w), c + 1, rate, dur))
    print("F6 TC at the start:  %s%s" % (fmt(r["start"], fps), "   (iXML %s/%s fps)" % x.groups() if x else ""))
    print("TC-1 LTC on the track: %s -> %s, %d frames at %d fps"
          % (fmt(tc_secs(fr[0], fps), fps), fmt(tc_secs(fr[-1], fps), fps), len(fr), fps))
    print("\nF6 - TC-1: mediana %+.4f s (%+.2f frames)   min %+.4f   max %+.4f"
          % (med, med * fps, min(d), max(d)))
    if len(d) > 1:
        span = (fr[-1][4] - fr[0][4]) / rate or 1
        print("Drift over the take: %+.4f s in %.0f s (%+.1f ppm)"
              % (d[-1] - d[0], span, (d[-1] - d[0]) / span * 1e6))
    if abs(med) <= 1.0 / fps:
        print("ALIGNED: within 1 frame, the F6 records the same TC as the TC-1.")
    else:
        print("MISALIGNED: the TC in the file is %+.3f s (%+.1f frames) off the TC-1." % (med, med * fps))
    print("\nFile audio, at the TC-1 time: %s -> %s" % (fmt(r["ini"], fps), fmt(r["fim"], fps)))
    if gen_start:
        y = wrap(r["ini"] - parse_tc(gen_start, fps))
        print("Start:  the audio starts %.2f s %s the first frame from the Atom"
              % (abs(y), "after" if y >= 0 else "BEFORE (the F6 keeps audio from before the LTC)"))
    if gen_stop:
        y = wrap(r["fim"] - parse_tc(gen_stop, fps))
        print("End:    the audio ends %.2f s %s the last frame from the Atom" % (abs(y), "after" if y >= 0 else "before"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("caminho", nargs="?", help="WAV file or take folder; by default, the card")
    ap.add_argument("--channel", type=int, help="track inside each file (1 = first)")
    ap.add_argument("--max-seconds", type=float, default=60, help="analyse only the start (default 60 s)")
    ap.add_argument("--gen-start", help="first frame sent by ltc_gen.py")
    ap.add_argument("--gen-stop", help="last frame sent by ltc_gen.py")
    ap.add_argument("--takes", type=int, default=1,
                    help="analyse the N most recent takes, each matched with its ltc_gen.py run")
    o = ap.parse_args()
    # Sem --gen-start/--gen-stop, usa o que o ltc_gen.py guardou da ultima vez
    # (se for de ha menos de 2 h).
    if o.takes == 1 and not (o.gen_start or o.gen_stop):
        try:
            import json
            import time
            with open("/tmp/ltc_gen_last.json") as f:
                g = json.load(f)
            if time.time() - g.get("at", 0) < 7200:
                o.gen_start, o.gen_stop = g["start"], g["stop"]
                print("Ultima emissao do ltc_gen.py: %s -> %s" % (o.gen_start, o.gen_stop))
        except (OSError, ValueError, KeyError):
            pass

    montei = None
    resultados = []
    try:
        if o.caminho and os.path.isfile(o.caminho):
            takes = [(os.path.dirname(o.caminho), [o.caminho])]
        else:
            raizes = [o.caminho] if o.caminho else montagens()
            takes = takes_recentes(raizes, o.takes)
            if not takes and not o.caminho:
                montei = montar_cartao()
                takes = takes_recentes([montei] if montei else [], o.takes)
        if not takes:
            print("No WAV files found. Mounts seen: %s" % (", ".join(montagens()) or "none"))
            print("Disks the system sees (the card should appear here):")
            subprocess.run(["lsblk", "-o", "NAME,TYPE,FSTYPE,SIZE,RM,TRAN,MOUNTPOINT"])
            sys.exit("Is the F6 card in the reader? Run with sudo so it can be mounted.")
        for pasta, fs in reversed(takes):              # do mais antigo para o mais recente
            print("Take: %s  (%d file(s))" % (pasta, len(fs)))
            resultados.append((pasta, analisar(fs, o)))
    finally:
        if montei:
            subprocess.run(["umount", montei])
            print("Card unmounted.")

    if o.takes == 1:
        pasta, m = resultados[0]
        if not m:
            sys.exit("\nNo track with readable LTC. Was the TC-1 on A-OUT, plugged into an input, with level?")
        if m[5] is None:
            sys.exit("%s has no bext/TimeReference: the F6 wrote no timecode." % m[2])
        relatorio(m, medir(m), o.gen_start, o.gen_stop)
        return

    # Varios takes: uma linha por take, casado com a emissao do ltc_gen.py que o
    # disparou (a que estava a emitir quando o audio comecou, com folga de 3 s).
    runs = emissoes()
    print("\n%-16s %-12s %-12s %-6s %10s %9s %9s" % ("take", "TC do F6", "1st Atom frame", "frame",
                                                     "F6-TC-1", "inicio", "fim"))
    for pasta, m in resultados:
        nome = os.path.basename(pasta)
        if not m or m[5] is None:
            print("%-16s no readable LTC or no timecode" % nome)
            continue
        r = medir(m)
        fps = r["fps"]
        g = next((x for x in runs if x[0] - 3 <= r["ini"] <= x[1] + 3), None)
        ini = fim = "-"
        g0 = "-"
        fr0 = "-"
        if g:
            g0 = g[2]["start"]
            fr0 = g0.split(":")[-1]
            ini = "%+.2f s" % wrap(r["ini"] - g[0])
            fim = "%+.2f s" % wrap(r["fim"] - g[1])
        print("%-16s %-12s %-12s %-6s %+9.3fs %9s %9s %s"
              % (nome, fmt(r["start"], fps), g0, fr0, r["med"], ini, fim,
                 "ok" if abs(r["med"]) <= 1.0 / fps else "DESALINHADO"))
    print("\nF6-TC-1: the TC error in the file. start/end: the audio relative to the first and\n"
          "last frame from the Atom (negative = the F6 has audio from before the LTC arrived).")


if __name__ == "__main__":
    main()

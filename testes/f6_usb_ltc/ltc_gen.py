#!/usr/bin/env python3
"""Gerador de LTC (25 fps) pela saida de audio, para o F6 em Ext Auto Rec.

O F6 em Ext Auto Rec grava enquanto recebe TC e para quando ele desaparece. Este
gerador emite o TC enquanto corre e cala-se quando e parado, por isso o bridge
comanda a gravacao ligando e desligando o processo.

  python3 ltc_gen.py --selftest                      codifica e descodifica com o decoder do bridge
  python3 ltc_gen.py --play --offset -0.78 --seconds 10
  python3 ltc_gen.py --play --device plughw:0,0 --level 0.05

--offset  segundos a somar ao relogio do sistema para dar a hora do TC-1, ou `jam`
          para usar o do ultimo GET LTC do bridge (cabo ou Bluetooth) -- o normal.
--latency tempo entre escrever o primeiro frame e ele soar. Calibra-se por loopback:
          saida ligada a entrada do ADC, --offset 0, e ve-se o `--compare`; a diferenca
          LTC - sistema e o erro em segundos (somar-lho ao --latency, com sinal trocado).
--level   amplitude, 0..1 de fundo de escala. Comecar baixo (0.05) e subir ate o F6 arrancar.

CABO: o sinal sai so pelo canal esquerdo (ponta). O anel do jack do F6 e a SAIDA de TC
dele, por isso nao ligar o anel: soldar so ponta e massa. Um jack mono (TS) poe o anel
em curto com a massa.
"""
import argparse
import array
import math
import os
import subprocess
import sys
import time

RATE = 48000
FPS = 25
SPB = RATE // FPS // 80            # amostras por bit: 24
SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]
UB_FIELDS = (4, 12, 20, 28, 36, 44, 52, 60)


def ub_nibbles(date):
    """(aaaa, mm, dd) -> oito nibbles, no formato que o bridge descodifica (fuso, ano, mes, dia)."""
    y, m, d = date
    y %= 100
    return [0, 0, y % 10, y // 10, m % 10, m // 10, d % 10, d // 10]


def frame_bits(h, m, s, f, ub):
    bits = [0] * 80

    def put(pos, val, n):
        for j in range(n):
            bits[pos + j] = (val >> j) & 1
    put(0, f % 10, 4)
    put(8, f // 10, 2)
    put(16, s % 10, 4)
    put(24, s // 10, 3)
    put(32, m % 10, 4)
    put(40, m // 10, 3)
    put(48, h % 10, 4)
    put(56, h // 10, 2)
    for i, pos in enumerate(UB_FIELDS):
        put(pos, ub[i], 4)
    bits[64:80] = SYNC
    # A 25 fps o bit 59 corrige a polaridade: numero par de zeros por frame,
    # para todos os frames comecarem com o mesmo nivel.
    if bits.count(0) % 2:
        bits[59] = 1
    return bits


def encode(bits, level, state, hi, lo):
    """Bi-phase mark: transicao no inicio de cada bit e outra a meio nos '1'."""
    out = []
    for b in bits:
        state = -state
        a, z = (hi, lo) if state > 0 else (lo, hi)
        if b:
            out.append(a * (SPB // 2) + z * (SPB // 2))
            state = -state
        else:
            out.append(a * SPB)
    return b"".join(out), state


class Gerador:
    def __init__(self, level, date, stereo):
        amp = int(32767 * level)
        self.hi = array.array("h", [amp]).tobytes()
        self.lo = array.array("h", [-amp]).tobytes()
        self.ub = ub_nibbles(date)
        self.state = 1
        self.stereo = stereo

    def frame(self, n):
        n %= 86400 * FPS
        secs, f = divmod(n, FPS)
        bits = frame_bits(secs // 3600, secs // 60 % 60, secs % 60, f, self.ub)
        raw, self.state = encode(bits, 1, self.state, self.hi, self.lo)
        if self.stereo:                     # esquerdo = LTC, direito = silencio
            a = array.array("h")
            a.frombytes(raw)
            st = array.array("h", bytes(len(a) * 4))
            st[0::2] = a
            raw = st.tobytes()
        return raw


def load_decoder():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in ("/opt/camera-bridge", here, os.path.join(here, "bk", "opt-camera-bridge")):
        if os.path.exists(os.path.join(p, "ltc_audio.py")):
            sys.path.insert(0, p)
            import ltc_audio
            return ltc_audio
    sys.exit("ltc_audio.py not found.")


def selftest(level):
    ltc = load_decoder()
    g = Gerador(level, (2026, 10, 7), False)
    n0 = (10 * 3600 + 30 * 60 + 59) * FPS + 20          # atravessa a mudanca de segundo e de minuto
    raw = b"".join(g.frame(n0 + i) for i in range(FPS * 4))
    frames, fps, ubs = ltc.decode(raw)
    ok = len(frames) >= FPS * 4 - 3 and fps == FPS
    nums = [((h * 3600 + m * 60 + s) * fps + f) for h, m, s, f in frames]
    seguidos = all(nums[i] - nums[i - 1] == 1 for i in range(1, len(nums)))
    data = ltc.date_from_user_bits(max(set(ubs), key=ubs.count)) if ubs else None
    print("frames decoded %d/%d  fps %s  consecutive %s  date %s" % (len(frames), FPS * 4, fps, seguidos, data))
    print("first %s  last %s" % (frames[0] if frames else None, frames[-1] if frames else None))
    print("peak %.1f%% of full scale" % (100 * level))
    print("RESULTADO:", "OK" if ok and seguidos and data == (2026, 10, 7) else "FALHOU")


def tod(t):
    lt = time.localtime(t)
    return lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (t % 1)


LAST_RUN = "/tmp/ltc_gen_last.json"
RUNS = "/tmp/ltc_gen_runs.jsonl"


def fmt_tc(n):
    n %= 86400 * FPS
    return "%02d:%02d:%02d:%02d" % (n // (FPS * 3600), n // (FPS * 60) % 60, n // FPS % 60, n % FPS)


def jam_offset(path="/opt/camera-bridge/settings.json"):
    """O desvio TC - sistema do ultimo GET LTC do bridge (cabo ou Bluetooth)."""
    import json
    try:
        with open(path) as f:
            j = json.load(f).get("_ltc_jam")
    except (OSError, ValueError) as e:
        sys.exit("Could not read the bridge jam (%s): %s" % (path, e))
    if not j or "offset" not in j:
        sys.exit("The bridge has no stored jam: do GET LTC first.")
    # O bridge corrige o jam quando o NTP da um salto, mas so quando da por
    # isso; aqui so se avisa, para o teste nao correr com um desvio velho.
    passo = (time.time() - time.monotonic()) - j.get("clock_ref", time.time() - time.monotonic())
    if abs(passo) > 0.02:
        print("WARNING: the clock jumped %+.3f s since the jam -- do GET LTC again." % passo)
    print("Jam do bridge: %s (%s), desvio %+.3f s" % (j.get("tc"), j.get("via", "cable"), j["offset"]))
    return float(j["offset"])


def play(a):
    import fcntl
    if a.offset == "jam":
        a.offset = jam_offset()
    else:
        a.offset = float(a.offset)
    p = subprocess.Popen(["aplay", "-q", "-D", a.device, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
                          "-t", "raw", "--buffer-time=100000", "-"], stdin=subprocess.PIPE)
    try:
        fcntl.fcntl(p.stdin.fileno(), 1031, 4096)       # F_SETPIPE_SZ: pipe curto, latencia estavel
    except OSError:
        pass
    t0 = time.time() + a.offset
    g = Gerador(a.level, time.localtime(t0)[:3], True)
    n = math.ceil(tod(t0 + a.latency) * FPS)
    if a.start_frame is not None:
        # Arrancar num frame escolhido dentro do segundo: o F6 em Ext Auto Rec
        # parece carimbar mal conforme o ponto do segundo em que o LTC chega.
        n += (a.start_frame - n) % FPS
    time.sleep(max(0.0, n / FPS - tod(time.time() + a.offset + a.latency)))
    n0 = n
    print("Sending LTC from %s (%s, level %.0f%%). Ctrl+C to stop."
          % (time.strftime("%H:%M:%S"), a.device, a.level * 100))
    # LTC parado antes/depois (--freeze-before/--freeze-after): o mesmo frame
    # repetido. Os frames contam-se desde n0, por isso o primeiro frame a andar
    # soa a hora certa, como sem paragem. Cada frame demora 1/FPS a soar: e o
    # aplay que marca o ritmo.
    fb, fa = round(a.freeze_before * FPS), round(a.freeze_after * FPS)
    rf = round(a.seconds * FPS) if a.seconds else None
    fase, k, run0, run1 = None, 0, n0 + fb, None
    try:
        while rf is None or k < fb + rf + fa:
            if k < fb:
                label, f = run0, "LTC STILL at %s (before)" % fmt_tc(run0)
            elif rf is None or k < fb + rf:
                label, f = n0 + k, "LTC A ANDAR"
                run1 = label
            else:
                label, f = run1, "LTC STILL at %s (after)" % fmt_tc(run1)
            if f != fase and (fb or fa):
                print("%s  %s" % (time.strftime("%H:%M:%S"), f))
                fase = f
            p.stdin.write(g.frame(label))
            k += 1
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        p.terminate()
    n0, n = run0, (run1 if run1 is not None else run0) + 1
    # Para cruzar com o ficheiro do F6 (analyze_f6_take.py --gen-start/--gen-stop).
    # O ultimo frame e aproximado: o que estava no buffer quando se parou.
    print("First running frame: %s   last: %s" % (fmt_tc(n0), fmt_tc(n - 1)))
    # O analyze_f6_take.py le isto sozinho, para nao se copiar frames a mao.
    try:
        import json
        reg = {"start": fmt_tc(n0), "stop": fmt_tc(n - 1), "at": time.time(),
               "offset": a.offset, "latency": a.latency}
        with open(LAST_RUN, "w") as f:
            json.dump(reg, f)
        # Historico, para analisar varios takes de uma vez (--takes N).
        with open(RUNS, "a") as f:
            f.write(json.dumps(reg) + "\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--device", default="plughw:0,0")
    ap.add_argument("--offset", default="0",
                    help="seconds to add to the system clock, or 'jam' to use the bridge one")
    ap.add_argument("--latency", type=float, default=0.114)
    ap.add_argument("--level", type=float, default=0.05)
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--start-frame", type=int, choices=range(FPS),
                    help="only start when the frame within the second is this one (0-24)")
    ap.add_argument("--freeze-before", type=float, default=0,
                    help="seconds of still LTC (repeated frame) before it runs")
    ap.add_argument("--freeze-after", type=float, default=0,
                    help="seconds of still LTC after --seconds running")
    a = ap.parse_args()
    if (a.freeze_before or a.freeze_after) and not a.seconds:
        ap.error("--freeze-before/--freeze-after precisam de --seconds")
    if a.selftest:
        selftest(a.level)
    elif a.play:
        play(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

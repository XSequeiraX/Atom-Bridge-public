"""Jam do Zoom F6: LTC pela saida de auscultadores do Atom, so durante o jam.

O F6 trabalha em TIMECODE Int Free Run e grava pelo FRC-8 (frc8.py, f6_ctl.py).
Para acertar o relogio dele, o jam toca LTC a andar uns segundos pela saida de
auscultadores do Atom (so a ponta do jack: o anel do F6 e a SAIDA dele), e o F6
fica com essa hora. Fora do jam a saida de som fica fechada: a placa HDA pode
adormecer (power_save) e o leitor do cartao (f6_card.py) usa-a.

Cada jam: abre o aplay, PREROLL de LTC parado no valor exacto com que vai
arrancar (cobre o acordar da placa), depois a andar, e fecha. A latencia
LATENCY foi medida com o som a correr: e a situacao do troco a andar.

O TC vem do jam do bridge (ltc_secs_at), e o fluxo de frames e mantido em fase
com ele: o relogio da placa de som deriva uns ppm do relogio do sistema (NTP),
e um frame com uma amostra a mais ou a menos corrige isso (uma amostra sao 4%
de um bit). Sem jam valido no bridge nao ha jam do F6.

(Antes, 2026-10-08: gerador sempre ligado para o F6 em Ext Auto Rec, com o LTC
parado entre takes. Abandonado: o F6 grava agora pelo FRC-8.)

Ambiente (opcional): F6_LTC_DEVICE (plughw:0,0), F6_LTC_LEVEL (0.05),
F6_LTC_LATENCY (0.114 s, calibrado por loopback e contra o ficheiro do F6).
"""
import array
import os
import subprocess
import threading
import time

RATE = 48000
DEVICE = os.environ.get("F6_LTC_DEVICE", "plughw:0,0")
LEVEL = float(os.environ.get("F6_LTC_LEVEL", "0.05"))
# Entre escrever um frame e ele soar. Medido a 2026-10-08: com 0.15 o TC saia
# +0,036 s adiantado; com 0.114 o ficheiro do F6 ficou a +0,006 s dos TC-1.
LATENCY = float(os.environ.get("F6_LTC_LATENCY", "0.114"))
SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]
UB_FIELDS = (4, 12, 20, 28, 36, 44, 52, 60)
# Correccao de fase: so com o erro medio acima disto (amostras), para o ruido
# da medicao nao estar sempre a mexer.
DEADBAND = 3.0
# Um salto maior que isto entre o TC a andar e o jam (um GET LTC novo a meio de
# uma take) realinha de uma vez em vez de esperar pela correccao lenta.
RESYNC_FRAMES = 3
# Antes de andar, o LTC fica parado no valor exacto com que vai arrancar este
# tempo: cobre o acordar da placa de som, e a transicao para "a andar" e a testada.
PREROLL = float(os.environ.get("F6_LTC_PREROLL", "1.0"))


def _bits(n, fps, date):
    """Os 80 bits do frame n (contado desde a meia-noite)."""
    n %= 86400 * fps
    secs, f = divmod(n, fps)
    h, m, s = secs // 3600, secs // 60 % 60, secs % 60
    y, mo, d = date
    y %= 100
    ub = [0, 0, y % 10, y // 10, mo % 10, mo // 10, d % 10, d // 10]   # data, SMPTE 309M
    bits = [0] * 80

    def put(pos, val, k):
        for j in range(k):
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
    # Bit de correccao de polaridade: numero par de zeros por frame. Fica no
    # bit 59 a 25 fps e no 27 a 24/30.
    pb = 59 if fps == 25 else 27
    if bits.count(0) % 2:
        bits[pb] = 1
    return bits


class _Encoder:
    """Bi-phase mark, estereo: LTC a esquerda (ponta), silencio a direita."""

    def __init__(self, fps, level):
        self.fps = fps
        self.spb = RATE // fps // 80          # amostras por bit (24 a 25 fps)
        amp = int(32767 * level)
        # Uma amostra estereo ja intercalada: (esquerda, direita=0).
        self.hi = array.array("h", [amp, 0]).tobytes()
        self.lo = array.array("h", [-amp, 0]).tobytes()
        self.state = 1

    def frame(self, n, date, adjust=0):
        """Bytes de um frame. `adjust` (-1, 0, +1) encurta ou alonga o frame
        uma amostra, no bit 64 (um zero da palavra de sincronismo)."""
        out = []
        spb, half = self.spb, self.spb // 2
        for i, b in enumerate(_bits(n, self.fps, date)):
            self.state = -self.state
            a, z = (self.hi, self.lo) if self.state > 0 else (self.lo, self.hi)
            if b:
                out.append(a * half + z * (spb - half))
                self.state = -self.state
            else:
                out.append(a * (spb + adjust if i == 64 else spb))
        return b"".join(out)


def _tc(n, fps):
    n %= 86400 * fps
    return "%02d:%02d:%02d:%02d" % (n // (fps * 3600), n // (fps * 60) % 60, n // fps % 60, n % fps)


class Generator:
    """O jam: set_running(True) abre a saida e poe o LTC a andar; set_running(False)
    para-o e fecha a saida. A mesma interface de antes para o f6_ctl.

    secs_at(when) -> segundos do dia em TC nesse instante, ou None sem jam.
    fps() -> fps do jam. log(msg) -> event log do bridge.
    """

    def __init__(self, secs_at, fps, log):
        self._secs_at, self._fps, self._log = secs_at, fps, log
        self._lock = threading.Lock()
        self._want = False
        self._running = False
        self._label = None
        self._since = None
        self._error = None
        self._fps_now = None
        self._thread = None

    # --- API para o bridge -------------------------------------------------
    def start(self):
        """Nada a fazer: a saida so abre no jam."""

    def ready(self):
        """(pronto, porque-nao). Pronto = ha um LTC valido no bridge."""
        try:
            if self._secs_at() is None:
                return False, "no valid LTC jam (GET LTC first)"
        except Exception as e:
            return False, str(e)
        return True, None

    def set_running(self, on):
        """on: abre a saida e poe o LTC a andar; off: para e fecha. (ok, erro)."""
        if on:
            ok, why = self.ready()
            if not ok:
                return False, why
        with self._lock:
            self._want = bool(on)
            if on and not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, daemon=True, name="f6-jam")
                self._thread.start()
        return True, None

    def running(self):
        return self._running

    def output_open(self):
        return bool(self._thread and self._thread.is_alive())

    def pause(self, on):
        """O leitor do cartao pede a saida (on) ou devolve-a. Fora do jam ela ja
        esta livre; durante o jam recusa-se. (ok, erro)."""
        if on and self.output_open():
            return False, "the F6 timecode jam is using the audio output: wait a few seconds"
        return True, None

    def status(self):
        ok, why = self.ready()
        lab = self._label
        return {"ready": ok, "why": why, "running": self._running,
                "requested": self._want, "output_open": self.output_open(),
                "tc": _tc(lab, self._fps_now) if lab is not None and self._fps_now else None,
                "since": self._since, "device": DEVICE, "error": self._error}

    # --- a thread do jam -------------------------------------------------------
    def _run(self):
        try:
            self._stream()
            self._error = None
        except Exception as e:
            self._error = str(e)
            self._log(f"F6 jam: timecode output failed: {e}")
        finally:
            self._running = False
            with self._lock:
                self._want = False

    def _stream(self):
        import fcntl
        fps = int(self._fps() or 25)
        p = subprocess.Popen(["aplay", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(RATE),
                              "-c", "2", "-t", "raw", "--buffer-time=100000", "-"],
                             stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            try:
                fcntl.fcntl(p.stdin.fileno(), 1031, 4096)      # F_SETPIPE_SZ: latencia estavel
            except OSError:
                pass
            enc = _Encoder(fps, LEVEL)
            self._fps_now = fps
            # O primeiro frame comeca numa fronteira de frame do TC: assim a
            # correccao de fase so tem de seguir a deriva.
            x = self._secs_at(time.time() + LATENCY)
            if x is not None:
                time.sleep(((-x * fps) % 1) / fps)
            ema, label = 0.0, None
            pre = round(PREROLL * fps)
            fase, falta = "preroll", pre
            while True:
                if p.poll() is not None:
                    err = (p.stderr.read() or b"").decode(errors="replace").strip().splitlines()
                    raise RuntimeError("aplay ended: " + (err[-1] if err else "no message"))
                x = self._secs_at(time.time() + LATENCY)
                if x is None:
                    # Perdeu-se o LTC do bridge a meio (relogio saltou, CLEAR LTC).
                    self._log("F6 jam: no valid LTC in the bridge any more -- stopped")
                    return
                xf = x * fps
                nearest = round(xf)
                err = xf - nearest              # >0: o frame vai comecar depois da fronteira
                ema = err if abs(err - ema) > 0.25 else ema * 0.96 + err * 0.04
                amostras = ema * RATE / fps
                # Atrasado -> encurta este frame (os seguintes chegam uma amostra
                # antes); adiantado -> alonga. A media ja conta com a correccao.
                adjust = -1 if amostras > DEADBAND else (1 if amostras < -DEADBAND else 0)
                ema += adjust * fps / RATE
                with self._lock:
                    want = self._want
                if not want:
                    return                      # fim do jam: o finally fecha a saida
                if label is None:
                    # Parado no valor exacto do primeiro frame a andar, que soa
                    # daqui a `pre` frames.
                    label = nearest + pre
                    self._running, self._since = True, time.time() + PREROLL
                    if not pre:
                        fase = "run"
                elif fase == "preroll":
                    falta -= 1
                    if falta <= 0:
                        fase = "run"            # este frame e o primeiro a andar
                else:
                    label += 1
                    # A diferenca conta-se a volta do dia: a meia-noite o nearest volta a 0 e o label nao
                    dia = 86400 * fps
                    salto = (nearest - label + dia // 2) % dia - dia // 2
                    if abs(salto) >= RESYNC_FRAMES:
                        self._log(f"F6 jam: timecode realigned by {salto:+d} frames")
                        label = nearest
                    else:
                        label %= dia
                self._label = label
                p.stdin.write(enc.frame(label, time.localtime()[:3], adjust))
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()

"""O bridge a fazer de FRC-8 para o Zoom F6, por USB-MIDI (sem o comando real).

O F6, no menu FRC-8 > connect, apresenta-se como dispositivo USB-MIDI
1686:0351 "F Series MIDI Interface"; o Atom e o host, como o FRC-8. Protocolo
confirmado a 2026-10-08 (testes/f6_usb_ltc/f6_frc.py), a partir do emulador publico
howardlin551155/Zoom-FRC-8-Emulator e da issue #2 dele:

  1. bridge -> F0 52 00 74 00 00 00 63 00 01 F7            pedido de ligacao
  2. F6     -> luzes (notas 90) e SysEx 63 10/11/13/16/0D/01/02/0E (estado;
               o 63 01 e o 63 02 sao perguntas). Decifrados pela issue #2 do emulador (F8n Pro): 63 10 = tensao nominal e de
               corte em centivolts (09 30 07 04 = 12,00 V e 9,00 V), 63 11 = tipo de bateria (0 alcalina), 63 13 = brilho do
               LED (3C = 60). Sao DEFINICOES, nao o nivel da bateria, que o FRC-8 nao da.
  3. bridge -> 63 03 01 01 00 00 00 00 00 00 (identificacao; sem ela os menus de versao do gravador ficam presos) e
               63 0F 01 00 09 30 09 30 00   (a tensao de alimentacao do anfitriao em centivolts: 09 30 = 1200 = 12,00 V; o
               63 0F e do anfitriao para o F6; a issue #2 so ve o 63 10 no outro sentido, mas
               este codigo filtra um 63 0F que chegue do F6 (sinal de vida?): por confirmar)
  4. bridge -> o 63 0F de 0,25 em 0,25 s: sem ele o F6 corta a USB ao fim de ~1 s
  Botoes: REC 90 24 7F/00, STOP 90 22 7F/00, PLAY 90 23 7F/00 (carregar/largar),
          USER 1-3 90 10/11/12 (no F6: None, Mark, Key Hold, Clear Clip
          Indicator, Circled)
  Estado: F6 -> 90 29 vel (a gravar), 90 28 (play), 90 00..07 (pistas armadas; cada uma
          vem com 90 18+pista 00, sempre 0: visto a 2026-10-15 pelo gateway S3),
          Dn vel (medidor da pista n; 8 e 9 = saida L/R; 0..12 segmentos)
  Teclado (POR TESTAR no F6; do protocolo do emulador): SCENE NAME 90 14 e
          SCENE INC 90 13; o F6 abre a caixa de texto (63 16 01) e fecha-a
          (63 16 00); as teclas vao como HID dentro de SysEx:
          63 0C 00 [mod] 00 [codigo HID] 00 00 00 00 00, e a largar tudo a zero.
          NOTE 90 15 e TRACK NAME 90 16 abrem os menus respectivos.
  Faders e trim: o F6 NAO os reporta pelo FRC-8, nem em 32-bit float nem em 24-bit com o
          trim a rodar de 12 a 75 (2026-10-15, medido com o F6); escrever (92, E0-E7,
          B0/B2, A2, 82) sem efeito visto. Ao mexer no trim de uma pista o F6 acende
          90 08+pista (e manda o estado das 8 pistas de uma vez: 90 n, 90 18+n, 90 08+n): e a
          pista SELECCIONADA, nao o PFL do emulador (o self.pfl abaixo e isso).
  Mesa (do protocolo do emulador, POR CONFIRMAR no F6; so se le, nunca se
          escreve; o F6 ignora os valores E1/E0 do FRC-8 real: testado a 2026-10-13): faders 92 [pista 0-8] [0-127], botao rotativo 98 [pista] [0-127]
          no modo TRIM (90 26) ou PAN (90 27), PFL 90 08-0F e 8B, armar 8A,
          saida LR/MAIN/SUB 90 10/11/12.

Ordem que funciona: carregar em connect no F6 com o cabo DESLIGADO, e so depois
ligar o cabo ao Atom (com o cabo ja ligado o F6 da "Error!"). Isto fica a
espera do F6 sozinho e volta a esperar se a ligacao cair.

Ou pelo gateway S3 no modo F6 (selector do firmware gopro_gw, 2026-10-15): o
S3 faz de FRC-8 (handshake e sinal de vida ficam nele) e o bridge fala com ele
pelo HTTP de sempre, com o mesmo endereco e o mesmo token: GET /api/f6 (estado,
5x por segundo), POST /api/f6/press (botoes) e /api/f6/midi (teclado, menu). Estas rotas sao as do
FIRMWARE do S3 (gopro_gw.ino), nao as do bridge: nao seguem a API /api/devices/<id>/...
Com o S3, a ordem e a contraria: o cabo do F6 ao S3 primeiro, depois connect.

So biblioteca padrao: o no ALSA /dev/snd/midiC*D* directamente.
"""
import collections
import glob
import json
import os
import re
import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

NOME = "F Series"
HDR = bytes([0xF0, 0x52, 0x00, 0x74, 0x00, 0x00, 0x00, 0x63])
HANDSHAKE = HDR + bytes([0x00, 0x01, 0xF7])
IDENT = HDR + bytes([0x03, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF7])
POWER = HDR + bytes([0x0F, 0x01, 0x00, 0x09, 0x30, 0x09, 0x30, 0x00, 0xF7])
NOTE_REC, NOTE_STOP, NOTE_PLAY = 0x24, 0x22, 0x23
NOTE_USER = {1: 0x10, 2: 0x11, 3: 0x12}     # USER 1-3 (a funcao escolhe-se no F6)
# Medidores: o F6 manda o numero de segmentos acesos do FRC-8, nao 0..127:
# 0..12, e 12 e o vermelho do F6 (visto no aparelho a 2026-10-08).
METER_MAX = 12
# O protocolo e o do FRC-8 (8 pistas); o F6 so tem 6 entradas.
TRACKS = int(os.environ.get("F6_TRACKS", "6"))
NOTE_SCENE_INC, NOTE_SCENE_NAME, NOTE_NOTE, NOTE_TRACK_NAME = 0x13, 0x14, 0x15, 0x16
NOTE_OK = 0x21         # carregar na roda do menu (OK)
HID_ENTER, HID_BACKSPACE, HID_SHIFT = 0x28, 0x2A, 0x02
PERIOD = 0.25          # 63 0F e FE
REPLY_AFTER = 0.10     # o F6 espera ~0,5 s pela identificacao
STATE_WAIT = 2.0       # s a espera do estado das pistas antes de o pedir outra vez
PEAK_HOLD = 1.0       # medidores: pico seguro este tempo (s)
GW_POLL = 0.2         # o estado do F6 no gateway S3 (s)
GW_FALHAS = 10        # pedidos seguidos sem resposta do S3 (2 s): a sessao cai


def hid(ch):
    """(modificador, codigo HID) de um caracter, ou None se nao houver tecla."""
    if "a" <= ch <= "z":
        return 0, 0x04 + ord(ch) - ord("a")
    if "A" <= ch <= "Z":
        return HID_SHIFT, 0x04 + ord(ch) - ord("A")
    if "1" <= ch <= "9":
        return 0, 0x1E + ord(ch) - ord("1")
    # Os caracteres que o F6 aceita (manual, p. 14), num teclado US (SYSTEM > USB >
    # FRC-8 > Keyboard Type). Nao ha "." nem "/"; espaco e @ nao no inicio.
    S = HID_SHIFT
    return {"0": (0, 0x27), " ": (0, 0x2C), "-": (0, 0x2D), "_": (S, 0x2D),
            "!": (S, 0x1E), "@": (S, 0x1F), "#": (S, 0x20), "$": (S, 0x21), "^": (S, 0x23),
            "(": (S, 0x26), ")": (S, 0x27), "=": (0, 0x2E), "+": (S, 0x2E),
            "[": (0, 0x2F), "]": (0, 0x30), "{": (S, 0x2F), "}": (S, 0x30),
            ";": (0, 0x33), "'": (0, 0x34), "`": (0, 0x35), ",": (0, 0x36)}.get(ch)


def find_node():
    """(no rawmidi, nome) do F6 em modo FRC, ou None."""
    try:
        cards = open("/proc/asound/cards").read()
    except OSError:
        return None
    for m in re.finditer(r"^\s*(\d+)\s+\[[^\]]*\]:\s*(.*)\n\s*(.*)$", cards, re.M):
        if NOME in m.group(2) or NOME in m.group(3):
            nos = sorted(glob.glob("/dev/snd/midiC%sD*" % m.group(1)))
            if nos:
                return nos[0], m.group(2).strip()
    return None


class Link:
    """Ligacao FRC-8 ao F6, numa thread propria.

    log(msg): event log do bridge. on_rec_change(a_gravar): chamada quando o F6
    muda de estado de gravacao (comandado ou nao: quem chama decide).
    """

    def __init__(self, log, on_rec_change=None):
        self.log = log
        self.on_rec_change = on_rec_change
        self._lock = threading.Lock()
        self._fd = None
        self.connected = False
        self.recording = None          # None = ainda nao sei
        self.rec_since = None          # quando o F6 disse que comecou a gravar
        self.playing = None
        self.arms = [False] * 8
        self.arms_known = False        # o F6 ja disse quais pistas estao armadas (senao "armed" vai None)
        self.meters = [0] * 10
        self._peaks = [(0, 0.0)] * 10
        self.since = None
        self.last_rx = 0.0
        self.error = None
        self.kbd_open = False          # a caixa de texto do F6 esta aberta (63 16 01)
        self.on_clip = None            # on_clip(pista 1-6): uma pista armada chegou ao vermelho
        self.rx_log = collections.deque(maxlen=120)     # o que o F6 manda, sem os medidores (para descobrir o protocolo)
        self.via = None                # "usb" (cabo no Atom) ou "s3" (o gateway S3 no modo F6), com a sessao aberta
        self.gw_f6 = None              # gw_f6() -> (host, nome) do S3 no modo F6, ou None (o bridge liga)
        self.gw_cab = None             # gw_cab() -> cabecalhos dos pedidos ao S3 (o token; o bridge liga)
        self._gw_host = None
        self._mesa_reset()

    def _mesa_reset(self):
        """O que o F6 diz da mesa. None = ainda nao disse nada."""
        self.faders = [None] * 9       # 1-8 + master, 0-127
        self.knob_mode = None          # "trim" / "pan" (o modo do botao rotativo)
        self.trim = [None] * 8
        self.pan = [None] * 8          # 0-127, 64 = centro
        self.pfl = [False] * 8
        self.output = None             # "LR" / "MAIN" / "SUB"
        self.clips = [0] * 8           # vezes que cada pista chegou ao vermelho nesta take
        self._clip_on = [False] * 8

    # --- API ------------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="frc8").start()

    def press(self, note):
        """Carrega e larga um botao, como no FRC-8. (ok, erro)."""
        if not self.connected:
            return False, "F6 not connected (FRC)"
        # PLAY com o F6 a gravar poe uma marca na take e deixa a faixa em pausa: nunca se envia (nenhuma rota do Atom o faz,
        # fica aqui por se vier a haver uma).
        if note == NOTE_PLAY and self.recording:
            return False, "the F6 is recording: PLAY would add a mark and pause the track"
        if self.via == "s3":
            # Carregar e largar no proprio S3 (dois pedidos HTTP com 80 ms entre eles nao sao de confianca).
            try:
                self._gw("POST", "/api/f6/press", {"note": "%02x" % note})
            except OSError as e:
                return False, "F6 via the S3: %s" % e
            return True, None
        try:
            self._send(bytes([0x90, note, 0x7F]))
            time.sleep(0.08)
            self._send(bytes([0x90, note, 0x00]))
        except OSError as e:
            return False, "F6 link lost: %s" % e
        return True, None

    def key(self, mod, code):
        """Uma tecla do teclado USB do FRC-8: carregar e largar."""
        self._send(HDR + bytes([0x0C, 0x00, mod, 0x00, code, 0, 0, 0, 0, 0, 0xF7]))
        time.sleep(0.03)
        self._send(HDR + bytes([0x0C, 0x00, 0, 0x00, 0, 0, 0, 0, 0, 0, 0xF7]))
        time.sleep(0.03)

    def menu_up(self, n=1):
        """A roda do menu do FRC-8 para cima (o botao "up" do emulador: B0 3C 41)."""
        for _ in range(n):
            self._send(bytes([0xB0, 0x3C, 0x41]))
            time.sleep(0.05)
            self._send(bytes([0xB0, 0x3C, 0x00]))
            time.sleep(0.12)

    def menu_down(self, n=1):
        """A roda do menu do FRC-8 para baixo (B0 3C 01)."""
        for _ in range(n):
            self._send(bytes([0xB0, 0x3C, 0x01]))
            time.sleep(0.05)
            self._send(bytes([0xB0, 0x3C, 0x00]))
            time.sleep(0.12)

    def wait_kbd(self, state, timeout):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.kbd_open == state:
                return True
            time.sleep(0.02)
        return False

    def wait_rec(self, state, timeout):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.recording == state:
                return True
            time.sleep(0.02)
        return False

    def status(self):
        return {"connected": self.connected, "recording": self.recording, "rec_since": self.rec_since,
                "playing": self.playing,
                "armed": [i + 1 for i, a in enumerate(self.arms[:TRACKS]) if a] if self.arms_known else None,
                "meters": self.peaks(), "meter_max": METER_MAX, "tracks": TRACKS,
                "kbd_open": self.kbd_open, "since": self.since,
                "faders": self.faders[:TRACKS] + [self.faders[8]], "knob_mode": self.knob_mode,
                "trim": self.trim[:TRACKS], "pan": self.pan[:TRACKS],
                "pfl": [i + 1 for i, a in enumerate(self.pfl[:TRACKS]) if a], "output": self.output,
                "clips": self.clips[:TRACKS], "rx_log": list(self.rx_log),
                "rx_age": round(time.time() - self.last_rx, 1) if self.last_rx else None,
                "error": self.error, "via": self.via, "gateway": self._gw_host if self.via == "s3" else None}

    # --- a thread -------------------------------------------------------------
    def _send(self, b):
        if self.via == "s3":
            self._gw("POST", "/api/f6/midi", {"hex": b.hex(" ")})
            return
        with self._lock:
            if self._fd is None:
                raise OSError("no F6")
            # O no e O_NONBLOCK: com o buffer cheio espera-se um pouco em vez de
            # deitar a sessao abaixo (EAGAIN nao e o F6 a sair).
            fim = time.monotonic() + 0.5
            while b:
                try:
                    b = b[os.write(self._fd, b):]
                except BlockingIOError:
                    if time.monotonic() > fim:
                        raise
                    select.select([], [self._fd], [], 0.05)

    def _loop(self):
        while True:
            node = find_node()
            gw = None if node else self._gw_pronto()
            if not node and not gw:
                time.sleep(0.2)
                continue
            try:
                if node:
                    self._session(*node)
                else:
                    self._session_gw(*gw)
            except OSError as e:
                self.error = str(e)
            except Exception as e:      # nunca deixar a thread morrer
                self.error = repr(e)
                self.log("F6 (FRC): %r" % e)
            if self.connected:
                self.log("F6 disconnected (%s)" % ("FRC-8 link" if node else "S3 %s: %s" % (gw[1], self.error)))
            self.connected = False
            self.via = None
            if not node:
                time.sleep(1)
                continue
            with self._lock:
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
            # Normalmente o F6 saiu da USB e o no desaparece. Se continuar la
            # (erro de escrita/leitura com o F6 ainda ligado), tenta outra vez
            # ao fim de 3 s em vez de ficar a espera de um cabo desligado.
            fim = time.monotonic() + 3
            while find_node() and time.monotonic() < fim:
                time.sleep(0.2)

    def _session(self, path, name):
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        with self._lock:
            self._fd = fd
        self.via = "usb"
        self.recording = self.playing = self.rec_since = None
        self.arms = [False] * 8
        self.arms_known = False
        self.kbd_open = False
        self._mesa_reset()
        self.error = None
        self._send(HANDSHAKE)
        t0 = time.monotonic()
        replied, last, repetido = False, 0.0, False
        sysex, cur, status = None, b"", None
        while True:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                data = os.read(fd, 512)            # ENODEV quando o F6 sai
                if not data:                       # fim do no sem erro: nao ficar a rodar no select
                    raise OSError("F6 link closed")
                self.last_rx = time.time()
                for x in data:
                    if x >= 0xF8:
                        continue
                    if sysex is not None:
                        sysex += bytes([x])
                        if x == 0xF7:
                            self._sysex(sysex)
                            sysex = None
                        elif len(sysex) > 4096:
                            sysex = None
                        continue
                    if x == 0xF0:
                        sysex = b"\xf0"
                        continue
                    if x & 0x80:
                        status, cur = x, bytes([x])
                    elif status is not None:
                        if not cur:
                            cur = bytes([status])
                        cur += bytes([x])
                    else:
                        continue
                    if len(cur) == (2 if (status & 0xF0) in (0xC0, 0xD0) else 3):
                        self._msg(cur)
                        cur = b""
            now = time.monotonic()
            if not replied and now - t0 >= REPLY_AFTER:
                self._send(IDENT)
                self._send(POWER)
                replied, last = True, now
                self.connected, self.since = True, time.time()
                self.log("F6 connected (FRC-8 link via %s)" % path)
            # O F6 que ja estava em sessao (o bridge reiniciou com o cabo ligado) nao manda o estado ao ligar:
            # as pistas armadas ficavam por saber. Pede-se o arranque outra vez, uma vez; se nem assim, ficam
            # "desconhecidas" (armed None) ate o F6 as mostrar, em vez de "nenhuma armada".
            if replied and not repetido and not self.arms_known and now - t0 >= STATE_WAIT:
                repetido = True
                self.log("F6: no track state at connect, asking again")
                self.rx_log.append((round(time.time(), 2), "TX handshake again"))
                self._send(HANDSHAKE)
                self._send(IDENT)
                self._send(POWER)
            if replied and now - last >= PERIOD:
                self._send(POWER)
                self._send(b"\xfe")
                last = now

    # --- pelo gateway S3 (modo F6) ------------------------------------------------
    def _gw_pronto(self):
        try:
            return self.gw_f6() if self.gw_f6 else None
        except Exception:                       # pylint: disable=broad-except
            return None

    def _gw(self, metodo, caminho, args=None, timeout=2.0):
        """Um pedido ao S3 do F6: o dict da resposta, ou OSError."""
        host = self._gw_host
        if not host:
            raise OSError("no F6")
        url = "http://%s%s" % (host, caminho)
        if args:
            url += "?" + urllib.parse.urlencode(args)
        cab = self.gw_cab() if (self.gw_cab and metodo == "POST") else {}      # o token so nos POST (o GET /api/f6 a 5 Hz nao o leva)
        req = urllib.request.Request(url, method=metodo, data=b"" if metodo == "POST" else None, headers=cab)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except ValueError:
                d = {}
            raise OSError(d.get("error") or "HTTP %d" % e.code)
        except ValueError:
            raise OSError("bad answer from the S3")
        if not isinstance(d, dict) or not d.get("ok"):
            raise OSError((d or {}).get("error") or "the S3 refused")
        return d

    def _session_gw(self, host, nome):
        self._gw_host = host
        self.recording = self.playing = self.rec_since = None
        self.arms = [False] * 8
        self.arms_known = False
        self.kbd_open = False
        self._mesa_reset()             # a mesa (faders, pan, trim) nao vem pelo S3: fica por saber
        self.error = None
        falhas, ligado = 0, False
        while True:
            t = time.monotonic()
            try:
                f = self._gw("GET", "/api/f6", timeout=1.5).get("f6") or {}
                falhas = 0
            except OSError as e:
                falhas += 1
                if falhas >= GW_FALHAS:
                    raise OSError("the S3 stopped answering (%s)" % e)
                time.sleep(GW_POLL)
                continue
            if not f.get("connected"):
                if ligado:
                    raise OSError("the F6 left the S3 (USB)")
                raise OSError("F6 not connected to the S3")
            if not ligado:
                ligado = True
                self.via = "s3"
                self.connected, self.since = True, time.time()
                self.log("F6 connected (FRC-8 via the S3 gateway %s at %s)" % (nome, host))
            self.last_rx = time.time()
            self._gw_estado(f)
            if find_node():
                raise OSError("the F6 cable is on the Atom now")
            time.sleep(max(0.0, GW_POLL - (time.monotonic() - t)))

    def _gw_estado(self, f):
        """O estado do F6 que o S3 da -> as mesmas mensagens que viriam pela USB (so as que mudaram)."""
        armed = f.get("armed")
        if isinstance(armed, list):
            for i in range(8):
                on = (i + 1) in armed
                if not self.arms_known or self.arms[i] != on:
                    self._msg(bytes([0x90, i, 0x7F if on else 0]))
        rec = f.get("recording")
        if rec is not None and rec != self.recording:
            self._msg(bytes([0x90, 0x29, 0x7F if rec else 0]))
        if rec and f.get("rec_ms") is not None:
            self.rec_since = time.time() - f["rec_ms"] / 1000.0
        play = f.get("playing")
        if play is not None and play != self.playing:
            self._msg(bytes([0x90, 0x28, 0x7F if play else 0]))
        for n, v in enumerate((f.get("meters") or [])[:10]):
            if isinstance(v, int):
                self._msg(bytes([0xD0 | n, max(0, min(127, v))]))
        fad = f.get("faders")
        if isinstance(fad, list) and len(fad) == 9:            # firmware de 2026-10-15+: o que o F6 reporta (92 nn vv)
            self.faders = [v if isinstance(v, int) else None for v in fad]
        k = bool(f.get("kbd_open"))
        if k != self.kbd_open:
            self.kbd_open = k
            self.log("F6: text box %s" % ("open" if k else "closed"))

    def _msg(self, m):
        st = m[0]
        if st & 0xF0 != 0xD0:
            self.rx_log.append((round(time.time(), 2), m.hex(" ")))
        if st == 0x90:
            note, on = m[1], m[2] > 0
            if note == 0x29:
                antes, self.recording = self.recording, on
                if on and not antes:
                    self.rec_since = time.time()
                    self.clips = [0] * 8          # os clips contam-se por take
                elif not on:
                    self.rec_since = None
                if antes is not None and antes != on and self.on_rec_change:
                    try:
                        self.on_rec_change(on)
                    except Exception as e:
                        self.log("F6 (FRC) callback: %r" % e)
            elif note == 0x28:
                self.playing = on
            elif note < 8:
                self.arms[note] = on
                self.arms_known = True
            elif note < 0x10:
                self.pfl[note - 8] = on
            elif note in (0x10, 0x11, 0x12) and on:
                self.output = ("LR", "MAIN", "SUB")[note - 0x10]
            elif note in (0x26, 0x27) and on:
                self.knob_mode = "trim" if note == 0x26 else "pan"
        elif st == 0x92 and m[1] < 9:
            self.faders[m[1]] = m[2]
        elif st == 0x98 and m[1] < 8:
            # O valor do botao rotativo e do modo que estiver activo.
            if self.knob_mode == "pan":
                self.pan[m[1]] = m[2]
            elif self.knob_mode == "trim":
                self.trim[m[1]] = m[2]
        elif st == 0x8A and m[1] < 8:
            self.arms[m[1]] = m[2] > 0
            self.arms_known = True
        elif st == 0x8B and m[1] < 8:
            self.pfl[m[1]] = m[2] > 0
        elif st & 0xF0 == 0xD0 and (st & 0x0F) < 10:
            n, v, now = st & 0x0F, m[1], time.monotonic()
            self.meters[n] = v
            # Clip: uma pista armada chega ao vermelho a gravar. Conta-se a subida
            # (nao cada mensagem), e o aviso vai uma vez por pista e por take.
            if n < TRACKS:
                no_vermelho = v >= METER_MAX
                if no_vermelho and not self._clip_on[n] and self.arms[n] and self.recording:
                    self.clips[n] += 1
                    if self.clips[n] == 1 and self.on_clip:
                        try:
                            self.on_clip(n + 1)
                        except Exception as e:
                            self.log("F6 (FRC) clip callback: %r" % e)
                self._clip_on[n] = no_vermelho
            # Pico seguro PEAK_HOLD s: quem le de 0,2 em 0,2 s nao perde os picos.
            pv, pt = self._peaks[n]
            if v >= pv or now - pt > PEAK_HOLD:
                self._peaks[n] = (v, now)

    def _sysex(self, b):
        """Do estado em SysEx so interessa a caixa de texto (63 16 01/00)."""
        if b[8:9] != b"\x0f":
            self.rx_log.append((round(time.time(), 2), b[:24].hex(" ")))
        if len(b) >= 11 and b[:8] == HDR and b[8] == 0x16:
            aberta = b[9] == 0x01
            if aberta != self.kbd_open:
                self.kbd_open = aberta
                self.log("F6: text box %s" % ("open" if aberta else "closed"))

    def peaks(self):
        now = time.monotonic()
        return [pv if now - pt <= PEAK_HOLD else self.meters[i]
                for i, (pv, pt) in enumerate(self._peaks)]

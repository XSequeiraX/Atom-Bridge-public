#!/usr/bin/env python3
"""Dispositivos USB class compliant no bridge: MIDI, audio (UAC), HID e video (UVC) (2026-10-13).

Servico a parte (bridge-usb.service): fala com o bridge so pela API HTTP (bridge_client). Se cair,
o bridge nao da por isso.

Detecao: le o /sys/bus/usb de 2 em 2 s (sem dependencias). Cada dispositivo e reconhecido pelo fabricante,
modelo e numero de serie (vid:pid:serie), por isso o mapeamento acompanha-o em qualquer porta USB.
  MIDI  (audio, subclasse 3)  le notas, CC e program change do /dev/snd/midiC*D*; acende LEDs com o estado
  HID   (classe 3)            le teclas do /dev/input/event* (pedais, keypads); um HID mapeado deixa de
                              escrever na consola (EVIOCGRAB), os outros ficam como estavam
  AUDIO (classe 1)            detetado e listado; recebe um papel (ltc_in, audio_out) que fica guardado
  VIDEO (classe 14, UVC)      detetado e listado; recebe um papel (video_return) que fica guardado
Os papeis de audio e video ficam guardados mas o bridge ainda nao os usa (o LTC de uma placa USB precisa do
codigo do LTC, que so existe no bridge: issue #8).

ACCOES de um botao/nota/tecla (so ao carregar; o largar e ignorado):
  rec                REC/STOP dos alvos (video e audio)
  target:<id>        arma/desarma um device        target:slot:<n>    o mesmo pelo slot (ordem do ecra)
  devrec:<id>        REC/STOP so desse device      devrec:slot:<n>
  targets:all  targets:none
  mark:good  mark:no-good  mark:mark
LEDs (so MIDI): a nota/CC de uma accao acende quando o que ela controla esta ligado (rec: a gravar; target: armado;
devrec: esse device a gravar). Velocidades por dispositivo: "led_on" (127) e "led_off" (0); no APC40, 1 verde, 3 vermelho.

API local (so 127.0.0.1:8091, para a consola):
  GET  /devices         presentes e guardados, com mapeamentos, papel e o ultimo evento (para aprender)
  GET  /actions         as accoes possiveis agora (pelos devices do bridge), os nomes do ecra por slot e por id (labels)
  POST /map    {"key", "event", "action"}     ("action": "" tira)
  POST /role   {"key", "role"}               POST /enable {"key", "enabled"}     POST /forget {"key"}
  POST /learn  {"key", "secs"}   a consola esta a aprender um botao (secs 0: acabou): os botoes desse device nao fazem a
                                 sua accao e um HID e agarrado, para a tecla nao chegar a consola
Guardado em usb_map.json (ao lado deste ficheiro).
"""
import glob
import json
import os
import re
import select
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge_client as bc
from vigiado import vigiado
import gravar

try:
    import fcntl                    # Linux (o Atom); sem ele os HID nao se agarram (EVIOCGRAB)
except ImportError:
    fcntl = None

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(AQUI, "usb_map.json")
SYS_USB = "/sys/bus/usb/devices"
ITF_SEP = ":"               # as interfaces sao "<dispositivo>:<config>.<n>" no sysfs
PORTA_API = 8091
SCAN_S = 2.0                # de quanto em quanto se procura dispositivos novos
ESTADO_S = 0.25             # de quanto em quanto se le o bridge (para os LEDs)
REPIQUE_S = 0.3             # o mesmo evento outra vez dentro disto e ignorado (contactos que saltam)
EVIOCGRAB = 0x40044590
APRENDER_S = 25.0           # quanto tempo vale um /learn sem ser renovado (a consola caiu a meio)
PAPEIS = {"audio": ["", "ltc_in", "audio_out"], "video": ["", "video_return"]}
CLASSES = {"midi": "MIDI", "audio": "AUDIO", "hid": "HID", "video": "VIDEO"}

# Teclas com nome (linux/input-event-codes.h); as outras aparecem pelo codigo.
TECLAS = {1: "ESC", 14: "BACKSPACE", 15: "TAB", 28: "ENTER", 57: "SPACE", 96: "KPENTER", 102: "HOME",
          103: "UP", 104: "PAGEUP", 105: "LEFT", 106: "RIGHT", 107: "END", 108: "DOWN", 109: "PAGEDOWN",
          110: "INSERT", 111: "DELETE", 164: "PLAYPAUSE", 163: "NEXTSONG", 165: "PREVIOUSSONG", 166: "STOPCD",
          167: "RECORD", 113: "MUTE", 114: "VOLUMEDOWN", 115: "VOLUMEUP"}
TECLAS.update({c: k for c, k in zip(range(2, 12), "1234567890")})
TECLAS.update({c: k for c, k in zip(list(range(16, 26)) + list(range(30, 39)) + list(range(44, 51)),
                                     "QWERTYUIOPASDFGHJKLZXCVBNM")})
TECLAS.update({59 + i: "F%d" % (i + 1) for i in range(10)})
TECLAS.update({87: "F11", 88: "F12"})
TECLAS.update({183 + i: "F%d" % (13 + i) for i in range(12)})        # F13-F24: comuns em pedais
TECLAS.update({c: "KP%s" % k for c, k in zip((82, 79, 80, 81, 75, 76, 77, 71, 72, 73), "0123456789")})


def nome_tecla(code):
    return TECLAS.get(code, str(code))


# ---- detecao (sysfs) ----
def _ler(p, defeito=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return defeito


# Fabricantes cujos aparelhos sao do proprio bridge e nunca se abrem aqui: a Zoom (o F6 pelo FRC-8, frc8.py). Com
# o F6 aberto aqui tambem, este servico lia o estado das pistas que o F6 manda ao ligar e o bridge nunca o via.
DO_BRIDGE = {"1686"}


def _eventos_hid(itf):
    """Os /dev/input/event* de uma interface HID. O sysfs tem ligacoes que voltam ao principio (driver, subsystem): um
    glob com "**" segue-as e nao acaba (ou demora minutos); aqui nao se segue nenhuma e so se desce ate 5 niveis."""
    out = []
    for raiz, dirs, _f in os.walk(itf):
        if raiz[len(itf):].count(os.sep) >= 5:
            dirs[:] = []
        out += [x for x in dirs if x.startswith("event") and x[5:].isdigit()
                and not os.path.islink(os.path.join(raiz, x))]
    return sorted(set(out), key=lambda x: int(x[5:]))


def detetar(base=SYS_USB):
    """{chave: {name, classes, nodes, vid, pid, serial, path}} dos dispositivos MIDI/audio/HID/video ligados."""
    out = {}
    for d in sorted(glob.glob(os.path.join(base, "*"))):
        if not os.path.isfile(os.path.join(d, "idVendor")):
            continue                        # interfaces e raizes: so os dispositivos
        vid, pid = _ler(os.path.join(d, "idVendor")), _ler(os.path.join(d, "idProduct"))
        if vid.lower() in DO_BRIDGE:
            continue                        # o bridge ja fala com ele: abrir a porta aqui roubava-lhe as mensagens
        serial = _ler(os.path.join(d, "serial"))
        classes, nodes = set(), {}
        for itf in glob.glob(d + ITF_SEP + "*"):
            cls = _ler(os.path.join(itf, "bInterfaceClass")).lower()
            sub = _ler(os.path.join(itf, "bInterfaceSubClass")).lower()
            if cls == "01" and sub == "03":
                classes.add("midi")
                for m in glob.glob(os.path.join(itf, "sound", "card*", "midiC*D*")):
                    nodes.setdefault("midi", "/dev/snd/" + os.path.basename(m))
            elif cls == "01":
                classes.add("audio")
                for c in glob.glob(os.path.join(itf, "sound", "card*")):
                    nodes.setdefault("audio", "hw:" + os.path.basename(c)[4:])
            elif cls == "03":
                classes.add("hid")
                for e in _eventos_hid(itf):
                    nodes.setdefault("hid", []).append("/dev/input/" + e)
            elif cls == "0e":
                classes.add("video")
                for v in glob.glob(os.path.join(itf, "video4linux", "video*")):
                    nodes.setdefault("video", "/dev/" + os.path.basename(v))
        if not classes:
            continue
        if "midi" in classes:
            classes.discard("audio")        # um controlador MIDI vem dentro de uma interface de audio
            if "midi" not in nodes:         # o cartao de som fica na 1.a interface (AudioControl), nao na do MIDIStreaming
                for m in sorted(glob.glob(os.path.join(d + ITF_SEP + "*", "sound", "card*", "midiC*D*"))):
                    nodes.setdefault("midi", "/dev/snd/" + os.path.basename(m))
        chave = "%s:%s:%s" % (vid, pid, serial or os.path.basename(d))
        nome = " ".join(x for x in (_ler(os.path.join(d, "manufacturer")), _ler(os.path.join(d, "product"))) if x)
        out[chave] = {"name": nome or "%s:%s" % (vid, pid), "classes": sorted(classes), "nodes": nodes,
                      "vid": vid, "pid": pid, "serial": serial, "path": os.path.basename(d)}
    return out


# ---- MIDI ----
class MidiParser:
    """Bytes de MIDI -> eventos ("note:<canal>:<nota>", carregado?). Com running status; ignora tempo-real e sysex."""

    def __init__(self):
        self.status, self.dados, self.sysex = 0, [], False

    def alimentar(self, bs):
        out = []
        for b in bs:
            if b >= 0xF8:
                continue                    # clock, start, stop... (tempo real): pode vir a meio de tudo
            if self.sysex:                  # dentro de um sysex: ignora ate ao F7 (ou ate um status novo)
                if b < 0x80:
                    continue
                self.sysex = False
                if b == 0xF7:
                    continue
            if b == 0xF0:
                self.sysex, self.status = True, 0
                continue
            if b >= 0x80:
                # canal (80-EF): novo running status; sistema (F1-F7): acaba com o running status
                self.status, self.dados = (b if b < 0xF0 else 0), []
                continue
            if not self.status:
                continue
            self.dados.append(b)
            tipo, canal = self.status & 0xF0, (self.status & 0x0F) + 1
            precisa = 1 if tipo in (0xC0, 0xD0) else 2
            if len(self.dados) < precisa:
                continue
            d = self.dados
            self.dados = []
            if tipo == 0x90:
                out.append(("note:%d:%d" % (canal, d[0]), d[1] > 0))
            elif tipo == 0x80:
                out.append(("note:%d:%d" % (canal, d[0]), False))
            elif tipo == 0xB0:
                out.append(("cc:%d:%d" % (canal, d[0]), d[1] >= 64))
            elif tipo == 0xC0:
                out.append(("pc:%d:%d" % (canal, d[0]), True))
        return out


def midi_led(evento, ligado, on=127, off=0):
    """Os bytes para acender/apagar o LED de uma nota ou CC (o que os controladores class compliant fazem)."""
    p = evento.split(":")
    if len(p) != 3 or p[0] not in ("note", "cc") or not (p[1].isdigit() and p[2].isdigit()):
        return b""                          # "key:F13" (um HID no mesmo device) e afins: sem LED
    tipo, canal, n = p
    c = (int(canal) - 1) & 0x0F
    v = on if ligado else off
    if tipo == "note":
        return bytes([0x90 | c, int(n) & 0x7F, v & 0x7F])
    if tipo == "cc":
        return bytes([0xB0 | c, int(n) & 0x7F, v & 0x7F])
    return b""


# ---- HID (evdev) ----
EV_FMT = "llHHi"            # struct input_event (timeval, type, code, value)
EV_TAM = struct.calcsize(EV_FMT)


def hid_eventos(dados):
    """Bytes do /dev/input/event* -> eventos ("key:<nome>", carregado?). So EV_KEY (1); repeticao ignorada."""
    out = []
    for i in range(0, len(dados) - EV_TAM + 1, EV_TAM):
        _s, _us, tipo, code, valor = struct.unpack(EV_FMT, dados[i:i + EV_TAM])
        if tipo == 1 and valor in (0, 1):
            out.append(("key:%s" % nome_tecla(code), valor == 1))
    return out


def _vel(c, nome, defeito):
    """A velocidade do LED de um device (127 / 0 por omissao); um valor que nao e numero conta como o de omissao."""
    try:
        return int(c.get(nome, defeito))
    except (TypeError, ValueError):
        return defeito


# ---- o que se pode guardar no mapa ----
FIXAS = ("rec", "targets:all", "targets:none", "mark:good", "mark:no-good", "mark:mark")


def evento_valido(ev):
    """"note:<canal 1-16>:<0-127>", "cc:..", "pc:..", ou "key:<nome>" (o que o aprender da consola devolve)."""
    p = ev.split(":")
    if len(p) == 2 and p[0] == "key":
        return bool(re.fullmatch(r"[A-Za-z0-9_]+", p[1]))
    return (len(p) == 3 and p[0] in ("note", "cc", "pc") and p[1].isdigit() and p[2].isdigit()
            and 1 <= int(p[1]) <= 16 and int(p[2]) <= 127)


def accao_valida(a):
    """A accao e uma das que o executar() conhece, com a forma certa (um erro de escrita nao fica no mapa a nao fazer nada,
    nem "targets:x" a limpar os alvos todos)."""
    if a in FIXAS:
        return True
    p = a.split(":")
    if p[0] not in ("target", "devrec") or len(p) < 2:
        return False
    if p[1] == "slot":
        return len(p) == 3 and p[2].isdigit() and int(p[2]) >= 1
    return len(p) == 2 and bool(p[1]) and p[1] not in ("all", "none") and not p[1].isdigit()


# ---- o servico ----
class Usb:
    def __init__(self, rig=None):
        self.rig = rig if rig is not None else bc.Rig()
        self.lock = threading.Lock()
        self.cfg = self._cfg()
        self.presentes = {}                 # chave -> detecao
        self.leitores = {}                  # (chave, node) -> thread viva
        self.midi_fd = {}                   # chave -> fd do rawmidi (para os LEDs)
        self.hid_fd = {}                    # (chave, node) -> fd do /dev/input/event* aberto (pedal, teclado)
        self.ultimo = {}                    # chave -> {"event", "at"} (o "aprender" da consola)
        self.repique = {}                   # (chave, evento) -> quando
        self.leds = {}                      # (chave, evento) -> ultimo estado mandado
        self.aprender = {}                  # chave -> ate quando a consola esta a aprender um botao deste device
        self.erros = {}                     # (chave, node) -> ultimo erro de abertura escrito (nao se repete de 2 em 2 s)

    # -- configuracao --
    def _cfg(self):
        try:
            c = gravar.ler_json(CFG_PATH)
            if isinstance(c, dict) and isinstance(c.get("devices"), dict):
                # Um device que nao e um objecto (ficheiro mexido a mao) deitava abaixo a API da consola
                c["devices"] = {k: v for k, v in c["devices"].items() if isinstance(v, dict)}
                for v in c["devices"].values():
                    if not isinstance(v.get("map"), dict):
                        v["map"] = {}
                return c
        except (OSError, ValueError):
            pass
        return {"devices": {}}

    def _guardar(self):
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cfg, f, indent=1)
        gravar.trocar(tmp, CFG_PATH)

    def dev_cfg(self, chave):
        return self.cfg["devices"].setdefault(chave, {"name": "", "enabled": True, "map": {}, "role": ""})

    # -- detecao e leitores --
    def procurar(self, base=SYS_USB):
        novos = detetar(base)
        with self.lock:
            for k, d in novos.items():
                if k not in self.presentes:
                    print("usb: %s (%s) %s" % (d["name"], "/".join(CLASSES[c] for c in d["classes"]), k), flush=True)
                c = self.cfg["devices"].get(k)
                if c is not None and c.get("name") != d["name"]:
                    c["name"] = d["name"]
            for k in set(self.presentes) - set(novos):
                print("usb: %s removed" % self.presentes[k]["name"], flush=True)
                self.midi_fd.pop(k, None)
            self.presentes = novos
        for k, d in novos.items():
            if "midi" in d["classes"] and d["nodes"].get("midi"):
                self._arrancar(k, d["nodes"]["midi"], self._ler_midi)
            for ev in d["nodes"].get("hid") or []:
                self._arrancar(k, ev, self._ler_hid)

    def _arrancar(self, chave, node, alvo):
        t = self.leitores.get((chave, node))
        if t and t.is_alive():
            return
        t = threading.Thread(target=alvo, args=(chave, node), daemon=True)
        self.leitores[(chave, node)] = t
        t.start()

    def _erro_abrir(self, chave, node, e):
        """Escreve o erro uma vez: a procura repete a abertura de 2 em 2 s e enchia o registo."""
        if self.erros.get((chave, node)) != str(e):
            self.erros[(chave, node)] = str(e)
            print("usb: %s: %s" % (node, e), file=sys.stderr, flush=True)

    def _ler_midi(self, chave, node):
        try:
            fd = os.open(node, os.O_RDWR)
        except OSError as e:
            self._erro_abrir(chave, node, e)
            return
        self.erros.pop((chave, node), None)
        self.midi_fd[chave] = fd
        self.leds = {k: v for k, v in self.leds.items() if k[0] != chave}     # religado: manda os LEDs outra vez
        p = MidiParser()
        try:
            while True:
                b = os.read(fd, 64)
                if not b:
                    break
                for ev, carregado in p.alimentar(b):
                    self.evento(chave, ev, carregado)
        except OSError:
            pass
        finally:
            if self.midi_fd.get(chave) == fd:
                self.midi_fd.pop(chave, None)
            os.close(fd)

    def hid_aberto(self):
        """Ha algum leitor HID (pedal, teclado) aberto e mapeado? Um teclado qualquer sem mapa nao obriga a ler o bridge."""
        for (chave, _node) in list(self.hid_fd):
            c = self.cfg["devices"].get(chave) or {}
            if c.get("map") and c.get("enabled", True):
                return True
        return False

    def a_aprender(self, chave):
        return self.aprender.get(chave, 0) > time.time()

    def midi_mapeado(self):
        """Ha algum controlador MIDI aberto, ligado e com botoes mapeados (LEDs a manter)? Sem mapa nao ha nada a acender."""
        for chave in list(self.midi_fd):
            c = self.cfg["devices"].get(chave) or {}
            if c.get("map") and c.get("enabled", True):
                return True
        return False

    def _ler_hid(self, chave, node):
        try:
            fd = os.open(node, os.O_RDONLY)
        except OSError as e:
            self._erro_abrir(chave, node, e)
            return
        self.erros.pop((chave, node), None)
        self.hid_fd[(chave, node)] = fd
        agarrado = False
        try:
            while True:
                c = self.cfg["devices"].get(chave) or {}
                quer = (bool(c.get("map")) and c.get("enabled", True) and c.get("grab", True)) or self.a_aprender(chave)
                if quer != agarrado and fcntl:      # mapeado (ou a aprender): as teclas deixam de chegar a consola
                    try:
                        fcntl.ioctl(fd, EVIOCGRAB, 1 if quer else 0)
                        agarrado = quer
                    except OSError:
                        pass
                if not select.select([fd], [], [], 0.25)[0]:
                    continue                # de 0,25 em 0,25 s revê o agarrar (mapeado, ou a consola a aprender)
                b = os.read(fd, EV_TAM * 16)
                if not b:
                    break
                for ev, carregado in hid_eventos(b):
                    self.evento(chave, ev, carregado)
        except OSError:
            pass
        finally:
            self.hid_fd.pop((chave, node), None)
            os.close(fd)

    # -- eventos e accoes --
    def evento(self, chave, ev, carregado):
        if not carregado:
            return
        agora = time.time()
        self.ultimo[chave] = {"event": ev, "at": agora}
        if agora - self.repique.get((chave, ev), 0) < REPIQUE_S:
            return
        self.repique[(chave, ev)] = agora
        if self.a_aprender(chave):
            return                          # a consola esta a aprender este botao: so o regista (nao faz a accao)
        c = self.cfg["devices"].get(chave) or {}
        accao = (c.get("map") or {}).get(ev)
        if accao and c.get("enabled", True):
            try:
                self.executar(accao)
            except Exception as e:          # pylint: disable=broad-except
                print("usb: %s %s: %s" % (ev, accao, e), file=sys.stderr, flush=True)

    def _alvo(self, partes):
        """O id de device de "<id>" ou "slot:<n>". None se o slot nao existe (ainda): quem chama nao faz nada."""
        if partes and partes[0] == "slot":
            if len(partes) > 1 and partes[1].isdigit():
                return self.rig.slot(int(partes[1]))
            return None
        return ":".join(partes) or None

    def executar(self, accao):
        p = accao.split(":")
        if p[0] in ("target", "devrec", "targets"):
            self.rig.ler_se_velho()         # accao antes do primeiro ciclo (ou com o ciclo parado): le os devices agora
        if p[0] == "rec":
            self._resultado(accao, self.rig.rec())
        elif p[0] in ("target", "devrec"):
            did = self._alvo(p[1:])
            if not did:                     # slot sem device (lista por ler ou slot a mais): nada, nunca outro device
                print("usb: %s: no device for this action yet" % accao, file=sys.stderr, flush=True)
            elif p[0] == "target":
                self._resultado(accao, self.rig.toggle_target(did))    # o bridge inverte (atomico); num erro os alvos ficam
            else:
                self._resultado(accao, self.rig.device_rec(did))
        elif p[0] == "targets" and len(p) == 2 and p[1] in ("all", "none"):
            if p[1] == "all":
                s, d = self.rig.alvos_todos()
            else:
                s, d = self.rig.set_targets([])
            if s == 0:                      # lista de devices desconhecida: os alvos ficam como estao
                print("usb: %s refused: %s" % (accao, (d or {}).get("error")), file=sys.stderr, flush=True)
        elif p[0] == "mark" and len(p) > 1 and p[1] in ("good", "no-good", "mark"):
            self._resultado(accao, self.rig.mark(p[1]))

    @staticmethod
    def _resultado(accao, r):
        """Um botao sem efeito deixa a razao no registo (o bridge recusou, ou nao responde). `r`: (status HTTP, JSON)."""
        s, d = r if isinstance(r, tuple) and len(r) == 2 else (200, {})
        if s != 200:
            print("usb: %s refused: %s" % (accao, (d or {}).get("error") if isinstance(d, dict) and d.get("error")
                                           else ("bridge not answering" if s == 0 else "HTTP %s" % s)),
                  file=sys.stderr, flush=True)

    def etiquetas(self):
        """{id: nome no ecra} dos devices de agora (a consola mostra o nome e nao o id)."""
        return {r["id"]: r.get("label") or r["id"] for r in list(self.rig.rows)}

    def accoes(self):
        """As accoes possiveis agora, para a consola escolher."""
        a = ["rec", "targets:all", "targets:none", "mark:good", "mark:no-good", "mark:mark"]
        for n, r in enumerate(self.rig.rows, 1):
            a += ["target:slot:%d" % n, "devrec:slot:%d" % n]
        for did in self.rig.ids():
            a += ["target:%s" % did, "devrec:%s" % did]
        return a

    # -- LEDs (MIDI) --
    def ligado(self, accao):
        p = accao.split(":")
        if p[0] in ("rec", "target", "devrec") and not self.rig.online:
            return False                    # bridge offline (ou dados velhos): nenhum LED de REC/ligado/alvo aceso
        if p[0] == "rec":
            return self.rig.a_gravar()
        did = self._alvo(p[1:]) if len(p) > 1 else None
        if p[0] == "target":
            return did in self.rig.targets
        if p[0] == "devrec":
            return any(r["id"] == did and r.get("rec") for r in self.rig.rows)
        return None

    def atualizar_leds(self):
        for chave, fd in list(self.midi_fd.items()):
            c = self.cfg["devices"].get(chave) or {}
            if not c.get("enabled", True):
                continue
            for ev, accao in list((c.get("map") or {}).items()):        # a consola pode mexer no mapa a meio
                est = self.ligado(accao)
                if est is None or self.leds.get((chave, ev)) == est:
                    continue
                b = midi_led(ev, est, _vel(c, "led_on", 127), _vel(c, "led_off", 0))
                if b:
                    try:
                        os.write(fd, b)
                        self.leds[(chave, ev)] = est
                    except OSError:
                        pass

    # -- ciclos --
    def ciclo_scan(self):
        while True:
            try:
                self.procurar()
            except Exception as e:          # pylint: disable=broad-except
                print("usb: scan: %s" % e, file=sys.stderr, flush=True)
            time.sleep(SCAN_S)

    def ciclo_estado(self):
        while True:
            # Um pedal ou teclado HID tambem precisa do estado (slots e alvos): so a falta de qualquer controlador
            # mapeado e em uso deixa o ciclo em repouso (um teclado MIDI sem mapa nao faz o bridge responder 4 vezes por
            # segundo). Os LEDs so se mandam aos MIDI.
            ativo = self.midi_mapeado() or self.hid_aberto()
            if ativo:
                self.rig.ler()
                self.atualizar_leds()
            time.sleep(ESTADO_S if ativo else 1.0)

    # -- API para a consola --
    def lista(self):
        with self.lock:
            out = []
            for k in set(self.presentes) | set(self.cfg["devices"]):
                d = self.presentes.get(k) or {}
                c = self.cfg["devices"].get(k) or {}
                u = self.ultimo.get(k)
                out.append({"key": k, "name": d.get("name") or c.get("name") or k, "present": k in self.presentes,
                            "classes": d.get("classes") or c.get("classes") or [], "nodes": d.get("nodes") or {},
                            "enabled": c.get("enabled", True), "map": dict(c.get("map") or {}), "role": c.get("role") or "",
                            "roles": sorted({r for cl in (d.get("classes") or []) for r in PAPEIS.get(cl, [])}),
                            "last_event": u["event"] if u else None,
                            "last_event_age": round(time.time() - u["at"], 3) if u else None})
            out.sort(key=lambda x: (not x["present"], x["name"].lower(), x["key"]))      # os ligados primeiro
            return out

    def mapear(self, chave, ev, accao):
        if accao and not evento_valido(ev):          # tirar (accao "") vale para qualquer evento ja guardado
            return False, "bad event %r" % ev
        if accao and accao.split(":")[0] not in ("rec", "target", "devrec", "targets", "mark"):
            return False, "unknown action %r" % accao
        if accao and not accao_valida(accao):
            return False, "bad action %r" % accao
        with self.lock:
            c = self.dev_cfg(chave)
            d = self.presentes.get(chave) or {}
            c["name"] = d.get("name") or c.get("name") or chave
            c["classes"] = d.get("classes") or c.get("classes") or []
            aceso = self.leds.get((chave, ev))
            if accao:
                c["map"][ev] = accao
            else:
                c["map"].pop(ev, None)
            self.leds = {k: v for k, v in self.leds.items() if k != (chave, ev)}
            if aceso and not accao and self.midi_fd.get(chave):      # tirou-se um botao com o LED aceso: apaga-o
                try:
                    os.write(self.midi_fd[chave], midi_led(ev, False, _vel(c, "led_on", 127), _vel(c, "led_off", 0)))
                except OSError:
                    pass
            self._guardar()
        return True, None

    def papel(self, chave, papel):
        d = self.presentes.get(chave) or {}
        validos = {r for cl in (d.get("classes") or []) for r in PAPEIS.get(cl, [])}
        if papel not in validos:
            return False, "role %r is not valid for this device (%s, or empty)" % (
                papel, ", ".join(sorted(r for r in validos if r)) or "none")
        with self.lock:
            c = self.dev_cfg(chave)
            c["name"], c["classes"], c["role"] = d.get("name") or chave, d.get("classes") or [], papel
            self._guardar()
        return True, None

    def ligar(self, chave, on):
        if not isinstance(on, bool):
            return False, "enabled: true or false"
        with self.lock:
            self.dev_cfg(chave)["enabled"] = bool(on)
            self._guardar()
        return True, None

    def aprender_botao(self, chave, segs):
        """A consola esta (ou deixou de estar) a aprender um botao deste device."""
        if segs and segs > 0:
            self.aprender[chave] = time.time() + min(float(segs), APRENDER_S)
        else:
            self.aprender.pop(chave, None)
        return True, None

    def esquecer(self, chave):
        with self.lock:
            self.cfg["devices"].pop(chave, None)
            self._guardar()
        return True, None

    def servidor(self, porta=PORTA_API):
        usb = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, o):
                b = json.dumps(o).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/devices":
                    return self._j(200, {"devices": usb.lista()})
                if self.path == "/actions":
                    usb.rig.ler()
                    return self._j(200, {"actions": usb.accoes(), "labels": usb.etiquetas(),
                                         "slots": [r.get("label") or r["id"] for r in list(usb.rig.rows)]})
                self._j(404, {"error": "no such route"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    d = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._j(400, {"ok": False, "error": "bad JSON"})
                if not isinstance(d, dict):
                    return self._j(400, {"ok": False, "error": "the body must be a JSON object"})
                k = str(d.get("key") or "")
                if not k:
                    return self._j(400, {"ok": False, "error": "missing key"})
                try:
                    if self.path == "/map":
                        ok, err = usb.mapear(k, str(d.get("event") or ""), str(d.get("action") or ""))
                    elif self.path == "/role":
                        ok, err = usb.papel(k, str(d.get("role") or ""))
                    elif self.path == "/enable":
                        ok, err = usb.ligar(k, d.get("enabled"))
                    elif self.path == "/forget":
                        ok, err = usb.esquecer(k)
                    elif self.path == "/learn":
                        try:
                            segs = float(d.get("secs") or 0)
                        except (TypeError, ValueError):
                            segs = 0.0
                        ok, err = usb.aprender_botao(k, segs)
                    else:
                        return self._j(404, {"ok": False, "error": "no such route"})
                except OSError as e:            # disco cheio ou so de leitura: a consola ve a razao (e nao "sem resposta")
                    ok, err = False, "could not save usb_map.json: %s" % e
                self._j(200 if ok else 409, {"ok": ok, "error": err})

        srv = ThreadingHTTPServer(("127.0.0.1", porta), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


def main():
    u = Usb()
    u.servidor()
    print("usb: watching %s; console API on 127.0.0.1:%d" % (SYS_USB, PORTA_API), flush=True)
    threading.Thread(target=vigiado(u.ciclo_estado, "USB state"), daemon=True).start()
    u.ciclo_scan()


if __name__ == "__main__":
    main()

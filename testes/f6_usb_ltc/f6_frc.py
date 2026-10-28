#!/usr/bin/env python3
"""Fazer de FRC-8 para o Zoom F6, por USB-MIDI, a partir do Atom.

Protocolo do emulador publico howardlin551155/Zoom-FRC-8-Emulator (capturado
entre um FRC-8 e um F4): o gravador, em modo F-Control, apresenta-se como
dispositivo MIDI "F Series MIDI Interface"; quem manda e o host (o FRC-8, aqui
o Atom).

  1. host -> F0 52 00 74 00 00 00 63 00 01 F7       (pedido de ligacao)
  2. F6   -> 80 00 00                               (aceite; so o F4: o F6 e o F8n Pro nao o mandam)
  3. host -> B7 02 00, 91 00 00, 93 00 00, CA 00 00 (sincronizacao, 50 ms entre)
  4. host -> FE a cada 0,25 s                       (manter viva a ligacao)
  Botoes: REC 90 24 7F/00, STOP 90 22 7F/00, PLAY 90 23 7F/00 (carregar/largar)
  Estado: F6 -> 90 29 vel (REC aceso/apagado), 90 28 vel (PLAY)

So biblioteca padrao: fala directamente com o no ALSA /dev/snd/midiC*D*.

  sudo python3 f6_frc.py          espera pelo F6, faz o handshake e fica a ouvir
      Depois, escrever e Enter:  r = REC   s = STOP   p = PLAY   q = sair

Montagem: F6 a pilhas, cabo USB-A (no Atom) para USB-C (no F6). Ordem que
funcionou a 2026-10-08: arrancar isto, carregar em connect no F6 com o cabo
DESLIGADO, e so depois ligar o cabo. Com o cabo ja ligado o F6 encontra a
corrente do Atom na linha e da "Error!". Se a ligacao cair, isto volta a
esperar pelo F6 sozinho.
"""
import argparse
import glob
import os
import re
import select
import sys
import threading
import time

NOME = "F Series"
HANDSHAKE = bytes([0xF0, 0x52, 0x00, 0x74, 0x00, 0x00, 0x00, 0x63, 0x00, 0x01, 0xF7])
SYNC = [bytes([0xB7, 0x02, 0x00]), bytes([0x91, 0x00, 0x00]),
        bytes([0x93, 0x00, 0x00]), bytes([0xCA, 0x00, 0x00])]
BOTOES = {"r": (0x24, "REC"), "s": (0x22, "STOP"), "p": (0x23, "PLAY")}
# Variantes de teste. O FRC-8 real (captura do emulador) so manda o pedido de
# ligacao; a sincronizacao B7/91/93/CA e o FE vem do emulador, nao da captura.
OPC = argparse.Namespace(sync=True, fe=True, mudo=False, apos=[], repetir=[], periodo=0.25)


def hora():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % (t % 1 * 1000)


def procurar():
    """Caminho do no rawmidi do F6, ou None."""
    try:
        cards = open("/proc/asound/cards").read()
    except OSError:
        return None
    for m in re.finditer(r"^\s*(\d+)\s+\[[^\]]*\]:\s*(.*)\n\s*(.*)$", cards, re.M):
        n, a, b = m.group(1), m.group(2), m.group(3)
        if NOME in a or NOME in b:
            nos = sorted(glob.glob("/dev/snd/midiC%sD*" % n))
            if nos:
                return nos[0], (a + " " + b).strip()
    return None


class Frc:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.lock = threading.Lock()
        self.ligado = False
        self.a_gravar = None
        self.fim = False

    def enviar(self, b, nota=""):
        with self.lock:
            os.write(self.fd, b)
        if b != b"\xfe" and nota != "repetido":   # o que se repete nao enche o ecra
            print("%s  -> %-36s %s" % (hora(), b.hex(" "), nota), flush=True)

    def manter_vivo(self):
        while not self.fim:
            if OPC.fe:
                self.enviar(b"\xfe")
            # O FRC-8 real parece mandar o estado da alimentacao (0F) com
            # regularidade: --repetir envia-o a cada --periodo, depois da resposta.
            if OPC.repetir and getattr(self, "respondi", False):
                for m in OPC.repetir:
                    self.enviar(m, "repetido")
            time.sleep(OPC.periodo)

    def botao(self, nota, nome):
        self.enviar(bytes([0x90, nota, 0x7F]), nome + " carregar")
        time.sleep(0.08)
        self.enviar(bytes([0x90, nota, 0x00]), nome + " largar")

    def sincronizar(self, porque):
        with self.lock:
            if self.ligado:
                return
            self.ligado = True
        if not OPC.sync:
            print("%s  CONNECTED (no sync). r = REC, s = STOP, p = PLAY, q = quit" % hora(), flush=True)
            return
        print("%s  %s: a sincronizar" % (hora(), porque), flush=True)
        for m in SYNC:
            time.sleep(0.05)
            self.enviar(m, "sync")
        if self.fim:
            return
        print("%s  CONNECTED. r = REC, s = STOP, p = PLAY, q = quit" % hora(), flush=True)

    def sincronizar_mais_tarde(self):
        # O F8n Pro nunca manda o 80 00 00 (issue #2 do emulador) e os comandos
        # funcionam na mesma. O F6 manda o estado todo em ~30 ms e desligou-se aos
        # 0,55 s sem sinal de vida: sincroniza-se 0,15 s depois do pedido.
        time.sleep(0.15)
        if not self.fim:
            self.sincronizar("status received (without 80 00 00)")

    def recebido(self, msg):
        st = msg[0]
        texto = ""
        if msg == b"\x80\x00\x00" and not self.ligado:
            print("%s  <- %-36s %s" % (hora(), msg.hex(" "), "<- ACEITE"), flush=True)
            self.sincronizar("aceite")
            return
        if st & 0xF0 == 0x90 and len(msg) == 3 and msg[1] == 0x29:
            self.a_gravar = msg[2] > 0
            texto = ">>> F6 RECORDING" if self.a_gravar else ">>> F6 STOPPED"
        elif st & 0xF0 == 0x90 and len(msg) == 3 and msg[1] == 0x28:
            texto = "PLAY " + ("on" if msg[2] else "off")
        elif st & 0xF0 == 0xD0:
            return                                   # medidores de nivel: muitos, nao mostrar
        print("%s  <- %-36s %s" % (hora(), msg.hex(" "), texto), flush=True)
        # Fim do estado inicial do F6 (a SysEx 0e e a ultima): responder ja com o
        # que se pediu em --apos. Hipoteses do que o FRC-8 real responde.
        if st == 0xF0 and len(msg) > 9 and msg[8] == 0x0E and OPC.apos and not getattr(self, "respondi", False):
            self.respondi = True
            for m in OPC.apos:
                self.enviar(m, "after the status")

    def ler(self):
        """Parte o fluxo em mensagens MIDI (SysEx, canal com status corrido, tempo real)."""
        sysex, corrente, status = None, b"", None
        while not self.fim:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if not r:
                continue
            try:
                dados = os.read(self.fd, 256)
            except OSError as e:
                print("%s  link lost: %s" % (hora(), e), flush=True)
                self.fim = True
                return
            for x in dados:
                if x >= 0xF8:                         # tempo real: pode vir a meio de tudo
                    continue
                if sysex is not None:
                    sysex += bytes([x])
                    if x == 0xF7:
                        self.recebido(sysex)
                        sysex = None
                    continue
                if x == 0xF0:
                    sysex = b"\xf0"
                    continue
                if x & 0x80:
                    status, corrente = x, bytes([x])
                elif status is not None:
                    if not corrente:
                        corrente = bytes([status])    # status corrido
                    corrente += bytes([x])
                else:
                    continue
                precisa = 2 if (status & 0xF0) in (0xC0, 0xD0) else 3
                if len(corrente) == precisa:
                    self.recebido(corrente)
                    corrente = b""


def sessao(path, nome):
    print("%s  apareceu: %s em %s" % (hora(), nome, path), flush=True)
    f = Frc(path)
    threading.Thread(target=f.ler, daemon=True).start()
    if OPC.mudo:
        print("%s  mute mode: sending nothing, only listening" % hora(), flush=True)
        return f
    f.enviar(HANDSHAKE, "link request")
    # O FRC-8 manda FE de 0,25 em 0,25 s desde o inicio: sem isso o F6 desliga-se.
    threading.Thread(target=f.manter_vivo, daemon=True).start()
    threading.Thread(target=f.sincronizar_mais_tarde, daemon=True).start()
    return f


def main():
    global OPC
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-sync", action="store_true", help="do not send B7/91/93/CA (the real FRC-8 does not)")
    ap.add_argument("--sem-fe", action="store_true", help="do not send the FE keep-alive")
    ap.add_argument("--mudo", action="store_true", help="send nothing, only listen")
    ap.add_argument("--repetir", default="",
                    help="messages to repeat every --periodo after the status answer (hex, ';')")
    ap.add_argument("--periodo", type=float, default=0.25)
    ap.add_argument("--apos", default="",
                    help="messages to send right after the F6 status, in hex, separated by ';'")
    a = ap.parse_args()
    apos = [bytes.fromhex(x) for x in a.apos.split(";") if x.strip()]
    rep = [bytes.fromhex(x) for x in a.repetir.split(";") if x.strip()]
    OPC = argparse.Namespace(sync=not a.sem_sync, fe=not a.sem_fe, mudo=a.mudo, apos=apos,
                             repetir=rep, periodo=a.periodo)
    print("%s  variante: sync=%s fe=%s mudo=%s apos=%s" % (hora(), OPC.sync, OPC.fe, OPC.mudo,
          [x.hex(" ") for x in apos]), flush=True)
    sessao_actual = [None]

    def teclado():
        for linha in sys.stdin:
            c = linha.strip().lower()[:1]
            if c == "q":
                os._exit(0)
            f = sessao_actual[0]
            if c in BOTOES and f and not f.fim:
                f.botao(*BOTOES[c])
            elif c in BOTOES:
                print("%s  no link to the F6" % hora(), flush=True)

    threading.Thread(target=teclado, daemon=True).start()
    try:
        while True:
            print("%s  waiting for the F6 (\"%s\")..." % (hora(), NOME), flush=True)
            while True:
                r = procurar()
                if r:
                    break
                time.sleep(0.05)
            f = sessao(*r)
            sessao_actual[0] = f
            while not f.fim:
                time.sleep(0.1)
            # Esperar que o no desapareca antes de voltar a procurar.
            while procurar():
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""bridge-ctl: controlo do rig numa consola (curses), sem browser.

Plano B para quando o ecra tatil esta lento ou bloqueado, e para usar por SSH.
Fala com o bridge pela mesma API que a pagina (http://localhost:8080), por isso
nao duplica logica: tudo faz exactamente o mesmo que os botoes da pagina, e as
recusas do bridge (a gravar, sem rede, ...) aparecem tal como ele as diz.

Ecra principal
  cima/baixo  escolher a linha: devices, F6 e GATEWAYS (sem fim: da ultima volta a primeira)
  esq/dir     vista da lista: STATE, IMAGE (definicoes), NETWORK (ligacao e sinal), TAKE (ultima take e clip). Fica guardada
  ESPACO      alvo do REC (liga/desliga)       A  todas as camaras alvo
  R           REC / parar nos alvos            T  REC / parar so no device escolhido
  G / N / M   marca good / no-good / mark no shoot log (sem perguntas)
  C / D       ligar / desligar a camara escolhida (D pergunta antes)
  E           nome curto de referencia         PgUp/PgDn  mudar a linha de sitio (S guarda, U desfaz)
  1-9         as opcoes da linha seleccionada (a barra por cima do rodape)
  ENTER       as mesmas opcoes da linha seleccionada (device, F6 ou gateway), uma por linha
  Z           ecra do Zoom F6
  L           shoot log inteiro (ver, editar, apagar)   X  limpar o alarme de falha
  W           assistente de inicio de sessao (Atom, Wi-Fi, relogios, TC-1, LTC,
              camaras, F6, GoPro, definicoes, alvos, shoot log, take de teste)
  ESC         menu geral: WIZARD, DEVICES, TIME, LOGS, POWER, GLOBAL SETTINGS, HELP
  ? ou F1     ajuda: teclas e "como fazer" (texto fixo, funciona sem o bridge)
  Q           sair (pergunta antes)   (reiniciar / desligar: ESC > POWER, nunca a gravar)

Menus (ESC e os de dentro): PgUp/PgDn mudam o item de sitio, [ e ] a seccao inteira (S guarda, U desfaz);
F2 ou Ctrl+R muda o nome do item ou da seccao (fica guardado; vazio repoe o original).

REC: se ALGUMA camara alvo estiver a gravar, R para tudo; parar nunca pede
confirmacao (e a mesma regra do botao REC da pagina).

Dados velhos: se o /api/status (ou o /api/devices) nao chega ha mais de ~6 s, a
coluna STATE mostra "?" e os valores antigos aparecem apagados (nunca um REC velho
como se fosse actual).

Bloqueio: Win+L (no teclado do Atom, a consola a correr como root) ou F12 tapam a consola; so o PIN da pagina (GLOBAL
SETTINGS > Web PIN) a volta a abrir. A gravacao continua: o ecra bloqueado diz se alguma camara grava. Durante o bloqueio
a consola nao sai (Q, Ctrl+C), o menu de arranque nao abre e a troca de consola (Ctrl+Alt+Fn) fica desligada. O ficheiro
/run/bridge-ctl.locked guarda o estado (apagar o ficheiro, como root, desbloqueia por SSH).

Tem de estar aberto para ver o estado: enquanto corre, o bridge conta-o como
uma pagina aberta (o /api/status marca "ha alguem a ver"). Fechar com Q.
"""
import curses
import functools
import glob
import hashlib
import hmac
import json
import locale
import os
import queue
import re
import select
import signal
import struct
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:                    # so no Linux ha troca de consolas virtuais a trancar
    fcntl = None

os.environ.setdefault("ESCDELAY", "25")   # Esc sem esperar um segundo

# Mostrada no cabecalho: diz logo que versao esta instalada (ja houve confusao
# entre a copia em /home e a de /usr/local/bin).
# A versao a serio e o commit que o instalador escreveu (version.json, no /api/status): esta etiqueta nunca mudava e
# parecia que o Atom nao actualizava.
BASE = os.environ.get("BRIDGE_URL", "http://localhost:8080")
# Os mesmos degraus da pagina (ISO_STOPS no ui.html): o ISO da BMPCC escolhe-se
# desta lista, nunca um numero solto que a camara pode nao aceitar.
BMPCC_ISO_STOPS = (100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 3200)
# Uma cor por tipo de tarefa, a mesma no rodape das teclas e nos menus de
# detalhe, para se saber pela cor de que area e cada coisa.
COR = {"rec": "red", "log": "yellow", "cams": "cyan",
       "time": "green", "net": "blue", "sys": "white", "f6": "blue"}
# Todas as teclas do ecra principal, por tarefa: a ajuda (F1 > Keys). O rodape so mostra as do dia-a-dia (footer_rows).
KEY_GROUPS = (
    ("REC", COR["rec"], ("R targets", "T this cam", "SPACE target", "A all")),
    ("LOG", COR["log"], ("G good", "N no-good", "M mark", "L shoot log", "P take data")),
    ("CAMS", COR["cams"], ("ENTER options", "1-9 option", "C connect", "D disconnect", "E alias", "PgUp up", "PgDn down")),
    ("F6", COR["f6"], ("Z F6 screen",)),
    ("GLOBAL", COR["sys"], ("ESC menu", "F1 help", "W start wizard", "X clear fault", "F12 lock", "Q quit")),
)
# As vistas da lista de camaras no ecra principal (setas esquerda e direita): so muda o que vem depois do estado.
VISTAS = ("STATE", "IMAGE", "NETWORK", "TAKE")
NOME_W = 12              # largura da coluna DEVICE do dashboard: o nome inteiro, sem abreviaturas ("iPhone A", "Android")
VISTA_CAB = {
    "STATE": "BATT   LEFT     ALIAS",
    "IMAGE": "MODE   SETTINGS",
    "NETWORK": "LINK",
    "TAKE": "TAKE  CLIP         AT",
}
# As colunas do dashboard (x): REC 0, VIA 6, DEVICE 11 (NOME_W + 1), depois o estado e as colunas de cada vista.
COL_ESTADO = 11 + NOME_W + 2
COL_V = COL_ESTADO + 12                 # onde comeca a primeira coluna de cada vista (BATT, MODE, LINK, TAKE)
# Ajuda (tecla F1 ou ?, e ESC > HELP): texto estatico, nao precisa do bridge. O ecra principal e os menus so dizem o
# que e verdade; os "como fazer" e as explicacoes vivem aqui. Cada pagina: (titulo, linhas) -- uma linha e um texto ou
# (texto, cor). Largura <= 76 colunas (cabe em 80x24).
def help_lines(texto):
    """Uma pagina de ajuda em texto simples -> linhas (texto, cor) para o pager. Marcas no inicio da linha:
    '# ' seccao (ciano), '! ' problema ou cuidado (amarelo), '~ ' nota (apagado);
    a linha vazia separa blocos."""
    cores = {"# ": "cyan", "! ": "yellow", "~ ": "dim"}
    L = []
    for linha in texto.strip("\n").split("\n"):
        cor = ""
        if linha[:2] in cores:
            cor, linha = cores[linha[:2]], linha[2:]
            if cor == "cyan":
                linha = linha.upper()
        L.append((linha, cor))
    return L


def help_keys_lines():
    """A folha das teclas, gerada a partir de KEY_GROUPS (a mesma do rodape) mais as teclas que o rodape nao mostra."""
    L = [("MAIN SCREEN", "cyan")]
    for label, cor, keys in KEY_GROUPS:           # partido a 74 colunas: cabe em 80x24 sem o pager o voltar a partir
        L += [(x, cor) for x in textwrap.wrap("%-7s %s" % (label, "  ".join(keys)), 74, subsequent_indent=" " * 8)]
    return L + help_lines("""
# Rows
VIA says how a camera is linked, in the colour of that link: EXT (camera Wi-Fi),
INT (uplink Wi-Fi), S3 / S3 2 / C3 (the gateway), BT (Atom Bluetooth). Each gateway
keeps its own colour; red is only for REC and faults.
Up/down pick a device, the F6 or a gateway.
Enter opens its options (same as 1-9): only the ones that apply now
(offline: no REC), REC first, the rare ones (remove, address...) in More.
PgUp/PgDn move a camera row: S saves, U undoes.

# REC
R stops everything if ANY target records. Stopping never asks.

# Take data (P)
Opens by itself as soon as the last camera stops (under a second), here and on the
web page: session name, scene, reel (1 to 999),
next shot, good / no-good and a note. All optional. Enter next field (last: save),
Esc skip. Whoever saves or skips closes it everywhere. A new session name restarts
the take numbers at 1. Until it is closed the BRIDGE refuses to start another
recording from any client (stopping always works). Slate menu: Take data lock.

# F6 screen (Z)
R rec/stop   J jam   SPACE target   M mark   G circle
C clear clips   S scene   + scene+1   N note   T track name   K card

# Menus
Up/down, Enter run, 1-9 or [letter] jump, q / Esc back.
Two columns: left/right changes column.
PgUp/PgDn move item, [ ] move section, F2 rename item or section.
""")


M5_HELP = """
~ A M5Stack M5StickS3 as a pocket remote for the F6: REC / STOP with a lock.
~ Works only with the F6's S3 in F6 standalone (its Wi-Fi F6-REMOTE-xxxx).

# Flash it from here
1. DEVICES > M5STICK (F6 REMOTE) > Flash a M5StickS3.
2. Plug the M5StickS3 into the Atom by USB (a data cable).
3. Type the name of the S3's Wi-Fi (F6-REMOTE-xxxx) and its password.
   They are written into the M5StickS3, not into any file here.
4. Unplug and plug it in again when it says so.
Update its program (keeps the Wi-Fi), or change its Wi-Fi only: the other two lines.

# If it does not flash
! Hold the reset button on its side for about 2 s, until the green LED
!   blinks, release it, and try again.
! Use a data cable (not a charge-only one). Another USB port helps.

# Using it
B (side): 3 quick presses unlock A for 10 s. One press locks it again.
A (front): unlocked, HOLD it: REC 0.7 s, STOP 0.45 s, by what the F6 says.
It locks itself after one command. There is no PLAY, on purpose.
Locked, tapping A turns the page: 1 tracks and levels, 2 Wi-Fi (network,
signal, IP), 3 batteries and does the S3 answer. Big letters on each page.
It shows REC / READY, the 6 tracks (armed in green) and levels, the Wi-Fi
signal and the battery. STATE UNKNOWN (flashing) means: look at the F6.
"""


# Cada pagina: (titulo, texto de help_lines ou funcao que devolve linhas). Largura <= 74 colunas (cabe em 80x24).
HELP_PAGES = (
    ("Keys", help_keys_lines),
    ("Reading the dashboard", lambda: help_lines("""
# State
ready, REC, connecting, reading..., offline.
"?" in STATE: no fresh data (older than ~6 s). Values go dim and the
  last state shows as "? was REC".
Name colour: green connected, yellow connecting, red offline.

# Rows
REC in the first column: the row is a REC target (SPACE).
R records / stops the targets.

# Top line
NTP / iPhone / CLK: the clock sources. Red = none confirmed.
Right side: AC or BATTERY, CPU C, RAM %, swap, LOAD %.
  LOAD 100% = busy; above it, processes queue.
  Yellow = high, red = too high.

# LTC line
Time, offset and age of the jam. A yellow date differs from the system.

# Networks
INT WI-FI = uplink.
EXT WI-FI = camera network and its cameras.

# Gateways
Green online, yellow connecting, red offline, grey off.
Grey = off in the bridge: not polled, no fault.
The 1 key on that line turns it on/off.

# Other lines
Message line: last result. Green ok, yellow notice, red refused (8 s).
FAULT line: X clears it.

~ C / D do nothing on GoPro, BMPCC, F6 or gateway rows: they connect by
~ themselves (gateway, C3, cable).
""")),
    ("Pair a GoPro", lambda: help_lines("""
~ Needs an S3 gateway that is on and answering (GATEWAYS, the S3 line).
~ No S3 yet: ESC > DEVICES > GOPRO > Add an S3 gateway.

# Steps
1. On the GoPro: Preferences > Connections > Connect Device > GoPro App.
   Not Smart Remote. Leave it on that screen.
2. S3 line > Add GoPro. Confirm the scan (about 12 s), pick the camera,
   name it. The gateway connects and pairs it by itself.
3. SPACE on its row makes it a REC target.

# Problems
! Nothing found: GoPro on, near the gateway, Bluetooth on, on the pairing
!   screen. Cameras already added are not listed.
! Stays offline: the gateway keeps trying. GoPro on, near it, auto power
!   off and screen saver off.

# Reset
S3 line > Reset: restart, forget Bluetooth pairings, or forget all.
""")),
    ("Pair a BMPCC (C3, or Atom Bluetooth)", lambda: help_lines("""
# C3 gateway (normal)
The C3 is on the network in Bridge mode.
1. C3 line > Pair camera (camera on, Bluetooth on). Pick the camera.
2. The camera shows a 6-digit code: type it.
   The C3 forgets its previous camera. Not while a take is recording.
C3 line > Set Bridge mode / Set standalone.

# More BMPCC
Each with its own C3: ESC > DEVICES > BMPCC (EXTRA) > Add a BMPCC.
Camera menu > C3 address.

# Plan B: Atom Bluetooth (no C3)
1. BMPCC camera menu > Connection > Pair another BMPCC to the Atom
   Bluetooth (camera in pairing mode), then Use it as the BMPCC.
2. The bridge connects by itself.
Back to the C3: Connection > C3 gateway.

# Problems
! Offline: the C3 must be on the network in Bridge mode with the BMPCC
!   paired to it.
""")),
    ("F6: link (FRC-8) and standalone", lambda: help_lines("""
# With the Atom (FRC-8, controls the F6)
1. S3 line > Mode > F6 with the Atom. Its GoPros stop; they stay saved.
2. Cable from the F6 to the S3 FIRST. Then on the F6:
   MENU > SYSTEM > USB > FRC-8 > connect.
Direct to the Atom: connect FRC-8 with the cable UNPLUGGED, then plug in.

# Jam
Headphone output of the bridge into the F6 timecode jack (tip and
sleeve only).
Then F6 line > Jam, or ESC > TIME > ZOOM F6 > Send audio LTC.
Check the F6 took it, then TIMECODE Int Free Run (25 fps).

# Standalone (no Atom)
1. S3 line > Mode > F6 standalone. Set a Wi-Fi password (8-63 chars).
   Cable and FRC-8 as above.
2. iPhone: join Wi-Fi F6-REMOTE-xxxx, open http://192.168.4.1/ in Safari
   (Share > Add to Home Screen).
3. REC / STOP / PLAY: hold ~0.5 s. Levels are the F6 track meters.
~ The Atom no longer sees that S3.
~ Back: Back to Atom mode on the page, or Setup / update by USB.
""")),
    ("F6 SD card reader", lambda: help_lines("""
# Steps
1. On the F6: MENU > SYSTEM > USB > SD Card Reader, then plug the USB
   cable into the bridge.
2. F6 line > SD Card reader mode (or K on the F6 screen, Z).

# Keys
Enter open          Backspace up          P play/stop (.WAV, headphones)
R rename            M move                N new folder
D to trash (F6_TRASH)                     T open trash
E empty trash (type EMPTY: deletes for good)       F refresh

# Report
S takes the report against the shoot log.
Yellow = no matching start in the shoot log (F6 started by hand, other day).
CSV for the edit: web page /f6card > DOWNLOAD CSV.

# After
On the F6 choose Off, unplug, then FRC-8 > connect again.
""")),
    ("GoPro clock (QR) and QR lead", lambda: help_lines("""
The GoPro clock is set by a QR code with the bridge time (GoPro Labs).

# Steps
1. On a phone or laptop open
   http://bridge.lemur-drum.ts.net:8080/gopro_qr and tap Full screen.
2. GoPro in video preview, pointed at the screen ~2 s, steady.
3. Camera menu > Check clock.
   Under +-0.25 s (fine) or +-1.5 s (whole seconds, old gateway
   firmware) means it worked.

# Times
LTC time follows the bridge LTC (the TC-1).
System time is the bridge clock (NTP).

# QR lead (ms)
How far ahead the QR shows the time. The camera takes a moment to apply
it and ends up behind.
Check clock suggests a lead; or S3 line > QR lead.
Then show the TIME QR again and re-check.
""")),
    ("Clock: LTC and NTP", lambda: help_lines("""
# LTC (ESC > TIME)
Get (cable): TC-1 L-OUT into the Atom microphone jack.
Get (cable, forced): the same, accepting a reading that disagrees with the clock.
Get (Bluetooth): Sidus Audio app open, TC-1 timecodes running. No cable.

# NTP
Get: needs the uplink (INT WI-FI) on a network with internet.

# Logs
NEW archives the current shoot log or event log and starts an empty one.
""")),
    ("Add a camera / iPhone / gateway", lambda: help_lines("""
New devices: SPACE on the row makes it a REC target.

# Canon
ESC > DEVICES > CANON > Add Canon.
Name, CCAPI IP, port (8080; 443 = https).
Reserve its IP (DHCP) or set it on the body. C connects.

# iPhone (Blackmagic Camera)
ESC > DEVICES > IPHONE > Add iPhone. Name, IP, port (4444).
Blackmagic Camera open, web server on, same network.
iOS Settings > Privacy > Local Network > Blackmagic Camera on.
~ Android: the same app (3.4 or later) also has REST remote control. Not
~ tested with the bridge yet: the port and routes may differ.
The bridge writes scene + take into the clip slate; G / N set good take.
! Timecode stuck: turn the network timecode option off in the app.

# GoPro, BMPCC, gateways
GoPro / BMPCC: see their pages.
S3 gateway: ESC > DEVICES > GOPRO > Add an S3 gateway (or the S3 line).
C3 for another BMPCC: DEVICES > BMPCC (EXTRA).

# Sony (Alpha, FX, ZV)
ESC > DEVICES > SONY > Add a Sony. Wi-Fi: on the camera, Network > Wi-Fi >
Access Point Set. > the Atom camera Wi-Fi; PC Remote on (Access Point). No gateway.
USB: PC Remote (Remote Shooting) on, connection method USB.

~ Removed devices stay in ESC > DEVICES > Saved devices (restore / forget).
""")),
    ("Gateway setup by USB", lambda: help_lines("""
First setup and updates of a C3 or S3: plug it into the Atom by USB.
Gateway line > Setup / update by USB (or ESC > DEVICES > GATEWAYS (USB)).

# Steps
1. Wi-Fi: EXT WI-FI, another network, or leave it (update only).
2. Update keeps what works.
   Erase installs from scratch (Wi-Fi and the paired camera / GoPro are
   lost). A blank board is written from scratch.
3. Follow the log. Do not unplug unless asked: unplug, plug again.

# Firmware over Wi-Fi
Gateway line > Firmware (the C3 without OTA: by USB).
All gateways: GLOBAL SETTINGS > Update gateways.

# Tools on the Atom
esptool, mpremote, pyserial. Update from GitHub installs them, or:
  sudo pip install esptool mpremote --break-system-packages
  sudo apt install python3-serial

! NO TOKEN on a gateway line: set it up by USB.
""")),
    ("M5StickS3: F6 pocket remote", lambda: help_lines(M5_HELP)),
)
# Linhas do shoot log que o ecra principal traz em cada poll (para o scroll), e
# quantas mostra de cada vez -- o resto do espaco fica para as teclas.
# Quanto o relogio do Atom foge ao TC-1 sem NTP (medido de 10:51 a 17:31 a 2026-10-07: 0,12 s/hora).
LTC_DRIFT_PPM = 33
POLL_SECONDS = 2.0
POLL_GRAVAR_SECONDS = 0.7         # enquanto algo grava: o fim da take (e a pagina de dados) nao pode esperar pelos 2 s
# O F6 le-se a parte e muito mais vezes (/api/devices/f6 e leve): REC, pistas e
# medidores mudam depressa. Sem o F6 (404) fica o do /api/status.
F6_POLL_SECONDS = 0.2
MSG_SECONDS = 8.0
LABELS = {"bmpcc": "BMPCC", "r10": "R10", "r6m2": "R6 II", "f6": "F6"}


def cursor(n):
    """curses.curs_set sem deitar a consola abaixo: ha terminais (SSH, tty) que nao sabem esconder ou mostrar o cursor."""
    try:
        curses.curs_set(n)
    except curses.error:
        pass


def iniciar_locale():
    """O locale do ambiente; se for invalido neste Atom (um SSH que passa LANG=pt_PT.UTF-8 que aqui nao existe) cai para
    C.UTF-8 ou fica o por omissao. Nunca impede a consola de arrancar."""
    for loc in ("", "C.UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, loc)
            return
        except locale.Error:
            continue


def bt_info():
    """(adaptadores, bloqueado): o Bluetooth do Atom, so por sysfs (nada de subprocessos).
    `bloqueado` e True se o rfkill desliga o Bluetooth, None se nao se sabe."""
    try:
        adaptadores = sorted(x for x in os.listdir("/sys/class/bluetooth") if x.startswith("hci"))
    except OSError:
        return None, None
    bloqueado = None
    try:
        base = "/sys/class/rfkill"
        for d in os.listdir(base):
            try:
                if open(os.path.join(base, d, "type")).read().strip() != "bluetooth":
                    continue
                b = (open(os.path.join(base, d, "soft")).read().strip() == "1"
                     or open(os.path.join(base, d, "hard")).read().strip() == "1")
                bloqueado = b if bloqueado is None else (bloqueado or b)
            except OSError:
                pass
    except OSError:
        pass
    return adaptadores, bloqueado


def bt_resumo(st):
    """(texto, cor) do Bluetooth do Atom: so o adaptador (presente, bloqueado ou nao).
    O estado da BMPCC e da linha dela, nao daqui."""
    ad, bloq = bt_info()
    if ad is None:
        return "Bluetooth ?", "dim"
    if not ad or bloq:
        return "Bluetooth: off", "red"
    return "Bluetooth: on", "blue"


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02\r]")
_BT_MAC = r"((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"
_BT_EOF = object()
_FICA = object()           # menu(): a ordem foi decidida e o menu continua (nao corre o item)


class BtCtl:
    """O bluetoothctl por canos: serve para varrer e emparelhar. As linhas (e os
    pedidos sem fim de linha, como "Enter passkey:") chegam por get()."""

    def __init__(self, cmd=("bluetoothctl",)):
        self.p = subprocess.Popen(list(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, bufsize=0)
        self.q = queue.Queue()
        threading.Thread(target=self._ler, daemon=True).start()

    def _ler(self):
        buf = ""
        try:
            while True:
                raw = self.p.stdout.read(1)
                if raw == b"":
                    break
                ch = raw.decode("utf-8", "ignore")
                if ch == "\n":
                    if buf.strip():
                        self.q.put(_ANSI.sub("", buf).strip())
                    buf = ""
                else:
                    buf += ch
                    low = buf.rstrip().lower()
                    if low.endswith("(yes/no):") or ("passkey" in low and low.endswith("):")) or low.endswith("pin code:"):
                        self.q.put(_ANSI.sub("", buf).strip())
                        buf = ""
        except (OSError, ValueError):
            pass
        self.q.put(_BT_EOF)

    def send(self, cmd):
        try:
            self.p.stdin.write((cmd + "\n").encode())
            self.p.stdin.flush()
        except (OSError, ValueError):
            pass

    def get(self, timeout=0.0):
        """Uma linha; None se nao ha; _BT_EOF se o bluetoothctl acabou."""
        try:
            return self.q.get(timeout=timeout) if timeout else self.q.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        self.send("quit")
        try:
            self.p.terminate()
        except OSError:
            pass


def bt_oneshot(args, timeout=15):
    """Um comando do bluetoothctl (sem sessao): a saida, ou "ERR ..." se nao correu."""
    try:
        r = subprocess.run(["bluetoothctl"] + list(args), capture_output=True, text=True, timeout=timeout)
        return _ANSI.sub("", (r.stdout or "") + (r.stderr or ""))
    except (OSError, subprocess.SubprocessError) as e:
        return "ERR %s" % e


def bt_device_info(mac):
    """O estado de um aparelho no BlueZ: present (o BlueZ conhece-o), paired, trusted, connected, name."""
    out = bt_oneshot(["info", mac], 6)
    low = out.lower()
    nome = re.search(r"^\s*(?:Alias|Name):\s*(.+)$", out, re.M)
    return {"present": bool(nome) and "not available" not in low,
            "paired": "paired: yes" in low, "trusted": "trusted: yes" in low,
            "connected": "connected: yes" in low, "name": nome.group(1).strip() if nome else ""}


def bt_paired_macs():
    """Os enderecos ja emparelhados (para marcar na lista de procura)."""
    out = bt_oneshot(["devices", "Paired"], 6)
    if "invalid" in out.lower() or "unknown" in out.lower():          # BlueZ antigo
        out = bt_oneshot(["paired-devices"], 6)
    return {m.upper() for m in re.findall(r"Device\s+" + _BT_MAC, out)}


def bt_classificar(linha):
    """O que uma falha do bluetoothctl quer dizer, para saber como reagir:
    ja_emparelhado | bond_velho | em_curso | ausente | outro."""
    low = re.sub(r"[\s_.-]", "", linha.lower())
    if "alreadyexists" in low or "alreadypaired" in low:
        return "ja_emparelhado"
    if "authentication" in low or "bonding" in low and "fail" in low:
        return "bond_velho"
    if "inprogress" in low or "alreadyinprogress" in low or "busy" in low:
        return "em_curso"
    if any(k in low for k in ("notavailable", "doesnotexist", "connectionattemptfailed", "pagetimeout", "hostisdown",
                              "notready", "unreachable", "timeout")):
        return "ausente"
    return "outro"


def bt_parse_device(linha, achados):
    """Actualiza `achados` (mac -> nome) com uma linha do bluetoothctl."""
    m = re.search(r"\[(NEW|CHG|DEL)\]\s+Device\s+" + _BT_MAC + r"\s*(.*)$", linha)
    if not m:
        m2 = re.match(r"^Device\s+" + _BT_MAC + r"\s*(.*)$", linha)      # resposta ao comando `devices`
        if m2:
            achados.setdefault(m2.group(1).upper(), m2.group(2).strip())
        return
    ev, mac, resto = m.group(1), m.group(2).upper(), m.group(3).strip()
    if ev == "DEL":
        achados.pop(mac, None)
    elif ev == "NEW":
        achados[mac] = resto
    elif resto.startswith("Name:"):
        achados[mac] = resto[5:].strip()
    else:
        achados.setdefault(mac, "")


# Onde ficam as ordens dos menus. Um sitio so, igual para qualquer utilizador que corra a
# consola (a do tty1 corre como root, a do SSH como sequeira): senao cada um tinha o seu
# ficheiro e a ordem "desaparecia" ao reiniciar. Se /var/lib nao for gravavel, usa o HOME.
MENUS_DIR = os.environ.get("BRIDGE_CTL_MENUS", "/var/lib/bridge-ctl")   # os testes (ci/) apontam para uma pasta temporaria
MENUS_HOME = os.path.join(os.path.expanduser("~"), ".bridge-ctl-menus.json")


def _menus_ficheiros():
    return [os.path.join(MENUS_DIR, "menus.json"), MENUS_HOME]


def _menus_ler_tudo():
    for caminho in _menus_ficheiros():
        try:
            with open(caminho) as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d, caminho
        except (OSError, ValueError):
            continue
    return {}, None


def _tamanho(n):
    """1234567 -> '1.2 MB'."""
    for u in ("B", "kB", "MB", "GB"):
        if n < 1000 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1000.0


def _chave_item(it):
    """Identifica um item do menu pela parte fixa do texto (antes do estado, que vai
    depois de dois espacos): "IP  192.168.4.11:4444" -> "IP"."""
    txt = it[0] if isinstance(it, tuple) else str(it)
    return txt.split("  ")[0].strip()


def ordenar_itens(items, menu_id):
    """A ordem guardada de um menu (PgUp/PgDn) aplicada a uma lista simples de itens (texto, ...):
    o que a ordem conhece vai pela ordem dela, o resto fica onde estava. A barra de opcoes do
    dashboard usa-a para os numeros baterem com o menu Camera."""
    guardada = menu_ordem_ler(menu_id)
    if guardada:
        conhecidos = sorted((it for it in items if _chave_item(it) in guardada),
                            key=lambda it: guardada.index(_chave_item(it)))
        it_ = iter(conhecidos)
        items = [next(it_) if _chave_item(it) in guardada else it for it in items]
    # Os nomes que o operador deu (F2 no menu Camera) valem tambem na barra 1-9
    nomes = menu_nomes(menu_id)
    if nomes:
        items = [((renomear_rotulo(it[0], nomes),) + tuple(it[1:])) if isinstance(it, tuple)
                 else renomear_rotulo(it, nomes) for it in items]
    return list(items)


def menu_colunas(n, h, w, maior):
    """(colunas, linhas por coluna, largura da coluna) para um menu de `n` linhas num ecra de h x w, em que o texto mais
    comprido tem `maior` caracteres. Cabe numa coluna: (1, ...). Nao cabe: duas colunas, a lista a descer na primeira e
    depois na segunda, se o ecra tiver largura e o texto mais comprido couber em meio ecra. Senao fica numa coluna e a lista
    desliza, como sempre."""
    linhas = max(1, h - 4)
    if n > linhas and w >= 72 and n <= 2 * linhas and maior + 9 <= w // 2:
        return 2, -(-n // 2), w // 2
    return 1, linhas, w


def menu_ordem_ler(menu_id):
    o = _menus_ler_tudo()[0].get(menu_id)
    # "Ref" chamou-se assim ate 2026-10-23 (agora "Alias"): a ordem que o operador guardou continua a valer.
    return ["Alias" if x == "Ref" else x for x in o] if isinstance(o, list) else []


def menu_ordem_gravar(menu_id, chaves):
    """Grava a ordem. Devolve o caminho onde ficou (ou None se nao deu)."""
    return _menus_gravar({menu_id: chaves})


# Personalizacao de cada menu, no mesmo ficheiro das ordens: "<menu>#names" {chave do item: nome novo},
# "<menu>#sections" {titulo original da seccao: titulo novo}, "<menu>#secorder" [titulos originais pela ordem].
def menu_nomes(menu_id):
    o = _menus_ler_tudo()[0].get(menu_id + "#names")
    if not isinstance(o, dict):
        return {}
    return {("Alias" if k == "Ref" else k): v for k, v in o.items()}


def menu_seccoes(menu_id):
    o = _menus_ler_tudo()[0].get(menu_id + "#sections")
    return o if isinstance(o, dict) else {}


def menu_secordem(menu_id):
    o = _menus_ler_tudo()[0].get(menu_id + "#secorder")
    return o if isinstance(o, list) else []


def menu_renomear(menu_id, tipo, original, novo):
    """tipo "names" (item) ou "sections". Nome vazio repoe o original. Devolve o caminho onde ficou, ou None."""
    d = _menus_ler_tudo()[0]
    m = d.get(menu_id + "#" + tipo)
    m = dict(m) if isinstance(m, dict) else {}
    novo = " ".join((novo or "").split())         # um espaco so: dois espacos separam o nome do estado no menu
    if novo and novo != original:
        m[original] = novo
    else:
        m.pop(original, None)
    return _menus_gravar({menu_id + "#" + tipo: m})


def renomear_rotulo(label, nomes):
    """O texto de um item com o nome que o operador lhe deu: troca a parte fixa (antes de dois espacos) e mantem o
    estado que vem depois ("Demo mode  OFF" -> "Training  OFF")."""
    chave = label.split("  ")[0].strip()
    if chave not in nomes:
        return label
    i = label.find("  ")
    return nomes[chave] + (label[i:] if i >= 0 else "")


def _menus_gravar(novos):
    """Junta estas chaves ao ficheiro dos menus. Devolve o caminho onde ficou (ou None se nao deu)."""
    d = _menus_ler_tudo()[0]
    d.update(novos)
    for caminho in _menus_ficheiros():
        try:
            pasta = os.path.dirname(caminho)
            if pasta == MENUS_DIR and not os.path.isdir(pasta):
                os.makedirs(pasta, exist_ok=True)
                os.chmod(pasta, 0o777)               # partilhada: root e sequeira escrevem
            tmp = caminho + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=1)
            try:
                os.chmod(tmp, 0o666)
            except OSError:
                pass
            os.replace(tmp, caminho)
            return caminho
        except OSError:
            continue
    return None


# O tipo de cada device (canon | iphone | gopro | bmpcc | bmpcc_extra | sony | f6): o build_rows regista-o, vindo da lista de onde
# cada linha saiu (o campo `type` do GET /api/devices) -- nunca do prefixo do id. Um id que ele nao registou e uma Canon.
TIPOS = {"bmpcc": "bmpcc", "f6": "f6"}


def tipo_de(rid):
    return TIPOS.get(rid, "canon")


def eh_bm_extra(rid):
    """As BMPCC extra, cada uma no seu C3; a principal e a "bmpcc"."""
    return tipo_de(rid) == "bmpcc_extra"


def devs_tipo(dv, tipo):
    """Os devices de um tipo no GET /api/devices (o `dv` do Snapshot), como {id: item}, pela ordem da lista."""
    out = {}
    for d in (dv or {}).get("devices") or []:
        if isinstance(d, dict) and d.get("type") == tipo and d.get("id"):
            out[d["id"]] = d
    return out


def velho_da_linha(rid, velho_st, velho_cn):
    """Que 'dados velhos' vale para esta linha: a BMPCC principal e o F6 saem do /api/status; todas as outras (Canon, iPhone,
    GoPro, BMPCC extra) do /api/devices."""
    return velho_st if rid in ("bmpcc", "f6") else velho_cn


def carga_txt(st):
    """A carga do CPU em percentagem da capacidade (load1 a dividir pelos nucleos; 100% = ocupado, acima ha fila de espera).
    O bridge manda load_pct; um bridge mais antigo so tem load1 e faz-se a conta aqui."""
    pct = _num(st.get("load_pct"))
    if pct is None:
        ld = _num(st.get("load1"))
        pct = None if ld is None else round(ld / (os.cpu_count() or 1) * 100)
    return "?" if pct is None else "%d%%" % pct


def ram_txt(st):
    """A RAM em uso em percentagem (o bridge manda mem_percent: usada = total - disponivel)."""
    pct = _num(st.get("mem_percent"))
    return "?" if pct is None else "%d%%" % pct


def nao_canon(rid):
    """Alvos que nao sao Canon (a ligacao CCAPI e as definicoes Canon nao lhes tocam): tudo menos o tipo canon."""
    return tipo_de(rid) != "canon"


CLOCK_ORDER = ("bm", "ltc", "ntp", "iphone")
CLOCK_LABELS = {"bm": "BM TC", "ltc": "LTC", "ntp": "NTP", "iphone": "iPhone"}


class _Reabrir(Exception):
    """A consola foi substituida por uma versao nova: sobe ate ao loop principal, que a volta a abrir."""


class Sec(str):
    """Cabecalho de seccao num menu (ver menu()): mostra-se, nao se escolhe."""

# As cores das fontes de relogio, as mesmas da pagina: LTC amarelo, NTP azul.
CLOCK_COR = {"ltc": "yellow", "ntp": "blue"}
CANON_SETTINGS = ("av", "tv", "iso", "shuttermode", "moviequality")
# Letra de atalho dos itens do menu Camera (por defeito, a inicial do texto).
CAM_KEYS = {"Alias": "e", "Remove BMPCC": "x", "Check TC": "c", "Config": "o", "Set clock": "l", "Poll settings": "p", "Strict REC": "t", "ISO": "o"}
SHOOT_EVENTS = ("start", "stop", "slate", "mark", "good", "no-good", "fault", "note")
# A pagina de dados da take (P, e sozinha no fim de uma gravacao): (campo do /api/slate/take, rotulo, tamanho maximo, ajuda).
TAKE_CAMPOS = (("name", "Session name", 40, "a new name restarts the numbering at 1; the same keeps counting"),
               ("scene", "Scene", 5, "up to 5 letters or digits (the cameras get it between takes)"),
               ("reel", "Reel", 3, "1 to 999"),
               ("shot", "Next shot", 80, "short description of the next take"),
               ("good", "Last take", 0, "Space: good / no-good / nothing"),
               ("note", "Note on the last take", 80, "filed under the take that just ended"))
# O shoot log abre com uma fonte de consola mais pequena (cabem mais linhas e a
# nota inteira). A fonte que la estava e guardada com setfont -O e reposta ao
# sair. Outro tamanho: BRIDGE_CTL_LOG_FONT=Uni3-Terminus18x10.psf.gz bridge-ctl
LOG_FONT = os.environ.get("BRIDGE_CTL_LOG_FONT", "Uni3-Terminus16.psf.gz")
FONT_BACKUP = "/run/bridge-ctl-font.psf"


USB_BASE = "http://127.0.0.1:8091"      # o servico bridge-usb (dispositivos USB class compliant)


# O token dos gateways (a mesma chave que o bridge usa, fw_ota.token_cab): o C3 so aceita POST de quem o tiver
# (cabecalho X-GW-Token). A consola corre no Atom, ao lado do /opt/camera-bridge, e le o ficheiro (nao o cria).
GW_TOKEN_FICHEIROS = ("/var/lib/camera-bridge/gw_token", "/opt/camera-bridge/gw_token")


def gw_token_cab():
    """{"X-GW-Token": ...} ou {} se o Atom nao tem token."""
    for caminho in GW_TOKEN_FICHEIROS:
        try:
            with open(caminho) as f:
                t = f.read().strip()
        except OSError:
            continue
        if t:
            return {"X-GW-Token": t}
    return {}


def api(method, path, body=None, timeout=4, base=None, gw=False):
    """(status HTTP, JSON). Status 0 quando o bridge (ou o servico em `base`) nao responde.
    `gw=True`: `base` e um gateway que o bridge identificou (o C3 do status) e o POST leva o token dele. Nunca num GET, nem para
    a API do proprio bridge (localhost) nem para os servicos locais, nem sem uma `base` explicita."""
    data, headers = None, {}
    if method == "POST":
        data = json.dumps(body or {}).encode()
        headers["Content-Type"] = "application/json"
        if gw and base and base not in (BASE, USB_BASE) \
                and (urllib.parse.urlsplit(base).hostname or "") not in ("", "localhost", "127.0.0.1", "::1"):
            headers.update(gw_token_cab())
    req = urllib.request.Request((base or BASE) + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def api_text(path, timeout=10):
    """(status, texto) para as rotas que devolvem ficheiros e nao JSON."""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)


def a_gravar_st(st, dv):
    """Alguma camara (ou o F6) esta a gravar, segundo este /api/status e /api/devices?"""
    if bool(((st.get("f6") or {}).get("frc") or {}).get("recording")):
        return True
    return any(r.get("rec") for r in build_rows(st, dv))


class Snapshot:
    """O que o ultimo ciclo de polling trouxe. Lido pelo desenho, escrito so
    pela thread de polling: o ecra nunca fica preso a espera da rede."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {}
        self.devices = {}          # o ultimo GET /api/devices bom (devices, removed, order, alias, bmpcc)
        self.shoot = []
        self.ok = False
        self.error = ""
        self.at = 0.0              # o ultimo /api/status que chegou bem
        self.dev_at = 0.0          # o ultimo /api/devices que chegou bem
        self.f6 = None             # o ultimo /api/devices/f6
        self.f6_at = 0.0
        self.wake = threading.Event()   # acorda o poll completo antes dos 2 s
        # Quando a consola mandou a ultima ordem. Um /api/status pedido antes dela e que chega depois (o Atom
        # ocupado) e um retrato de antes da ordem: punha o ecra como estava (o alvo armado voltava a desarmado ate
        # ao poll seguinte, e o toque seguinte partia desse retrato).
        self.acao_em = 0.0
        self.gravando = False      # o ultimo poll viu algo a gravar: o proximo vem mais depressa (POLL_GRAVAR_SECONDS)
        self.versao = 0            # sobe quando chega algo diferente: o ecra principal so se redesenha todo quando muda

    def acao(self):
        with self.lock:
            self.acao_em = time.time()

    def alvos_locais(self, alvos):
        """Mostra ja os alvos novos, sem esperar pelo bridge (o poll a seguir a ordem confirma)."""
        with self.lock:
            self.acao_em = time.time()
            self.status = dict(self.status, rec_targets=list(alvos))
            self.versao += 1

    def poll_once(self):
        t0 = time.time()
        s1, st = api("GET", "/api/status")
        s2, dv = api("GET", "/api/devices")
        with self.lock:
            if t0 < self.acao_em:
                return                  # retrato de antes da ultima ordem: o proximo poll ja a traz
            antes = (self.ok, self.status, self.devices)
            self.ok = s1 == 200
            self.error = "" if self.ok else st.get("error", "offline")
            if self.ok:
                self.status = st
                self.at = time.time()
            if s2 == 200 and isinstance(dv, dict) and isinstance(dv.get("devices"), list):
                self.devices = dv       # um poll falhado deixa os ultimos bons
                self.dev_at = time.time()
            if (self.ok, self.status, self.devices) != antes:
                self.versao += 1
            try:
                self.gravando = bool(self.ok and (a_gravar_st(self.status, self.devices) or self.status.get("take_pending")))
            except Exception:           # um dado estranho do bridge nao pode parar o poll
                self.gravando = False

    def run(self, stop):
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # nunca deixar a thread morrer
                with self.lock:
                    self.ok, self.error = False, str(e)
            self.wake.wait(POLL_GRAVAR_SECONDS if self.gravando else POLL_SECONDS)
            self.wake.clear()

    def run_f6(self, stop):
        """So o F6, 5x/s. Quando ele comeca ou para de gravar acorda o poll
        completo, para o shoot log e o resto aparecerem logo."""
        antes = None
        while not stop.is_set():
            try:
                s, d = api("GET", "/api/devices/f6", timeout=1)
            except Exception:
                s, d = 0, None
            if s == 404:
                stop.wait(10)              # bridge sem o F6: fica o /api/status
                continue
            if s == 200 and isinstance(d, dict):
                d = d.get("device")
            if s == 200 and isinstance(d, dict):
                with self.lock:
                    if d != self.f6:
                        self.versao += 1
                    self.f6, self.f6_at = d, time.time()
                agora = ((d.get("frc") or {}).get("recording"), d.get("running"), d.get("jamming"))
                if antes is not None and agora != antes:
                    self.wake.set()
                antes = agora
            stop.wait(F6_POLL_SECONDS)

    def get(self):
        with self.lock:
            st = dict(self.status)
            if self.f6 is not None and time.time() - self.f6_at < 2 and "f6" in st:
                st["f6"] = self.f6
            return (st, dict(self.devices), list(self.shoot),
                    self.ok, self.error, self.at)


class Cache:
    """Pedidos lentos (iw, nmcli, tailscale...) feitos fora da thread do ecra:
    devolve o ultimo valor e refresca em segundo plano quando passa o TTL."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.busy = set()

    def get(self, key, ttl, fn):
        with self.lock:
            val, at = self.data.get(key, (None, 0.0))
            if time.time() - at > ttl and key not in self.busy:
                self.busy.add(key)
                threading.Thread(target=self._run, args=(key, fn), daemon=True).start()
        return val

    def drop(self, key):
        with self.lock:
            self.data.pop(key, None)

    def _run(self, key, fn):
        try:
            v = fn()
        except Exception as e:
            v = (0, {"error": str(e)})
        with self.lock:
            self.data[key] = (v, time.time())
            self.busy.discard(key)


def fmt_left(v):
    if v in (None, "", -1):
        return "-"
    if isinstance(v, str):
        return v
    v = int(v)
    if v >= 3600:
        return "%dh%02dm" % (v // 3600, v % 3600 // 60)
    if v >= 60:
        return "%dm" % (v // 60)
    return "%ds" % v


def fmt_age(v):
    if v is None:
        return "?"
    v = int(v)
    if v >= 3600:
        return "%dh%02dm" % (v // 3600, v % 3600 // 60)
    if v >= 60:
        return "%dm%02ds" % (v // 60, v % 60)
    return "%ds" % v


def ltc_now(st, at):
    """Timecode LTC agora, calculado aqui: (texto, fps) ou None sem jam valido.

    O bridge guarda do ultimo GET LTC o desvio entre o gerador e o relogio do
    sistema (`offset`), e o contador da pagina faz a mesma conta. Na mesma
    maquina o relogio e o mesmo, por isso basta hora local + desvio -- sem
    pedidos e sem ler audio. Com o bridge noutra maquina (BRIDGE_URL), parte do
    `now_secs` do ultimo poll e soma o tempo que passou desde entao.
    """
    l = st.get("ltc") or {}
    if l.get("now_secs") is None or l.get("offset") is None:
        return None
    fps = int(l.get("fps") or 25)
    now = time.time()
    if "localhost" in BASE or "127.0.0.1" in BASE:
        lt = time.localtime(now)
        secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (now % 1) + float(l["offset"])
    else:
        secs = float(l["now_secs"]) + (now - at)
    n = int((secs % 86400) * fps + 1e-6) % (86400 * fps)
    return ("%02d:%02d:%02d:%02d" % (n // (fps * 3600), n // (fps * 60) % 60, n // fps % 60, n % fps),
            fps)


def ltc_date(st):
    """Data do LTC agora, (struct_time, texto) ou None se o gerador nao a mandou.

    A data vem nos user bits do ultimo GET LTC, ou seja, e a do instante do jam.
    Se desde entao se passou a meia-noite, avanca-se pelo tempo decorrido (o
    timecode do jam + a idade da leitura), em vez de mostrar o dia anterior.
    """
    l = st.get("ltc") or {}
    if not l.get("date"):
        return None
    try:
        hh, mm, ss = (int(x) for x in str(l.get("tc") or "0:0:0").split(":")[:3])
        dias = int((hh * 3600 + mm * 60 + ss + int(l.get("age") or 0)) // 86400)
        return _ltc_dia(str(l["date"]), dias)
    except (ValueError, OverflowError):
        return None


@functools.lru_cache(maxsize=8)
def _ltc_dia(data, dias):
    """(struct_time, texto) do dia `data` + `dias`. Em cache: a linha do LTC pede-o 25x/s e a data muda uma vez por dia."""
    base = time.strptime(data, "%Y-%m-%d")
    t = time.localtime(time.mktime(base) + dias * 86400 + 3600)   # +1h: longe da meia-noite (DST)
    return t, time.strftime("%A %Y-%m-%d", t)


GW_COR = {"online": "green", "connecting": "yellow", "offline": "red", "off": "dim"}
# Por onde passa a ligacao de cada camara (issue #21). Cada uma leva uma marca curta na cor de quem a liga. Sem castanhos
# (o amarelo e para avisos) e sem vermelho (REC e falha): os gateways tiram as cores desta paleta, a primeira livre, e ficam
# guardadas com o nome do gateway; com mais de cinco repetem-se, e a marca (S3 2, C3) e que os distingue.
AP_REDE = "192.168.4."                      # a rede do AP das camaras (EXT WI-FI)
TRANSP_FIXAS = {"ext": "cyan", "int": "blue", "bt": "white", "usb": "white"}
TRANSP_PALETA = ("green", "magenta", "white", "cyan", "blue")


def via_wifi(ip):
    """(chave, marca) do Wi-Fi por onde um aparelho IP chega ao Atom: o AP das camaras (EXT) ou a rede do uplink (INT)."""
    ip = str(ip or "")
    if not ip:
        return None
    return ("ext", "EXT") if ip.startswith(AP_REDE) else ("int", "INT")
SINAL_FRACO = -70            # dBm: abaixo disto o Wi-Fi do gateway aparece no dashboard mesmo estando online


def _gw_alertas(g):
    """O que um gateway online tem de anormal, para a linha curta do dashboard: sinal fraco, pouca memoria, sem token.
    O resto (endereco, tempo ligado, sinal bom) so se ve na info da linha seleccionada."""
    out = ""
    wf, ble = _num(g.get("wifi_rssi")), _num(g.get("rssi"))
    if wf is not None and wf < SINAL_FRACO:
        out += "  weak Wi-Fi %s dBm" % g["wifi_rssi"]
    if ble is not None and ble < -80:
        out += "  weak BLE %s dBm" % g["rssi"]
    if g.get("mem_low"):
        out += "  " + str(g["mem_low"]).upper()
    if g.get("token") is False:
        out += "  NO TOKEN: set it up by USB"
    return out


def gateways(st, dv=None):
    """Os gateways ESP32 que o bridge conhece, pela ordem do ecra: o C3 (BMPCC) e o S3 (GoPro).
    Cada um: id, name, what, state (online | connecting | offline | off), detail e on (ligado no bridge)."""
    out = []
    c3 = st.get("c3_gateway")
    if isinstance(c3, dict):
        est, h = c3.get("state") or "offline", c3.get("host") or "?"
        if est == "off":
            det = "off"
        elif est == "offline":
            det = "offline"
        elif c3.get("mode") != "bridge":
            det = "%s  standalone" % h
        elif est == "connecting":
            det = "%s  BMPCC %s" % (h, c3.get("phase") or "?")
        else:
            det = "%s  BMPCC ready%s" % (h, "  REC" if c3.get("recording") else "")
        if st.get("bmpcc_via") == "bluez" and est != "off":
            det = "not used (BMPCC on Atom Bluetooth)"
        if est not in ("off", "offline") and c3.get("wifi_rssi") is not None:
            det += "  Wi-Fi %s dBm" % c3["wifi_rssi"]
        if est not in ("off", "offline") and c3.get("rssi") is not None:
            det += "  BLE %s dBm" % c3["rssi"]
        if est not in ("off", "offline") and c3.get("mem_low"):
            det += "  " + c3["mem_low"].upper()
        if est != "off" and c3.get("token") is False:     # firmware com token, mas o C3 nao o recebeu
            det += "  NO TOKEN: set it up by USB"
        curto = det
        if est == "online" and c3.get("mode") == "bridge" and st.get("bmpcc_via") != "bluez":
            curto = "BMPCC ready%s%s" % ("  REC" if c3.get("recording") else "", _gw_alertas(c3))
        out.append({"id": "c3", "name": "C3", "what": "BMPCC", "state": est, "detail": det, "short": curto,
                    "on": c3.get("enabled", True) is not False, "cams": [LABELS["bmpcc"]]})
    gws = st.get("gopro_gateways")
    if not isinstance(gws, list):                        # bridge so com um gateway (uma lista vazia e "nenhum S3")
        gws = [dict(st["gopro_gateway"], idx=1)] if isinstance(st.get("gopro_gateway"), dict) else []
    gps = devs_tipo(dv, "gopro")
    for gw in gws:
        n = gw.get("idx") or 1
        mine = {k: v for k, v in gps.items() if (v.get("gw") or 1) == n}
        on = gw.get("enabled") is not False
        curto = None
        if not on:
            est, det = "off", "off"
        elif not gw.get("host"):
            est, det = "offline", "offline  no address"
        elif not gw.get("ok"):
            est, det = "offline", "offline"
        elif gw.get("mode") == "f6":
            # Selector do S3 no modo F6: o S3 faz de FRC-8 do Zoom F6 (a linha ZOOM F6 mostra o resto).
            f = gw.get("f6") or {}
            est = "online" if f.get("connected") else "connecting"
            det = gw.get("ip") or gw.get("host")
            if gw.get("wifi_rssi") is not None:
                det += "  Wi-Fi %s dBm" % gw["wifi_rssi"]
            det += ("  F6 linked%s" % ("  REC" if f.get("recording") else "") if f.get("connected") else
                    "  F6 not linked")
            if est == "online":
                curto = "F6 linked%s%s" % ("  REC" if f.get("recording") else "", _gw_alertas(gw))
        else:
            lig = sum(1 for g in mine.values() if g.get("connected"))
            est = "online" if not mine or lig == len(mine) else "connecting"
            det = gw.get("ip") or gw.get("host")
            if gw.get("wifi_rssi") is not None:
                det += "  Wi-Fi %s dBm" % gw["wifi_rssi"]
            u = _num(gw.get("uptime_s"))
            if u is not None:
                det += "  up %dh%02d" % (int(u) // 3600, int(u) // 60 % 60)
            det += "  GoPro %d/%d" % (lig, len(mine))
            if gw.get("token") is False:                 # firmware com token, mas este S3 nao o recebeu: aberto na rede
                det += "  NO TOKEN: set it up by USB"
            if est == "online":
                curto = "GoPro %d/%d%s" % (lig, len(mine), _gw_alertas(gw))
        out.append({"id": "s3" if n == 1 else "s3-%d" % n, "gw": n, "name": gw.get("name") or ("S3" if n == 1 else "S3 %d" % n),
                    "what": "F6 (FRC-8)" if gw.get("mode") == "f6" else "GoPro", "state": est, "detail": det, "short": curto or det, "on": on,
                    "mode": gw.get("mode"),
                    "cams": [v.get("name") or k for k, v in sorted(mine.items())]})
    return out


def _desvio_txt(st, cid):
    """'  +0.04s' (desvio do timecode da camara para o LTC do Atom) ou ''."""
    dv = (st.get("tc_offsets") or {}).get(cid)
    return "  %+.2fs" % dv["offset"] if dv and dv.get("offset") is not None else ""


def _sinais_txt(wifi, ble):
    """"Wi-Fi -52  BLE -60 dBm" (so o que o aparelho deu)."""
    p = [("Wi-Fi %s" % wifi) if wifi is not None else "", ("BLE %s" % ble) if ble is not None else ""]
    p = [x for x in p if x]
    return ("  ".join(p) + " dBm") if p else "-"


def _ou(v):
    """O valor, ou "-" quando o bridge nao o deu (nunca "None" no ecra)."""
    return "-" if v is None else v


def build_rows(st, dv):
    """As linhas do ecra principal. `st`: o /api/status (a BMPCC principal); `dv`: o GET /api/devices (as outras, a ordem
    e as referencias). Cada item de dv["devices"] traz `id` e `type`."""
    dv = dv or {}
    rows = []
    if st.get("bmpcc_active") is not False:           # retirada: nao tem linha
        rows.append({
            "id": "bmpcc", "label": LABELS["bmpcc"], "conn": bool(st.get("camera_connected")),
            "busy": bool(st.get("camera_connecting")), "rec": bool(st.get("is_recording")),
            "batt": st.get("battery_percent", -1), "left": fmt_left(st.get("remaining_time")),
            "mode": "VIDEO", "cfg": "TC " + str(st.get("timecode") or "--:--:--") + _desvio_txt(st, "bmpcc"),
            "via": ("bt", "BT") if st.get("bmpcc_via") == "bluez" else ("c3", "C3"),
            "net": _sinais_txt((st.get("c3_gateway") or {}).get("wifi_rssi"), (st.get("c3_gateway") or {}).get("rssi")),
        })
    for cid, c in devs_tipo(dv, "canon").items():
        TIPOS[cid] = "canon"
        movie = bool(c.get("movie_mode"))
        cfg = " ".join(str(c.get(k)) for k in ("av", "tv", "iso") if c.get(k))
        rows.append({
            "id": cid, "label": LABELS.get(cid) or str(c.get("name") or cid.upper())[:NOME_W], "conn": bool(c.get("connected")),
            "busy": bool(c.get("connecting")), "rec": bool(c.get("recording")),
            "batt": c.get("battery_percent", -1),
            "left": fmt_left(c.get("remaining_time")) if movie else "-",
            "mode": "VIDEO", "cfg": cfg, "via": via_wifi(c.get("ip")),
            "net": str(c.get("ip") or "-"),
            "sem_video": bool(c.get("connected")) and not movie and not c.get("recording"),   # as Canon so gravam em video
        })
    # iPhones com a app Blackmagic Camera (bmcam.py no bridge), um por linha.
    for cid, ip in sorted(devs_tipo(dv, "iphone").items()):
        TIPOS[cid] = "iphone"
        cfg = " ".join(x for x in (
            ("ISO %s" % ip["iso"]) if ip.get("iso") else "",
            ("%s\u00b0" % ip["shutter"]) if ip.get("shutter") else "",
            ("%sK" % ip["wb"]) if ip.get("wb") else "",
            (ip.get("format") or "").replace("3840x2160", "UHD").replace("1920x1080", "HD"),
            ("TC STUCK %ss" % ip["tc_frozen_s"]) if ip.get("tc_frozen_s") is not None
            else (("TC %+.2fs" % ip["tc_offset"]) if ip.get("tc_offset") is not None else "")) if x)
        rows.append({
            "id": cid, "label": str(ip.get("name") or cid)[:NOME_W],
            "conn": bool(ip.get("connected")),
            "busy": False, "rec": bool(ip.get("running")),
            "batt": ip.get("battery") if ip.get("battery") is not None else -1,
            "left": fmt_left(ip.get("remaining_s")), "mode": "VIDEO", "via": via_wifi(ip.get("host")),
            "net": str(ip.get("host") or "-"),
            "cfg": cfg if ip.get("connected") else ("disabled" if not ip.get("enabled")
                                                   else "offline"),
        })
    # GoPro, pelo gateway ESP32-S3 (gopro.py no bridge): uma por linha.
    for cid, gp in sorted(devs_tipo(dv, "gopro").items()):
        TIPOS[cid] = "gopro"
        rows.append({
            "id": cid, "label": str(gp.get("name") or cid)[:NOME_W],
            "conn": bool(gp.get("connected")), "busy": False, "rec": bool(gp.get("running")),
            "pending": gp.get("pending"),
            "batt": gp.get("battery") if gp.get("battery") is not None else -1,
            "left": fmt_left(gp.get("remaining_s")), "mode": "VIDEO",
            "net": _sinais_txt(None, gp.get("rssi")),
            "via": ("s3", "S3") if (gp.get("gw") or 1) == 1 else ("s3-%d" % gp["gw"], "S3 %d" % gp["gw"]),
            "cfg": ("BLE %s dBm" % gp["rssi"] if gp.get("rssi") is not None else "") if gp.get("connected") else (
                "gateway not answering" if not gp.get("gateway_ok") else "no Bluetooth link"),
        })
    # BMPCC extra, cada uma no seu C3 (bmfleet.py no bridge): uma por linha.
    for cid, bm in sorted(devs_tipo(dv, "bmpcc_extra").items()):
        TIPOS[cid] = "bmpcc_extra"
        rows.append({
            "id": cid, "label": str(bm.get("name") or cid)[:NOME_W],
            "conn": bool(bm.get("connected")), "busy": False, "rec": bool(bm.get("running")),
            "pending": bm.get("pending"),
            "batt": bm.get("battery") if bm.get("battery") is not None else -1,
            "left": "-", "mode": "VIDEO", "via": ("c3", "C3"),
            "net": _sinais_txt(bm.get("wifi_rssi"), bm.get("rssi")),
            "cfg": (("TC %s" % bm["timecode"]) + _desvio_txt(st, cid) if bm.get("timecode") else "")
                   + ("  Wi-Fi %s dBm" % bm["wifi_rssi"] if bm.get("wifi_rssi") is not None else "")
                   + ("  BLE %s dBm" % bm["rssi"] if bm.get("rssi") is not None else "")
                   if bm.get("connected") else (bm.get("why") or "")[:60],
        })
    # Sony pelo Camera Remote SDK (sony.py no bridge): uma por linha, por Wi-Fi (sem gateway) ou USB.
    for cid, so in sorted(devs_tipo(dv, "sony").items()):
        TIPOS[cid] = "sony"
        cfg = " ".join(x for x in (("ISO %s" % so["iso"]) if so.get("iso") else "",
                                   ("f/%s" % so["fnumber"]) if so.get("fnumber") else "",
                                   so.get("shutter") or "", "REC FAILED" if so.get("rec_failed") else "") if x)
        rows.append({
            "id": cid, "label": str(so.get("name") or cid)[:NOME_W],
            "conn": bool(so.get("connected")), "busy": False, "rec": bool(so.get("running")),
            "pending": so.get("pending"),
            "batt": so.get("battery") if so.get("battery") is not None else -1,
            "left": fmt_left(so.get("remaining_s")), "mode": "VIDEO",
            "via": ("usb", "USB") if so.get("via") == "usb" else via_wifi(so.get("host")),
            "net": str(so.get("host") or so.get("serial") or "-"),
            "cfg": cfg if so.get("connected") else ("disabled" if not so.get("enabled", True)
                                                   else (so.get("why") or so.get("error") or "offline")[:60]),
        })
    # O Zoom F6 nao e uma camara: tem painel proprio (draw_f6, tecla Z).
    # A ordem que o operador escolheu (devices.json no bridge); os que nao estao
    # nela ficam no fim, pela ordem de sempre. E o nome curto de referencia de cada um.
    ordem = dv.get("order") or []
    alias = dv.get("alias") or {}
    for n, r in enumerate(rows):
        r["alias"] = alias.get(r["id"], "")
        r["_n"] = ordem.index(r["id"]) if r["id"] in ordem else 1000 + n
        if not r["conn"]:                        # desligado: sem bateria nem tempo de cartao da ultima leitura
            r["batt"], r["left"] = -1, "-"
    rows.sort(key=lambda r: r["_n"])
    return rows


# Cores do medidor do F6, como no proprio F6 (visto a 2026-10-08): ate 8 verde,
# 9 a 11 amarelo, 12 (o maximo) vermelho.
METER_YELLOW = 9


def meter_segs(v, n=6, top=12, armada=True):
    """Medidor do F6 em n caracteres, como [(texto, cor)]: verde, amarelo e
    vermelho nos mesmos degraus do F6. O FRC-8 recebe segmentos (0..top)."""
    top = float(top or 12)
    k = max(0, min(n, int(round((v or 0) * n / top))))
    out = []
    for j in range(n):
        if j >= k:
            cor, ch = "dim", "."
        elif not armada:
            cor, ch = "dim", "#"
        else:
            seg = (j + 1) * top / n          # o degrau que este caracter representa
            cor = "red" if seg > top - 1 else ("yellow" if seg > METER_YELLOW - 1 else "green")
            ch = "#"
        if out and out[-1][1] == cor:
            out[-1] = (out[-1][0] + ch, cor)
        else:
            out.append((ch, cor))
    return out


class MeterRow:
    """Uma linha do ecra do F6 com varias cores (nao passa pelo wrap_lines)."""

    def __init__(self, segs):
        self.segs = segs


def fmt_pan(v):
    """Pan do F6 (0-127, 64 = centro) em texto curto: C, L40, R100."""
    if v is None:
        return "-"
    if abs(v - 64) <= 1:
        return "C"
    return ("L%d" if v < 64 else "R%d") % round(abs(v - 64) * 100 / 64.0)


def fmt_size(n):
    if n is None:
        return ""
    return "%.2f GB" % (n / 1e9) if n > 1e9 else ("%.1f MB" % (n / 1e6) if n > 1e6 else "%d kB" % max(1, n // 1000))


def _num(v):
    """v como numero, ou None se nao for (o que vem do bridge e do gateway nao e de confianca)."""
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def fmt_ago(t):
    if not t:
        return None
    s = max(0, int(time.time() - t))
    return "%ds" % s if s < 60 else ("%dm" % (s // 60) if s < 3600 else "%dh%02d" % (s // 3600, s // 60 % 60))


_ANSI_TODO = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78Mc]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def limpar_linha(txt):
    """Uma linha de um programa (apt, curl, o instalador) pronta para o ecra: sem codigos de cor nem de cursor, sem
    caracteres de controlo e, se o programa redesenhou a mesma linha com \\r (barras de progresso), so o que ficou
    no fim. O curses nao sabe o que fazer com um ESC ou um \\r e estraga a linha toda."""
    txt = _ANSI_TODO.sub("", txt)
    if "\r" in txt:
        partes = [p for p in txt.split("\r") if p.strip()]
        txt = partes[-1] if partes else ""
    return _CTRL.sub("", txt.expandtabs(4)).rstrip()


def limpar_log(linhas):
    """As linhas de um registo limpas, sem linhas vazias seguidas e sem o ruido do apt/dpkg (lista de ficheiros
    descomprimidos, "Reading database ...")."""
    out = []
    for ln in linhas:
        ln = limpar_linha(str(ln))
        if re.match(r"^(\(Reading database|Reading database|Preparing to unpack|Unpacking|A ler a base|A preparar para)", ln):
            continue
        if re.search(r"(\.\.\.|:)\s+\d{1,3}%$", ln):
            continue                        # um passo de uma barra de progresso que chegou partido em linhas (o \r)
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return out


def wrap_lines(lines, width):
    """Linhas (texto ou (texto, cor)) -> linhas partidas a largura do ecra."""
    out = []
    for item in lines:
        text, color = item if isinstance(item, tuple) else (item, "")
        if not text:
            out.append(("", color))
            continue
        for part in textwrap.wrap(text, width, subsequent_indent="    ") or [""]:
            out.append((part, color))
    return out


# ---- Bloqueio do ecra (Win+L, ou F12) ---------------------------------------------------------------------------------
# O Atom fica ligado no meio da filmagem com a consola aberta. O bloqueio tapa-a e so o PIN da pagina (o mesmo do
# GLOBAL SETTINGS > Web PIN) a volta a abrir. A gravacao nao e tocada: a consola so mostra, quem grava e o bridge.
# A tecla Windows nao chega a um terminal de texto (o teclado do Atom e lido directamente em /dev/input, por isso o
# Win+L so funciona no ecra do Atom e com a consola a correr como root). O F12 faz o mesmo e vale tambem por SSH.
LOCK_FILE = "/run/bridge-ctl.locked"        # existe enquanto bloqueado (em /run: um reinicio do Atom desbloqueia)
PIN_FILES = ("/opt/camera-bridge/pin.json",
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "atom", "app", "pin.json"))
EV_KEY, KEY_L, KEY_LEFTMETA, KEY_RIGHTMETA = 1, 38, 125, 126
VT_LOCKSWITCH, VT_UNLOCKSWITCH = 0x560B, 0x560C      # linux/vt.h: Ctrl+Alt+Fn deixa de trocar de consola
PIN_ESPERA_MAX = 5                                    # s de espera depois de um PIN errado (sobe 1 s por erro)


def pin_guardado():
    """O PIN da pagina como o acesso.py o guarda (sal + PBKDF2), ou None se nao ha."""
    for caminho in PIN_FILES:
        try:
            with open(caminho) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("hash") and d.get("salt"):
            return d
    return None


def pin_certo(pin, d):
    """O PIN bate com o guardado? Comparacao em tempo constante. Sem bloqueio por erros (ver Bloqueio._ecra)."""
    if not (isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 12):
        return False
    try:
        h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(d["salt"]), int(d.get("iterations") or 200000)).hex()
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(h, str(d.get("hash")))


def tecla_no_mapa(palavras, n, bits=None):
    """A tecla n esta no mapa de teclas do sysfs (/sys/class/input/eventN/device/capabilities/key)? O mapa vem como
    palavras em hexadecimal, a mais significativa primeiro, cada uma com o tamanho de um `long` da maquina."""
    bits = bits or struct.calcsize("l") * 8
    i = n // bits
    return i < len(palavras) and bool((palavras[-1 - i] >> (n % bits)) & 1)


def teclados_win_l():
    """Os /dev/input/eventN que tem a tecla L e a tecla Windows (um teclado)."""
    out = []
    for p in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(p + "/device/capabilities/key") as f:
                palavras = [int(x, 16) for x in f.read().split()]
        except (OSError, ValueError):
            continue
        if tecla_no_mapa(palavras, KEY_L) and (tecla_no_mapa(palavras, KEY_LEFTMETA)
                                               or tecla_no_mapa(palavras, KEY_RIGHTMETA)):
            out.append("/dev/input/" + os.path.basename(p))
    return out


def vt_trancar(sim):
    """Desliga (ou volta a ligar) a troca de consolas virtuais (Ctrl+Alt+Fn). Precisa de root; sem ele fica como esta."""
    if fcntl is None:
        return
    try:
        fd = os.open("/dev/tty0", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return
    try:
        fcntl.ioctl(fd, VT_LOCKSWITCH if sim else VT_UNLOCKSWITCH)
    except OSError:
        pass
    finally:
        os.close(fd)


class AtalhoWinL(threading.Thread):
    """Le o teclado do Atom em /dev/input e chama `ao_carregar` no Win+L. Corre em fundo; sem permissao (nao e root)
    ou sem teclado, fica quieto e o F12 continua a funcionar."""

    def __init__(self, ao_carregar):
        super().__init__(daemon=True, name="win-l")
        self.ao_carregar = ao_carregar
        self.meta = False            # a tecla Windows esta carregada
        self.meta_ate = 0.0          # ate quando um 'l' vindo do terminal e o do Win+L (e nao e para a consola)
        self.parar = threading.Event()

    def evento(self, codigo, valor, agora=None):
        agora = time.time() if agora is None else agora
        if codigo in (KEY_LEFTMETA, KEY_RIGHTMETA):
            self.meta = valor != 0
            self.meta_ate = agora + 0.5
        elif codigo == KEY_L and valor == 1 and self.meta:
            self.meta_ate = agora + 0.5
            self.ao_carregar()

    def engolir(self, agora=None):
        """O terminal tambem recebe o 'l' do Win+L: enquanto o Windows esta carregado (e meio segundo depois) as teclas
        nao chegam a consola."""
        agora = time.time() if agora is None else agora
        return self.meta or agora < self.meta_ate

    def run(self):
        fmt = "llHHi"                               # struct input_event (16 bytes em 32 bit, 24 em 64)
        tam = struct.calcsize(fmt)
        fds = {}
        while not self.parar.is_set():
            if not fds:
                for caminho in teclados_win_l():
                    try:
                        fds[os.open(caminho, os.O_RDONLY | os.O_NONBLOCK)] = caminho
                    except OSError:
                        pass
                if not fds:
                    self.parar.wait(5.0)           # sem teclado ou sem permissao: tenta mais tarde (USB que volta)
                    continue
            try:
                prontos, _, _ = select.select(list(fds), [], [], 1.0)
            except (OSError, ValueError):
                prontos = []
            for fd in prontos:
                try:
                    dados = os.read(fd, tam * 32)
                except BlockingIOError:
                    continue
                except OSError:                    # o teclado saiu
                    os.close(fd)
                    del fds[fd]
                    continue
                for i in range(0, len(dados) - tam + 1, tam):
                    _s, _us, tipo, codigo, valor = struct.unpack(fmt, dados[i:i + tam])
                    if tipo == EV_KEY:
                        self.evento(codigo, valor)
        for fd in fds:
            os.close(fd)


class Bloqueio:
    """O ecra de bloqueio. `TelaBloqueavel` chama-o em todos os getch/get_wch da consola, menus incluidos."""

    def __init__(self, ui):
        self.ui = ui
        self.scr = None
        self.pedido = os.path.exists(LOCK_FILE)    # reabriu-se (ou arrancou) com o ecra bloqueado: continua bloqueado
        self.atalho = None
        self.criado = False                         # este processo criou o LOCK_FILE (outra consola o pode apagar: desbloqueou)
        self.a_mostrar = False                      # o ecra de bloqueio esta aberto: o Win+L / F12 nao pedem outro
        if ui.vt:                                   # so no ecra do Atom ha teclado fisico para ler
            self.atalho = AtalhoWinL(self.pedir)
            self.atalho.start()
        if not self.pedido:
            vt_trancar(False)                       # um bloqueio que ficou preso (a consola morreu) nao prende a troca de consola

    def pedir(self):
        # A thread do Win+L nao pára com o ecra bloqueado: sem isto, um Win+L carregado ja bloqueado ficava pendente e
        # bloqueava outra vez logo a seguir ao PIN.
        if not self.a_mostrar:
            self.pedido = True

    def parar(self):
        if self.atalho:
            self.atalho.parar.set()

    def tratar(self, k):
        """True: esta tecla nao e para a consola (era o atalho, foi o Win+L, ou o ecra esteve bloqueado)."""
        if k == curses.KEY_F0 + 12:
            self.pedido = True
        # Outro pediu o bloqueio de fora (a tampa do portatil fechada: brilho.py cria o LOCK_FILE; ou uma consola por SSH):
        # um ficheiro que este processo nao criou e que apareceu com a consola aberta. Abrir a tampa nao o tira.
        if not self.pedido and not self.a_mostrar and not self.criado and os.path.exists(LOCK_FILE):
            self.pedido = True
        engolida = k not in (-1, None) and bool(self.atalho and self.atalho.engolir())
        if self.pedido:
            self.mostrar()
            return True
        return engolida

    def gravacao(self):
        """(texto, cor) do que o bridge diz agora. Mostra-se no ecra bloqueado: bloquear nunca esconde que se grava."""
        try:
            st, dv, _shoot, ok, _err, _at = self.ui.snap.get()
            if not ok:
                return "no data from the bridge", "yellow"
            n = sum(1 for r in build_rows(st, dv) if r.get("rec"))
            if bool((st.get("f6") or {}).get("frc", {}).get("recording")):
                n += 1
            return ("RECORDING (%d)" % n, "red") if n else ("not recording", "green")
        except Exception:                           # pylint: disable=broad-except
            return "", "white"

    def mostrar(self):
        self.pedido = False
        d = pin_guardado()
        if d is None:
            if os.path.exists(LOCK_FILE):           # estava bloqueado e o PIN foi retirado: ja nao ha com que desbloquear
                try:
                    os.remove(LOCK_FILE)
                except OSError:
                    pass
                vt_trancar(False)
            else:
                self.ui.say("Cannot lock: set a Web PIN first", 2)
            return
        self.criado = False
        try:
            fd = os.open(LOCK_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
            self.criado = True
        except OSError:
            pass                                    # sem /run: bloqueia na mesma, so nao sobrevive a um reinicio da consola
        vt_trancar(True)
        antigos = {}
        for nome in ("SIGINT", "SIGTSTP", "SIGQUIT"):       # Ctrl+C, Ctrl+Z e Ctrl+barra nao saem da consola
            s = getattr(signal, nome, None)
            if s is None:
                continue
            try:
                antigos[s] = signal.signal(s, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        curses.raw()
        self.a_mostrar = True
        try:
            desbloqueado = self._ecra(d)
        finally:
            self.a_mostrar = False
            curses.cbreak()
            for s, h in antigos.items():
                try:
                    signal.signal(s, h)
                except (ValueError, OSError):
                    pass
        if desbloqueado:
            self.pedido = False                     # um Win+L / F12 que chegou entre o PIN e aqui nao volta a bloquear
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
            self.criado = False                     # o proximo bloqueio vindo de fora (a tampa) volta a ser visto
            vt_trancar(False)
        curses.flushinp()
        self.scr.clearok(True)
        self.scr.touchwin()

    def _ecra(self, d):
        scr = self.scr
        entrada, msg, ate, erros = "", "", 0.0, 0
        scr.timeout(250)
        while True:
            if self.criado and not os.path.exists(LOCK_FILE):
                return True                         # outra consola (SSH) desbloqueou com o PIN
            d = pin_guardado()                      # le-se sempre: um PIN mudado vale ja, e sem PIN nao ha com que desbloquear
            if d is None:
                return True
            self._desenhar(entrada, msg)
            try:
                k = scr.get_wch()
            except curses.error:
                continue                            # passaram 250 ms: volta a desenhar (a hora e o estado andam)
            if time.time() < ate:
                continue                            # castigo por um PIN errado: as teclas nao contam
            msg = ""
            if isinstance(k, str) and k.isdigit() and len(entrada) < 12:
                entrada += k
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                entrada = entrada[:-1]
            elif k == "\x1b":
                entrada = ""
            elif k in ("\n", "\r", curses.KEY_ENTER):
                if pin_certo(entrada, d):
                    return True
                erros += 1
                espera = min(PIN_ESPERA_MAX, erros)
                ate = time.time() + espera
                msg = "wrong PIN: wait %d s" % espera
                entrada = ""

    def _desenhar(self, entrada, msg):
        scr = self.scr
        try:
            h, w = scr.getmaxyx()
            scr.erase()
            grav, cor = self.gravacao()
            cores = {"red": 1, "green": 2, "yellow": 3, "white": 7} if self.ui.colors else {}
            par = curses.color_pair(cores.get(cor, 0)) if cores else 0
            linhas = [("LOCKED", curses.A_BOLD | curses.A_REVERSE),
                      (time.strftime("%H:%M:%S"), 0),
                      ("", 0),
                      (grav, par | curses.A_BOLD),
                      ("", 0),
                      ("PIN: " + "*" * len(entrada), curses.A_BOLD),
                      (msg, par if msg else 0),
                      ("", 0),
                      ("Enter = unlock    Win+L / F12 = lock", curses.A_DIM)]
            y0 = max(0, (h - len(linhas)) // 2)
            for i, (txt, attr) in enumerate(linhas):
                if y0 + i >= h:
                    break
                txt = txt[:max(0, w - 1)]
                scr.addstr(y0 + i, max(0, (w - len(txt)) // 2), txt, attr)
            scr.refresh()
        except curses.error:
            pass


class TelaBloqueavel:
    """Embrulha a janela do curses: todo o getch/get_wch da consola passa pelo bloqueio, mesmo dentro dos menus."""

    def __init__(self, scr, bloqueio):
        self._scr = scr
        self._b = bloqueio
        self._ms = -1
        bloqueio.scr = scr

    def __getattr__(self, nome):
        return getattr(self._scr, nome)

    def timeout(self, ms):
        self._ms = ms
        self._scr.timeout(ms)

    def getch(self):
        k = self._scr.getch()
        if self._b.tratar(k):
            self._scr.timeout(self._ms)
            return -1
        return k

    def get_wch(self):
        try:
            k = self._scr.get_wch()
        except curses.error:
            if self._b.tratar(None):
                self._scr.timeout(self._ms)
            raise
        if self._b.tratar(k):
            self._scr.timeout(self._ms)
            raise curses.error("locked")
        return k


class Ui:
    def __init__(self, scr, snap):
        self.scr = scr
        self.snap = snap
        self.cache = Cache()
        self.cur = 0
        self.on_f6 = False                 # o cursor esta na linha do F6 (a ultima; nao vem de build_rows)
        self.has_f6 = False
        self._upd_pedido = 0.0             # quando se pediu a actualizacao do GitHub (o registo velho nao se mostra)
        self.on_gw = None                  # o cursor esta numa linha dos GATEWAYS (indice em self.gws)
        self.gws = []
        self.msg = ("", 0.0, 0)
        self.rows = []
        self.pending_order = None          # ordem das camaras mudada e ainda nao guardada
        self.targets = []
        # O cursor segue o device e nao a posicao: quando a lista muda (pela pagina) volta-se a achar o id.
        self.cur_id = None
        self._cur_visto = None             # o self.cur no ultimo desenho (se as teclas o mudaram, manda o cur)
        v = menu_ordem_ler("dashboard-view")
        self.vista = v[0] if v and v[0] in VISTAS else VISTAS[0]    # a vista da lista de camaras (<- ->), lembrada
        self.dev_top = 0                   # a primeira linha de device visivel (ecra pequeno: a lista desliza)
        self.colors = curses.has_colors()
        if self.colors:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for i, fg in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                                    curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_MAGENTA,
                                    curses.COLOR_WHITE), 1):
                curses.init_pair(i, fg, bg)
            # Blocos com fundo de cor (titulos, selecao, rotulos do rodape): letra
            # branca nos fundos escuros (vermelho, magenta, castanho/amarelo da
            # consola, azul), preta nos claros (branco, ciano, verde). Com
            # A_REVERSE a letra ficava sempre preta e mal se lia nos escuros.
            W, K = curses.COLOR_WHITE, curses.COLOR_BLACK
            self.hl_pairs = {}
            for n, (nome, fundo, letra) in enumerate((
                    ("red", curses.COLOR_RED, W), ("magenta", curses.COLOR_MAGENTA, W),
                    ("yellow", curses.COLOR_YELLOW, W), ("blue", curses.COLOR_BLUE, W),
                    ("white", curses.COLOR_WHITE, K), ("cyan", curses.COLOR_CYAN, K),
                    ("green", curses.COLOR_GREEN, K)), 11):
                try:
                    curses.init_pair(n, letra, fundo)
                    self.hl_pairs[nome] = (n, letra == W)
                except curses.error:
                    pass
        cursor(0)
        scr.timeout(250)
        scr.keypad(True)
        # So na consola do ecra (/dev/ttyN) ha fonte para trocar; por SSH nao.
        try:
            tty = os.ttyname(sys.stdin.fileno())
        except OSError:
            tty = ""
        self.vt = tty if re.match(r"^/dev/tty[0-9]+$", tty) else ""
        self.font_changed = False
        # Win+L / F12: o bloqueio vive entre a consola e o curses (ver TelaBloqueavel).
        self.bloqueio = Bloqueio(self)
        self.scr = TelaBloqueavel(scr, self.bloqueio)

    # --- fonte da consola (so para o ecra do shoot log) --------------------
    def resync(self):
        """Depois de trocar a fonte a grelha de caracteres muda: relê o tamanho."""
        time.sleep(0.15)
        try:
            sz = os.get_terminal_size(sys.stdin.fileno())
            curses.resizeterm(sz.lines, sz.columns)
        except Exception:
            pass
        self.scr.clear()

    @staticmethod
    def as_root(cmd):
        """O setfont so funciona como root: fora disso tenta sudo sem password."""
        return cmd if os.geteuid() == 0 else ["sudo", "-n"] + cmd

    def font_small(self):
        if not self.vt or self.font_changed:
            return
        cmd = self.as_root(["setfont", "-C", self.vt, "-O", FONT_BACKUP, LOG_FONT])
        problem = None
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.font_changed = r.returncode == 0 and os.path.exists(FONT_BACKUP)
            if r.returncode != 0:
                problem = "exit code %s\n%s" % (r.returncode, (r.stderr or r.stdout or "").strip())
            elif not self.font_changed:
                problem = "setfont ran but did not save the previous font in %s" % FONT_BACKUP
        except Exception as e:
            problem = str(e)
        if problem:
            # Texto completo numa pagina: numa linha cortada nao se percebia o erro.
            lines = [("The small font was not applied.", "yellow"),
                     "", "Command: " + " ".join(cmd), "", problem, "",
                     "Running as: uid %s%s" % (os.geteuid(), "" if os.geteuid() == 0 else
                                               " (not root: setfont needs root; sudo -n was tried)")]
            self.pager("Small font", lambda: lines, live=False, cor=COR["log"])
        self.resync()

    def font_restore(self):
        if not self.font_changed:
            return
        try:
            subprocess.run(self.as_root(["setfont", "-C", self.vt, FONT_BACKUP]),
                           capture_output=True, text=True, timeout=10)
        except Exception:
            pass
        finally:
            self.font_changed = False
            try:
                os.remove(FONT_BACKUP)
            except OSError:
                pass
        self.resync()

    # --- desenho base ------------------------------------------------------
    def attr(self, name):
        if not self.colors:
            return curses.A_BOLD if name in ("red", "green", "bold") else 0
        # Azul e magenta em negrito: na consola do Linux o azul escuro mal se le.
        return {"red": curses.color_pair(1) | curses.A_BOLD,
                "green": curses.color_pair(2), "yellow": curses.color_pair(3) | curses.A_BOLD,
                "cyan": curses.color_pair(4), "blue": curses.color_pair(5) | curses.A_BOLD,
                "magenta": curses.color_pair(6) | curses.A_BOLD,
                "white": curses.color_pair(7) | curses.A_BOLD,
                "dim": curses.A_DIM, "bold": curses.A_BOLD}.get(name, 0)

    def attr_suave(self, name):
        """A cor sem negrito (descricoes ao lado de uma tecla). O amarelo fica em negrito: sem ele a consola do Linux
        desenha-o castanho."""
        a = self.attr(name)
        return a if name == "yellow" else a & ~curses.A_BOLD

    def via_cor(self, chave):
        """A cor de um transporte: fixa para o Wi-Fi e o Bluetooth; um gateway fica com a primeira cor livre da paleta e
        guarda-a (menus.json), para nao mudar entre arranques nem conforme a ordem em que os gateways aparecem."""
        if chave in TRANSP_FIXAS:
            return TRANSP_FIXAS[chave]
        if getattr(self, "transp_cores", None) is None:
            guardadas = _menus_ler_tudo()[0].get("transport-colors")
            self.transp_cores = {k: v for k, v in guardadas.items() if v in TRANSP_PALETA} if isinstance(guardadas, dict) else {}
        if chave not in self.transp_cores:
            usadas = set(self.transp_cores.values())
            livres = [c for c in TRANSP_PALETA if c not in usadas]
            self.transp_cores[chave] = livres[0] if livres else TRANSP_PALETA[len(self.transp_cores) % len(TRANSP_PALETA)]
            _menus_gravar({"transport-colors": self.transp_cores})
        return self.transp_cores[chave]

    def hl(self, name):
        """Bloco com fundo na cor `name` e letra legivel (ver hl_pairs)."""
        par = getattr(self, "hl_pairs", {}).get(name)
        if not self.colors or not par:
            return curses.A_REVERSE | self.attr(name)
        n, branca = par
        # Negrito so na letra branca (fica branco vivo); na preta dava cinzento.
        return curses.color_pair(n) | (curses.A_BOLD if branca else 0)

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass
        except UnicodeError:
            # Locale sem UTF-8 (a consola, um servico systemd): o event log do
            # bridge tem tracos longos e afins. Mostra-se com '?' em vez de falhar.
            try:
                self.scr.addnstr(y, x, text.encode("ascii", "replace").decode(),
                                 max(0, w - x - 1), attr)
            except curses.error:
                pass

    def termos(self):
        """Os avisos de licenca, uma so vez e antes de a consola carregar (o aceite fica no bridge, vale para a pagina e
        para todas as camaras). False se o operador nao aceita: a consola sai. Sem o bridge a responder (ou um mais
        antigo, sem a rota) nao ha como saber: segue-se, o bridge volta a perguntar quando houver."""
        s, d = api("GET", "/api/system/terms", timeout=6)
        if s != 200 or not isinstance(d, dict) or d.get("accepted") is not False:
            return True
        texto = [str(l) for l in d.get("text") or []]
        while True:
            self.pager("License notices", lambda: texto, live=False, cor=COR["cams"])
            r = self.ask("Accept these notices to use Atom Bridge? (y = accept, n = quit)", "yn")
            if r == "y":
                s, d = api("POST", "/api/system/terms", {"accept": True}, timeout=8)
                if s == 200:
                    return True
                self.say("Could not save the acceptance (HTTP %s): try again" % s, 2)
            elif r == "n":
                return False

    def say(self, text, level=0):
        self.msg = (text, time.time(), level)

    def st(self):
        return self.snap.get()[0]

    def dv(self):
        """O ultimo GET /api/devices bom (devices, removed, order, alias, bmpcc)."""
        return self.snap.get()[1]

    def devs(self, tipo):
        """Os devices de um tipo, {id: item}."""
        return devs_tipo(self.dv(), tipo)

    def show_msg(self, y):
        """A ultima mensagem, onde quer que se esteja (menus e paginas tambem):
        sem isto, as razoes de uma recusa so apareciam no ecra principal."""
        text, t0, level = self.msg
        if text and time.time() - t0 < MSG_SECONDS:
            self.put(y, 1, text, self.attr(("green", "yellow", "red")[level]))

    # --- ecras modais ------------------------------------------------------
    def title_bar(self, title, cor):
        """Titulo de um ecra de detalhe: uma barra na cor da area."""
        h, w = self.scr.getmaxyx()
        self.put(0, 0, (" " + title).ljust(w - 1), self.hl(cor))

    def pager(self, title, producer, live=True, cor="cyan", every=2.0, seguir=False):
        """Texto com scroll. `producer()` devolve as linhas; se `live`, e
        chamado de novo a cada `every` s (barato: so le o snapshot ou a Cache).
        `seguir`: acompanha o fim do texto (um registo que cresce, como o da actualizacao): sobe-se com as setas para ler
        o que passou, e volta-se a acompanhar ao chegar ao fim ou com End."""
        top, last, raw = 0, 0.0, []
        colar = bool(seguir)
        visto_em = time.time()
        while True:
            # Consola nova instalada (Update from GitHub) com esta pagina aberta: reabre-se ja. Antes so ao sair dela,
            # e o registo da actualizacao dizia "Console reopens by itself" e ficava parado ali.
            if time.time() - visto_em > 2:
                visto_em = time.time()
                self.verificar_versao()
            if not raw or (live and time.time() - last > every):
                try:
                    raw = producer()
                except _Reabrir:
                    raise
                except Exception as e:
                    raw = [("error: %s" % e, "red")]
                last = time.time()
            h, w = self.scr.getmaxyx()
            rows = wrap_lines(raw, max(20, w - 3))
            view = max(1, h - 4)
            fim = max(0, len(rows) - view)
            if colar:
                top = fim
            top = max(0, min(top, fim))
            self.scr.erase()
            self.title_bar(title, cor)
            for i, (text, color) in enumerate(rows[top:top + view]):
                self.put(2 + i, 1, text, self.attr(color))
            more = " (%d more)" % (len(rows) - top - view) if len(rows) - top > view else ""
            if seguir:
                more += "   [following the end]" if colar else "   [End: follow]"
            self.show_msg(h - 2)
            self.put(h - 1, 0, " up/down scroll   q or Enter: back" + more, self.attr(cor))
            self.scr.refresh()
            k = self.scr.getch()
            if k in (ord("q"), ord("Q"), 27, 10, 13, curses.KEY_ENTER):
                return
            if k in (curses.KEY_UP, ord("k")):
                top -= 1
                colar = False
            elif k in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif k == curses.KEY_PPAGE:
                top -= view
                colar = False
            elif k == curses.KEY_NPAGE:
                top += view
            elif k == curses.KEY_HOME:
                top = 0
                colar = False
            elif k == curses.KEY_END:
                top = len(rows)
            if seguir and top >= fim and k != -1:
                colar = True                       # chegou ao fim: volta a acompanhar

    def save_undo(self, texto):
        """Pergunta na linha de baixo: S guarda, U (ou qualquer outra tecla) desfaz."""
        h, w = self.scr.getmaxyx()
        self.put(h - 2, 0, " " * (w - 1))
        self.put(h - 2, 1, texto + "   S/Enter save   U/Esc undo", self.attr("yellow") | curses.A_BOLD)
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            while True:
                k = self.scr.getch()
                if k in (ord("s"), ord("S"), ord("y"), ord("Y"), 10, 13, curses.KEY_ENTER):
                    return "s"                      # Enter e Y tambem guardam
                if k in (ord("u"), ord("U"), ord("n"), ord("N"), 27):
                    return "u"
                # qualquer outra tecla: continua a esperar (antes contava como "desfazer")
        finally:
            self.scr.timeout(250)

    def menu(self, title, items, cor="cyan", keys=None, ordem=None):
        """Lista para escolher. Devolve o indice, ou None se se cancelou.

        `cor` e a cor da area (titulo e selecao). Cada item pode ser um texto ou
        (texto, cor), quando uma opcao pertence a outra area (REC, LOG...), ou um
        Sec("titulo"): cabecalho de seccao, que nao se escolhe. O indice
        devolvido conta tambem os cabecalhos (e a posicao na lista dada).

        Numeros 1-9 so em menus curtos sem seccoes: num menu maior que isso os
        numeros deixavam de corresponder ao que se ve. Valem tambem com `keys`
        (letras): o menu Enter de uma linha tem os mesmos numeros da barra 1-9.
        Os numeros mandam: uma tecla de `keys` que seja um algarismo nao conta.

        `ordem` (um nome): PageUp/PageDown mudam o item de sitio, dentro da sua
        seccao; [ e ] mudam a seccao inteira. Ao sair com a ordem mudada pergunta-se
        se guarda (S) ou desfaz (U). F2 (ou Ctrl+R) muda o nome do item ou da seccao
        (fica logo guardado; um nome vazio repoe o original).
        O indice devolvido e sempre o da lista dada, nao o do ecra."""
        items = [it if isinstance(it, (tuple, Sec)) else (it, cor) for it in items]
        if not [i for i, it in enumerate(items) if not isinstance(it, Sec)]:
            return None
        keys = keys or {}                  # letra -> indice (da lista dada): salta e corre o item
        numeros = len(items) <= 9 and not [it for it in items if isinstance(it, Sec)]
        if numeros:
            keys = {k: v for k, v in keys.items() if not k.isdigit()}
        # perm[posicao no ecra] = indice na lista dada. A ordem guardada aplica-se
        # dentro de cada seccao; o que ela nao conhece fica onde estava.
        perm = list(range(len(items)))
        if ordem:
            guardada = menu_ordem_ler(ordem)
            if guardada:
                # Ordens gravadas antes de "Camera  <nome>" ter dois espacos: "Camera R10"
                # e "Camera BMPCC" eram itens diferentes. Passam a ser o mesmo, "Camera".
                guardada = ["Camera" if g.startswith("Camera ") else g for g in guardada]
                seg, novo = [], []
                for i in perm + [None]:
                    if i is None or isinstance(items[i], Sec):
                        # Os itens que a ordem gravada conhece vao pela ordem dela; os que
                        # nao conhece ficam no lugar que tinham (antes iam para o fim).
                        conhecidos = sorted((j for j in seg if _chave_item(items[j]) in guardada),
                                            key=lambda j: guardada.index(_chave_item(items[j])))
                        it_ = iter(conhecidos)
                        novo += [j if _chave_item(items[j]) not in guardada else next(it_) for j in seg]
                        seg = []
                        if i is not None:
                            novo.append(i)
                    else:
                        seg.append(i)
                perm = novo
        nomes = menu_nomes(ordem) if ordem else {}
        nomes_sec = menu_seccoes(ordem) if ordem else {}

        def blocos(pp):
            """A lista do ecra em blocos: o que vem antes da primeira seccao, e cada seccao com titulo (com os itens
            e as linhas em branco que a seguem). [(titulo ou None, [indices])]."""
            out = [(None, [])]
            for i in pp:
                if isinstance(items[i], Sec) and str(items[i]).strip():
                    out.append((str(items[i]), [i]))
                else:
                    out[-1][1].append(i)
            return out
        if ordem:
            so = menu_secordem(ordem)
            if so:
                bl = blocos(perm)
                cab, resto = bl[0], bl[1:]
                conhecidos = sorted((b for b in resto if b[0] in so), key=lambda b: so.index(b[0]))
                it_ = iter(conhecidos)
                resto = [next(it_) if b[0] in so else b for b in resto]
                perm = cab[1] + [i for b in resto for i in b[1]]
        original = list(perm)
        sujo = False
        sel = next(n for n, i in enumerate(perm) if not isinstance(items[i], Sec))

        def sair(valor, correr=True):
            """Com a ordem mudada, pergunta se guarda (S) ou desfaz (U) antes de sair.
            Enter, atalhos e numeros NAO correm o item nesse caso: so decidem a ordem e
            o menu continua (devolve _FICA). Q/Esc decidem e saem."""
            nonlocal sujo, original
            if not sujo:
                return valor
            if self.save_undo("Menu order changed") == "s":
                onde = _menus_gravar({ordem: [_chave_item(items[i]) for i in perm if not isinstance(items[i], Sec)],
                                      ordem + "#secorder": [b[0] for b in blocos(perm)[1:]]})
                self.say("Menu order saved in %s" % onde if onde else "Could not save the menu order (no writable folder)",
                         1 if onde else 2)
                if onde:
                    original = list(perm)
            else:
                self.say("Menu order undone", 1)
                perm[:] = original
            sujo = False
            return valor if not correr else _FICA

        while True:
            h, w = self.scr.getmaxyx()
            self.scr.erase()
            self.title_bar(title, cor)
            vis = [items[i] for i in perm]
            dkeys = {k: perm.index(v) for k, v in keys.items()}
            maior = max([len(nomes_sec.get(str(it), str(it))) if isinstance(it, Sec) else len(renomear_rotulo(it[0], nomes))
                         for it in vis] or [0])
            ncols, nlin, col_w = menu_colunas(len(vis), h, w, maior)
            first = max(0, min(sel - (h - 6), len(vis) - (h - 4))) if ncols == 1 and len(vis) > h - 4 else 0
            ate = len(vis) if ncols == 2 else first + max(1, h - 4)
            for n, it in enumerate(vis[first:ate], first):
                if ncols == 2:                  # duas colunas: a primeira desce ate nlin, a segunda continua
                    y, x0, cw = 2 + n % nlin, (n // nlin) * col_w, col_w
                else:
                    y, x0, cw = 2 + n - first, 0, w
                if isinstance(it, Sec):
                    self.put(y, x0 + 1, nomes_sec.get(str(it), str(it)), self.attr(cor) | curses.A_BOLD)
                    continue
                label, c = it
                label = renomear_rotulo(label, nomes)
                style = self.hl(c) if n == sel else self.attr(c)
                self.put(y, x0, " " * (cw - (2 if ncols == 2 else 1)), self.hl(c) if n == sel else 0)
                self.put(y, x0 + 1, ("%d  %s" % (n + 1, label)) if numeros else ("   " + label), style)
                tecla = next((kk for kk, v in dkeys.items() if v == n), None)
                if tecla:
                    self.put(y, x0 + cw - 6 if ncols == 2 else max(20, w - 8), "[%s]" % tecla.upper(), style)
            self.show_msg(h - 2)
            if sujo:
                self.put(h - 2, 1, "ORDER CHANGED: it asks save or undo when you leave", self.attr("yellow") | curses.A_BOLD)
            # So o essencial; mover, renomear e saltar por letra estao na ajuda (F1 > Keys).
            self.put(h - 1, 0, " Enter run   %sEsc back" % ("1-9 jump   " if numeros else ""), self.attr(cor))
            self.scr.refresh()
            k = self.scr.getch()
            escolhiveis = [n for n, i in enumerate(perm) if not isinstance(items[i], Sec)]
            # Sem fim: do primeiro salta-se para o ultimo e vice-versa.
            if k in (curses.KEY_UP, ord("k")):
                sel = max([n for n in escolhiveis if n < sel] or [escolhiveis[-1]])
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = min([n for n in escolhiveis if n > sel] or [escolhiveis[0]])
            elif ncols == 2 and k in (curses.KEY_LEFT, curses.KEY_RIGHT):
                # De uma coluna para a outra, na linha mais proxima que se escolha.
                alvo = sel + (nlin if k == curses.KEY_RIGHT else -nlin)
                alvo = min(alvo, len(vis) - 1)
                cand = [n for n in escolhiveis if n // nlin == alvo // nlin] if alvo >= 0 else []
                if cand and alvo // nlin != sel // nlin:
                    sel = min(cand, key=lambda n: abs(n - alvo))
            elif ordem and k in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                j = sel + (-1 if k == curses.KEY_PPAGE else 1)
                if 0 <= j < len(perm) and not isinstance(items[perm[j]], Sec):
                    perm[sel], perm[j] = perm[j], perm[sel]
                    sel, sujo = j, perm != original
            elif ordem and k in (ord("["), ord("]")):
                # A seccao inteira (titulo, itens e linhas em branco) troca com a de cima / de baixo
                bl = blocos(perm)
                n_bl = next(n for n, b in enumerate(bl) if perm[sel] in b[1])
                alvo = n_bl + (-1 if k == ord("[") else 1)
                if n_bl == 0 or not 1 <= alvo < len(bl):
                    self.say("Only a titled section moves, and not past the first or last", 1)
                else:
                    bl[n_bl], bl[alvo] = bl[alvo], bl[n_bl]
                    item_sel = perm[sel]
                    perm[:] = [i for b in bl for i in b[1]]
                    sel, sujo = perm.index(item_sel), perm != original
            elif ordem and k in (curses.KEY_F2, 18):         # F2 ou Ctrl+R
                item = items[perm[sel]]
                sec = next((b[0] for b in blocos(perm) if perm[sel] in b[1]), None)
                o_que = "i"
                if sec:
                    o_que = self.ask("Rename: I this item   S its section (%s)   (Esc cancels)"
                                     % nomes_sec.get(sec, sec), "isIS").lower()
                if o_que == "i":
                    chave = _chave_item(item)
                    novo = self.prompt("New name for '%s' (empty: back to the original)" % chave,
                                       nomes.get(chave, chave))
                    if novo is not None:
                        onde = menu_renomear(ordem, "names", chave, novo)
                        nomes = menu_nomes(ordem)
                        self.say("Renamed" if onde else "Could not save the name (no writable folder)", 1 if onde else 2)
                elif o_que == "s":
                    novo = self.prompt("New name for the section '%s' (empty: back to the original)" % sec,
                                       nomes_sec.get(sec, sec))
                    if novo is not None:
                        onde = menu_renomear(ordem, "sections", sec, novo)
                        nomes_sec = menu_seccoes(ordem)
                        self.say("Renamed" if onde else "Could not save the name (no writable folder)", 1 if onde else 2)
            elif k in (10, 13, curses.KEY_ENTER):
                r_ = sair(perm[sel])
                if r_ is not _FICA:
                    return r_
                sel = min(sel, len(perm) - 1)
            elif numeros and ord("1") <= k <= ord("9") and k - ord("1") < len(perm):
                r_ = sair(perm[k - ord("1")])
                if r_ is not _FICA:
                    return r_
            elif 32 < k < 127 and chr(k).lower() in keys:
                r_ = sair(keys[chr(k).lower()])
                if r_ is not _FICA:
                    return r_
            elif k in (ord("q"), ord("Q"), 27):
                return sair(None, correr=False)

    def prompt(self, label, initial="", secret=False):
        """Uma linha de texto. None se se cancelou com Esc."""
        buf = list(initial)
        cursor(1)
        self.scr.timeout(-1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.put(1, 1, label, self.attr("yellow") | curses.A_BOLD)
                shown = ("*" * len(buf)) if secret else "".join(buf)
                self.put(3, 1, "> " + shown)
                self.put(h - 1, 0, " Enter ok   Esc cancel   Ctrl+U clear", self.attr("cyan"))
                self.scr.move(3, min(w - 2, 3 + len(shown)))
                self.scr.refresh()
                # get_wch e nao getch: devolve o caracter inteiro (acentos, c cedilhado, euro...). Com o getch
                # so passava o ASCII, e o resto desaparecia sem aviso -- numa palavra-passe, que se ve em
                # asteriscos, nao se dava por isso.
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                if isinstance(k, str):
                    if k in ("\n", "\r"):
                        return "".join(buf)
                    if k == "\x1b":
                        return None
                    if k in ("\x08", "\x7f"):
                        if buf:
                            buf.pop()
                    elif k == "\x15":                  # Ctrl+U: apaga a linha (um nome novo sem apagar letra a letra)
                        buf.clear()
                    elif k.isprintable():
                        buf.append(k)
                elif k == curses.KEY_ENTER:
                    return "".join(buf)
                elif k == curses.KEY_BACKSPACE:
                    if buf:
                        buf.pop()
        finally:
            cursor(0)
            self.scr.timeout(250)

    def ask(self, text, keys, aviso=None):
        """Pergunta de uma tecla na linha de mensagens. Devolve a tecla ou ''. `aviso`: texto a vermelho antes da pergunta."""
        h, w = self.scr.getmaxyx()
        y = h - len(self.footer_rows(w)) - 2       # a linha das mensagens, acima do rodape
        self.put(y, 0, " " * (w - 1))
        segs = [(" %s " % aviso, self.hl("red")), ("  ", 0)] if aviso else []
        self.put_segs(y, 1, segs + [(text, self.attr("yellow") | curses.A_BOLD)])
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            while True:
                k = self.scr.getch()
                if k == curses.KEY_RESIZE:
                    continue
                c = chr(k).lower() if 0 <= k < 256 else ""
                return c if c in keys else ""
        finally:
            self.scr.timeout(250)

    def confirm(self, text):
        """Pergunta y/n. A 1.a linha do texto e o titulo (amarelo); as outras, o corpo, em letra normal e
        partidas a largura do ecra (o curses nao trata \\n: numa so linha as palavras colavam-se)."""
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        titulo, *corpo = text.split("\n")
        self.put(1, 1, titulo, self.attr("yellow") | curses.A_BOLD)
        y = 3
        if any(c.strip() for c in corpo):
            linhas = wrap_lines(corpo, max(20, w - 5))
            cabem = max(1, h - 7)
            for ln, _cor in linhas[:cabem]:
                self.put(y, 2, ln)
                y += 1
            if len(linhas) > cabem:
                self.put(y, 2, "...")
                y += 1
            y += 1
        self.put_segs(y, 1, [(" Y ", self.hl("green")), (" yes    ", 0), (" N ", self.hl("red")), (" no", 0)])
        self.scr.refresh()
        self.scr.timeout(-1)
        try:
            k = self.scr.getch()
            return 0 <= k < 256 and chr(k).lower() == "y"
        finally:
            self.scr.timeout(250)

    # --- chamadas ao bridge ------------------------------------------------
    def call(self, label, method, path, body=None, timeout=15, base=None, gw=False):
        """Pedido que bloqueia o ecra (mostra que esta a esperar). `base`: outro servidor (o C3, por exemplo)."""
        self.scr.erase()
        self.put(1, 1, "%s ..." % label, self.attr("bold"))
        self.put(3, 1, "waiting for the %s (up to %d s)" % ("bridge" if base is None else "gateway", timeout),
                 self.attr("dim"))
        self.scr.refresh()
        out = api(method, path, body, timeout, base=base, gw=gw)
        try:
            self.snap.poll_once()
        except Exception:
            pass
        return out

    def done(self, label, status, d, extra=None):
        """Mostra o resultado. Sem detalhes e a correr bem: so uma linha em
        baixo. Com detalhes ou com erro: uma pagina, para se ler o texto todo."""
        err = d.get("error") if isinstance(d, dict) else None
        ok = status == 200 and not err and (d or {}).get("ok", True) is not False
        if ok and not extra:
            self.say("%s: ok" % label, 0)
            return True
        lines = [("%s: %s" % (label, "ok" if ok else "FAILED"), "green" if ok else "red"), ""]
        if not ok:
            lines.append(err or ("bridge offline" if status == 0 else "refused (HTTP %s)" % status))
            lines.append("")
        lines += extra or []
        self.pager(label, lambda: lines, live=False, cor="green" if ok else "red")
        return ok

    def act(self, label, method, path, body=None):
        """Pedido sem bloquear o ecra principal; o resultado vai para a mensagem."""
        def run():
            self.say("%s ..." % label, 1)
            self.snap.acao()
            status, d = api(method, path, body, timeout=15)
            if status == 200 and d.get("ok", True) is not False and not d.get("error"):
                self.say("%s: ok %s" % (label, d.get("tc") or ""), 0)
            elif status == 0:
                self.say("%s: bridge offline" % label, 2)
            else:
                self.say("%s: %s" % (label, d.get("error") or "refused (%s)" % status), 2)
            try:
                self.snap.poll_once()
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    # --- ecra principal ----------------------------------------------------
    def draw(self):
        st, dv, shoot, ok, err, at = self.snap.get()
        self.rows = build_rows(st, dv)
        po = self.pending_order
        if po is None and not dv.get("order"):
            po = menu_ordem_ler("devices-order") or None        # o bridge nao a guarda: a desta consola
        if po is not None:
            self.rows.sort(key=lambda r: po.index(r["id"]) if r["id"] in po else 1000)
        self.targets = list(st.get("rec_targets") or [])
        # O cursor segue o device: se as teclas nao o mexeram desde o ultimo desenho e a lista mudou (linhas
        # novas, tiradas ou reordenadas na pagina), volta a ficar no mesmo id. Sem o id, fica onde estava.
        ids = [r["id"] for r in self.rows]
        if self.cur == self._cur_visto and self.cur_id in ids:
            self.cur = ids.index(self.cur_id)
        self.cur = max(0, min(self.cur, len(self.rows) - 1))
        self.cur_id = ids[self.cur] if ids else None
        self._cur_visto = self.cur
        self.has_f6 = st.get("f6") is not None
        if not self.has_f6:
            self.on_f6 = False
        self.gws = gateways(st, dv)
        if self.on_gw is not None and self.on_gw >= len(self.gws):
            self.on_gw = len(self.gws) - 1 if self.gws else None
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        # Dados velhos: o /api/status ou o /api/devices sem chegar ha mais de ~6 s (ou o bridge sem responder).
        agora = time.time()
        idade_st = (agora - at) if at else None
        ca = self.snap.dev_at
        idade_cn = (agora - ca) if ca else None
        velho_st = not ok or idade_st is None or idade_st > 6
        velho_cn = velho_st or idade_cn is None or idade_cn > 6

        # Relogio do sistema (o que o NTP ou o iPhone acertou), com o dia da semana
        # na lingua do sistema. Sem o nome da maquina: e sempre o mesmo Atom.
        # A origem da hora vem ANTES do relogio, como o "LTC" antes do timecode na
        # linha de baixo, e nas cores da pagina: NTP azul.
        src = st.get("clock_sources") or {}
        if (src.get("ntp") or {}).get("ok"):
            tag, cor = "NTP", CLOCK_COR["ntp"]
        elif (src.get("iphone") or {}).get("ok"):
            tag, cor = "iPhone", "green"
        else:
            tag, cor = "CLK", "red"         # sem origem confirmada
        # Ordem: hora, minuto, segundo, dia da semana, data (dd-mm-aaaa). A hora fica na mesma coluna que a
        # do LTC, na linha de baixo.
        pad = max(0, 4 - len(tag) - 1)
        x = self.put_segs(0, 1, [(tag, self.attr(cor) | curses.A_BOLD),
                                 (" " * (1 + pad) + time.strftime("%H:%M:%S     %A  %d-%m-%Y"), self.attr("bold"))]
                          + ([("   ", 0), (" DEMO MODE ", self.attr("red") | curses.A_BOLD | curses.A_REVERSE)]
                             if ok and st.get("demo") else []))
        # Canto direito: o Atom (alimentacao e recursos), compacto. Com o dia da
        # semana por extenso e a letra grande o relogio tem prioridade: o que nao
        # cabe cai, pela ordem inversa da importancia. A versao do codigo vive em
        # GLOBAL SETTINGS > Version.
        if ok:
            segs, larg = self.atom_segs(st, w - x - 3)
            if segs:
                self.put_segs(0, w - larg - 1, segs)
        # O rodape e a barra primeiro: quantas linhas ocupam depende da largura (com a letra grande da consola nao
        # cabe numa so), e o resto arruma-se acima deles.
        foot = self.footer_rows(w)
        top_foot = h - len(foot) - 1
        barra = self.bar_linhas(w)                  # as opcoes 1-9, em quantas linhas precisarem
        msg_y = top_foot - 2 - max(1, len(barra))
        info_y = msg_y - 1                          # a info da linha seleccionada, por cima das mensagens
        tem_f6 = ok and st.get("f6") is not None
        f6_alt = self.f6_altura(st, w) if tem_f6 else 0
        # Blocos, por tarefa: 0-1 relogio e LTC (draw_tc) | rede: INT WI-FI, EXT WI-FI e, so se algo falha, BLUETOOTH |
        # GATEWAYS e uma linha por gateway | falha / dados velhos | devices | F6. Uma linha em branco entre eles
        # quando o ecra tem altura de sobra (senao a lista dos devices e que a precisa).
        bt_txt, bt_cor = bt_resumo(st) if ok else ("", "blue")
        bt_vis = ok and bt_cor != "blue"            # o Bluetooth ligado e o normal: so aparece desligado ou desconhecido
        n_net = 2 + (1 if bt_vis else 0)
        n_gw = (1 + len(self.gws)) if self.gws else 0
        # A gravar: uma faixa vermelha a toda a largura por baixo do relogio, para se ver de longe que o take corre.
        faixa = self.faixa_rec(st) if ok else None
        sobra = info_y - (2 + (1 if faixa else 0) + n_net + n_gw + 2 + len(self.rows) + f6_alt)
        g1, g2 = (1 if sobra >= 1 else 0), (1 if sobra >= 2 else 0)
        g3 = 1 if (sobra >= 3 and tem_f6) else 0
        if faixa:
            self.put(2, 0, faixa.ljust(w - 1), self.hl("red") | curses.A_BOLD)
        y_net = 2 + (1 if faixa else 0) + g1
        y_gws = y_net + n_net + g2
        y_falha = y_gws + n_gw                      # a falha (ou os dados velhos) e a linha que separa os gateways dos devices
        if not ok:
            self.put(y_net, 1, "BRIDGE OFFLINE (%s)" % (err or "-"), self.attr("red"))
            self.put(y_net + 3, 1, "check: systemctl status camera-bridge", self.attr("dim"))
            if self.rows:
                self.put(y_falha, 1, ("no fresh data for %ds" % idade_st) if idade_st is not None
                         else "no data from the bridge yet", self.attr("yellow"))
        else:
            eff = st.get("clock_effective") or "none"
            clk = "none picked" if eff == "unset" else eff
            # A fonte em uso nas cores da pagina: LTC amarelo, NTP azul.
            cor_eff = {"ltc": CLOCK_COR["ltc"], "ntp": CLOCK_COR["ntp"], "phone": "green",
                       "bmtc": "cyan"}.get(eff, "red")
            netd = self.net_info()
            ssid = st.get("uplink_ssid")
            if ssid:
                canal = (netd or {}).get("uplink_chan")
                wf = [("INT WI-FI", self.attr("green") | curses.A_BOLD),
                      ("  %s%s" % (ssid, ("  channel %s" % canal) if canal else ""), 0)]
            else:
                wf = [("INT WI-FI", self.attr("dim")), ("  none", self.attr("dim"))]
            self.put_segs(y_net, 1, wf + [("   cameras set from: ", 0),
                                          (clk.upper() if eff in ("ltc", "ntp") else clk,
                                           self.attr(cor_eff) | curses.A_BOLD)])
            self.draw_ap(y_net + 1, w)
            if bt_vis:
                self.draw_system(st, y_net + 2)
            self.draw_gws(y_gws, w)
            if st.get("fault"):
                self.put(y_falha, 1, "FAULT: %s   (X clears it)" % (st.get("fault_reason") or "unknown"),
                         self.attr("red"))
            elif velho_st:
                self.put(y_falha, 1, "no fresh data for %ds" % (idade_st or 0), self.attr("yellow"))
            elif velho_cn and any(r["id"] not in ("bmpcc", "f6") for r in self.rows):
                self.put(y_falha, 1, ("no fresh device data for %ds" % idade_cn)
                         if idade_cn is not None else "no device data yet", self.attr("yellow"))

        y = y_falha + 1
        self.put(y, 0, (" REC  VIA  %-" + str(NOME_W + 2) + "s%-12s") % ("DEVICE", "STATE") + VISTA_CAB[self.vista], self.attr("dim"))
        self.put(y, w - len(self.vista) - 5, "< %s >" % self.vista, self.attr("cyan") | curses.A_BOLD)
        # A legenda: os transportes em uso, cada um na sua cor (as marcas de cada linha usam as mesmas).
        xl = COL_V + 16 + 10 + 2 if self.vista == "STATE" else 0
        for chave, marca in dict.fromkeys(r["via"] for r in self.rows if r.get("via") and xl):
            if xl + len(marca) + 1 >= w - 14:
                break
            self.put(y, xl, marca, self.attr(self.via_cor(chave)) | curses.A_BOLD)
            xl += len(marca) + 1
        y += 1
        # Quantas linhas de device cabem acima da info (o F6 vai por baixo delas). Nao cabendo todas, a lista
        # desliza com o cursor e uma linha em cima e outra em baixo dizem quantas ficaram de fora.
        cabem = max(1, info_y - y - f6_alt - g3)
        n_rows = len(self.rows)
        if n_rows <= cabem:
            self.dev_top, vis, marcas = 0, n_rows, False
        else:
            marcas = cabem >= 3
            vis = cabem - 2 if marcas else cabem
            if self.cur < self.dev_top:
                self.dev_top = self.cur
            elif self.cur >= self.dev_top + vis:
                self.dev_top = self.cur - vis + 1
            self.dev_top = max(0, min(self.dev_top, n_rows - vis))
        if marcas:
            if self.dev_top:
                self.put(y, 6, "^ %d more above" % self.dev_top, self.attr("dim"))
            y += 1
        takes_por_camara = self.takes_por_camara(dv) if self.vista == "TAKE" else {}
        for i in range(self.dev_top, self.dev_top + vis):
            r = self.rows[i]
            armado = r["id"] in self.targets
            velho = velho_da_linha(r["id"], velho_st, velho_cn)
            # O bridge deixa connecting=True ate acabar a primeira leitura das
            # definicoes, com a camara ja ligada e a aceitar REC: nesse caso
            # esta ligada, a ler (nao "a ligar").
            if velho:                                 # sem dados frescos: nunca um REC velho como se fosse actual
                state, sa = "?" + (" was REC" if r["rec"] else ""), self.attr("dim")
            elif r.get("pending"):                    # a GoPro demora ~1 s a confirmar o comando
                state, sa = r["pending"] + "...", self.attr("yellow") | curses.A_BOLD
            elif r["rec"]:
                state, sa = "REC", self.attr("red")
            elif r["conn"] and r["busy"]:
                state, sa = "reading...", self.attr("yellow")
            elif r["conn"] and r.get("sem_video"):
                state, sa = "not video", self.attr("yellow")    # a Canon esta em modo foto: nao grava
            elif r["conn"]:
                state, sa = "ready", 0
            elif r["busy"]:
                state, sa = "connecting", self.attr("yellow")
            else:
                state, sa = "offline", self.attr("dim")
            # A ligacao vai na cor do NOME, separada do estado: assim uma camara
            # que cai a meio de um REC ve-se logo (nome vermelho com REC ao lado).
            cor_lig = "dim" if velho else "green" if r["conn"] else ("yellow" if r["busy"] else "red")
            la = self.attr(cor_lig) | curses.A_BOLD
            va = self.attr("dim") if velho else 0       # os valores velhos apagados
            batt = ("%d%%" % r["batt"]) if isinstance(r["batt"], (int, float)) and r["batt"] >= 0 else "-"
            sel = curses.A_REVERSE if (i == self.cur and not self.on_f6 and self.on_gw is None) else 0
            self.put(y, 0, " " * (w - 1), sel)
            self.put(y, 0, " REC " if armado else "     ", self.hl("red") if armado else sel)
            # Por onde passa a ligacao desta camara, na cor do transporte (no bloco da cor quando e a linha seleccionada).
            if r.get("via"):
                cor_via = self.via_cor(r["via"][0])
                self.put(y, 6, "%-4s" % r["via"][1], (self.hl(cor_via) if sel else self.attr(cor_via) | curses.A_BOLD))
            # Na linha selecionada (video inverso) a cor perdia-se: ai o nome vai
            # num bloco da cor da ligacao.
            self.put(y, 11, (" %-" + str(NOME_W) + "s") % r["label"], (self.hl(cor_lig) if cor_lig != "dim" else sel | va) if sel else la)
            self.put(y, COL_ESTADO, "%-12s" % state, sel | sa)
            if self.vista == "STATE":
                self.put(y, COL_V, "%-6s" % batt, sel | va)
                self.put(y, COL_V + 7, "%-8s" % r["left"], sel | va)
                if r.get("alias"):
                    self.put(y, COL_V + 16, "%-10.10s " % r["alias"], sel | self.attr("cyan") | curses.A_BOLD)
            elif self.vista == "IMAGE":
                self.put(y, COL_V, "%-6s" % r["mode"], sel | va)
                self.put(y, COL_V + 7, ("%-" + str(max(1, w - COL_V - 9)) + "." + str(max(1, w - COL_V - 9)) + "s") % (r.get("cfg") or "-"), sel | va)
            elif self.vista == "NETWORK":
                self.put(y, COL_V, ("%-" + str(max(1, w - COL_V - 2)) + "." + str(max(1, w - COL_V - 2)) + "s") % (
                    (r.get("net") or "-") if r["conn"] else "offline"), sel | va)
            else:
                t = takes_por_camara.get(r["id"]) or {}
                self.put(y, COL_V, "%-6.6s" % (t.get("take") or "-"), sel | va)
                self.put(y, COL_V + 6, "%-12.12s" % (t.get("clip") or "-"), sel | va)
                self.put(y, COL_V + 19, "%-8.8s" % ((t.get("timestamp") or "")[11:19] or "-"), sel | va)
            y += 1
        if marcas:
            abaixo = n_rows - self.dev_top - vis
            if abaixo:
                self.put(y, 6, "v %d more below" % abaixo, self.attr("dim"))
            y += 1

        if tem_f6:
            y = self.draw_f6(st, y + g3)

        for n, segs in enumerate(foot):
            self.put_segs(top_foot + n, 0, segs)
        for n, segs in enumerate(barra):            # uma linha em branco entre a barra e o rodape
            self.put_segs(top_foot - 1 - len(barra) + n, 0, segs)
        self.draw_info(st, info_y, w, velho_st, velho_cn)

        self.show_msg(msg_y)
        if self.pending_order is not None:
            self.put(msg_y, 1, "ORDER CHANGED: PgUp/PgDn move   S save   U undo",
                     self.attr("yellow") | curses.A_BOLD)
        self.draw_tc(refresh=False)
        scr.refresh()

    def takes_por_camara(self, dv):
        """{id: ultima linha do shoot log com take ou clip} de cada camara. O shoot log guarda o nome da camara em
        `source`: casa-se pelo nome do ecra, pelo nome inteiro no bridge e pelo alias. Pedido em segundo plano."""
        r = self.cache.get("shootlog_dash", 3, lambda: api("GET", "/api/log/shoot?limit=300", timeout=8))
        if not r or r[0] != 200 or not isinstance(r[1], dict):
            return {}
        nomes = {"bmpcc": {"BMPCC"}, "r10": {"R10"}, "r6m2": {"R6"}}
        for d in (dv or {}).get("devices") or []:
            if d.get("id") and d.get("name"):
                nomes.setdefault(d["id"], set()).add(str(d["name"]))
        por_nome = {}
        for r_ in self.rows:
            for n in nomes.get(r_["id"], set()) | {r_["label"]}:
                por_nome[n] = r_["id"]
        out = {}
        for x in r[1].get("rows") or []:                    # as mais recentes primeiro
            rid = por_nome.get(x.get("source"))
            if rid and rid not in out and (x.get("take") or x.get("clip")):
                out[rid] = x
        return out

    def muda_vista(self, passo):
        self.vista = VISTAS[(VISTAS.index(self.vista) + passo) % len(VISTAS)]
        menu_ordem_gravar("dashboard-view", [self.vista])

    def faixa_rec(self, st):
        """O texto da faixa de REC ("REC 00:03:12   take 7   3 cameras"), ou None se nada grava."""
        tr = st.get("take_rec") or {}
        n = sum(1 for r in self.rows if r.get("rec")) + (1 if ((st.get("f6") or {}).get("frc") or {}).get("recording") else 0)
        if not n and not tr:
            return None
        txt = " \u25cf REC"
        if tr.get("since"):
            t = max(0, int(time.time() - tr["since"]))
            txt += "  %02d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)
        if tr.get("take"):
            txt += "   take %s" % tr["take"]
        n = n or tr.get("cameras") or 0
        return txt + "   %d camera%s" % (n, "" if n == 1 else "s")

    def put_segs(self, y, x, segs):
        """Varios bocados de texto seguidos na mesma linha, cada um com o seu estilo."""
        for text, style in segs:
            self.put(y, x, text, style)
            x += len(text)
        return x

    def cd_inutil(self):
        """C e D nao fazem nada na linha seleccionada? A BMPCC liga-se sozinha pelo C3, as GoPro pelo S3,
        o F6 pelo cabo; nos GATEWAYS liga-se/desliga-se com 1. Ai o rodape nao os mostra."""
        if self.on_gw is not None or self.on_f6:
            return True
        if not self.rows:
            return False
        rid = self.rows[min(self.cur, len(self.rows) - 1)]["id"]
        return tipo_de(rid) in ("bmpcc", "gopro", "bmpcc_extra")

    def footer_rows(self, w):
        """Uma linha (duas num ecra estreito) so com as teclas do dia-a-dia, cada uma na cor da sua tarefa. As outras
        (G/N/M, C/D, E, PgUp/PgDn, W, X, F12, Q) estao na ajuda: F1 > Keys."""
        teclas = [("R", "rec", "rec"), ("T", "this cam", "rec"), ("SPACE", "target", "rec"),
                  ("P", "take data", "log"), ("L", "shoot log", "log")]
        if self.on_gw is not None:
            teclas = [t for t in teclas if t[0] not in ("T", "SPACE")]      # numa linha de gateway nao fazem nada
        if self.has_f6:
            teclas.append(("Z", "F6", "f6"))
        teclas += [("ESC", "menu", "sys"), ("F1", "help", "sys")]
        rows, segs, larg = [], [(" ", 0)], 1
        for tecla, txt, area in teclas:
            cor = COR[area]
            bloco = [(" " + tecla, self.attr(cor) | curses.A_BOLD), (" " + txt + " ", self.attr_suave(cor))]
            n = sum(len(t) for t, _a in bloco)
            if larg + n >= w - 1 and len(segs) > 1:
                rows.append(segs)
                segs, larg = [(" ", 0)], 1
            segs += bloco
            larg += n
        rows.append(segs)
        return rows

    def atom_segs(self, st, espaco):
        """O Atom em poucas palavras, por ordem de importancia:
        so entra o que cabe em `espaco` colunas. Devolve (segmentos, largura)."""
        pw = st.get("power") or {}
        bt = pw.get("battery") or {}
        pct = bt.get("percent")
        partes = []                                  # (texto, atributo), da mais a menos importante
        if pw.get("ac") is True:
            partes.append(("AC" + (" %d%%" % pct if pct is not None else ""), self.attr("green")))
        elif pw.get("ac") is False and pct is not None:
            # A percentagem e o tempo que falta vao juntos, na mesma cor: verde com
            # folga, amarelo a meio, vermelho quando e pouco. Quando e preocupante
            # (vermelho: menos de 30 min ou menos de 20 %) pisca.
            mins = bt.get("minutes")
            txt = "BATTERY %d%%" % pct + ((" ~%dh%02d" % (mins // 60, mins % 60)) if mins is not None else "")
            if (mins is not None and mins < 15) or pct < 10:
                cor, pisca = "red", True
            elif (mins is not None and mins < 30) or pct < 20:
                cor, pisca = "red", True
            elif (mins is not None and mins < 90) or pct < 40:
                cor, pisca = "yellow", False
            else:
                cor, pisca = "green", False
            at = self.attr(cor) | curses.A_BOLD
            if pisca and int(time.time() * 2) % 2:
                at |= curses.A_REVERSE                  # liga e desliga a 1 Hz (o ecra redesenha 4x/s)
            partes.append((txt, at))
        else:
            partes.append(("power ?", self.attr("dim")))
        tc = st.get("soc_temp_c")
        if tc is not None:
            partes.append(("CPU %sC" % tc, self.attr("red") if tc > 80 else self.attr("yellow") if tc > 65 else 0))
        mp = _num(st.get("mem_percent"))
        if mp is not None:                    # RAM em uso: amarelo acima de 85%, vermelho acima de 95%
            partes.append(("RAM %s" % ram_txt(st), self.attr("red") if mp > 95 else self.attr("yellow") if mp > 85 else 0))
        # Swap e load: como a CPU e a RAM, cor normal quando estao bem; amarelo e vermelho quando preocupam.
        # Swap: amarelo a partir de 100 MB (o limiar do preflight), vermelho a partir de 500 MB.
        # Carga do CPU (load1 / nucleos, em %): amarelo acima de 100% (ha fila), vermelho acima de 200%.
        sw, ld = _num(st.get("swap_used_mb")), _num(st.get("load1"))
        if sw is not None:
            partes.append(("swap %sM" % st["swap_used_mb"],
                           self.attr("red") if sw >= 500 else self.attr("yellow") if sw >= 100 else 0))
        if ld is not None:
            nucleos = os.cpu_count() or 1
            partes.append(("load %s" % carga_txt(st),
                           self.attr("red") if ld > 2 * nucleos else self.attr("yellow") if ld > nucleos else 0))
        escolhidas, larg = [], 0
        for txt, at in partes:
            if larg + len(txt) + 2 > espaco:
                break
            escolhidas.append((txt, at))
            larg += len(txt) + 2
        if not escolhidas:
            return [], 0
        segs = []
        for n, (txt, at) in enumerate(escolhidas):
            segs.append((txt + ("  " if n < len(escolhidas) - 1 else ""), at))
        return segs, sum(len(s[0]) for s in segs)

    def net_info(self):
        """O estado das redes (iw/nmcli) em cache; None se o bridge nao o deu."""
        net = self.cache.get("net", 10, lambda: api("GET", "/api/net/status", timeout=15))
        if net is None or net[0] != 200 or not isinstance(net[1], dict):
            return None
        return net[1]

    def ap_ssid_usb(self):
        """O nome da rede do EXT WI-FI quando ela nao esta no ar (o iw so o da com o AP ligado): vem do NetworkManager."""
        r = self.cache.get("ap_ssid", 60, lambda: api("GET", "/api/gateways/usb", timeout=15))
        return r[1].get("ap_ssid") if r and r[0] == 200 and isinstance(r[1], dict) else None

    def draw_ap(self, y, w):
        """O Wi-Fi externo (EXT WI-FI, o dongle do AP das camaras), por baixo do INT WI-FI: no ar,
        canal e quais as camaras ligadas."""
        d = self.net_info()
        if d is None:
            self.put_segs(y, 1, [("EXT WI-FI: reading ...", self.attr("dim"))])
            return
        nome = d.get("ap_ssid") or self.ap_ssid_usb() or "-"
        if not d.get("ap_up"):
            self.put_segs(y, 1, [("EXT WI-FI", self.attr("red") | curses.A_BOLD), ("  %s  offline" % nome, self.attr("red"))])
            return
        cams = d.get("ap_cams") or []
        segs = [("EXT WI-FI", self.attr("green") | curses.A_BOLD),
                ("  %s  channel %s   " % (nome, d.get("ap_chan") or "-"), 0),
                ("%d camera%s" % (len(cams), "" if len(cams) == 1 else "s"),
                 self.attr("green") if cams else self.attr("yellow"))]
        if cams:
            segs.append((": " + ", ".join(cams), 0))
        # Os outros aparelhos (gateways, iPads...) estao em Devices > WIRELESS > Settings <AP> > Devices.
        x = 1
        for n, (t, a) in enumerate(segs):           # nao passa da largura do ecra
            if x + len(t) >= w - 1:
                segs = segs[:n] + [(t[:max(0, w - 2 - x)], a)]
                break
            x += len(t)
        self.put_segs(y, 1, segs)

    def draw_gws(self, y, w):
        """GATEWAYS (sem cor) e, por baixo, uma linha por gateway ESP32 na cor do estado: verde online,
        amarelo a ligar, vermelho sem resposta, cinzento desligado no bridge (nao se procura nem da
        alarme). Seleccionam-se com as setas (sobe-se da primeira camara); Enter abre as opcoes, 1 liga/desliga."""
        if not self.gws:
            return
        self.put(y, 1, "GATEWAYS")
        for n, g in enumerate(self.gws):
            cor = GW_COR[g["state"]]
            sel = curses.A_REVERSE if self.on_gw == n else 0
            yy = y + 1 + n
            self.put(yy, 0, " " * (w - 1), sel)
            # Como nos devices: na linha seleccionada o nome vai num bloco da cor do estado.
            self.put(yy, 3, " %-4s" % g["name"][:4], (self.hl(cor) if cor != "dim" else sel) if sel
                     else self.attr(cor) | curses.A_BOLD)
            self.put(yy, 9, "%-12s" % g["what"], sel | self.attr(self.via_cor(g["id"])) | (0 if sel else curses.A_BOLD))
            # Online: so o essencial e o que destoa; o resto (endereco, tempo ligado, sinal) esta na info da linha seleccionada.
            self.put(yy, 22, g.get("short") or g["detail"] if g["state"] == "online" else g["detail"],
                     sel | (0 if g["state"] == "online" else self.attr(cor)))

    def gw_items(self, g):
        """As opcoes de um gateway na barra do dashboard (1-9), como as dos devices: (texto, accao, cor).
        So as que se aplicam: desligado so se liga; o que e do GoPro nao aparece num S3 em modo F6; as raras
        (firmware, endereco, USB, retirar...) ficam no submenu More."""
        cc = COR["cams"]
        itens = [("Switch %s" % ("OFF" if g["on"] else "ON"), lambda: self.gw_toggle(g), "red" if g["on"] else "green")]
        raros = []
        if g["id"] == "c3":
            modo = (self.st().get("c3_gateway") or {}).get("mode")
            if g["on"]:
                # Como o "Switch OFF": o botao diz o que faz (o modo actual ja esta na linha do gateway).
                acao = "Set standalone" if modo == "bridge" else "Set Bridge mode" if modo else "Set mode"
                itens += [(acao, lambda: self.gw_c3_modo(), cc)]
                if modo == "bridge":
                    itens += [("Pair camera", lambda: self.gw_c3_camera(), cc)]
            raros += [("Firmware", lambda: self.gw_firmware(g), cc),
                      ("Setup / update by USB", lambda: self.gw_usb_setup(), cc),
                      ("Address", lambda: self.gw_c3_host(), cc)]
        else:
            n = g.get("gw", 1)
            em_f6 = g.get("mode") in ("f6", "f6solo")
            if g["on"]:
                # O selector do S3, como o modo do C3: o botao diz o que faz.
                acao = "Mode  (now: %s)" % {"f6": "F6 with the Atom", "gopro": "GoPro"}.get(g.get("mode"), "?")
                itens += [(acao, lambda: self.gw_s3_modo(g), cc)]
                if not em_f6:
                    itens += [("Add GoPro", lambda: self.gopro_add(n), cc), ("QR lead", self.gopro_lead, COR["time"])]
            raros += [("LED type  (now: %s)" % (g.get("led_type") or "?"), lambda: self.gw_s3_led(g), cc)]
            if g["on"] and not em_f6:
                raros += [("Reset", lambda: self.gopro_reset_menu(n), COR["rec"])]
            raros += [("Firmware", lambda: self.gw_firmware(g), cc),
                      ("Address", lambda: self.gopro_host(n), cc), ("Add S3 gateway", self.gopro_gw_add, cc),
                      ("Setup / update by USB", lambda: self.gw_usb_setup(), cc)]
            if n > 1:
                raros += [("Remove gateway", lambda: self.gopro_gw_remove(n), COR["rec"])]
        if raros:
            itens += [("More", lambda: self.mais("%s gateway" % g["name"], raros), cc)]
        return itens

    def gw_c3_modo(self):
        """Autonomo <-> Bridge no proprio C3 (a consola corre no Atom, na mesma rede do C3)."""
        c3 = self.st().get("c3_gateway") or {}
        if not c3.get("host"):
            self.say("C3: no address", 2)
            return
        novo = "standalone" if c3.get("mode") == "bridge" else "bridge"
        if self.a_gravar() or c3.get("recording"):
            self.say("Recording: not now (stop the take first)", 2)
            return
        if novo == "standalone" and not self.confirm("Set the C3 to STANDALONE?\n\nThe bridge stops controlling this BMPCC."):
            return
        s, d = self.call("C3 mode", "POST", "/api/mode?m=%s" % novo, timeout=6, base="http://%s" % c3["host"], gw=True)
        self.done("C3 mode: %s" % ("BRIDGE" if novo == "bridge" else "STANDALONE"), s, d)
        self.snap.poll_once()

    def gw_s3_modo(self, g):
        """Os 3 modos do S3, pelo bridge (que tem o token): GoPro, F6 com o Atom (FRC-8 pela USB) e F6 autonomo (o S3 abre o
        seu Wi-Fi e serve a pagina do comando para o iPhone/Watch, sem o Atom). O S3 guarda e reinicia."""
        if not g.get("mode"):
            self.say("%s: offline" % g["name"], 3)
            return
        atual = g["mode"]
        opcoes = [("gopro", "GoPro"),
                  ("f6", "F6 with the Atom  (FRC-8)"),
                  ("f6solo", "F6 standalone")]
        marca = lambda k, t: ("%s%s" % (t, "   <- now" if k == atual else ""), COR["cams"])
        i = self.menu("Mode of %s" % g["name"], [marca(k, t) for k, t in opcoes], cor=COR["cams"])
        if i is None:
            return
        novo = opcoes[i][0]
        if novo == atual:
            self.say("%s is already in that mode" % g["name"], 1)
            return
        if self.a_gravar():
            self.say("Recording: not now (stop the take first)", 2)
            return
        corpo = {"mode": novo, "gw": g.get("gw", 1)}
        if novo == "f6":
            txt = "Set %s to F6 mode (with the Atom)?\n\nIts GoPros stop (they stay saved for GoPro mode)."
        elif novo == "gopro":
            txt = "Set %s to GoPro mode?\n\nThe F6 link through this S3 stops."
        else:
            txt = "Set %s to F6 STANDALONE?\n\nThe Atom will NOT see this S3 any more (Help: how to go back)."
        if not self.confirm(txt % g["name"]):
            return
        if novo == "f6solo":
            psk = self.prompt("Wi-Fi password of the F6 remote (8 to 63 characters):", secret=True)
            if psk is None:
                return
            if not 8 <= len(psk) <= 63:
                self.say("The password has 8 to 63 characters", 2)
                return
            again = self.prompt("Repeat the password:", secret=True)
            if again != psk:
                self.say("The two passwords differ: nothing changed", 2)
                return
            corpo["psk"] = psk
        s, d = self.call("%s mode" % g["name"], "POST", "/api/gateways/s3/mode", corpo, timeout=10)
        nome = {"f6": "F6", "gopro": "GoPro", "f6solo": "F6 standalone"}[novo]
        if novo == "f6solo" and s == 200 and isinstance(d, dict) and d.get("ok"):
            ap = str(g.get("ap") or "")
            rede = "F6-REMOTE-%s" % ap[-4:].upper() if len(ap) >= 4 else "F6-REMOTE-XXXX"
            self.pager("F6 standalone", lambda: [
                ("%s: restarting in F6 standalone mode." % g["name"], "green"), "",
                "Wi-Fi  %s" % rede, "Page   http://192.168.4.1/", "",
                ("Help: F6 link and standalone", "dim")], live=False, cor=COR["cams"])
        else:
            self.done("%s mode: %s (restarting)" % (g["name"], nome), s, d)
        self.snap.poll_once()

    def gw_s3_led(self, g):
        """O tipo de LED da placa S3: rgb (M5Stack StampS3), simple (Seeed XIAO ESP32S3) ou off. Aplica-se logo."""
        if not g.get("led_type"):
            self.say("%s: offline" % g["name"], 3)
            return
        opcoes = [("rgb", "rgb  (RGB LED: M5Stack StampS3)"), ("simple", "simple  (plain LED: Seeed XIAO ESP32S3)"),
                  ("off", "off  (no LED)")]
        i = self.menu("LED type of %s" % g["name"],
                      [("%s%s" % (t, "   <- now" if k == g.get("led_type") else ""), COR["cams"]) for k, t in opcoes],
                      cor=COR["cams"])
        if i is None:
            return
        s, d = self.call("%s LED" % g["name"], "POST", "/api/gateways/s3/led", {"type": opcoes[i][0], "gw": g.get("gw", 1)}, timeout=10)
        self.done("%s LED type: %s" % (g["name"], opcoes[i][0]), s, d)
        self.snap.poll_once()

    def gw_firmware(self, g):
        """Firmware do gateway por Wi-Fi (OTA): actualizar ou voltar ao anterior. O C3 recebe o do repositorio;
        o S3 o .bin que veio do GitHub (atom/app/fw/s3)."""
        url = "/api/gateways/c3/firmware" if g["id"] == "c3" else "/api/gateways/s3/firmware?gw=%d" % g.get("gw", 1)
        nome = "%s firmware" % g["name"]
        s, d = api("GET", url, timeout=10)
        if s != 200:
            self.done(nome, s, {"error": "update the bridge from GitHub"} if s == 404 else d)
            return
        if g["id"] == "c3":
            if d.get("error"):
                # Sem OTA (firmware antigo) ou sem resposta: com o C3 ligado por USB ao Atom grava-se por la.
                if self.confirm("%s\n\nWrite the firmware through the USB cable now? (C3 plugged into the Atom)" % d["error"]):
                    self.c3_usb_flash()
                else:
                    self.done(nome, 409, d)
                return
            novo = d.get("changed") or []
            acao = "Update  (%s)" % ", ".join(novo) if novo else "Update  (up to date)"
        else:
            b = d.get("bin")
            novo = [1] if b else []
            acao = "Update with the Atom's firmware  (%d KB)" % (b["size"] // 1024) if b else "Update  (no firmware on the Atom)"
        i = self.menu("%s   %s%s" % (nome, d.get("fw") or "?", "   (on trial)" if d.get("trial") else ""),
                      [("Update from GitHub  (the latest)", COR["rec"]), (acao, COR["cams"]),
                       ("Back to the previous firmware", COR["rec"])], cor=COR["cams"])
        if i is None:
            return
        if i == 0:
            self.gw_firmware_github(g, nome, novo)
            return
        i -= 1
        if i == 0:
            if not novo:
                self.say("%s: nothing to write" % nome, 1)
                return
            if not self.confirm("Update the %s firmware?\n\nThe gateway restarts (about 15 s)." % g["name"]):
                return
            s, r = self.call(nome, "POST", url, {}, timeout=150)
        else:
            if not self.confirm("Go back to the previous %s firmware?\n\nThe gateway restarts." % g["name"]):
                return
            s, r = self.call(nome, "POST", url + ("&" if "?" in url else "?") + "action=rollback", {}, timeout=20)
        self.done(nome, s, r)
        self.snap.poll_once()

    def gw_firmware_github(self, g, nome, novo):
        """O firmware mais recente do GitHub para este gateway: com novidades no GitHub, o Atom actualiza-se (o bridge
        reinicia) e, ao voltar, grava-o sozinho; sem novidades, grava ja o que o Atom tem (se o gateway estiver atrasado)."""
        s, d = self.call("Checking GitHub", "GET", "/api/system/update", timeout=100)
        if s != 200 or not isinstance(d, dict) or not d.get("ok"):
            self.done("Checking GitHub", s, d if isinstance(d, dict) else {"error": "bridge offline"})
            return
        if d.get("running"):
            self.pager("Update log", self._update_log_lines, cor=COR["sys"], every=1.0, seguir=True)
            return
        novos = d.get("new") or []
        fw_id = "c3" if g["id"] == "c3" else "s3-%d" % g.get("gw", 1)
        if novos:
            if not self.confirm("GitHub has %d new change(s).\n\nUpdate the Atom now? The bridge restarts (1-2 min), then "
                                "the %s firmware is written."
                                % (len(novos), g["name"])):
                return
            self._upd_pedido = time.time()
            s, r = self.call("Update from GitHub", "POST", "/api/system/update", {"then_gateways": [fw_id]}, timeout=30)
            if s != 200 or not (isinstance(r, dict) and r.get("ok")):
                self._upd_pedido = 0.0
                self.done("Update from GitHub", s, r)
                return
            self.pager("Update log", self._update_log_lines, cor=COR["sys"], every=1.0, seguir=True)
            return
        if not novo:
            self.done(nome, 200, {"ok": True}, ["The %s is up to date." % g["name"]])
            return
        if not self.confirm("The Atom is up to date with GitHub.\n\nWrite its firmware to the %s now? The gateway restarts "
                            "(about 15 s)." % g["name"]):
            return
        s, r = self.call(nome, "POST", "/api/gateways/firmware", {"ids": [fw_id]}, timeout=200)
        self.done(nome, s, r, ["%s: %s" % (x["id"], x["msg"]) for x in (r.get("results") or [])] if isinstance(r, dict) else None)
        self.snap.poll_once()

    def c3_usb_flash(self):
        self.gw_usb_setup()

    def gw_usb_setup(self):
        """Configuracao inicial ou actualizacao de um gateway (C3 ou S3) ligado por USB ao Atom: identifica a placa, grava-lhe o
        firmware (de raiz se estiver em branco) e o Wi-Fi da rede das cameras. A configuracao inicial faz-se sempre assim."""
        self.verificar_versao()
        s, d = api("GET", "/api/gateways/usb", timeout=20)
        if s == 404:
            self.done("Gateway by USB", s, {"error": "update the bridge from GitHub first"})
            return
        if s != 200 or not isinstance(d, dict):
            self.done("Gateway by USB", s, d if isinstance(d, dict) else {"error": "bridge offline"})
            return
        t = d.get("tools") or {}
        falta = [n for n, ok in (("esptool", t.get("esptool")), ("mpremote", t.get("mpremote")), ("pyserial", t.get("pyserial"))) if not ok]
        if falta:
            self.pager("Tools missing on the Atom", lambda: [
                ("Missing: %s" % ", ".join(falta), "yellow"), "",
                ("Help: Gateway setup by USB", "dim")],
                live=False, cor="yellow")
            return
        portas = d.get("ports") or []
        if not portas:
            self.say("No USB serial port: plug the gateway in", 2)
            return
        if len(portas) > 1:
            j = self.menu("Which USB port?", ["%s  %s" % (p["port"], "(Espressif)" if p.get("espressif") else
                                                          "%s:%s" % (p.get("vid") or "?", p.get("pid") or "?")) for p in portas],
                          cor=COR["cams"])
            if j is None:
                return
            porta = portas[j]["port"]
        else:
            porta = portas[0]["port"]
        opcoes = []
        if d.get("ap_wifi"):
            opcoes.append(("EXT WI-FI: %s" % d.get("ap_ssid"), "ap"))
        opcoes += [("Another network (type its name and password)", "outra"), ("Leave the Wi-Fi as it is (update only)", "nada")]
        w = self.menu("Wi-Fi for the gateway", [o[0] for o in opcoes], cor=COR["cams"])
        if w is None:
            return
        corpo = {"port": porta}
        if opcoes[w][1] == "ap":
            corpo["use_ap"] = True
        elif opcoes[w][1] == "outra":
            ssid = self.prompt("Wi-Fi network name:")
            if not ssid or not ssid.strip():
                return
            psk = self.prompt("Password (empty = open network):", secret=True)
            if psk is None:
                return
            corpo["wifi"] = {"ssid": ssid.strip(), "psk": psk}
        k = self.menu("How?", ["Update (keeps what works)", "Erase everything and install from scratch"], cor=COR["cams"])
        if k is None:
            return
        if k == 1:
            if not self.confirm("Erase the gateway completely?\n\nIts Wi-Fi and paired camera / GoPro are lost."):
                return
            corpo["erase"], corpo["mode"] = True, "setup"
        if not self.confirm("Set up / update the gateway on %s?\n\nA working gateway keeps its Wi-Fi and cameras "
                            "unless you chose a network." % porta):
            return
        corpo["async"] = True
        s, r = self.call("Gateway by USB", "POST", "/api/gateways/usb", corpo, timeout=30)
        if s != 200 or not r.get("ok"):
            self.done("Gateway by USB", s, r)
            return
        # Os passos em directo: nada de ecra parado a "pensar". O fim do texto acompanha-se sozinho.
        def linhas():
            s2, j = api("GET", "/api/gateways/usb?job=1", timeout=6)
            if s2 != 200 or not isinstance(j, dict):
                return [("Bridge offline.", "red")]
            log_ = j.get("log") or []
            L = [(x, "yellow" if ">>>" in x else "dim") for x in log_]
            res = j.get("result")
            religar = any(">>> UNPLUG" in x for x in log_[-6:]) and not any("now plug it in again" in x for x in log_[-3:])
            if j.get("running") and religar:
                L += ["", ("UNPLUG the gateway from the USB now, then plug it in again.", "yellow")]
            elif j.get("running"):
                L += ["", ("Working ... do not unplug the gateway", "yellow")]
            elif res:
                if res.get("ok"):
                    L += ["", ("DONE: %s on %s" % ((res.get("kind") or "?").upper(), res.get("port")), "green"),
                          (res.get("note") or "", "yellow" if res.get("wifi_warning") else "green"), "", ("q: back", "dim")]
                else:
                    L += ["", ("FAILED: %s" % (res.get("error") or "?"), "red"), "", ("q: back", "dim")]
            return L or ["starting ..."]
        self.pager("Gateway by USB", linhas, cor=COR["cams"], every=1.0, seguir=True)
        self.snap.poll_once()
        # Uma placa que nao respondeu como gateway a funcionar: so se apaga com a confirmacao do operador.
        s3_, j = api("GET", "/api/gateways/usb?job=1", timeout=6)
        res = (j or {}).get("result") if s3_ == 200 and isinstance(j, dict) else None
        if res and res.get("needs_erase") and not (j or {}).get("running"):
            if self.confirm("%s\n\nErase the board and reinstall? Its Wi-Fi and paired camera / GoPro are lost."
                            % (res.get("error") or "")):
                corpo["erase"] = True
                s, r = self.call("Gateway by USB", "POST", "/api/gateways/usb", corpo, timeout=30)
                if s == 200 and r.get("ok"):
                    self.pager("Gateway by USB", linhas, cor=COR["cams"], every=1.0, seguir=True)
                    self.snap.poll_once()
                else:
                    self.done("Gateway by USB", s, r)

    def m5_stick(self, acao):
        """O comando de bolso do F6 (M5StickS3) ligado por USB ao Atom: grava o programa e o Wi-Fi do S3 do F6 (F6-REMOTE-xxxx)."""
        modo = {"flash": "setup", "update": "update", "wifi": "wifi"}.get(acao)
        if not modo:
            return
        titulo = "M5StickS3 by USB"
        self.verificar_versao()
        s, d = api("GET", "/api/gateways/usb", timeout=20)
        if s == 404 or (s == 200 and isinstance(d, dict) and "m5_app" not in (d.get("images") or {})):
            self.done(titulo, s, {"error": "update the bridge from GitHub first"})
            return
        if s != 200 or not isinstance(d, dict):
            self.done(titulo, s, d if isinstance(d, dict) else {"error": "bridge offline"})
            return
        if not (d.get("tools") or {}).get("esptool"):
            self.pager("Tools missing on the Atom", lambda: [("Missing: esptool", "yellow"), "", ("Help: Gateway setup by USB", "dim")],
                       live=False, cor="yellow")
            return
        im = d.get("images") or {}
        if (modo == "setup" and not im.get("m5_full")) or (modo != "wifi" and not im.get("m5_app")):
            self.done(titulo, 200, {"error": "the M5StickS3 firmware is not on the Atom: update the bridge from GitHub"})
            return
        portas = d.get("ports") or []
        if not portas:
            self.say("No USB serial port: plug the M5StickS3 in (data cable)", 2)
            return
        if len(portas) > 1:
            j = self.menu("Which USB port?", ["%s  %s" % (p["port"], "(Espressif)" if p.get("espressif") else
                                                          "%s:%s" % (p.get("vid") or "?", p.get("pid") or "?")) for p in portas],
                          cor=COR["cams"])
            if j is None:
                return
            porta = portas[j]["port"]
        else:
            porta = portas[0]["port"]
        corpo = {"kind": "m5stick", "mode": modo, "port": porta}
        if modo in ("setup", "wifi"):
            ssid = self.prompt("S3 Wi-Fi name (F6-REMOTE-xxxx):", initial="F6-REMOTE-")
            if not ssid or not ssid.strip():
                return
            psk = self.prompt("Its password (8 to 63 characters):", secret=True)
            if psk is None:
                return
            if not 8 <= len(psk) <= 63:
                self.say("The Wi-Fi password has 8 to 63 characters", 2)
                return
            corpo["wifi"] = {"ssid": ssid.strip(), "psk": psk}
        texto = {"setup": "Install the F6 remote on the M5StickS3 on %s?\n\nThe whole board is rewritten. Wi-Fi: %s" % (
                    porta, (corpo.get("wifi") or {}).get("ssid")),
                 "update": "Update the program of the M5StickS3 on %s?\n\nIts Wi-Fi is kept." % porta,
                 "wifi": "Change the Wi-Fi of the M5StickS3 on %s to %s?\n\nThe program stays." % (
                    porta, (corpo.get("wifi") or {}).get("ssid"))}[modo]
        if not self.confirm(texto + "\n\nPlugged in? If it does not answer, hold its side reset button about 2 s until the green LED blinks."):
            return
        corpo["async"] = True
        s, r = self.call(titulo, "POST", "/api/gateways/usb", corpo, timeout=30)
        if s != 200 or not r.get("ok"):
            self.done(titulo, s, r)
            return

        def linhas():
            s2, j = api("GET", "/api/gateways/usb?job=1", timeout=6)
            if s2 != 200 or not isinstance(j, dict):
                return [("Bridge offline.", "red")]
            L = [(x, "yellow" if ">>>" in x else "dim") for x in (j.get("log") or [])]
            res = j.get("result")
            if j.get("running"):
                L += ["", ("Working ... do not unplug the M5StickS3", "yellow")]
            elif res and res.get("ok"):
                L += ["", ("DONE: M5StickS3 on %s" % res.get("port"), "green"), (res.get("note") or "", "green"), "", ("q: back", "dim")]
            elif res:
                L += ["", ("FAILED: %s" % (res.get("error") or "?"), "red"),
                      ("Hold its side reset button about 2 s until the green LED blinks, then try again.", "yellow"), "", ("q: back", "dim")]
            return L or ["starting ..."]
        self.pager(titulo, linhas, cor=COR["cams"], every=1.0, seguir=True)

    def esperar(self, titulo, pedir, pronto, segs):
        """Ecra de espera: pedir() a cada segundo ate pronto(status, dados) devolver algo. Esc cancela (None)."""
        fim, ult = time.time() + segs, 0.0
        while time.time() < fim:
            if time.time() - ult >= 1:
                ult = time.time()
                r = pronto(*pedir())
                if r is not None:
                    return r
            h, w = self.scr.getmaxyx()
            self.scr.erase()
            self.put(1, 1, titulo, self.attr("yellow") | curses.A_BOLD)
            self.put(3, 1, "%d s" % max(0, int(fim - time.time())), self.attr("dim"))
            self.put(h - 1, 0, " Esc cancel", self.attr("cyan"))
            self.scr.refresh()
            if self.scr.getch() == 27:
                return None
        self.say("%s: timeout" % titulo, 2)
        return None

    def gw_c3_camera(self):
        """A BMPCC do C3: procura, escolhe-se e escreve-se o codigo que a camera mostra (o C3 guarda o bond)."""
        st = self.st()
        # Os C3 que o bridge conhece: o principal e os das BMPCC extra. Com mais do que um, pergunta-se qual.
        opcoes = []
        if (st.get("c3_gateway") or {}).get("host"):
            opcoes.append(("C3 (main BMPCC)", st["c3_gateway"]["host"], bool(st.get("is_recording"))))
        for cid, bm in sorted(self.devs("bmpcc_extra").items()):
            if bm.get("host"):
                opcoes.append(("C3 of %s" % (bm.get("name") or cid), bm["host"], bool(bm.get("running"))))
        if not opcoes:
            self.say("C3: no address", 2)
            return
        if len(opcoes) > 1:
            j = self.menu("Pair the camera of which C3?", ["%-22s %s" % (o[0], o[1]) for o in opcoes], cor=COR["cams"])
            if j is None:
                return
            opcoes = [opcoes[j]]
        _nome_c3, host_c3, a_gravar = opcoes[0]
        # Emparelhar faz o C3 esquecer a camara actual: a meio de uma take perdia-se o controlo dela.
        if a_gravar:
            self.say("That camera is recording: pair after the take", 2)
            return
        base = "http://%s" % host_c3
        s, d = api("POST", "/api/cam/scan", base=base, timeout=4, gw=True)
        if s != 200:
            self.done("Pair camera", s, {"error": "this C3 firmware cannot pair a camera: update it"}
                      if s == 404 else d)
            return
        d = self.esperar("Searching for cameras ...",
                         lambda: api("GET", "/api/cam/scan", base=base, timeout=3),
                         lambda s, d: d if s == 200 and d.get("scan") == "done" else None, 20)
        if d is None:
            return
        found = d.get("found") or []
        if not found:
            self.say("C3: no cameras found", 2)
            return

        def mac(m):
            return ":".join(m[i:i + 2] for i in range(0, 12, 2)).upper()
        i = self.menu("Camera for the C3", ["%-24s %s %5s dBm%s" % ((f.get("name") or "?")[:24], mac(f["mac"]),
                                                                    f.get("rssi"), "  BM" if f.get("bm") else "")
                                            for f in found], cor=COR["cams"])
        if i is None:
            return
        f = found[i]
        nome = f.get("name") or mac(f["mac"])
        if not self.confirm("Pair %s with the C3?\n\nThe C3 forgets its current camera." % nome):
            return
        s, d = api("POST", "/api/cam/pair?mac=%s&t=%d" % (f["mac"], f.get("t") or 0), base=base, timeout=4, gw=True)
        if s != 200:
            self.done("Pair camera", s, d)
            return

        def estado():
            return api("GET", "/api/cam", base=base, timeout=3)

        def cancelar():
            api("POST", "/api/cam/cancel", base=base, timeout=3, gw=True)
        d = self.esperar("Connecting to %s" % nome, estado,
                         lambda s, d: d if s == 200 and d.get("pair") in ("need_code", "ok", "failed") else None, 60)
        if d is None:
            cancelar()
            return
        if d.get("pair") == "need_code":
            code = self.prompt("Code shown on the camera (6 digits):")
            if not code:
                cancelar()
                return
            s, d = api("POST", "/api/cam/code?c=%s" % code.strip(), base=base, timeout=4, gw=True)
            if s != 200:
                self.done("Pair camera", s, d)
                cancelar()
                return
            d = self.esperar("Checking the code", estado,
                             lambda s, d: d if s == 200 and d.get("pair") in ("ok", "failed") else None, 30)
            if d is None:
                return
        if d.get("pair") == "ok":
            self.say("C3: %s paired" % nome, 0)
        else:
            self.done("Pair camera", 409, {"error": d.get("error") or "pairing failed"})
        self.snap.poll_once()

    def gw_c3_host(self):
        c3 = self.st().get("c3_gateway") or {}
        h = self.prompt("C3 address:", c3.get("host") or "")
        if h is None:
            return
        s, d = self.call("C3 address", "POST", "/api/gateways/c3/host", {"host": h.strip()})
        if s == 404:
            d = {"error": "update the bridge"}
        self.done("C3 address", s, d)
        self.snap.poll_once()

    def gw_toggle(self, g):
        """Liga ou desliga um gateway no bridge. Desligar pede confirmacao."""
        if g["on"] and not self.confirm("Switch the %s gateway (%s) OFF in the bridge?\n\n"
                                        "The bridge stops looking for it (no fault).%s"
                                        % (g["name"], g["what"],
                                           "\nThe BMPCC is not reachable meanwhile."
                                           if g["id"] == "c3" else "")):
            return
        url = "/api/gateways/c3/enabled" if g["id"] == "c3" else "/api/gateways/s3/enabled"
        nome = "%s gateway %s" % (g["name"], "on" if not g["on"] else "off")
        corpo = {"on": not g["on"]} if g["id"] == "c3" else {"on": not g["on"], "gw": g.get("gw", 1)}
        s, d = self.call(nome, "POST", url, corpo)
        if s == 404 and not (isinstance(d, dict) and "error" in d):
            d = {"error": "update the bridge from GitHub first"}
        self.done(nome, s, d)
        self.snap.poll_once()

    def draw_system(self, st, y):
        """O Bluetooth: se o adaptador existe e esta ligado."""
        txt, cor = bt_resumo(st)
        txt = txt.replace("Bluetooth", "BLUETOOTH")          # so no dashboard; o menu fica como esta
        segs = [(" ", 0), (txt, self.attr(cor) | (curses.A_BOLD if cor == "blue" else 0))]
        self.put_segs(y, 0, segs)

    @staticmethod
    def f6_altura(st, w):
        """Quantas linhas o draw_f6 ocupa (para a lista dos devices saber quanto espaco lhe sobra)."""
        f6 = st.get("f6") or {}
        frc = f6.get("frc") or {}
        n = 2                                                # a linha do F6 e a do timecode
        if not frc.get("connected"):
            return n
        arm = [a for a in (frc.get("armed") or []) if 1 <= a <= 8]
        n += (len(arm) + 2) // 3 if arm else 1               # a grelha de 3 colunas, ou o aviso sem pistas
        return n + (1 if f6.get("note") else 0)

    def draw_f6(self, st, y):
        """O Zoom F6 no ecra principal: uma linha de device (a ultima da lista) e, por baixo, o timecode e os
        medidores das pistas armadas em grelha de 3 colunas. Devolve o y seguinte."""
        f6 = st.get("f6") or {}
        frc = f6.get("frc") or {}
        h, w = self.scr.getmaxyx()
        alvo = "f6" in self.targets
        sel_f6 = self.on_f6
        rv = curses.A_REVERSE if sel_f6 else 0               # seleccionado: a barra toda em video inverso, como as camaras
        lig = frc.get("connected")
        rec = frc.get("recording")
        cor_lig = "green" if lig else "red"
        self.put(y, 0, " " * (w - 1), rv)
        self.put(y, 0, " REC " if alvo else "     ", self.hl("red") if alvo else rv)
        self.put(y, 6, " %-7s" % "ZOOM F6", self.hl(cor_lig) if sel_f6 else self.attr(cor_lig) | curses.A_BOLD)
        if not lig:
            estado, ea = "offline", self.attr("dim")
        elif rec:
            dur = ""
            if frc.get("rec_since"):
                t = max(0, int(time.time() - frc["rec_since"]))
                dur = " %d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)
            estado, ea = "REC" + dur, self.attr("red")
        else:
            estado, ea = "online", 0
        self.put(y, 15, "%-12s" % estado, rv | ea)
        if lig and frc.get("via") == "s3" and w > 40:
            self.put(y, 28, "via S3 gateway %s" % (frc.get("gateway") or ""), rv | self.attr("dim"))
        y += 1
        # Timecode (jam)
        if f6.get("jamming"):
            quem = f6.get("jam_to") or "F6"
            tc = [("JAM%s %s" % ("" if quem == "F6" else " " + quem, f6.get("tc") or "-"),
                   self.attr("yellow") | curses.A_BOLD)]
        elif f6.get("last_jam"):
            tc = [("TC jam %s ago" % fmt_ago(f6["last_jam"]), self.attr("dim"))]
        else:
            tc = [("TC not jammed", self.attr("yellow"))]
        self.put_segs(y, 0, [("   ", 0)] + tc)
        y += 1
        if not lig:
            return y
        # Um medidor por pista armada, em grelha de 3 colunas (as saidas L/R so no ecra Z).
        arm = [a - 1 for a in (frc.get("armed") or []) if 1 <= a <= 8]
        m = (frc.get("meters") or []) + [0] * 10
        top = frc.get("meter_max") or 12
        if not arm:
            if alvo:
                # Alvo de REC sem pistas: o bridge recusa o REC. Pisca vermelho/branco para se ver.
                at = self.hl("red") if int(time.time() * 2) % 2 else (curses.A_REVERSE | curses.A_BOLD)
                self.put(y, 3, " NO TRACK ARMED: REC BLOCKED ", at)
            else:
                self.put(y, 3, "NO TRACK ARMED on the F6", self.attr("yellow") | curses.A_BOLD)
            y += 1
        nomes = f6.get("track_names") or {}
        clips = (frc.get("clips") or []) + [0] * 8
        cols = 3
        cw = max(18, (w - 4) // cols)
        mw = max(4, cw - 11)                                 # 6 de nome + espaco + medidor + " nn" + folga
        for n0 in range(0, len(arm), cols):
            x = 3
            for i in arm[n0:n0 + cols]:
                nome = "%d %s" % (i + 1, nomes.get(str(i + 1), ""))
                segs = [("%-6.6s " % nome, 0)]
                segs += [(t, self.attr(c)) for t, c in meter_segs(m[i], mw, top)]
                clip = m[i] >= top or clips[i]
                segs += [(" %2d" % m[i], self.attr("red") | curses.A_BOLD if clip else self.attr("dim"))]
                self.put_segs(y, x, segs)
                x += cw
            y += 1
        if f6.get("note"):
            self.put(y, 3, "note: %s" % f6["note"], self.attr("dim"))
            y += 1
        return y

    # --- ecra do F6 ----------------------------------------------------------
    def f6_lines(self, f6=None):
        st = self.st()
        f6 = f6 if f6 is not None else (st.get("f6") or {})
        frc = f6.get("frc") or {}
        L = []
        if not f6:
            return [("F6: not reported by the bridge", "red")]
        alvo = "f6" in (st.get("rec_targets") or [])
        L.append(("REC target: %s" % ("YES" if alvo else "no"), "green" if alvo else "yellow"))
        lig = frc.get("connected")
        rec = frc.get("recording")
        L.append(("FRC-8 link: %s" % ("connected" if lig else "NOT CONNECTED"), "green" if lig else "red"))
        if lig:
            L.append(("Recording: %s" % ("-" if rec is None else ("YES" if rec else "no")),
                      "red" if rec else "white"))
            arm = frc.get("armed") or []
            L.append(("Armed tracks: %s" % ("unknown (arm or disarm one on the F6)" if frc.get("armed") is None
                                            else " ".join(map(str, arm)) if arm else "NONE"),
                      "white" if arm else "yellow"))
            m = (frc.get("meters") or []) + [0] * 10
            top = frc.get("meter_max") or 12
            L.append("")
            n = min(8, frc.get("tracks") or 6)
            nomes = f6.get("track_names") or {}
            fad = (frc.get("faders") or []) + [None] * 9
            pan = (frc.get("pan") or []) + [None] * 8
            trim = (frc.get("trim") or []) + [None] * 8
            clips = (frc.get("clips") or []) + [0] * 8
            pfl = frc.get("pfl") or []
            L.append(("              level                       fader  pan   trim", "dim"))
            for i in list(range(n)) + [8, 9]:
                armada = i >= 8 or (i + 1) in arm
                if i < 8:
                    nome = "%d %-9.9s" % (i + 1, nomes.get(str(i + 1), ""))
                    mesa = "  %5s  %-4s  %-4s" % ("-" if fad[i] is None else fad[i], fmt_pan(pan[i]),
                                                 "-" if trim[i] is None else trim[i])
                    extra = ("  CLIP x%d" % clips[i] if clips[i] else "") + ("  PFL" if (i + 1) in pfl else "")
                else:
                    nome = "%-11s" % ("Out L", "Out R")[i - 8]
                    mesa = "  %5s" % ("-" if fad[8] is None else fad[8]) if i == 8 else ""
                    extra = ""
                L.append(MeterRow([(nome + " ", "white" if armada else "dim")]
                                  + meter_segs(m[i], 24, top, armada)
                                  + [(" %3d" % m[i], "dim"), (mesa, "white" if armada else "dim"),
                                     (extra, "red" if "CLIP" in extra else "dim")]))
            modo = frc.get("knob_mode")
            if modo or frc.get("output"):
                L.append(("Knobs on %s   output %s" % ((modo or "?").upper(), frc.get("output") or "?"), "dim"))
        L.append("")
        if f6.get("jamming"):
            L.append(("Timecode: JAM %s" % (f6.get("tc") or "-"), "yellow"))
        elif f6.get("last_jam"):
            L.append("Timecode: jam %s ago (%s)" % (
                fmt_ago(f6["last_jam"]), time.strftime("%H:%M", time.localtime(f6["last_jam"]))))
        else:
            L.append(("Timecode: not jammed", "yellow"))
        l = st.get("ltc") or {}
        if l.get("age") is None:
            L.append(("LTC stored: NO", "red"))
        L.append("")
        L.append("Scene: %s" % (f6.get("scene") or "-"))
        L.append("Next take note: %s" % (f6.get("note") or "-"))
        nomes = f6.get("track_names") or {}
        L.append("Track names: %s" % (", ".join("%s=%s" % (k, v) for k, v in sorted(nomes.items())) or "-"))
        uk = f6.get("user_keys") or {}
        if uk:
            L.append(("User keys: U%s Mark, U%s Clear Clip, U%s Circled"
                      % (uk.get("mark") or "-", uk.get("clear_clip") or "-", uk.get("circled") or "-"), "dim"))
        if f6.get("error"):
            L.append(("Audio output error: %s" % f6["error"], "red"))
        return L

    def f6_screen(self):
        """Ecra so do F6: estado ao vivo e as accoes dele."""
        cor = COR["f6"]
        topo = 0
        while True:
            self.scr.timeout(100)          # o F6 chega 5x/s pelo Snapshot.run_f6
            h, w = self.scr.getmaxyx()
            self.scr.erase()
            self.title_bar("Zoom F6", cor)
            try:
                linhas = []
                for item in self.f6_lines():
                    if isinstance(item, MeterRow):
                        linhas.append(item)
                    else:
                        linhas += wrap_lines([item], max(20, w - 3))
            except Exception as e:
                linhas = [("error: %s" % e, "red")]
            vista = max(1, h - 5)
            topo = max(0, min(topo, len(linhas) - vista))
            for i, item in enumerate(linhas[topo:topo + vista]):
                if isinstance(item, MeterRow):
                    self.put_segs(2 + i, 1, [(t, self.attr(c)) for t, c in item.segs])
                else:
                    self.put(2 + i, 1, item[0], self.attr(item[1]))
            self.show_msg(h - 3)
            k_ = lambda t, c: [("  " + t.split(" ", 1)[0], self.attr(c) | curses.A_BOLD),
                               (" " + t.split(" ", 1)[1] + " ", self.attr(c))]
            self.put_segs(h - 2, 0, k_("R REC/stop", COR["rec"]) + k_("J jam", COR["time"])
                          + k_("SPACE target", cor) + k_("M mark", COR["log"]) + k_("G circle", COR["log"])
                          + k_("C clear-clips", cor))
            self.put_segs(h - 1, 0, k_("S scene", cor) + k_("+ scene+", cor) + k_("N note", cor)
                          + k_("T track-name", cor) + k_("K card", cor)
                          + [("  up/down scroll  q back", 0)])
            self.scr.refresh()
            k = self.scr.getch()
            if k in (ord("q"), ord("Q"), 27, ord("z"), ord("Z")):
                return
            if k in (curses.KEY_UP, curses.KEY_DOWN):
                topo += -1 if k == curses.KEY_UP else 1
                continue
            if k in (ord("r"), ord("R")):
                self.act("REC F6", "POST", "/api/devices/f6/rec")
            elif k in (ord("j"), ord("J")):
                self.f6_jam()
            elif k == ord(" "):
                self.targets = list(self.st().get("rec_targets") or [])
                self.toggle_target("f6")
            elif k in (ord("m"), ord("M")):
                self.act("F6 Mark", "POST", "/api/devices/f6/user", {"func": "mark"})
            elif k in (ord("g"), ord("G")):
                self.act("F6 Circled", "POST", "/api/devices/f6/user", {"func": "circled"})
            elif k in (ord("c"), ord("C")):
                self.act("F6 clear clip indicators", "POST", "/api/devices/f6/user", {"func": "clear_clip"})
            elif k in (ord("s"), ord("S")):
                atual = (self.st().get("f6") or {}).get("scene") or ""
                nome = self.prompt("F6 scene name (A-Z 0-9 - _ space):", atual)
                if nome:
                    self.act("F6 scene name", "POST", "/api/devices/f6/scene", {"name": nome})
            elif k == ord("+"):
                self.act("F6 scene +1", "POST", "/api/devices/f6/scene", {"inc": True})
            elif k in (ord("n"), ord("N")):
                atual = (self.st().get("f6") or {}).get("note") or ""
                txt = self.prompt("Note for the NEXT F6 take (metadata NOTE):", atual)
                if txt:
                    self.act("F6 note", "POST", "/api/devices/f6/note", {"text": txt})
            elif k in (ord("t"), ord("T")):
                pista = self.prompt("Track number (1-6):")
                if pista and pista.strip().isdigit():
                    nomes = (self.st().get("f6") or {}).get("track_names") or {}
                    nome = self.prompt("Name for track %s:" % pista.strip(),
                                       nomes.get(pista.strip(), ""))
                    if nome:
                        self.act("F6 track name", "POST", "/api/devices/f6/track_name",
                                 {"track": int(pista), "name": nome})
            elif k in (ord("k"), ord("K")):
                self.f6card_screen()

    # --- cartao do F6 --------------------------------------------------------------
    def f6card_screen(self):
        """Explorador do cartao do F6 (em SD Card Reader): pastas, nomes, mover,
        apagar para o lixo, e o relatorio das takes contra o shoot log."""
        cor = COR["f6"]
        path, sel, d, erro = "", 0, None, None

        def ler(p):
            s, r = api("GET", "/api/devices/f6/card/ls?path=" + urllib.parse.quote(p), timeout=30)
            if s == 200 and r.get("ok"):
                return r, None
            return None, r.get("error") or ("bridge offline" if s == 0 else "HTTP %s" % s)

        def opera(corpo, depois=None):
            s, r = self.call("F6 card", "POST", "/api/devices/f6/card/op", corpo, timeout=60)
            self.say(r.get("message") or r.get("error") or "?", 0 if s == 200 and r.get("ok") else 2)
            return depois

        d, erro = ler(path)
        pl, pl_at = {}, 0.0
        while True:
            self.scr.timeout(500)          # a linha do leitor anda sozinha
            if time.time() - pl_at > 0.5:
                s_, st_ = api("GET", "/api/devices/f6/card/status", timeout=2)
                pl, pl_at = ((st_ or {}).get("player") or {}) if s_ == 200 else {}, time.time()
            h, w = self.scr.getmaxyx()
            self.scr.erase()
            self.title_bar("SD Card reader mode   /%s%s" % (d["path"] if d else path,
                           ("   %s GB free of %s" % (d.get("free_gb"), d.get("total_gb"))) if d and d.get("free_gb") is not None else ""), cor)
            ents = (d or {}).get("entries") or []
            if erro:
                self.put(2, 1, erro, self.attr("red"))
            elif not ents:
                self.put(2, 1, "(empty folder)", self.attr("dim"))
            vista = max(1, h - 6)
            sel = max(0, min(sel, len(ents) - 1))
            topo = max(0, sel - vista + 1)
            for i, e in enumerate(ents[topo:topo + vista]):
                y = 2 + i
                txt = ("[%s]" % e["name"]) if e["dir"] else e["name"]
                linha = "%-48.48s %10s  %s" % (txt, fmt_size(e.get("size")),
                                              time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"])))
                a = curses.A_REVERSE if topo + i == sel else 0
                self.put(y, 1, linha, a | (self.attr("cyan") | curses.A_BOLD if e["dir"] else 0))
            if pl.get("playing"):
                fp = lambda v: "%d:%02d" % (int(v or 0) // 60, int(v or 0) % 60)
                self.put(h - 4, 1, "PLAYING %s  %s / %s   (P stops)" % (
                    pl.get("file"), fp(pl.get("pos")), fp(pl.get("dur"))), self.attr("green") | curses.A_BOLD)
            elif pl.get("error"):
                self.put(h - 4, 1, "playback: %s" % pl["error"], self.attr("red"))
            self.show_msg(h - 3)
            self.put(h - 2, 0, "  Enter open  Backspace up  P play/stop  R rename  M move  N new folder  D delete",
                     self.attr(cor))
            self.put(h - 1, 0, "  S takes report  T trash  E empty trash  F refresh  q back", self.attr(cor))
            self.scr.refresh()
            k = self.scr.getch()
            if k == -1:
                continue
            alvo = ents[sel] if ents else None
            atual = d["path"] if d else path
            junta = lambda n: (atual + "/" + n) if atual else n
            if k in (ord("q"), ord("Q"), 27):
                return
            if k in (curses.KEY_UP, ord("k")):
                sel -= 1
                continue
            if k in (curses.KEY_DOWN, ord("j")):
                sel += 1
                continue
            novo = None
            if k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT) and alvo and alvo["dir"]:
                novo = junta(alvo["name"])
            elif k in (8, 127, curses.KEY_BACKSPACE, curses.KEY_LEFT):
                novo = "/".join(atual.split("/")[:-1])
            elif k in (ord("f"), ord("F")):
                novo = atual
            elif k in (ord("t"), ord("T")):
                novo = "F6_TRASH"
            elif k in (ord("r"), ord("R")) and alvo:
                n = self.prompt("New name for %s:" % alvo["name"], alvo["name"])
                if n and n != alvo["name"]:
                    novo = opera({"op": "rename", "path": junta(alvo["name"]), "name": n}, atual)
            elif k in (ord("m"), ord("M")) and alvo:
                dest = self.prompt("Move %s to folder (empty = card root):" % alvo["name"], atual)
                if dest is not None:
                    novo = opera({"op": "move", "path": junta(alvo["name"]), "dest": dest}, atual)
            elif k in (ord("n"), ord("N")):
                n = self.prompt("New folder name in /%s:" % atual)
                if n:
                    novo = opera({"op": "mkdir", "path": atual, "name": n}, atual)
            elif k in (ord("d"), ord("D")) and alvo:
                if self.ask("Move %s to the trash (F6_TRASH)? (y/n)" % alvo["name"], "yn") == "y":
                    novo = opera({"op": "trash", "path": junta(alvo["name"])}, atual)
            elif k in (ord("e"), ord("E")):
                c = self.prompt("Empty F6_TRASH deletes its files FOR GOOD. Type EMPTY to confirm:")
                if c:
                    novo = opera({"op": "empty_trash", "confirm": c}, "" if atual.startswith("F6_TRASH") else atual)
            elif k in (ord("s"), ord("S")):
                self.f6card_report()
                novo = atual
            elif k in (ord("p"), ord("P")):
                if pl.get("playing"):
                    self.call("F6 card", "POST", "/api/devices/f6/card/stop", {}, timeout=10)
                    self.say("playback stopped", 0)
                elif alvo and not alvo["dir"] and alvo["name"].lower().endswith(".wav"):
                    s_, r_ = self.call("F6 card", "POST", "/api/devices/f6/card/play", {"path": junta(alvo["name"])},
                                       timeout=30)
                    self.say(("playing %s" % alvo["name"]) if s_ == 200 and r_.get("ok")
                             else (r_.get("error") or "?"), 0 if s_ == 200 and r_.get("ok") else 2)
                else:
                    self.say("P plays a .WAV file: select one", 1)
                pl_at = 0.0
            if novo is not None:
                d2, erro = ler(novo)
                if d2:
                    d, path, sel = d2, novo, 0 if novo != atual else sel
                else:
                    d = None

    def f6card_report(self):
        """Le o cartao e mostra cada take do F6 ao lado da take do shoot log."""
        self.say("Reading the F6 card...", 1)
        s, r = self.call("F6 card", "POST", "/api/devices/f6/card/scan", {}, timeout=180)
        if not (s == 200 and r.get("ok")):
            self.done("F6 card", s, r)
            return
        rep = r["report"]
        L = [("%d takes on the card" % len(rep["takes"]), "green"), "",
             ("FOLDER              SCENE      TAKE  START TC     DUR    CIRC  LOG  MARKS / CAMERAS", "dim")]
        for t in rep["takes"]:
            dur = t.get("duration_s")
            dur = "%d:%02d" % (int(dur) // 60, int(dur) % 60) if dur is not None else "-"
            cor = "red" if t.get("error") else ("white" if t.get("log_take") else "yellow")
            L.append(("%-19.19s %-10.10s %-5s %-12s %-6s %-5s %-4s %s %s" % (
                t["folder"], t.get("scene") or "-", t.get("take") or "-", t.get("start_tc") or "-", dur,
                "YES" if t.get("circled") == "TRUE" else "", t.get("log_take") or "-",
                t.get("marks") or "", t.get("cameras") or ""), cor))
            extra = " | ".join(x for x in (t.get("note") and "note: " + t["note"],
                                           t.get("tracks") and "tracks: " + t["tracks"],
                                           t.get("log_notes") and "log: " + t["log_notes"],
                                           t.get("error")) if x)
            if extra:
                L.append(("    " + extra, "dim"))
        if rep.get("log_only"):
            L += ["", ("In the shoot log but not on this card:", "yellow")]
            L += ["  take %s  %s  TC %s" % (x["take"], x["time"], x["tc"]) for x in rep["log_only"]]
        L += ["", ("Yellow: no matching start in the shoot log.", "dim")]
        self.pager("F6 card: takes", lambda: L, live=False, cor=COR["f6"])

    def draw_tc(self, refresh=True):
        """So a linha do LTC (por baixo da hora do sistema): corre a cada frame sem redesenhar o resto.
        Ordem: hora, minuto, segundo, frame, dia da semana, data (dd-mm-aaaa)."""
        st, _cn, _sh, ok, _err, at = self.snap.get()
        h, w = self.scr.getmaxyx()
        self.put(1, 0, " " * (w - 1))
        r = ltc_now(st, at) if ok else None
        if r:
            l = st.get("ltc") or {}
            self.put(1, 1, "LTC", self.attr(CLOCK_COR["ltc"]) | curses.A_BOLD)
            self.put(1, 5, r[0], self.attr("green") | curses.A_BOLD)
            d = ltc_date(st)
            if d:
                # Amarelo se o dia do gerador nao for o do sistema: um dos dois
                # esta errado, e e o dia que mais estraga a organizacao em pos.
                igual = d[0][:3] == time.localtime()[:3]
                texto = time.strftime("%A  %d-%m-%Y", d[0])
                self.put(1, 18, texto, self.attr("green") if igual else self.attr("yellow") | curses.A_BOLD)
                x = 18 + len(texto) + 2
            else:
                self.put(1, 18, "no date", self.attr("dim"))
                x = 27
            # A via do ultimo jam: Bluetooth (Sidus/TC-1) ou o cabo na entrada de microfone (L-IN).
            via = {"ble": "  via BT", "cable": "  via L-IN"}.get(l.get("via"), "")
            x = self.put_segs(1, x, [("%s fps  offset %+.2f s  jam %s ago%s%s" % (
                r[1], float(l.get("offset") or 0), fmt_age(l.get("age")), via,
                "  STALE" if l.get("stale") else ""), self.attr("dim"))])
            # Sem NTP (INT WI-FI em baixo) o LTC vem do relogio do Atom: depois de um reinicio so a +-1 s
            # (o relogio de hardware so guarda segundos), e a derivar ~33 ppm desde o jam.
            aviso = None
            if l.get("approx") == "rtc":
                aviso = "  ~1 s after reboot: LTC Get again"
            elif not ((st.get("clock_sources") or {}).get("ntp") or {}).get("ok") and l.get("age") is not None:
                frames = LTC_DRIFT_PPM * 1e-6 * float(l["age"]) * int(r[1] or 25)
                if frames >= 1:
                    aviso = "  no NTP: ~%d frame%s drift, LTC Get again" % (frames, "" if int(frames) == 1 else "s")
            if aviso:
                self.put(1, x, aviso, self.attr("yellow") | curses.A_BOLD)
        else:
            l = (st.get("ltc") or {}) if ok else {}
            if l.get("invalid_reason"):
                self.put(1, 1, "LTC --:--:--:--   LOST: %s" % l["invalid_reason"],
                         self.attr("red") | curses.A_BOLD)
            else:
                self.put(1, 1, "LTC --:--:--:--   no valid LTC",
                         self.attr("dim"))
        if refresh:
            self.scr.refresh()

    def f6_jam(self):
        self.act("F6 jam (5 s of LTC)", "POST", "/api/devices/f6/jam")

    def f6_jam_menu(self):
        """O jam do F6 a partir do menu, com o resultado e o que fazer a seguir."""
        s, d = self.call("F6 jam", "POST", "/api/devices/f6/jam")
        extra = ["The headphone output now plays 5 s of timecode."]
        if not (s == 200 and d.get("ok")):
            extra = None
        self.done("F6 jam", s, d, extra)

    def rec_one(self, r):
        """REC / parar so nesta camara (o canonRec da pagina). Serve para
        reanimar uma camara que falhou a take sem mexer nas outras."""
        self.act("REC %s" % r["label"], "POST", "/api/devices/%s/rec" % r["id"])

    def _iphone(self, cid):
        return self.devs("iphone").get(cid) or {}

    def iphone_check(self, cid):
        ip = self._iphone(cid)
        self.say("Measuring %s timecode..." % (ip.get("name") or cid), 1)
        s, d = self.call("TC check", "POST", "/api/devices/%s/tc" % cid, timeout=30)
        if s == 200 and d.get("ok"):
            self.done("TC check", s, d, [d.get("text") or "",
                                         "", "Under +-0.08 s is within a frame or two at 25 fps."])
        else:
            self.done("TC check", s, d)

    def iphone_scene(self, cid):
        ip = self._iphone(cid)
        sc = self.prompt("Scene for the %s clip slate:" % (ip.get("name") or cid), ip.get("scene") or "")
        if sc:
            self.act("%s scene" % (ip.get("name") or cid), "POST", "/api/devices/%s/scene" % cid, {"scene": sc})

    def iphone_config(self, cid):
        ip = self._iphone(cid)
        n = ip.get("name") or cid
        h = self.prompt("IP address of %s (Blackmagic Camera > web server):" % n, ip.get("host") or "")
        if not h:
            return
        pt = self.prompt("Port:", str(ip.get("port") or 4444))
        if pt and pt.strip().isdigit():
            self.act("%s address" % n, "POST", "/api/devices/%s/config" % cid,
                     {"host": h.strip(), "port": int(pt), "enabled": True})

    def iphone_rename(self, cid):
        ip = self._iphone(cid)
        n = self.prompt("New name for %s:" % (ip.get("name") or cid), ip.get("name") or cid)
        if n and n.strip():
            self.act("rename", "POST", "/api/devices/%s/config" % cid, {"name": n.strip()})

    def iphone_add(self):
        n = self.prompt("Name for the new iPhone (e.g. iPhone B):")
        if n is None:
            return
        h = self.prompt("IP of the phone (Blackmagic Camera > web server):")
        if not h:
            return
        pt = self.prompt("Port:", "4444")
        if pt and pt.strip().isdigit():
            s, d = self.call("Add iPhone", "POST", "/api/devices",
                             {"type": "iphone", "name": n.strip(), "host": h.strip(), "port": int(pt)})
            self.done("Add iPhone", s, d, ["Added as %s." % d.get("id")]
                      if s == 200 and d.get("ok") else None)

    # --- aparelhos: acrescentar e retirar (iPhone, Canon, BMPCC) ----------------
    def devices_menu(self):
        """Acrescentar e retirar aparelhos. Existe a parte do menu Camera porque,
        sem nenhum iPhone/Canon, nao ha linha onde abri-lo."""
        while True:
            h_, _w = self.scr.getmaxyx()                 # o pedido pode demorar: o ecra nao parece parado
            self.put(h_ - 2, 1, "Reading devices ...", self.attr("dim"))
            self.scr.refresh()
            s, d = api("GET", "/api/devices", timeout=10)
            velho = s != 200
            d = d if isinstance(d, dict) and not velho else {}
            canons = list(devs_tipo(d, "canon").values())
            tirados = d.get("removed") or []
            bm_ativa = (d.get("bmpcc") or {}).get("active", True)
            entradas = [
                Sec("IPHONE"),
                ("ip_add", "Add iPhone"),
                Sec("CANON"),
                ("cn_add", "Add Canon"),
                ("cn_del", "Remove Canon" + ("" if canons else "  (none)")),
                ("cn_idle", "Force Canons idle"),
            ]
            gw_on = (self.st().get("gopro_gateway") or {}).get("enabled", True) is not False
            if self.st().get("gopro_gateways") == []:             # tirou-se todos os S3: so se pode juntar um
                entradas += [Sec("GOPRO"), ("gp_gwadd", "Add an S3 gateway")]
            else:
                entradas += [Sec("GOPRO"),
                             ("gp_onoff", "Gateway ON / OFF"),
                             ("gp_add", "Add a GoPro"),
                             ("gp_reset", "Reset the gateway"),
                             ("gp_lead", "QR lead (ms)"),
                             ("gp_host", "Gateway address"),
                             ("gp_gwadd", "Add an S3 gateway")]
            entradas += [Sec("BMPCC (EXTRA)"),
                         ("bmx_add", "Add a BMPCC (own C3)")]
            entradas += [Sec("SONY"),
                         ("so_add", "Add a Sony"),
                         ("so_sdk", "Sony SDK and search")]
            entradas += [Sec("ZOOM"),
                         ("f6", "F6 screen  (Z)"),
                         ("f6card", "SD Card reader mode")]
            entradas += [Sec("GATEWAYS (USB)"),
                         ("gw_usb", "Set up / update a gateway by USB")]
            entradas += [Sec("M5STICK (F6 REMOTE)"),
                         ("m5_flash", "Flash a new M5StickS3"),
                         ("m5_update", "Update its program"),
                         ("m5_wifi", "Change its Wi-Fi only")]
            entradas += [Sec("WIRELESS")] + self.wifi_entradas()
            entradas += [Sec("BLUETOOTH"),
                         ("bt", "Bluetooth  " + bt_resumo(self.st())[0].replace("Bluetooth", "").lstrip(": ").strip())]
            entradas += [Sec("USB"),
                         ("usb", "USB devices")]
            entradas += [Sec("SAVED"),
                         ("back", "Saved devices" + ("" if tirados else "  (none)"))]
            # A BMPCC liga-se pelo gateway C3 (emparelha com ele, nao com o Bluetooth do Atom): aqui so se retira.
            bm_guardada = next((x for x in tirados if x.get("type") == "bmpcc"), None)
            if bm_ativa:
                entradas += [Sec("BMPCC"), ("bm_del", "Remove the BMPCC from this session")]
            elif bm_guardada:
                entradas += [Sec("BMPCC"), ("bm_back", "Restore the BMPCC  (removed)")]
            i = self.menu("DEVICES", [e if isinstance(e, Sec) else e[1] for e in entradas], cor=COR["cams"], ordem="devices")
            if i is None:
                return
            k = entradas[i][0]
            if isinstance(k, tuple) and k[0] == "wifi":
                self.wifi_settings(k[1])
                continue
            if k == "nm":
                self.restart_nm()
                continue
            if k == "bt":
                self.bt_menu()
                continue
            if k == "f6":
                self.f6_screen()
                continue
            if k == "f6card":
                self.f6card_screen()
                continue
            if k == "cn_idle":
                if self.confirm("Force every connected Canon back to idle?"):
                    s, d = self.call("Force idle", "POST", "/api/devices/batch", {"action": "reset", "type": "canon"})
                    self.done("Force idle", s, d, ["Cameras: %s" % ", ".join(d.get("cameras") or [])]
                              if isinstance(d, dict) and d.get("ok") else None)
                continue
            if k == "ip_add":
                self.iphone_add()
                continue
            if k == "bmx_add":
                self.bmx_add()
                continue
            if k == "so_add":
                self.sony_add()
                continue
            if k == "so_sdk":
                self.sony_sdk()
                continue
            if k == "gw_usb":
                self.gw_usb_setup()
                continue
            if isinstance(k, str) and k.startswith("m5_"):
                self.m5_stick(k[3:])
                continue
            if k == "gp_onoff":
                # Com varios S3 escolhe-se qual (antes so mudava o primeiro).
                n = self.gopro_pick_gw()
                if n is None:
                    continue
                g = next((x for x in self.st().get("gopro_gateways") or [] if (x.get("idx") or 1) == n), {})
                gw_on = g.get("enabled", True) is not False
                if not gw_on or self.confirm("Switch the GoPro gateway %s OFF in the bridge?\n\n"
                                             "The bridge stops asking it (no fault)." % (g.get("name") or n)):
                    s, d = self.call("GoPro gateway %s" % ("on" if not gw_on else "off"), "POST",
                                     "/api/gateways/s3/enabled", {"on": not gw_on, "gw": n})
                    if s == 404 and not (isinstance(d, dict) and "error" in d):
                        d = {"error": "update the bridge from GitHub first"}
                    self.done("GoPro gateway %s" % ("on" if not gw_on else "off"), s, d)
                    self.snap.poll_once()
                continue
            if k == "gp_gwadd":
                self.gopro_gw_add()
                continue
            if k == "gp_add":
                self.gopro_add()
                continue
            if k == "gp_lead":
                self.gopro_lead()
                continue
            if k == "gp_reset":
                self.gopro_reset_menu()
                continue
            if k == "gp_host":
                self.gopro_host()
                continue
            if k == "usb":
                self.usb_all_menu()
                continue
            if velho:
                self.say("Update the bridge from GitHub first (HTTP %s)" % s, 2)
                continue
            if k == "cn_add":
                self.canon_add()
            elif k == "cn_del":
                self.canon_remove(canons)
            elif k == "back":
                if not tirados:
                    self.say("Nothing saved: removed devices appear here", 1)
                    continue
                j = self.menu("Saved devices", ["%-8s %-10s %s" % (x.get("type", "canon").capitalize(), x["name"], x.get("ip") or "")
                                                for x in tirados], cor=COR["cams"])
                if j is not None:
                    self.saved_device(tirados[j])
            elif k == "bm_del":
                self.bmpcc_remove()
            elif k == "bm_back":
                s, d = self.call("Restore BMPCC", "POST", "/api/devices/%s/restore" % bm_guardada["id"])
                self.done("Restore BMPCC", s, d)
                self.snap.poll_once()

    def saved_device(self, x):
        """Um aparelho guardado: repor em servico, ou esquecer de vez (sai da lista)."""
        kind, nome = x.get("type", "canon"), x.get("name") or x["id"]
        i = self.menu("%s %s" % (kind.capitalize(), nome), [
            ("Restore  (back in service)", COR["cams"]),
            ("Forget  (removes it from the saved list for good)", COR["rec"])], cor=COR["cams"])
        if i == 0:
            s, d = self.call("Restore %s" % nome, "POST", "/api/devices/%s/restore" % x["id"])
            self.done("Restore %s" % nome, s, d)
        elif i == 1 and self.confirm("Forget %s for good?\n\nIt cannot be restored." % nome):
            s, d = self.call("Forget %s" % nome, "POST", "/api/devices/%s/forget" % x["id"])
            self.done("Forget %s" % nome, s, d)

    def gopro_pick_gw(self, gw=None):
        """O numero do gateway S3 a usar: o dado, o unico que existe, ou o que o operador escolher. None = cancelado."""
        if gw:
            return gw
        s, d = api("GET", "/api/gateways/s3", timeout=10)
        gws = (d.get("gateways") or []) if s == 200 and isinstance(d, dict) else []
        if s == 200 and not gws:
            self.say("No S3 gateway yet", 2)
            return None
        if len(gws) <= 1:
            return gws[0]["idx"] if gws else 1
        j = self.menu("Which S3 gateway?", ["%s  %s" % (g.get("name"), g.get("host") or "no address") for g in gws], cor=COR["cams"])
        return None if j is None else gws[j]["idx"]

    def gopro_gw_add(self):
        """Mais um gateway S3 (outro alcance BLE, mais GoPro)."""
        s, d = api("GET", "/api/gateways/s3", timeout=10)
        achados = (d.get("found") or []) if s == 200 and isinstance(d, dict) else []
        h = self.prompt("Address of the new S3 gateway%s:" % ("  found: " + ", ".join(achados) if achados else ""),
                        achados[0] if achados else "")
        if not h or not h.strip():
            return
        n = self.prompt("Name (optional, e.g. Rooftop):", "")
        if n is None:
            return
        s, d = self.call("Add S3 gateway", "POST", "/api/gateways/s3/add", {"host": h.strip(), "name": n.strip()})
        self.done("Add S3 gateway", s, d)

    def gopro_gw_remove(self, n):
        if self.confirm("Remove S3 gateway %d from the bridge?\n\nThe gateway itself is not changed." % n):
            s, d = self.call("Remove S3 gateway", "POST", "/api/gateways/s3/remove", {"gw": n})
            self.done("Remove S3 gateway", s, d)

    def gopro_reset_menu(self, gw=None):
        """Repoe o gateway ESP32-S3 das GoPro: reiniciar, esquecer emparelhamentos, ou tudo."""
        gw = self.gopro_pick_gw(gw)
        if gw is None:
            return
        i = self.menu("Reset the GoPro gateway", [
            ("Restart it  (a few seconds without the GoPro)", COR["cams"]),
            ("Forget its Bluetooth pairings  (the GoPro pairs again)", COR["cams"]),
            ("Reset completely  (forget all GoPro and pairings)", COR["rec"])], cor=COR["cams"])
        if i is None:
            return
        what, pergunta = (("reboot", None), ("bonds", "Forget all Bluetooth pairings of the gateway?"),
                          ("factory", "Forget ALL GoPro and pairings of the gateway?"))[i]
        if pergunta and not self.confirm(pergunta):
            return
        if any(g.get("running") for g in self.devs("gopro").values()):
            self.say("A GoPro is recording: stop it first", 2)
            return
        s, d = self.call("GoPro gateway reset", "POST", "/api/gateways/s3/reset", {"what": what, "gw": gw}, timeout=15)
        self.done("GoPro gateway reset", s, d, [d.get("note") or "Done.",
                                                  "The gateway is back in about 15 seconds." if what != "bonds" else "The GoPro reconnects by itself."]
                  if s == 200 and d.get("ok") else None)

    def gopro_host(self, gw=None):
        gw = self.gopro_pick_gw(gw)
        if gw is None:
            return
        s, d = api("GET", "/api/gateways/s3", timeout=10)
        g = next((x for x in (d.get("gateways") or []) if x.get("idx") == gw), {}) if s == 200 and isinstance(d, dict) else {}
        h = self.prompt("Address of the GoPro gateway (ESP32-S3), e.g. 192.168.4.96:", g.get("host") or "")
        if h is None or not h.strip():
            return
        s, d = self.call("GoPro gateway", "POST", "/api/gateways/s3/host", {"host": h.strip(), "gw": gw})
        self.done("GoPro gateway", s, d)

    def gopro_add(self, gw=None):
        """Procura GoPro pelo gateway e acrescenta a escolhida."""
        gw = self.gopro_pick_gw(gw)
        if gw is None:
            return
        s, d = api("GET", "/api/gateways/s3", timeout=10)
        if s != 200:
            self.say("Update the bridge from GitHub first (HTTP %s)" % s, 2)
            return
        alvo = next((x for x in (d.get("gateways") or []) if x.get("idx") == gw), d.get("gateway") or {})
        if alvo.get("enabled") is False:
            self.say("GoPro gateway: off in the bridge", 2)
            return
        if not alvo.get("ok"):
            self.say("GoPro gateway: offline", 2)
            return
        if not self.confirm("Scan for GoPro?\n\nThe GoPro must be on its GoPro App pairing screen (Help: Pair a GoPro)."):
            return
        self.say("Scanning ...", 1)
        s, d = self.call("GoPro scan", "POST", "/api/gateways/s3/scan", {"secs": 10, "gw": gw}, timeout=40)
        if s != 200 or not d.get("ok"):
            self.done("GoPro scan", s, d)
            return
        lista = [x for x in d.get("found") or [] if x.get("connectable") and not x.get("configured")]
        if not lista:
            self.pager("No GoPro found", lambda: [
                ("No new GoPro answered.", "yellow"), "",
                "Check: GoPro on, near the gateway, on the pairing screen (Help: Pair a GoPro)."], live=False, cor="yellow")
            return
        j = self.menu("Which GoPro?", ["%-18s %s   %s dBm" % (x.get("name") or "(no name)", x["mac"], x.get("rssi"))
                                       for x in lista], cor=COR["cams"])
        if j is None:
            return
        x = lista[j]
        nome = self.prompt("Name for this GoPro (e.g. GoPro A):", "GoPro %s" % chr(65 + len(self.devs("gopro"))))
        if nome is None:
            return
        s, d = self.call("Add GoPro", "POST", "/api/devices",
                         {"type": "gopro", "model": x.get("type") or 1, "mac": x["mac"], "name": nome.strip(), "gw": gw})
        self.done("Add GoPro", s, d, ["Added as %s." % d.get("id")] if s == 200 and d.get("ok") else None)

    def gopro_sync_help(self):
        """Onde se acerta a hora da GoPro: na pagina do QR do bridge (nao ha botao na consola)."""
        self.help_page("GoPro clock")

    def gopro_clock(self, cid):
        """A hora da GoPro contra a do bridge: medida fina (~0,1 s) pela fase do relogio, ou a de 1 s
        se o gateway for antigo. Sugere o avanco do QR (LEAD) que a deixa certa."""
        self.say("Measuring the camera clock (a few seconds) ...", 1)
        s, d = self.call("GoPro clock", "POST", "/api/devices/%s/clock" % cid, timeout=25)
        if not (s == 200 and d.get("ok")):
            self.done("GoPro clock", s, d)
            return
        off = d.get("offset") or 0
        fino = bool(d.get("fine"))
        bom = abs(off) <= (0.25 if fino else 1.5)
        sug = d.get("suggest_lead_ms")
        self.pager("GoPro clock", lambda: [
            (d.get("text") or "", "green" if bom else "yellow"), "",
            "Camera clock: %s" % d.get("camera"),
            ("Precision 0.1 s" if fino else "Precision 1 s (old gateway firmware)"), "",
            ("In sync." if bom else
             "Not in sync: camera %.1f s behind." % abs(off)
             if off < 0 else "Not in sync: camera ahead."),
            "" if bom or sug is None else
            "QR lead now %s ms; suggested %s ms." % (d.get("lead_ms"), sug),
            ""], live=False, cor=COR["time"])
        if not bom and sug is not None and self.confirm("Set the QR lead to the suggested %s ms?" % sug):
            s2, d2 = self.call("QR lead", "POST", "/api/devices/batch",
                                {"action": "lead", "type": "gopro", "ms": sug})
            self.done("QR lead", s2, d2, ["Show the TIME QR again, then Check clock."]
                      if s2 == 200 and d2.get("ok") else None)

    def gopro_lead(self):
        s, d = api("GET", "/api/gateways/s3", timeout=10)
        atual = ((d.get("gateway") or {}).get("lead_ms") or 0) if s == 200 and isinstance(d, dict) else 0
        v = self.prompt("QR lead in ms (now %s):" % atual, str(atual))
        if v is None or not v.strip():
            return
        s, d = self.call("QR lead", "POST", "/api/devices/batch",
                         {"action": "lead", "type": "gopro", "ms": v.strip()})
        self.done("QR lead", s, d)

    def gopro_remove(self, cid):
        g = self.devs("gopro").get(cid) or {}
        if self.confirm("Remove %s from the gateway?\n\nIts pairing is forgotten." % (g.get("name") or cid)):
            s, d = self.call("Remove GoPro", "POST", "/api/devices/%s/remove" % cid)
            self.done("Remove GoPro", s, d)

    def _bmx(self, cid):
        return self.devs("bmpcc_extra").get(cid) or {}

    def bmx_add(self):
        """Mais uma BMPCC, com o seu C3 (Bridge mode). Sugere os C3 que se anunciaram."""
        s, d = api("GET", "/api/gateways/c3", timeout=10)
        achados = (d or {}).get("found") or [] if s == 200 else []
        h = self.prompt("Address of the new BMPCC's C3 (Bridge mode)%s:" % (
            "  found: " + ", ".join(achados) if achados else ""), achados[0] if achados else "")
        if not h or not h.strip():
            return
        n = self.prompt("Name:", "BMPCC %d" % (len(self.devs("bmpcc_extra")) + 2))
        if n is None:
            return
        s, d = self.call("Add BMPCC", "POST", "/api/devices",
                         {"type": "bmpcc_extra", "name": n.strip(), "host": h.strip()})
        self.done("Add BMPCC", s, d)

    def bmx_host(self, cid):
        h = self.prompt("Address of this BMPCC's C3:", self._bmx(cid).get("host") or "")
        if h and h.strip():
            s, d = self.call("C3 address", "POST", "/api/devices/%s/config" % cid, {"host": h.strip()})
            self.done("C3 address", s, d)

    def bmx_rename(self, cid):
        n = self.prompt("Name:", self._bmx(cid).get("name") or "")
        if n and n.strip():
            s, d = self.call("Rename", "POST", "/api/devices/%s/config" % cid, {"name": n.strip()})
            self.done("Rename", s, d)

    def bmx_remove(self, cid):
        if self.confirm("Remove %s? (its C3 is left as it is)" % (self._bmx(cid).get("name") or cid)):
            s, d = self.call("Remove BMPCC", "POST", "/api/devices/%s/remove" % cid)
            self.done("Remove BMPCC", s, d)

    # --- Sony (Camera Remote SDK) ------------------------------------------
    def _sony(self, cid):
        return self.devs("sony").get(cid) or {}

    def _sony_info(self):
        s, d = api("GET", "/api/system/sony_sdk", timeout=10)
        return d if s == 200 and isinstance(d, dict) else None

    def sony_add(self):
        """Mais uma Sony: ligacao (Wi-Fi sem gateway, ou USB), modelo, endereco, e SSH se o modelo o pedir."""
        info = self._sony_info()
        if info is None:
            self.say("Update the bridge from GitHub first", 2)
            return
        if not info.get("available") and not self.confirm(
                "The Sony helper is not running:\n%s\n\nAdd the camera anyway?" % (info.get("why") or "?")):
            return
        i = self.menu("Sony: connect how?", [
            ("Wi-Fi  (camera joins the Atom camera Wi-Fi, no gateway)", COR["cams"]),
            ("USB cable", COR["cams"])], cor=COR["cams"])
        if i is None:
            return
        via = ("ip", "usb")[i]
        caps = info.get("caps") or {}
        quer = ("wifi", "lan") if via == "ip" else ("usb",)
        modelos = [m for m in info.get("models") or [] if m not in caps or any(caps[m].get(q) for q in quer)]
        modelos.sort(key=lambda m: (m != "ILCE-7M4", m))
        if not modelos:
            self.say("No Sony model with that connection", 2)
            return
        j = self.menu("Sony model", ["%-12s %s%s" % (m, (caps.get(m) or {}).get("label", ""),
                                                      "  (SSH)" if via == "ip" and (caps.get(m) or {}).get("ssh") else "")
                                     for m in modelos], cor=COR["cams"])
        if j is None:
            return
        m = modelos[j]
        corpo = {"type": "sony", "model": m, "via": via}
        achadas = [c for c in info.get("found") or [] if c.get("model") == m]
        if via == "ip":
            sug = next((c.get("ip") for c in achadas if c.get("ip")), "")
            h = self.prompt("IP of the camera on the camera Wi-Fi (camera: Network > Wi-Fi > IP Address):", sug)
            if not h or not h.strip():
                return
            corpo["host"] = h.strip()
            if (caps.get(m) or {}).get("ssh") or self.confirm(
                    "Use SSH (user + password) instead of pairing?\n\n"
                    "No = pair: the camera asks to accept \"Atom Bridge\" on first connect."):
                u = self.prompt("SSH user (camera: Network > Access Authen. Settings):")
                if not u:
                    return
                pw = self.prompt("SSH password:")
                if not pw:
                    return
                corpo.update({"ssh": True, "user": u.strip(), "password": pw.strip()})
        else:
            sug = next((c.get("serial") for c in achadas if c.get("serial")), "")
            sr = self.prompt("USB serial of the camera (Sony SDK and search finds it):", sug)
            if not sr or not sr.strip():
                return
            corpo["serial"] = sr.strip()
        n = self.prompt("Name:", "%s %d" % ((caps.get(m) or {}).get("label") or m, len(self.devs("sony")) + 1))
        if n is None:
            return
        corpo["name"] = n.strip()
        s, d = self.call("Add Sony", "POST", "/api/devices", corpo)
        self.done("Add Sony", s, d, ["Added as %s." % d.get("id"), "",
                                     "Wi-Fi: the camera asks to accept the connection the first time (pairing)."]
                  if s == 200 and d.get("ok") else None)

    def sony_sdk(self):
        """O estado do ajudante (o SDK da Sony) e a procura de camaras (USB e rede local)."""
        if self._sony_info() is None:
            self.say("Update the bridge from GitHub first", 2)
            return
        api("GET", "/api/system/sony_sdk?scan=1", timeout=10)

        def linhas():
            info = self._sony_info() or {}
            L = [("Sony helper: %s" % ("running, SDK %s" % info.get("sdk") if info.get("available")
                                        else "NOT RUNNING"), "green" if info.get("available") else "red")]
            if not info.get("available"):
                L += [("  %s" % (info.get("why") or "?"), "red"), "",
                      "The helper comes with the bridge: update it from GitHub."]
            L += ["", "Models the SDK knows: %d" % len(info.get("models") or []), "", ("Cameras found:", "bold")]
            achadas = info.get("found") or []
            for c in achadas:
                L.append("  %-12s %-8s %s%s" % (c.get("model") or "?", c.get("connection") or "",
                                                c.get("ip") or c.get("serial") or "", "  SSH" if c.get("ssh") else ""))
            if not achadas:
                L.append(("  none (yet): USB cameras and the ones on this network", "dim"))
            return L
        self.pager("Sony SDK", linhas, cor=COR["cams"])

    def sony_tc(self, cid):
        so = self._sony(cid)
        n = so.get("name") or cid
        if so.get("running"):
            self.say("%s is recording: set TC after the take" % n, 2)
            return
        i = self.menu("Timecode preset of %s" % n, [("From the bridge LTC (now)", COR["time"]),
                                                    ("Type it (HH:MM:SS:FF)", COR["time"])], cor=COR["time"])
        if i is None:
            return
        corpo = {}
        if i == 1:
            tc = self.prompt("Timecode HH:MM:SS:FF:", "00:00:00:00")
            if not tc:
                return
            corpo["tc"] = tc.strip()
        s, d = self.call("%s TC" % n, "POST", "/api/devices/%s/tc" % cid, corpo)
        self.done("%s TC" % n, s, d, ["Preset %s written." % d.get("tc")] if s == 200 and d.get("ok") else None)

    def sony_addr(self, cid):
        so = self._sony(cid)
        if so.get("via") == "usb":
            sr = self.prompt("USB serial of %s:" % (so.get("name") or cid), so.get("serial") or "")
            if sr and sr.strip():
                self.act("Sony address", "POST", "/api/devices/%s/config" % cid, {"serial": sr.strip()})
            return
        h = self.prompt("IP of %s on the camera Wi-Fi:" % (so.get("name") or cid), so.get("host") or "")
        if h and h.strip():
            self.act("Sony address", "POST", "/api/devices/%s/config" % cid, {"host": h.strip()})

    def sony_cfg(self, cid, campo):
        so = self._sony(cid)
        if campo == "name":
            n = self.prompt("Name:", so.get("name") or "")
            if n and n.strip():
                self.act("Rename", "POST", "/api/devices/%s/config" % cid, {"name": n.strip()})
        else:
            on = not so.get("enabled", True)
            self.act("%s %s" % (so.get("name") or cid, "enabled" if on else "disabled"), "POST",
                     "/api/devices/%s/config" % cid, {"enabled": on})

    def sony_remove(self, cid):
        so = self._sony(cid)
        if self.confirm("Remove %s from the bridge?\n\nIt stays in the saved list." % (so.get("name") or cid)):
            s, d = self.call("Remove Sony", "POST", "/api/devices/%s/remove" % cid)
            self.done("Remove Sony", s, d)

    def canon_add(self):
        n = self.prompt("Name of the Canon (short, e.g. R5):")
        if not n or not n.strip():
            return
        ip = self.prompt("IP of the Canon (its CCAPI address, e.g. 192.168.4.80):")
        if not ip or not ip.strip():
            return
        pt = self.prompt("CCAPI port (8080; 443 = https):", "8080")
        if not pt or not pt.strip().isdigit():
            return
        s, d = self.call("Add Canon", "POST", "/api/devices",
                         {"type": "canon", "name": n.strip(), "ip": ip.strip(), "port": int(pt), "https": int(pt) == 443})
        self.done("Add Canon", s, d, ["Added as %s." % d.get("id")]
                  if s == 200 and d.get("ok") else None)

    def canon_remove(self, canons):
        if not canons:
            self.say("No Canon to remove", 1)
            return
        j = self.menu("Remove which Canon?", ["%s  %s" % (c["name"], c.get("ip") or "") for c in canons], cor=COR["rec"])
        if j is None:
            return
        c = canons[j]
        if self.confirm("Remove %s (%s) from the bridge?\n\nIt stays in the saved list." % (c["name"], c.get("ip") or "?")):
            s, d = self.call("Remove Canon", "POST", "/api/devices/%s/remove" % c["id"])
            self.done("Remove Canon", s, d)

    @staticmethod
    def bt_adapter_mac(adapter):
        """O endereco do adaptador (hci0...) que o bridge usa para a BMPCC."""
        try:
            out = subprocess.run(["hciconfig", adapter], capture_output=True, text=True, timeout=4).stdout
            m = re.search(r"BD Address:\s*" + _BT_MAC, out)
            return m.group(1).upper() if m else None
        except (OSError, subprocess.SubprocessError):
            return None

    def bt_scan_pick(self, bt, tempo=120):
        """Varre ao vivo e deixa escolher um aparelho. Devolve (mac, nome) ou None."""
        achados = {}
        pareados = bt_paired_macs()
        bt.send("devices")
        bt.send("scan on")
        sel, t0 = 0, time.time()
        self.scr.timeout(200)
        try:
            while True:
                while True:
                    ln = bt.get()
                    if ln is None:
                        break
                    if ln is _BT_EOF:
                        self.say("bluetoothctl closed (no permission or no adapter)", 2)
                        return None
                    bt_parse_device(ln, achados)
                bons = ("blackmagic", "pocket", "bmpcc", "cinema")
                lista = sorted(achados.items(), key=lambda kv: (
                    0 if any(b in kv[1].lower() for b in bons) else (1 if kv[1] and kv[1].replace("-", ":").upper() != kv[0] else 2),
                    kv[1].lower(), kv[0]))
                sel = max(0, min(sel, len(lista) - 1))
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.title_bar("PAIR A NEW BLUETOOTH DEVICE   scanning %ds" % (time.time() - t0), COR["cams"])
                self.put(2, 1, "Device in pairing mode", self.attr("dim"))
                if not lista:
                    self.put(4, 1, "Nothing found yet ...", self.attr("yellow"))
                for n, (mac, nome) in enumerate(lista[:max(1, h - 8)]):
                    bom = any(b in nome.lower() for b in bons)
                    st_ = self.hl(COR["cams"]) if n == sel else (self.attr("green") | curses.A_BOLD if bom else 0)
                    self.put(4 + n, 0, " " * (w - 1), st_ if n == sel else 0)
                    self.put(4 + n, 2, "%-30.30s %s%s" % (nome or "(no name)", mac, "  [paired]" if mac in pareados else ""), st_)
                self.put(h - 1, 0, " up/down choose   Enter pair   Q cancel", self.attr(COR["cams"]))
                self.scr.refresh()
                k = self.scr.getch()
                if k in (curses.KEY_UP, ord("k")) and lista:
                    sel = sel - 1 if sel > 0 else len(lista) - 1
                elif k in (curses.KEY_DOWN, ord("j")) and lista:
                    sel = sel + 1 if sel < len(lista) - 1 else 0
                elif k in (10, 13, curses.KEY_ENTER) and lista:
                    return lista[sel]
                elif k in (ord("q"), ord("Q"), 27) or time.time() - t0 > tempo:
                    return None
        finally:
            bt.send("scan off")
            self.scr.timeout(250)

    def bt_pair_device(self, bt, mac, nome, tentativa=1):
        """Emparelha e marca como confiavel. (ok, motivo). Aguenta o que costuma correr mal: o aparelho ja estar
        emparelhado (fica como esta, so se confia), um emparelhamento antigo que o outro lado esqueceu (remove-se e
        repete-se), um emparelhamento ja em curso (cancela-se e repete-se) e o aparelho ter saido do modo de
        emparelhamento (volta-se a procura-lo)."""
        info = bt_device_info(mac)
        if info["paired"]:
            bt.send("trust " + mac)
            time.sleep(0.5)
            return True, "already paired"
        bt.send("pair " + mac)
        fim, linhas = time.time() + 90, []
        while time.time() < fim:
            ln = bt.get(0.3)
            h, w = self.scr.getmaxyx()
            self.scr.erase()
            self.title_bar("PAIRING  %s  %s" % (nome or "(no name)", mac), COR["cams"])
            self.put(2, 1, "Confirm on the device, or type its code", self.attr("yellow"))
            for n, l in enumerate(linhas[-(h - 6):]):
                self.put(4 + n, 1, l[:w - 3], self.attr("dim"))
            self.put(h - 1, 0, " waiting ... (90 s)", self.attr(COR["cams"]))
            self.scr.refresh()
            if ln is None:
                continue
            if ln is _BT_EOF:
                return False, "bluetoothctl closed"
            linhas.append(ln)
            low = ln.lower()
            if "enter passkey" in low or "enter pin" in low or low.endswith("pin code:"):
                code = self.prompt("Type the code shown on the camera:")
                if code is None or not code.strip():
                    bt.send("cancel-pairing")
                    return False, "cancelled"
                bt.send(code.strip())
            elif "(yes/no)" in low:
                bt.send("yes" if self.confirm(ln) else "no")
            elif "pairing successful" in low or ("paired: yes" in low and mac.lower() in low):
                bt.send("trust " + mac)
                time.sleep(0.5)
                return True, ""
            elif "failed to pair" in low or "failed" in low and "pair" in low or "authentication" in low.replace(" ", ""):
                tipo = bt_classificar(ln)
                if tipo == "ja_emparelhado":
                    bt.send("trust " + mac)
                    time.sleep(0.5)
                    return True, "already paired"
                if tentativa >= 2 or tipo == "outro":
                    return False, ln
                if tipo == "bond_velho":
                    bt.send("remove " + mac)                # o outro lado esqueceu-o: apaga-se aqui e repete-se
                    self.say("Old pairing removed: pairing again", 1)
                elif tipo == "em_curso":
                    bt.send("cancel-pairing")
                else:
                    self.say("Device not reachable: looking for it again", 1)
                time.sleep(1.5)
                if not self.bt_scan_wait(bt, mac, nome):
                    return False, "%s (not found again: put it in pairing mode and retry)" % ln
                return self.bt_pair_device(bt, mac, nome, tentativa + 1)
        return False, "timed out"

    def bmpcc_planob(self, mac):
        """Esta camara (ja emparelhada ao Atom) passa a ser a BMPCC, pelo Bluetooth do Atom."""
        if self.a_gravar():
            self.say("Recording: not now (stop the take first)", 2)
            return
        s, d = self.call("BMPCC", "POST", "/api/devices", {"type": "bmpcc", "address": mac})
        if s != 200:
            self.done("BMPCC", s, d)
            return
        s, d = self.call("BMPCC path", "POST", "/api/devices/bmpcc/path", {"via": "bluez"})
        self.done("BMPCC through the Atom Bluetooth", s, d,
                  ["The BMPCC %s now goes through the Atom Bluetooth (plan B)." % mac]
                  if s == 200 else None)

    def bmpcc_path(self):
        """Por onde vai a BMPCC: o gateway C3 (o normal) ou o Bluetooth do Atom (plano B, sem C3)."""
        st = self.st()
        agora = st.get("bmpcc_via") or "c3"
        i = self.menu("BMPCC connection   now: %s" % ("Atom Bluetooth (plan B)" if agora == "bluez" else "C3 gateway"),
                      [("C3 gateway  (normal)%s" % ("   <" if agora == "c3" else ""), COR["cams"]),
                       ("Atom Bluetooth  (plan B: %s)%s" % (
                           st.get("bmpcc_address") or "?", "   <" if agora == "bluez" else ""), COR["rec"]),
                       ("Pair another BMPCC to the Atom Bluetooth ...", COR["cams"])], cor=COR["cams"])
        if i is None:
            return
        if i == 2:
            self.bt_pair()
            return
        via = ("c3", "bluez")[i]
        if via == agora:
            return
        if self.a_gravar():
            self.say("Recording: not now (stop the take first)", 2)
            return
        if via == "bluez" and not self.confirm(
                "Use the Atom Bluetooth for the BMPCC %s?\n\nThe C3 stops being used. The camera must be "
                "paired to the Atom already." % (st.get("bmpcc_address") or "?")):
            return
        s, d = self.call("BMPCC path", "POST", "/api/devices/bmpcc/path", {"via": via})
        self.done("BMPCC through the %s" % ("Atom Bluetooth" if via == "bluez" else "C3 gateway"), s, d)

    def bt_scan_wait(self, bt, mac, nome, tempo=30):
        """Volta a varrer ate o aparelho aparecer (tem de estar em modo de emparelhamento). True se apareceu."""
        achados = {}
        bt.send("scan on")
        fim = time.time() + tempo
        try:
            while time.time() < fim:
                ln = bt.get(0.3)
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.title_bar("LOOKING FOR  %s  %s" % (nome or "(no name)", mac), COR["cams"])
                self.put(2, 1, "Put it in pairing mode again (%d s)" % max(0, int(fim - time.time())),
                         self.attr("yellow"))
                self.scr.refresh()
                if ln is _BT_EOF:
                    return False
                if ln:
                    bt_parse_device(ln, achados)
                    if mac.upper() in achados:
                        return True
                if bt_device_info(mac)["present"] and int(time.time()) % 3 == 0:
                    return True
        finally:
            bt.send("scan off")
        return False

    def bt_pair(self):
        """Procurar, emparelhar e (se for a BMPCC) pô-la em servico."""
        s, d = api("GET", "/api/devices", timeout=10)
        adapter = ((d.get("bmpcc") or {}).get("adapter") if s == 200 and isinstance(d, dict) else None) or "hci0"
        mac_ad = self.bt_adapter_mac(adapter)
        if not self.confirm("Pair a new Bluetooth device on the Atom (%s)?\n\n"
                            "Scanning starts now: put the device in pairing mode." % adapter):
            return
        try:
            bt = BtCtl()
        except OSError as e:
            self.say("bluetoothctl not available: %s" % e, 2)
            return
        try:
            if mac_ad:
                bt.send("select " + mac_ad)
            bt.send("power on")
            bt.send("agent KeyboardDisplay")
            bt.send("default-agent")
            achado = self.bt_scan_pick(bt)
            if not achado:
                return
            mac, nome = achado
            ok, porque = self.bt_pair_device(bt, mac, nome)
            if ok and porque == "already paired":
                self.say("%s was already paired: kept it (trusted)" % (nome or mac), 1)
            if not ok:
                self.pager("Pairing failed", lambda: [("Could not pair %s %s" % (nome or "", mac), "red"), "", porque,
                                                      "", ("The device must be in pairing mode; try again.", "dim")],
                           live=False, cor="red")
                return
            # Normalmente a BMPCC emparelha-se no C3 (GATEWAYS > C3 > Pair camera). Aqui, so para o plano B: a BMPCC
            # pelo Bluetooth do proprio Atom.
            j = self.menu("Paired: %s  %s" % (nome or "(no name)", mac),
                          [("Keep paired", COR["cams"]),
                           ("Use it as the BMPCC through the Atom Bluetooth (plan B, no C3)", COR["rec"])],
                          cor=COR["cams"])
            if j == 1:
                self.bmpcc_planob(mac)
            else:
                self.say("%s is paired and trusted" % (nome or mac), 1)
        finally:
            bt.close()

    # --- USB class compliant (servico bridge-usb, usb_bridge.py) ---------------
    @staticmethod
    def usb_accao_txt(a, slots, nomes=None):
        """Uma accao do bridge-usb em palavras. `slots`: os nomes do ecra por ordem; `nomes`: {id: nome} (o que o
        /actions traz; sem ele, o id)."""
        fixas = {"rec": "REC / STOP  (the REC targets)", "targets:all": "Arm all devices",
                 "targets:none": "Disarm all devices", "mark:good": "Mark GOOD", "mark:no-good": "Mark NO GOOD",
                 "mark:mark": "Mark"}
        if a in fixas:
            return fixas[a]
        tipo, _, alvo = a.partition(":")
        if tipo not in ("target", "devrec"):
            return a
        verbo = "Arm / disarm" if tipo == "target" else "REC / STOP only"
        if alvo.startswith("slot:"):
            n = int(alvo[5:]) if alvo[5:].isdigit() else 0
            nome = slots[n - 1] if 1 <= n <= len(slots) else "?"
            return "%s slot %d  %s" % (verbo, n, nome)
        return "%s %s" % (verbo, (nomes or {}).get(alvo) or alvo)

    def usb_devices(self):
        s, d = api("GET", "/devices", base=USB_BASE, timeout=4)
        if s != 200 or not isinstance(d, dict):
            return None
        return d.get("devices") or []

    def usb_menu(self):
        """Os controladores USB class compliant (MIDI, HID, audio): os ligados e os ja guardados."""
        while True:
            devs = self.usb_devices()
            if devs is None:
                self.done("USB controllers", 0, {"error": "USB CONTROLLERS offline  (GLOBAL SETTINGS > SERVICES)"})
                return
            if not devs:
                self.say("No USB controller plugged in or saved", 1)
                return
            linhas = []
            for x in devs:
                classes = "/".join(c.upper() for c in x["classes"]) or "?"
                if not x["present"]:
                    extra = "not plugged in"
                elif x["map"]:
                    extra = "%d mapped" % len(x["map"])
                elif x["role"]:
                    extra = "role %s" % x["role"]
                else:
                    extra = "no mappings" if {"midi", "hid"} & set(x["classes"]) else "no role"
                linhas.append("%-26.26s %-12s %s%s" % (x["name"], classes, extra, "" if x["enabled"] else "  (OFF)"))
            i = self.menu("USB controllers", linhas, cor=COR["cams"])
            if i is None:
                return
            self.usb_device_menu(devs[i]["key"])

    def usb_device_menu(self, chave):
        while True:
            devs = self.usb_devices() or []
            x = next((d for d in devs if d["key"] == chave), None)
            if x is None:
                return
            entradas = []
            if {"midi", "hid"} & set(x["classes"]):
                entradas += [("learn", "Learn a button"),
                             ("maps", "Mappings  (%d)" % len(x["map"]))]
            if x["roles"]:
                entradas.append(("role", "Role  %s" % (x["role"] or "none")))
            entradas += [("on", "Enabled  %s" % ("ON" if x["enabled"] else "OFF")),
                         ("forget", "Forget this device")]
            i = self.menu("%s  %s" % (x["name"], "" if x["present"] else "(not plugged in)"),
                          [e[1] for e in entradas], cor=COR["cams"])
            if i is None:
                return
            k = entradas[i][0]
            if k == "learn":
                self.usb_learn(x)
            elif k == "maps":
                self.usb_maps(x)
            elif k == "role":
                papeis = x["roles"]
                j = self.menu("Role of %s  (stored; the bridge does not use it yet)" % x["name"],
                              [p or "none" for p in papeis], cor=COR["cams"])
                if j is not None:
                    s, d = self.call_usb("Role", "/role", {"key": chave, "role": papeis[j]})
                    self.done("Role", s, d)
            elif k == "on":
                s, d = self.call_usb("Enabled", "/enable", {"key": chave, "enabled": not x["enabled"]})
                if s != 200 or not d.get("ok"):
                    self.done("Enabled", s, d)
            elif k == "forget" and self.confirm("Forget %s and all its mappings?" % x["name"]):
                s, d = self.call_usb("Forget", "/forget", {"key": chave})
                if s != 200 or not d.get("ok"):
                    self.done("Forget", s, d)
                    continue
                return

    def call_usb(self, label, path, body):
        """Como call(), mas para o servico bridge-usb."""
        self.scr.erase()
        self.put(1, 1, "%s ..." % label, self.attr("bold"))
        self.scr.refresh()
        return api("POST", path, body, base=USB_BASE, timeout=6)

    def usb_learn(self, x):
        """Espera que se carregue num botao do dispositivo e pergunta o que ele faz. Enquanto espera, o servico nao faz
        a accao desse botao (se ja estiver mapeado) e agarra um teclado/pedal HID (a tecla nao chega a esta consola)."""
        if not x["present"]:
            self.say("%s is not plugged in" % x["name"], 2)
            return
        t0, ev, ping = time.time(), None, 0.0
        self.scr.timeout(300)
        try:
            while time.time() - t0 < 20:
                if time.time() - ping > 8:              # o servico larga o aprender sozinho se a consola cair
                    ping = time.time()
                    api("POST", "/learn", {"key": x["key"], "secs": 25}, base=USB_BASE, timeout=2)
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.title_bar("LEARN  %s" % x["name"], COR["cams"])
                self.put(2, 1, "Press a control on the device (%ds)" % (20 - (time.time() - t0)),
                         self.attr("yellow") | curses.A_BOLD)
                self.put(4, 1, "Any key cancels", self.attr("dim"))
                self.scr.refresh()
                if self.scr.getch() != -1:
                    return
                d = next((y for y in (self.usb_devices() or []) if y["key"] == x["key"]), None)
                if d and d.get("last_event") and d.get("last_event_age") is not None \
                        and d["last_event_age"] <= time.time() - t0:
                    ev = d["last_event"]
                    break
        finally:
            self.scr.timeout(250)
            api("POST", "/learn", {"key": x["key"], "secs": 0}, base=USB_BASE, timeout=2)
        if not ev:
            self.say("Nothing pressed on %s" % x["name"], 1)
            return
        s, d = api("GET", "/actions", base=USB_BASE, timeout=6)
        accoes, slots, nomes = ((d.get("actions") or [], d.get("slots") or [], d.get("labels") or {})
                                if s == 200 else ([], [], {}))
        if not accoes:
            self.say("USB CONTROLLERS did not give the actions", 2)
            return
        # Os botoes por grupos: o que vale para o rig, as marcas, um device (segue o device) e um slot (segue a ordem do ecra)
        grupos = [("REC AND TARGETS", [a for a in accoes if a == "rec" or a.startswith("targets:")]),
                  ("MARK", [a for a in accoes if a.startswith("mark:")]),
                  ("ONE DEVICE", [a for a in accoes if a.split(":")[0] in ("target", "devrec")
                                  and a.split(":")[1:2] != ["slot"]]),
                  ("ONE SLOT  (screen order)", [a for a in accoes if a.split(":")[0] in ("target", "devrec")
                                                and a.split(":")[1:2] == ["slot"]])]
        itens, acts = [], []
        for titulo, grupo in grupos:
            if grupo:
                itens.append(Sec(titulo))
                acts.append(None)
                for a in grupo:
                    itens.append(self.usb_accao_txt(a, slots, nomes))
                    acts.append(a)
        atual = x["map"].get(ev)
        j = self.menu("%s: what does it do?%s" % (ev, "  (now: %s)" % self.usb_accao_txt(atual, slots, nomes) if atual else ""),
                      itens, cor=COR["cams"])
        if j is None:
            return
        s, d = self.call_usb("Mapping", "/map", {"key": x["key"], "event": ev, "action": acts[j]})
        self.done("Mapping", s, d, ["%s -> %s" % (ev, self.usb_accao_txt(acts[j], slots, nomes))]
                  if s == 200 and d.get("ok") else None)

    def usb_maps(self, x):
        if not x["map"]:
            self.say("No mappings yet: use Learn a button", 1)
            return
        s, d = api("GET", "/actions", base=USB_BASE, timeout=6)
        slots, nomes = ((d.get("slots") or [], d.get("labels") or {}) if s == 200 else ([], {}))
        # note:1:9 antes de note:1:36 (os numeros como numeros)
        evs = sorted(x["map"], key=lambda e: [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in e.split(":")])
        j = self.menu("Mappings  %s" % x["name"],
                      ["%-14s %s" % (e, self.usb_accao_txt(x["map"][e], slots, nomes)) for e in evs], cor=COR["cams"])
        if j is not None and self.confirm("Remove %s -> %s?" % (evs[j], self.usb_accao_txt(x["map"][evs[j]], slots, nomes))):
            s, d = self.call_usb("Remove mapping", "/map", {"key": x["key"], "event": evs[j], "action": ""})
            self.done("Remove mapping", s, d)

    def bmpcc_remove(self):
        if self.confirm("Remove the BMPCC from this session?\n\nIt stays in the saved list."):
            s, d = self.call("Remove BMPCC", "POST", "/api/devices/bmpcc/remove")
            self.done("Remove BMPCC", s, d)

    # --- ordem e nome curto de referencia ---------------------------------------
    def move_row(self, delta):
        """Muda a camara seleccionada de sitio no ecra principal. Fica por guardar
        (S) ou desfazer (U) com S ou U -- ver order_save() e order_undo()."""
        j = self.cur + delta
        if not 0 <= j < len(self.rows):
            return
        ids = [r["id"] for r in self.rows]          # ja estao pela ordem pendente, se a ha
        ids[self.cur], ids[j] = ids[j], ids[self.cur]
        self.pending_order = ids
        self.cur = j

    def order_save(self):
        """Guarda a ordem das camaras no bridge; se ele a recusar, fica nesta consola, que a aplica ao desenhar."""
        ids = self.pending_order
        s, d = api("POST", "/api/devices/batch", {"action": "order", "ids": ids}, timeout=10)
        if s == 200:
            self.say("Camera order saved", 1)
        elif menu_ordem_gravar("devices-order", ids):
            self.say("Camera order saved on THIS console only (bridge HTTP %s)" % s, 2)
        else:
            self.say("Could not save the order (bridge %s, console file not writable)" % s, 2)
            return
        self.pending_order = None
        self.snap.poll_once()

    def order_undo(self):
        self.pending_order = None
        self.say("Camera order undone", 1)

    def alias_edit(self, row):
        """O alias da camara (nome curto de referencia): vai para a coluna alias do shoot log."""
        a = self.prompt("Alias for %s (max 10 characters, empty clears):" % row["label"],
                        row.get("alias") or "")
        if a is None:
            return
        s, d = self.call("Alias %s" % row["label"], "POST", "/api/devices/%s/alias" % row["id"],
                         {"alias": a.strip()})
        self.done("Alias %s" % row["label"], s, d)
        self.snap.poll_once()

    def iphone_remove(self, cid):
        ip = self._iphone(cid)
        if self.confirm("Remove %s from the bridge?\n\nIt stays in the saved list." % (ip.get("name") or cid)):
            s, d = self.call("Remove iPhone", "POST", "/api/devices/%s/remove" % cid)
            self.done("Remove iPhone", s, d)

    def toggle_target(self, rid):
        """Arma/desarma um device. Vai so a mudanca (o bridge aplica-a a lista que tem): dois toques seguidos, aqui ou
        num iPad ao mesmo tempo, nunca se desfazem."""
        on = rid not in self.targets
        self.targets = [x for x in self.targets if x != rid] + ([rid] if on else [])
        self.snap.alvos_locais(self.targets)
        self.act("targets", "POST", "/api/rec/targets", {"id": rid, "on": on})

    # --- assistente de inicio de sessao (W) ---------------------------------
    # Percorre a preparacao pela ordem certa. Cada passo mostra o estado, oferece
    # uma accao (Enter) e so avanca (N) quando esta certo, ou quando se salta (S)
    # de proposito. Nada destrutivo sem perguntar. Os passos sao funcoes que
    # devolvem (linhas, ok, texto da accao, accao).
    def wizard(self):
        steps = [
            ("Atom", COR["sys"], self.wz_system),
            ("Wi-Fi", COR["net"], self.wz_network),
            ("System clock", COR["time"], self.wz_clock),
            ("TC-1", CLOCK_COR["ltc"], self.wz_tc1),
            ("LTC", CLOCK_COR["ltc"], self.wz_ltc),
            ("Cameras", COR["cams"], self.wz_cams),
            ("Zoom F6", COR["sys"], self.wz_f6),
            ("DJI Mic 3", COR["time"], self.wz_mic3),
            ("GoPro", COR["cams"], self.wz_gopro),
            ("Camera clocks", COR["time"], self.wz_camclock),
            ("iPhone timecode", COR["time"], self.wz_iphones),
            ("BMPCC settings", COR["cams"], self.wz_bm_settings),
            ("Canon settings", COR["cams"], self.wz_canon_settings),
            ("REC targets", COR["rec"], self.wz_targets),
            ("Shoot log", COR["log"], self.wz_shootlog),
            ("Color card", COR["rec"], self.wz_colorcard),
            ("Test take", COR["rec"], self.wz_testtake),
        ]
        state = {"skipped": []}
        i = 0
        while i < len(steps):
            nome, cor, fn = steps[i]
            r = self.wz_screen(i, len(steps), nome, cor, fn, state)
            if r == "quit":
                return
            if r == "skip":
                state["skipped"].append(nome)
            i = max(0, i - 1) if r == "back" else i + 1
        self.wz_finish(state)

    def wz_screen(self, i, n, nome, cor, fn, state):
        self.scr.timeout(500)
        while True:
            try:
                lines, ok, alabel, afn = fn(state)
            except Exception as e:
                lines, ok, alabel, afn = [("error in this step: %s" % e, "red")], False, None, None
            h, w = self.scr.getmaxyx()
            self.scr.erase()
            self.title_bar("START OF SESSION   %d/%d   %s" % (i + 1, n, nome), cor)
            rows = wrap_lines(lines, max(20, w - 3))
            for j, (text, color) in enumerate(rows[:max(0, h - 6)]):
                self.put(2 + j, 1, text, self.attr(color))
            if ok:
                self.put(h - 4, 1, "OK. N: next step", self.attr("green") | curses.A_BOLD)
            else:
                self.put(h - 4, 1, ("Not done yet. Enter: %s   S: skip this step" % alabel) if alabel
                         else "Not done yet. S: skip this step", self.attr("yellow") | curses.A_BOLD)
            self.show_msg(h - 3)
            self.put(h - 1, 0, " %sN next   S skip   B back   Q leave" % (("Enter %s   " % alabel) if alabel else ""), self.attr(cor))
            self.scr.refresh()
            k = self.scr.getch()
            if k in (10, 13, curses.KEY_ENTER) and afn:
                afn(state)
                self.scr.timeout(500)
            elif k in (ord("n"), ord("N"), curses.KEY_RIGHT):
                if ok:
                    return "next"
                self.say("Not done yet: Enter to do it, or S to skip it", 1)
            elif k in (ord("s"), ord("S")):
                return "skip"
            elif k in (ord("b"), ord("B"), curses.KEY_LEFT):
                return "back"
            elif k in (ord("q"), ord("Q"), 27):
                if self.confirm("Leave the start-of-session wizard? What was done stays done."):
                    return "quit"
                self.scr.timeout(500)

    def fresh_rows(self):
        st, dv, *_ = self.snap.get()
        return build_rows(st, dv), list(st.get("rec_targets") or [])

    # 1
    def wz_system(self, state):
        st = self.st()
        pw = st.get("power") or {}
        bt = pw.get("battery") or {}
        L = [("Power and resources of the bridge computer.", "bold"), ""]
        if pw.get("ac") is True:
            L.append(("On AC power.", "green"))
        elif pw.get("ac") is False:
            falta = ("  (~%dh%02d left)" % divmod(bt["minutes"], 60)) if bt.get("minutes") is not None else ""
            L.append(("ON BATTERY: %s%%%s. Plug the charger in for a long session." % (bt.get("percent"), falta),
                      "yellow"))
        else:
            L.append(("Power state unknown.", "dim"))
        if bt.get("health") is not None:
            L.append("Battery health: %d%% of the original capacity." % bt["health"])
        L.append("RAM used %s (%s MB free)   swap %s MB   CPU load %s   CPU %s C" % (
            ram_txt(st), _ou(st.get("mem_available_mb")), _ou(st.get("swap_used_mb")), carga_txt(st), _ou(st.get("soc_temp_c"))))
        return L, True, None, None

    # 2
    def wz_network(self, state):
        net = self.cache.get("net", 6, lambda: api("GET", "/api/net/status", timeout=15))
        ok = bool(net and net[0] == 200 and net[1].get("ap_up"))
        L = self.net_lines()
        if not ok:
            L = [("The camera AP is not up: the Canon bodies cannot connect.", "yellow"), ""] + L

        def accao(state):
            if self.confirm("Restart the camera AP? The Canon bodies drop for a moment."):
                s, d = self.call("Restart camera AP", "POST", "/api/net/action", {"action": "ap_cameras"})
                self.done("Restart camera AP", s, d)
                self.cache.drop("net")
        return L, ok, "restart the camera AP", accao

    # 3
    def wz_clock(self, state):
        st = self.st()
        src = st.get("clock_sources") or {}
        ntp, tel = (src.get("ntp") or {}).get("ok"), (src.get("iphone") or {}).get("ok")
        L = [("The system clock has to be right before anything is set from it.", "bold"), "",
             "System clock: %s" % time.strftime("%A %Y-%m-%d %H:%M:%S"),
             ("Source: NTP" if ntp else "Source: iPhone" if tel else "Source: none confirmed",
              CLOCK_COR["ntp"] if ntp else "green" if tel else "red")]
        if not (ntp or tel):
            L += ["", "With an uplink: Get NTP. Without one: GET PHONE TIME on the phone page."]

        def accao(state):
            s, d = self.call("Get NTP", "POST", "/api/time/ntp/sync", timeout=45)
            extra = (["Clock now: %s" % d.get("now"), "Jump: %+.1f s" % (d.get("jump") or 0)]
                     if s == 200 and d.get("ok") else None)
            self.done("Get NTP", s, d, extra)
        return L, bool(ntp or tel), "Get NTP", accao

    # 4
    def wz_tc1(self, state):
        L = [("Check by hand, then confirm with Enter:", "bold"), "",
             "1. Jam the TC-1 units in the Sidus Audio app (they follow the phone's clock).",
             "2. Leave the app OPEN with the timecodes running: the next step reads them",
             "   over Bluetooth, no cable needed.",
             "3. No phone? TC-1 in L-OUT, 25 fps, its cable in the MICROPHONE jack.",
             "4. Each camera's TC-1 in place (and the one for the Zoom F6 in its timecode jack)."]

        def accao(state):
            state["tc1"] = True
        return L, bool(state.get("tc1")), "confirm it is done", accao

    # 5
    def wz_ltc(self, state):
        st, _cn, _sh, _ok, _e, at = self.snap.get()
        r = ltc_now(st, at)
        d = ltc_date(st)
        pref = st.get("clock_source_pref")
        L = [("Read the TC-1 and make it the source that sets the cameras.", "bold"), ""]
        if r:
            L.append(("LTC %s  %s fps   offset %+.2f s" % (r[0], r[1], float((st.get("ltc") or {}).get("offset") or 0)),
                      CLOCK_COR["ltc"]))
        else:
            L.append(("No valid LTC stored yet.", "dim"))
        data_ok = True
        if d:
            data_ok = d[0][:3] == time.localtime()[:3]
            L.append(("TC-1 date: %s%s" % (d[1], "" if data_ok else "   DIFFERENT from the system date: re-jam the TC-1"),
                      "green" if data_ok else "red"))
        L.append(("Cameras set from: %s" % (CLOCK_LABELS.get(pref, "none picked yet")),
                  CLOCK_COR["ltc"] if pref == "ltc" else "yellow"))

        def accao(state):
            # Primeiro pelo Bluetooth (app Sidus aberta, sem cabo); se falhar, pelo cabo.
            s, dd = self.call("Get LTC by Bluetooth", "POST", "/api/time/ltc/jam", {"via": "ble"}, timeout=45)
            via = "Bluetooth"
            if not (s == 200 and dd.get("ok")):
                erro_bt = (dd.get("error") if isinstance(dd, dict) else None) or "bridge offline"
                s, dd = self.call("Get LTC by cable", "POST", "/api/time/ltc/jam", {}, timeout=30)
                via = "cable (Bluetooth failed: %s)" % erro_bt
            if s == 200 and dd.get("ok"):
                s2, d2 = self.call("Source LTC", "POST", "/api/time/source", {"source": "ltc"})
                self.done("Get LTC + source LTC", s2, d2,
                          ["Timecode: %s @ %s fps" % (dd.get("tc"), dd.get("fps")),
                           "Offset: %s s   continuity: %s" % (dd.get("offset"), dd.get("continuity")),
                           "Read via: %s" % via,
                           "The cameras are now set from the LTC."] if s2 == 200 and not d2.get("error") else None)
            else:
                self.done("Get LTC", s, dd, ["Bluetooth failed too: %s" % erro_bt])
        return L, bool(r and data_ok and pref == "ltc"), "Get LTC and use it as the source", accao

    # 6
    def wz_cams(self, state):
        rows, targets = self.fresh_rows()
        L = [("Connect the cameras you will use (green name = connected).", "bold"), ""]
        for r in rows:
            batt = ("%d%%" % r["batt"]) if isinstance(r["batt"], (int, float)) and r["batt"] >= 0 else "-"
            cor = "green" if r["conn"] else ("yellow" if r["busy"] else "red")
            L.append(("%-12s %-11s battery %-5s left %-10s %s" % (
                r["label"], "connected" if r["conn"] else ("connecting" if r["busy"] else "offline"),
                batt, r["left"], "(REC target)" if r["id"] in targets else ""), cor))
        ok = any(r["conn"] for r in rows) and all(r["conn"] for r in rows if r["id"] in targets)
        if not ok and targets:
            L += ["", ("A REC target is not connected yet.", "yellow")]

        def accao(state):
            for r in rows:
                # A BMPCC liga-se sozinha pelo gateway C3: aqui so as Canon.
                if (not r["conn"] and not r["busy"] and r["id"] not in ("f6", "bmpcc")
                        and not nao_canon(r["id"])):
                    api("POST", "/api/devices/%s/connect" % r["id"], {}, timeout=10)
            self.say("Connecting ... the list updates by itself", 1)
        return L, ok, "connect the cameras that are off", accao

    # 7
    def wz_camclock(self, state):
        st = self.st()
        rows, _t = self.fresh_rows()
        L = [("Write the time to every connected camera.", "bold"), "",
             "Cameras set from: %s" % CLOCK_LABELS.get(st.get("clock_source_pref"), "none picked yet"),
             "Connected: %s" % (", ".join(r["label"] for r in rows if r["conn"]) or "none")]
        if state.get("camclock_txt"):
            L += [""] + state["camclock_txt"]

        def accao(state):
            s, d = self.call("Set all camera clocks", "POST", "/api/time/cameras/sync", timeout=60)
            if s == 200 and not d.get("error"):
                state["camclock"] = True
                state["camclock_txt"] = [("Set:    %s" % (", ".join(d.get("acertadas") or []) or "-"), "green"),
                                         ("Failed: %s" % (", ".join(d.get("falhadas") or []) or "-"),
                                          "red" if d.get("falhadas") else "dim")]
            else:
                self.done("Set all camera clocks", s, d)
        return L, bool(state.get("camclock")), "set ALL camera clocks", accao

    # 8
    def wz_bm_settings(self, state):
        st = self.st()
        rows, _t = self.fresh_rows()
        bm = next((r for r in rows if r["id"] == "bmpcc"), None)
        if not bm or not bm["conn"]:
            return [("BMPCC not connected: nothing to check here.", "dim")], True, None, None
        L = [("Check the BMPCC before rolling.", "bold"), "",
             "ISO: %s" % _ou(st.get("iso")), "Battery: %s%%" % st.get("battery_percent"),
             "Remaining: %s" % (st.get("remaining_time") or "-"), "Timecode: %s" % st.get("timecode")]
        for k in (1, 2, 3):
            if st.get("slot%d_present" % k):
                L.append("  %s: %s" % (st.get("slot%d_name" % k), st.get("slot%d_remaining" % k) or "-"))
        return L, True, "change the ISO", lambda state: self.bmpcc_iso(bm)

    # 9
    def wz_canon_settings(self, state):
        rows, _t = self.fresh_rows()
        cn = self.devs("canon")
        canons = [r for r in rows if not nao_canon(r["id"]) and r["conn"]]
        if not canons:
            return [("No Canon connected: nothing to check here.", "dim")], True, None, None
        L = [("Check the Canon settings before rolling.", "bold"), ""]
        polling = next((c.get("settings_polling", True) for c in cn.values()), True)
        for r in canons:
            c = cn.get(r["id"]) or {}
            L.append(("%s  (%s)" % (r["label"], r["mode"]), "bold"))
            for k in CANON_SETTINGS:
                L.append("   %-12s %s" % (k, c.get(k) if c.get(k) is not None else "-"))
        if not polling:
            L += ["", ("Reading the settings is OFF: these values may be old. Enter turns it on.", "yellow")]
            return L, True, "turn the settings reading on", lambda state: self.toggle_polling()

        def accao(state):
            alvo = canons[0]
            if len(canons) > 1:
                j = self.menu("Which camera?", [r["label"] for r in canons], cor=COR["cams"])
                if j is None:
                    return
                alvo = canons[j]
            self.canon_setting(alvo)
        return L, True, "change a Canon setting", accao

    # 10
    def wz_targets(self, state):
        rows, targets = self.fresh_rows()
        if self.st().get("f6") is not None:
            rows = rows + [{"id": "f6", "label": "Zoom F6"}]
        L = [("Which cameras record with R (REC targets):", "bold"), ""]
        for r in rows:
            L.append(("[%s] %s" % ("x" if r["id"] in targets else " ", r["label"]),
                      "green" if r["id"] in targets else "dim"))

        def accao(state):
            while True:
                rs, tg = self.fresh_rows()
                if self.st().get("f6") is not None:
                    rs = rs + [{"id": "f6", "label": "Zoom F6"}]
                j = self.menu("REC targets",
                              ["[%s] %s" % ("x" if r["id"] in tg else " ", r["label"]) for r in rs], cor=COR["rec"])
                if j is None:
                    return
                rid = rs[j]["id"]
                self.snap.acao()
                api("POST", "/api/rec/targets", {"id": rid, "on": rid not in tg}, timeout=10)
                self.snap.poll_once()
        return L, bool(targets), "choose the REC targets", accao

    # 11
    def wz_shootlog(self, state):
        sh = self.snap.get()[2]
        hoje = time.strftime("%Y-%m-%d")
        n_hoje = sum(1 for x in sh if (x.get("timestamp") or "").startswith(hoje))
        L = [("Shoot log for this session.", "bold"), "",
             "Entries today (in the last %d): %d" % (len(sh), n_hoje), ("Newest:", "dim")]
        for x in sh[:5]:
            L.append("  %s  %-8s %-8s %s" % ((x.get("timestamp") or "")[5:], x.get("source", ""),
                                            x.get("action", ""), x.get("note", "")))
        if state.get("log_txt"):
            L += [""] + state["log_txt"]

        def accao(state):
            feito = []
            if self.confirm("Start a NEW shoot log for this session?\n\n"
                            "The current one is archived and take numbers restart at 1. n = keep it."):
                s, d = self.call("New shoot log", "POST", "/api/log/shoot/new")
                if not self.done("New shoot log", s, d):
                    return
                feito.append(("New shoot log started (old one: %s)" % d.get("archived"), "green"))
            else:
                feito.append(("Kept the current shoot log.", "dim"))
            t = self.prompt("Opening note for the shoot log (project, place...). Empty = none")
            if t and t.strip():
                s, d = self.call("Opening note", "POST", "/api/log/shoot/note", {"note": "SESSION | " + t.strip()})
                if self.done("Opening note", s, d):
                    feito.append(("Opening note written.", "green"))
            state["log_done"], state["log_txt"] = True, feito
        return L, bool(state.get("log_done")), "new log + opening note", accao

    # Cartao de calibracao de cor: fisico, so um lembrete (depois das camaras, dos alvos e do shoot log)
    def wz_colorcard(self, state):
        rows, targets = self.fresh_rows()
        L = [("Color calibration card", "bold"), "",
             "Film the physical card with every camera before the first take:",
             "  - card in the scene, in the shooting light, filling the frame",
             "  - same white balance and picture profile on every camera",
             "  - a few seconds of each camera on the card", "",
             "Cameras: %s" % (", ".join(r["label"] for r in rows if r["id"] in targets) or "none")]
        if state.get("card_done"):
            L += ["", ("Marked as done.", "green")]

        def accao(state):
            state["card_done"] = True
        return L, bool(state.get("card_done")), "mark the card as done", accao

    # 12
    def wz_testtake(self, state):
        rows, targets = self.fresh_rows()
        L = [("A 3-second test take on the REC targets, checked in the shoot log.", "bold"), "",
             "Targets: %s" % (", ".join(r["label"] for r in rows if r["id"] in targets) or "none")]
        if state.get("test_txt"):
            L += [""] + state["test_txt"]
        return L, bool(state.get("test_ok")), "record the test take", self.wz_do_test

    def wz_do_test(self, state):
        rows, targets = self.fresh_rows()
        if any(r["rec"] for r in rows) or self.a_gravar():     # o a_gravar ve tambem o F6
            self.say("Something is recording: stop it first", 2)
            return
        alvos = [r for r in rows if r["id"] in targets]
        if not alvos:
            self.say("No REC targets", 2)
            return
        if not self.confirm("Record a 3-second test take on: %s?" % ", ".join(r["label"] for r in alvos)):
            return
        t0 = time.time() - 1
        s, d = self.call("Test take: REC", "POST", "/api/rec", timeout=20)
        if s != 200 or d.get("error"):
            self.done("Test take: REC", s, d)
            return
        for n in (3, 2, 1):
            self.scr.erase()
            self.title_bar("TEST TAKE", COR["rec"])
            self.put(3, 2, "Recording ... %d" % n, self.attr("red") | curses.A_BOLD)
            self.scr.refresh()
            time.sleep(1)
        s, d = self.call("Test take: STOP", "POST", "/api/rec", timeout=20)
        if s != 200 or (isinstance(d, dict) and d.get("error")) or (isinstance(d, dict) and d.get("recording")):
            self.pager("TEST TAKE", lambda: [("STILL RECORDING: the STOP did not go through.", "red"), "",
                                            ("Stop the cameras by hand (R on the dashboard, or on each camera).", "red"),
                                            "", "Bridge said: %s" % ((d or {}).get("error") if isinstance(d, dict) else s)],
                       live=False, cor=COR["rec"])
            return
        time.sleep(3)                         # a ultima linha de stop pode chegar um pouco depois
        s3, d3 = api("GET", "/api/log/shoot?limit=40", timeout=10)
        log = d3.get("rows") or [] if s3 == 200 else []
        # O shoot log escreve a fonte com o nome da camara no bridge (R6, nao R6 II).
        fonte = {"bmpcc": "BMPCC", "r10": "R10", "r6m2": "R6"}
        txt, tudo = [], True
        for r in alvos:
            ev = set()
            for x in log:
                try:
                    tx = time.mktime(time.strptime(x.get("timestamp") or "", "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
                if tx >= t0 and x.get("source") == fonte.get(r["id"], r["label"]):
                    ev.add(x.get("action"))
            bom = "start" in ev and "stop" in ev
            tudo = tudo and bom
            txt.append(("%-12s %s" % (r["label"], "start and stop in the log" if bom else
                                     "MISSING in the log (got: %s)" % (", ".join(sorted(ev)) or "nothing")),
                        "green" if bom else "red"))
        state["test_ok"], state["test_txt"] = tudo, txt
        self.snap.poll_once()

    # 13
    def wz_f6(self, state):
        if self.st().get("f6") is None:
            return [("No Zoom F6 in this bridge: nothing to set up here.", "dim")], True, None, None
        f6 = self.st().get("f6") or {}
        pronto = bool(f6.get("ready") or f6.get("running"))
        L = [("Zoom F6 (a REC target, like the cameras):", "bold"), "",
             "1. The bridge's HEADPHONE output in the F6 timecode jack: tip and sleeve only.",
             "2. Jam: ESC > TIME > ZOOM F6 > Send audio LTC (needs the LTC from the step before). Check the F6 took it,",
             "   then it keeps the timecode on its own (TIMECODE Int Free Run, 25 fps).",
             "3. On the F6: FRC-8 > connect with the USB cable UNPLUGGED, then plug the",
             "   cable into the bridge. REC/STOP then go to the F6 and it confirms them.",
             "",
             ("FRC-8 link: %s" % ("connected" if pronto else "NOT READY: %s" % (f6.get("why") or "?")),
              "green" if pronto else "red")]

        def accao(state):
            state["f6"] = True
        return L, bool(state.get("f6")) and pronto, "confirm it is set up", accao

    def wz_mic3(self, state):
        """O jam do DJI Mic 3 pela entrada de audio do recetor (so por cabo, como o F6). O Mic 3 nao
        diz nada ao bridge: a confirmacao e a mao."""
        f6 = self.st().get("f6")
        if f6 is None:
            return [("This bridge has no LTC output (the Zoom F6 module): nothing to do here.", "dim")], True, None, None
        enviado = state.get("mic3_sent")
        L = [("DJI Mic 3: jam its timecode from the bridge (by cable, like the F6).", "bold"), "",
             "1. Cable from the bridge's HEADPHONE output to the Mic 3 RECEIVER 3.5 mm port.",
             "2. On the receiver: timecode L-IN, at the cameras' frame rate (25 fps).",
             "3. Enter sends 15 s of running LTC: while it plays, tap Sync on the receiver.",
             "4. The receiver shows EXT and the timecode. Wait for the disconnect icon, then unplug:",
             "   the receiver passes the timecode to its transmitters and keeps it on its own.",
             "No Mic 3: S skips this step.", ""]
        if f6.get("jamming"):
            L.append(("Sending LTC now (%s): tap Sync on the receiver." % (f6.get("tc") or "-"), "yellow"))
        elif enviado:
            L.append(("LTC sent %s ago. The Mic 3 does not report back: check the receiver, then Enter confirms."
                      % fmt_ago(enviado), "green"))
        else:
            L.append(("Not sent yet.", "yellow"))
        if state.get("mic3"):
            L.append(("Confirmed by hand.", "green"))

        def accao(state):
            if state.get("mic3_sent"):
                r = self.confirm_resend()
                if r is None:
                    return
                if r == 0:
                    state["mic3"] = True
                    return
            s, d = self.call("LTC to the DJI Mic 3", "POST", "/api/time/ltc/send", {"to": "DJI Mic 3", "secs": 15}, timeout=10)
            if s == 200 and isinstance(d, dict) and d.get("ok"):
                state["mic3_sent"] = time.time()
                self.say("Sending 15 s of LTC: tap Sync on the receiver now", 0)
            else:
                if s == 404 and not (isinstance(d, dict) and "error" in d):
                    d = {"error": "update the bridge from GitHub first"}
                self.done("LTC to the DJI Mic 3", s, d)
        return L, bool(state.get("mic3")), ("confirm the Mic 3 took it" if enviado else "send 15 s of LTC"), accao

    def confirm_resend(self):
        """Depois de enviar: 0 = o Mic 3 ficou com o timecode, 1 = voltar a enviar, None = nada."""
        i = self.menu("DJI Mic 3: did the receiver take the timecode (EXT and the time shown)?", [
            ("Yes, it took it", COR["time"]),
            ("No: send the LTC again (15 s)", COR["rec"])], cor=COR["time"])
        return i

    def wz_gopro(self, state):
        """As GoPro pelo gateway: o gateway responde, cada GoPro esta ligada, e o relogio dela esta certo."""
        st = self.st()
        gps = sorted(self.devs("gopro").items())
        # Todos os S3 ligados no bridge (antes so o primeiro contava)
        gws = [g for g in (st.get("gopro_gateways") or []) if g.get("enabled", True) is not False]
        if not gws:
            return [("No S3 gateway on in the bridge: nothing to set up here.", "dim")], \
                True, None, None
        if not gps and not any(g.get("host") for g in gws):
            return [("No GoPro in this bridge: nothing to set up here.", "dim")], True, None, None
        gw_ok = all(bool(g.get("ok")) for g in gws)
        L = [("GoPro, through the ESP32-S3 gateway%s." % ("s" if len(gws) > 1 else ""), "bold"), ""]
        for g in gws:
            L.append(("%s %s: %s" % (g.get("name") or "S3", g.get("host") or "no address",
                                     "up" if g.get("ok") else "NOT ANSWERING"), "green" if g.get("ok") else "red"))
        if not gps:
            L.append(("No GoPro added yet.", "yellow"))
        relogios = state.setdefault("gp_clock", {})
        tudo = gw_ok and bool(gps)
        for cid, gp in gps:
            nome = gp.get("name") or cid
            bat = gp.get("battery") if gp.get("battery") is not None else "-"
            if not gp.get("connected"):
                L.append(("%-9s NOT CONNECTED" % nome, "red"))
                tudo = False
                continue
            off = relogios.get(cid)
            if off is None:
                L.append(("%-9s connected, battery %s%%   clock not checked yet" % (nome, bat), "yellow"))
                tudo = False
            else:
                bom = abs(off) <= 1.5
                L.append(("%-9s connected, battery %s%%   clock %+.1f s %s" % (nome, bat, off, "" if bom else "  OFF: show it the time QR"),
                          "green" if bom else "red"))
                tudo = tudo and bom
        L += ["", ("Clock: show the GoPro the TIME QR (Help: GoPro clock), then Enter checks again.", "dim")]

        def accao(state):
            if not gw_ok:
                self.gopro_reset_menu()
                return
            for cid, gp in gps:
                if gp.get("connected"):
                    s, d = api("POST", "/api/devices/%s/clock" % cid, timeout=20)
                    if s == 200 and isinstance(d, dict) and d.get("ok"):
                        state.setdefault("gp_clock", {})[cid] = d.get("offset")
            self.snap.poll_once()
        return L, tudo, ("reset the gateway" if not gw_ok else "check the GoPro clocks"), accao

    def wz_iphones(self, state):
        ips = sorted(self.devs("iphone").items())
        if not ips:
            return [("No iPhone in this bridge: nothing to check here.", "dim")], True, None, None
        L = [("How far each iPhone timecode is from the LTC (after the Sidus sync).", "bold"), ""]
        medidos = True
        for cid, ip in ips:
            if not ip.get("connected"):
                L.append(("%-9s NOT REACHABLE %s:%s" % (
                    ip.get("name") or cid, ip.get("host"), ip.get("port")), "red"))
                medidos = False
                continue
            off = ip.get("tc_offset")
            if off is None:
                L.append(("%-9s no LTC to compare with yet" % (ip.get("name") or cid), "yellow"))
                medidos = False
            else:
                L.append(("%-9s %+.2f s  (+-%s)   source %s" % (ip.get("name") or cid, off, ip.get("tc_err"),
                                                                ip.get("tc_source") or "-"),
                          "yellow" if abs(off) > 0.08 else "green"))
            if ip.get("tc_frozen_s") is not None:
                L.append(("          TIMECODE STUCK for %s s: turn network timecode off in the app" % ip["tc_frozen_s"], "red"))
                medidos = False
        L += ["", ("Under +-0.08 s is within a frame or two at 25 fps.", "dim")]

        def accao(state):
            for cid, ip in ips:
                if ip.get("connected"):
                    self.iphone_check_quiet(cid)
        return L, medidos, "measure them all now", accao

    def iphone_check_quiet(self, cid):
        self.say("Measuring %s ..." % (self._iphone(cid).get("name") or cid), 1)
        self.call("TC check", "POST", "/api/devices/%s/tc" % cid, timeout=30)
        self.snap.poll_once()

    def wz_finish(self, state):
        st = self.st()
        rows, targets = self.fresh_rows()
        cams = ", ".join("%s %s" % (r["label"], ("%d%%" % r["batt"]) if isinstance(r["batt"], (int, float))
                                    and r["batt"] >= 0 else "") for r in rows if r["conn"]) or "none"
        resumo = "clock %s | cams %s | targets %s | test %s" % (
            CLOCK_LABELS.get(st.get("clock_source_pref"), "none"), cams,
            "+".join([r["label"] for r in rows if r["id"] in targets]
                     + (["F6"] if "f6" in targets else [])) or "none",
            "ok" if state.get("test_ok") else "not done")
        resumo += " | color card %s" % ("done" if state.get("card_done") else "not done")
        if state["skipped"]:
            resumo += " | skipped: " + ", ".join(state["skipped"])
        s, d = self.call("Session summary", "POST", "/api/log/shoot/note", {"note": "SESSION START | " + resumo})
        self.pager("Session ready", lambda: [
            ("Start of session done.", "green"), "",
            "Summary written to the shoot log:", "  " + resumo, "",
            ("Skipped: %s" % ", ".join(state["skipped"]), "yellow") if state["skipped"] else ("Nothing skipped.", "dim")],
            live=False, cor="green")

    # --- menu geral --------------------------------------------------------
    def main_menu(self):
        while True:
            self.verificar_versao()
            # Cada area na sua cor -- a mesma do rodape e do menu que abre.
            i = self.menu("MENU", ordem="main", items=[
                ("WIZARD", COR["sys"]),
                # A rede, o Bluetooth, o USB e o Zoom estao em DEVICES; as opcoes de cada linha do dashboard no Enter.
                ("DEVICES", COR["cams"]),
                ("TIME", COR["time"]),
                ("LOGS", COR["log"]),
                ("POWER", COR["rec"]),
                ("GLOBAL SETTINGS", COR["sys"]),
                ("HELP", COR["sys"]),
            ], cor=COR["sys"])
            if i is None:
                return
            [self.wizard, self.devices_menu, self.time_menu, self.logs_menu, self.power_menu, self.sys_menu,
             self.help_menu][i]()

    def help_menu(self):
        """Ajuda: as teclas e os "como fazer", uma pagina por assunto. Texto fixo (HELP_PAGES): nao usa o bridge."""
        while True:
            i = self.menu("HELP", [(t, COR["sys"]) for t, _ in HELP_PAGES], cor=COR["sys"])
            if i is None:
                return
            self.help_page(i)

    def help_page(self, i):
        """Uma pagina da ajuda (indice, ou o inicio do titulo). A lista das teclas e gerada a cada abertura."""
        if isinstance(i, str):
            i = next(n for n, (t, _) in enumerate(HELP_PAGES) if t.startswith(i))
        titulo, corpo = HELP_PAGES[i]
        self.pager("Help: " + titulo, lambda: corpo() if callable(corpo) else list(corpo), live=False, cor=COR["sys"])

    # --- camara ------------------------------------------------------------
    def camera_lines(self, row):
        st, dv, *_ = self.snap.get()
        cn = devs_tipo(dv, "canon")
        L = []
        if tipo_de(row["id"]) == "gopro":
            gp = devs_tipo(dv, "gopro").get(row["id"]) or {}
            L.append(("%s (GoPro via gateway %s)  %s" % (gp.get("name") or row["id"], gp.get("gateway") or "-",
                                                         "connected" if row["conn"] else "NOT CONNECTED"),
                      "green" if row["conn"] else "red"))
            L.append("MAC %s" % (gp.get("mac") or "-"))
            if not row["conn"]:
                L.append(("  %s" % (gp.get("error") or gp.get("why") or "?"), "red"))
                return L
            L.append("Recording: %s" % ("YES" if gp.get("running") else "no"))
            L.append("Battery %s%%   video left %s" % (gp.get("battery") if gp.get("battery") is not None else "-",
                                                      fmt_left(gp.get("remaining_s"))))
            return L
        if eh_bm_extra(row["id"]):
            bm = devs_tipo(dv, "bmpcc_extra").get(row["id"]) or {}
            L.append(("%s (BMPCC via its own C3 %s)  %s" % (bm.get("name") or row["id"], bm.get("host") or "-",
                                                          "connected" if row["conn"] else "NOT CONNECTED"),
                      "green" if row["conn"] else "red"))
            if not row["conn"]:
                L.append(("  %s" % (bm.get("why") or bm.get("error") or "?"), "red"))
                return L
            L.append("Recording: %s   timecode %s" % ("YES" if bm.get("running") else "no", bm.get("timecode") or "-"))
            L.append("Battery %s%%" % (bm.get("battery") if bm.get("battery") is not None else "-"))
            return L
        if tipo_de(row["id"]) == "sony":
            so = devs_tipo(dv, "sony").get(row["id"]) or {}
            via = "USB %s" % (so.get("serial") or "-") if so.get("via") == "usb" else "Wi-Fi %s%s" % (
                so.get("host") or "-", "  SSH" if so.get("ssh") else "")
            L.append(("%s (Sony %s, %s)  %s" % (so.get("name") or row["id"], (so.get("caps") or {}).get("label") or
                                                so.get("model") or "?", via,
                                                "connected" if row["conn"] else "NOT CONNECTED"),
                      "green" if row["conn"] else "red"))
            L.append("Model %s   %s" % (so.get("model") or "-", "enabled" if so.get("enabled", True) else "DISABLED"))
            if not row["conn"]:
                L.append(("  %s" % (so.get("why") or so.get("error") or "?"), "red"))
                return L
            L.append(("Recording: %s" % ("YES" if so.get("running") else "no") +
                      ("   RECORDING FAILED (camera)" if so.get("rec_failed") else ""),
                      "red" if so.get("rec_failed") else "white"))
            L.append("Battery %s%%%s%s   video left %s" % (
                so.get("battery") if so.get("battery") is not None else "-",
                ("  %s min" % so["battery_min"]) if so.get("battery_min") is not None else "",
                ("  %.2f V" % so["battery_v"]) if so.get("battery_v") is not None else "",
                fmt_left(so.get("remaining_s"))))
            L.append("ISO %s   f/%s   shutter %s" % (so.get("iso") or "-", so.get("fnumber") or "-", so.get("shutter") or "-"))
            if so.get("fingerprint"):
                L.append(("SSH fingerprint %s" % so["fingerprint"], "dim"))
            return L
        if tipo_de(row["id"]) == "iphone":
            ip = devs_tipo(dv, "iphone").get(row["id"]) or {}
            L.append(("%s (Blackmagic Camera app)  %s" % (ip.get("name") or row["id"],
                                                        "connected" if row["conn"] else "NOT REACHABLE"),
                      "green" if row["conn"] else "red"))
            L.append("Address: %s:%s   %s   %s" % (ip.get("host"), ip.get("port"),
                                                   "enabled" if ip.get("enabled") else "DISABLED",
                                                   "websocket" if ip.get("websocket") else "polling"))
            if not row["conn"]:
                L.append(("  %s" % (ip.get("why") or ip.get("error") or "?"), "red"))
                return L
            L.append("%s  app %s" % (ip.get("product") or "-", ip.get("app_version") or "-"))
            L.append("Recording: %s   clip %s" % ("YES" if ip.get("running") else "no", ip.get("clip") or "-"))
            L.append("Battery %s%%   left %s   clips %s   free %s GB" % (
                ip.get("battery") if ip.get("battery") is not None else "-", fmt_left(ip.get("remaining_s")),
                ip.get("clips") if ip.get("clips") is not None else "-",
                ("%.0f" % (ip["free_bytes"] / 1e9)) if ip.get("free_bytes") else "-"))
            L.append("Format %s  %s" % (ip.get("format") or "-", ip.get("codec") or "-"))
            L.append("ISO %s   shutter %s%s   WB %sK   f/%s   %smm" % (
                ip.get("iso") or "-", ip.get("shutter") or "-", " auto" if ip.get("shutter_auto") else "",
                ip.get("wb") or "-", ("%.1f" % ip["iris"]) if ip.get("iris") else "-", ip.get("focal") or "-"))
            if ip.get("tc_frozen_s") is not None:
                L.append(("EXTERNAL TIMECODE STUCK for %s s: turn network timecode off in the app" % ip["tc_frozen_s"], "red"))
            off = ip.get("tc_offset")
            L.append(("Timecode %s   source %s   vs bridge LTC: %s" % (
                ip.get("tc") or "-", ip.get("tc_source") or "-", ("%+.2f s" % off) if off is not None else "no LTC"),
                "yellow" if off is not None and abs(off) > 0.08 else "white"))
            L.append("Slate: scene %s   last take from the shoot log %s" % (ip.get("scene") or "-", ip.get("take_no") or "-"))
            return L
        if row["id"] == "bmpcc":
            L.append(("BMPCC  %s" % ("connected" if row["conn"] else "offline"),
                      "green" if row["conn"] else "dim"))
            L.append("Recording: %s   Timecode: %s" % ("YES" if row["rec"] else "no", st.get("timecode")))
            L.append("ISO: %s" % _ou(st.get("iso")))
            L.append("Battery: %s%%   power source: %s" % (st.get("battery_percent"), st.get("power_source")))
            L.append("Remaining: %s   active slot: %s" % (st.get("remaining_time") or "-", st.get("active_slot")))
            for n in (1, 2, 3):
                if st.get("slot%d_present" % n):
                    L.append("  %s: %s" % (st.get("slot%d_name" % n), st.get("slot%d_remaining" % n) or "-"))
            L.append("")
            L.append("Last event: %s" % st.get("last_event"))
            return L
        c = cn.get(row["id"]) or {}
        L.append(("%s  %s" % (row["label"], c.get("model") or ""), "green" if row["conn"] else "dim"))
        L.append("State: %s   battery: %s%%" % (
            "REC" if row["rec"] else ("NOT IN VIDEO MODE: switch the camera to video" if row.get("sem_video")
                                      else "ready" if row["conn"] else "offline"), c.get("battery_percent")))
        L.append("Remaining: %s   temperature: %s" % (row["left"], c.get("temperature")))
        L.append("Reading the settings: %s%s" % (
            "ON" if c.get("settings_polling", True) else "OFF (values are not refreshed)",
            "   (first read still running)" if row["busy"] and row["conn"] else ""))
        L.append("")
        for k in CANON_SETTINGS:
            opts = c.get(k + "_options") or []
            L.append("%-12s %-14s (%d options)" % (k, c.get(k) if c.get(k) is not None else "-", len(opts)))
        L.append("")
        L.append(("Raw from the bridge: connected=%s connecting=%s movie_mode=%s settings_polling=%s" % (
            c.get("connected"), c.get("connecting"), c.get("movie_mode"), c.get("settings_polling")), "dim"))
        empty = [k for k in CANON_SETTINGS if c.get(k) is None]
        if empty:
            L.append(("No value yet for: %s" % ", ".join(empty), "yellow"))
        L.append("")
        L.append("Last event: %s" % c.get("last_event"))
        for line in (c.get("log") or [])[-8:]:
            L.append(("  " + line, "dim"))
        return L

    def opcoes_menu(self, titulo, itens_fn, cor):
        """As opcoes de uma linha do dashboard (F6, gateway), uma por linha: as mesmas da barra 1-9, com o nome
        inteiro. Volta ao menu depois de cada uma (os textos mudam: Switch ON/OFF, modo...)."""
        while True:
            itens = itens_fn()
            if not itens:
                return
            i = self.menu(titulo, [(t, c) for t, _, c in itens], cor=cor)
            if i is None:
                return
            itens[i][1]()

    def camera_menu(self, row):
        if not row:
            return
        rid = row["id"]

        def fresh():
            return next((r for r in self.rows if r["id"] == rid), row)

        while True:
            row = fresh()
            items = self.device_items(row, fresh)
            teclas, usadas = {}, set()
            for n, (txt, _, _) in enumerate(items):          # tecla = inicial do item (a 1.a livre)
                for ch in (CAM_KEYS.get(txt.split("  ")[0]) or txt[0]).lower():
                    if ch not in usadas and not ch.isdigit():      # os algarismos sao os numeros 1-9 do menu
                        usadas.add(ch)
                        teclas[ch] = n
                        break
            i = self.menu("Camera %s" % row["label"], [(t, c) for t, _, c in items], cor=COR["cams"], keys=teclas,
                          ordem=self.device_ordem(rid))
            if i is None:
                return
            items[i][1]()

    @staticmethod
    def device_ordem(rid):
        """O nome da ordem guardada do menu Camera deste tipo de device."""
        return "camera-" + ("iphone" if tipo_de(rid) == "iphone" else "gopro" if tipo_de(rid) == "gopro"
                            else "bmpcc_extra" if eh_bm_extra(rid) else "sony" if tipo_de(rid) == "sony"
                            else rid if rid in ("bmpcc", "f6") else "canon")

    def device_items(self, row, fresh=None):
        """As opcoes de um device (texto, accao, cor): as do menu Camera e da barra de opcoes do dashboard.

        So as que se aplicam ao estado: sem ligacao nao ha REC, TC, cena nem definicoes. A accao principal (REC) vai
        primeiro e as raras (retirar, mudar endereco ou nome, a ligacao...) ficam no submenu More."""
        rid = row["id"]
        if fresh is None:
            def fresh():
                return next((r for r in self.rows if r["id"] == rid), row)
        r = fresh()
        ligado = bool(r.get("conn"))
        cc = COR["cams"]
        tipo = tipo_de(rid)
        # A gravar, o mesmo item para (a chave do item continua "REC", antes dos 2 espacos)
        rec_txt = "REC  (stops)" if r.get("rec") else "REC"
        # Sem ligacao nao ha REC, a nao ser que a camara tenha caido a gravar: ai ainda se tenta o STOP.
        items = [(rec_txt, lambda: self.rec_one(fresh()), COR["rec"])] if (ligado or r.get("rec")) else []
        items.append(("Info", lambda: self.pager("Camera %s" % row["label"], lambda: self.camera_lines(fresh()), cor=cc), cc))
        raros = []
        if tipo == "iphone":
            if ligado:
                items += [("Check TC", lambda: self.iphone_check(rid), COR["time"]),
                          ("Scene", lambda: self.iphone_scene(rid), cc)]
            items += [("Config", lambda: self.iphone_cfg_menu(rid), cc)]       # ja e o submenu dos raros do iPhone
        elif tipo == "gopro":
            items += [("Sync time", self.gopro_sync_help, COR["time"])]
            if ligado:
                items += [("Check clock", lambda: self.gopro_clock(rid), COR["time"])]
            raros += [("Remove", lambda: self.gopro_remove(rid), COR["rec"])]
        elif tipo == "sony":
            so = self._sony(rid)
            if ligado:
                items += [("Set TC", lambda: self.sony_tc(rid), COR["time"])]
            raros += [("Address  %s" % (so.get("host") or "USB %s" % (so.get("serial") or "-")),
                       lambda: self.sony_addr(rid), cc),
                      ("Rename", lambda: self.sony_cfg(rid, "name"), cc),
                      ("%s in the bridge" % ("Disable" if so.get("enabled", True) else "Enable"),
                       lambda: self.sony_cfg(rid, "enabled"), cc),
                      ("Remove", lambda: self.sony_remove(rid), COR["rec"])]
        elif eh_bm_extra(rid):
            raros += [("C3 address", lambda: self.bmx_host(rid), cc),
                      ("Rename", lambda: self.bmx_rename(rid), cc),
                      ("Remove", lambda: self.bmx_remove(rid), COR["rec"])]
        elif rid == "bmpcc":
            if ligado:
                items += [("ISO  %s" % _ou(self.st().get("iso")), lambda: self.bmpcc_iso(fresh()), cc)]
            items += [("Strict REC  %s" % ("ON" if self.st().get("strict_rec") else "off"), self.toggle_strict, COR["rec"])]
            raros += [("Connection  %s" % ("Atom Bluetooth (plan B)" if self.st().get("bmpcc_via") == "bluez"
                                           else "C3 gateway"), self.bmpcc_path, cc),
                      ("Remove BMPCC", self.bmpcc_remove, COR["rec"])]
        else:
            poll = (self.devs("canon").get(rid) or {}).get("settings_polling", True)
            if ligado:
                items += [("Settings", lambda: self.canon_setting(fresh()), cc),
                          ("Set clock", lambda: self.canon_clock(fresh()), COR["time"])]
            raros += [("Poll settings  %s" % ("ON" if poll else "OFF"), self.toggle_polling, cc)]
        items += [("Alias  %s" % (r.get("alias") or "-"), lambda: self.alias_edit(fresh()), cc)]
        if raros:
            items += [("More", lambda: self.mais(r["label"], raros), cc)]
        return items

    def mais(self, titulo, itens):
        """O submenu More: as opcoes raras de um device ou gateway (retirar, mudar o endereco ou o nome...)."""
        self.opcoes_menu("More  %s" % titulo, lambda: itens, COR["cams"])

    def f6_items(self):
        """As opcoes do Zoom F6 na barra do dashboard."""
        alias = (self.dv().get("alias") or {}).get("f6", "")
        ligado = bool(((self.st().get("f6") or {}).get("frc") or {}).get("connected"))
        # Sem o FRC-8 ligado nao ha REC nem jam; o leitor de cartao SD, que e raro, vai para More.
        items = [("REC", lambda: self.act("REC F6", "POST", "/api/devices/f6/rec"), COR["rec"])] if ligado else []
        items += [("F6 screen", self.f6_screen, COR["f6"])]
        if ligado:
            items += [("Jam", self.f6_jam_menu, COR["time"])]
        items += [("Alias  %s" % (alias or "-"),
                   lambda: self.alias_edit({"id": "f6", "label": "Zoom F6", "alias": alias}), COR["cams"]),
                  ("More", lambda: self.mais("Zoom F6", [("SD Card reader mode", self.f6card_screen, COR["f6"])]), COR["cams"])]
        return items

    def bar_items(self):
        """As opcoes do device seleccionado, pela ordem do menu Camera (None fora da lista dos devices)."""
        if self.on_gw is not None and self.on_gw < len(self.gws):
            g = self.gws[self.on_gw]
            return "%s gateway" % g["name"], self.gw_items(g)
        if self.on_f6:
            return "ZOOM F6", ordenar_itens(self.f6_items(), "camera-f6")[:9]
        if not self.rows:
            return None, None
        r = self.rows[min(self.cur, len(self.rows) - 1)]
        return r["label"], ordenar_itens(self.device_items(r), self.device_ordem(r["id"]))[:9]

    @staticmethod
    def estado_txt(r):
        """O estado de uma linha de device em poucas palavras (o da coluna STATE do dashboard)."""
        if r.get("pending"):
            return r["pending"] + "...", "yellow"
        if r["rec"]:
            return "REC", "red"
        if r["conn"]:
            if r.get("sem_video"):
                return "not video", "yellow"        # a Canon esta em modo foto: nao grava
            return ("reading" if r["busy"] else "ready"), ("yellow" if r["busy"] else "green")
        return ("connecting" if r["busy"] else "offline"), ("yellow" if r["busy"] else "red")

    def info_segs(self, st, velho_st=False, velho_cn=False):
        """A info da linha seleccionada, numa linha: [(texto, cor)]. Muda com o cursor, sem Enter.
        Com dados velhos (velho_st: /api/status; velho_cn: as Canon) o estado e "?" e o resto vai apagado."""
        if self.on_gw is not None and self.on_gw < len(self.gws):
            g = self.gws[self.on_gw]
            return [("%s %s" % (g["name"], g["what"]), "bold"), ("  " + g["detail"], GW_COR[g["state"]])]
        if self.on_f6:
            f6 = st.get("f6") or {}
            frc = f6.get("frc") or {}
            arm = [str(a) for a in (frc.get("armed") or [])]
            cor = "green" if frc.get("connected") else "red"
            return [("ZOOM F6", "bold"), ("  " + ("REC" if frc.get("recording") else "online" if frc.get("connected") else "offline"), cor),
                    ("  tracks %s" % ("?" if frc.get("armed") is None else " ".join(arm) if arm else "none armed"),
                     "yellow" if not arm else ""),
                    ("  TC %s" % ("jam" if f6.get("jamming") else "ok" if f6.get("last_jam") else "not jammed"), "")]
        if not self.rows:
            return []
        r = self.rows[min(self.cur, len(self.rows) - 1)]
        velho = velho_da_linha(r["id"], velho_st, velho_cn)
        est, cor = self.estado_txt(r)
        if velho:
            est, cor = "?  no fresh data" + ("  (was REC)" if r["rec"] else ""), "dim"
        batt = ("%d%%" % r["batt"]) if isinstance(r["batt"], (int, float)) and r["batt"] >= 0 else None
        partes = [(r["label"], "bold"), ("  " + est, cor)]
        if r.get("via"):
            partes.append(("  via " + r["via"][1], self.via_cor(r["via"][0])))
        for txt in (("bat " + batt) if batt else None, ("left " + r["left"]) if r.get("left") and r["left"] != "-" else None,
                    r.get("mode") if r["conn"] else None, r.get("cfg") or None, ("alias " + r["alias"]) if r.get("alias") else None):
            if txt:
                partes.append(("  " + str(txt), "dim" if velho else ""))
        return partes

    def draw_info(self, st, y, w, velho_st=False, velho_cn=False):
        """Uma linha: o essencial da linha seleccionada (device, F6 ou gateway)."""
        segs, x = [], 1
        for txt, cor in self.info_segs(st, velho_st, velho_cn):
            at = self.attr(cor) if cor else 0           # "bold" e "" ja tem o seu atributo (ou nenhum)
            if x + len(txt) >= w - 1:
                txt = txt[:max(0, w - 2 - x)]
            segs.append((txt, at))
            x += len(txt)
        if segs and y > 0:
            self.put_segs(y, 1, segs)

    def bar_linhas(self, w):
        """A barra das opcoes do device seleccionado, por cima do rodape: muda com o cursor, sem Enter. Os numeros
        1-9 correm a opcao. Uma opcao nunca se corta: o que nao cabe passa para a linha seguinte. [[segmentos]]"""
        _nome, items = self.bar_items()
        if not items:
            return []
        linhas, atual, x = [], [], 0
        for n, (txt, _, cor) in enumerate(items, 1):
            bloco = [("  %d" % n, self.attr(cor) | curses.A_BOLD), (" " + txt, self.attr_suave(cor))]
            larg = sum(len(t) for t, _a in bloco)
            if atual and x + larg >= w - 1:
                linhas.append(atual)
                atual, x = [], 0
            atual += bloco
            x += larg
        if atual:
            linhas.append(atual)
        return linhas[:3]

    def iphone_cfg_menu(self, cid):
        """Os itens raros do iPhone: endereco, nome, acrescentar, retirar."""
        while True:
            ip = self._iphone(cid)
            i = self.menu("Config  %s" % (ip.get("name") or cid), [
                ("IP  %s:%s" % (ip.get("host"), ip.get("port")), COR["cams"]),
                ("Rename  %s" % ip.get("name"), COR["cams"]),
                ("Add iPhone", COR["cams"]),
                ("Remove", COR["rec"]),
            ], cor=COR["cams"])
            if i is None:
                return
            if i == 3:
                self.iphone_remove(cid)
                return                                 # o iPhone pode ter deixado de existir
            [lambda: self.iphone_config(cid), lambda: self.iphone_rename(cid), self.iphone_add][i]()

    def canon_clock(self, row):
        if not row["conn"]:
            self.say("%s is not connected" % row["label"], 2)
        elif self.confirm("Write the bridge's current clock to %s?" % row["label"]):
            s, d = self.call("Set clock on %s" % row["label"], "POST", "/api/devices/%s/sync_time" % row["id"])
            self.done("Set clock on %s" % row["label"], s, d)

    def toggle_polling(self):
        # O interruptor e global no bridge; vem repetido no estado de cada Canon.
        cur = next((c.get("settings_polling", True) for c in self.devs("canon").values()), True)
        on = not cur
        s, d = self.call("Settings polling %s" % ("on" if on else "off"), "POST",
                         "/api/devices/batch",
                         {"action": "settings_polling", "type": "canon", "on": on})
        self.done("Settings polling %s" % ("on" if on else "off"), s, d)

    def canon_setting(self, row):
        if not row["conn"]:
            self.say("%s is not connected" % row["label"], 2)
            return
        if row["rec"]:
            self.say("%s is recording: not changing settings now" % row["label"], 2)
            return
        while True:
            c = self.devs("canon").get(row["id"]) or {}
            labels = ["%-12s now: %s" % (k, c.get(k) if c.get(k) is not None else "-") for k in CANON_SETTINGS]
            i = self.menu("%s: change which setting?" % row["label"], labels, cor=COR["cams"])
            if i is None:
                return
            name = CANON_SETTINGS[i]
            opts = c.get(name + "_options") or []
            if not opts:
                why = ["%s: the camera gave no options for this setting." % name, "",
                       "Value now: %s" % c.get(name),
                       "Video mode: %s" % ("yes" if c.get("movie_mode") else "NO: switch the camera to video"),
                       "Reading the settings: %s" % ("ON" if c.get("settings_polling", True) else "OFF"),
                       ]
                self.pager("%s %s" % (row["label"], name), lambda w=why: w, live=False, cor=COR["cams"])
                continue
            j = self.menu("%s %s (now: %s)" % (row["label"], name, c.get(name)), [str(o) for o in opts],
                          cor=COR["cams"])
            if j is None:
                continue
            s, d = self.call("%s %s" % (name, opts[j]), "POST",
                             "/api/devices/%s/setting/%s" % (row["id"], name), {"value": opts[j]})
            self.done("%s %s" % (name, opts[j]), s, d)

    def bmpcc_iso(self, row):
        if not row["conn"]:
            self.say("BMPCC is not connected", 2)
            return
        if row["rec"]:
            self.say("BMPCC is recording: not changing ISO now", 2)
            return
        agora = self.st().get("iso")
        labels = [("%d  <- now" % v) if v == agora else str(v) for v in BMPCC_ISO_STOPS]
        i = self.menu("BMPCC ISO (now %s)" % agora, labels, cor=COR["cams"])
        if i is None:
            return
        v = BMPCC_ISO_STOPS[i]
        s, d = self.call("Set ISO %d" % v, "POST", "/api/devices/bmpcc/iso", {"iso": v})
        self.done("Set ISO %d" % v, s, d)

    def toggle_strict(self):
        on = not bool(self.st().get("strict_rec"))
        # O mesmo aviso da pagina: pela Bluetooth, o REC da propria camara e
        # igual a uma paragem nao comandada.
        if on and not self.confirm("Turn strict REC on?\n\n"
                                   "REC pressed on the camera itself will raise the alarm."):
            return
        s, d = self.call("Strict REC %s" % ("on" if on else "off"), "POST", "/api/rec/strict", {"on": on})
        self.done("Strict REC %s" % ("on" if on else "off"), s, d)

    # --- relogios ----------------------------------------------------------
    def clock_lines(self):
        st = self.st()
        src = st.get("clock_sources") or {}
        pref = st.get("clock_source_pref")
        L = [("Chosen source: %s      in use: %s" % (CLOCK_LABELS.get(pref, "none picked yet"),
                                                      st.get("clock_effective") or "-"), "bold"), ""]
        for k in CLOCK_ORDER:
            s = src.get(k) or {}
            age = (" (%s old%s)" % (fmt_age(s["age"]), ", STALE" if s.get("stale") else "")) if s.get("age") is not None else ""
            L.append((" %-7s %s%s" % (CLOCK_LABELS[k], "have" if s.get("ok") else "none", age),
                      CLOCK_COR.get(k, "green") if s.get("ok") else "dim"))
        L.append("")
        ltc = st.get("ltc")
        if ltc:
            L.append("LTC stored: %s @ %s fps   continuity: %s   offset: %s s%s" % (
                ltc.get("tc"), ltc.get("fps"), ltc.get("continuity"), ltc.get("offset"),
                "   FORCED" if ltc.get("forced") else ""))
            d = ltc_date(st)
            # Pelo Bluetooth nao ha user bits (a data vem no anuncio): "-" e nao
            # "None (unstable)".
            ub = ltc.get("user_bits")
            L.append("  read %s ago   user bits: %s%s   date: %s" % (
                fmt_age(ltc.get("age")), ub or "-",
                " (unstable)" if ub and ltc.get("user_bits_stable") is False else "",
                d[1] if d else "none"))
            if ltc.get("stale"):
                L.append(("  The reading is stale.", "yellow"))
            if ltc.get("stepped") or ltc.get("invalid_reason"):
                L.append(("  INVALID: %s" % (ltc.get("invalid_reason") or "the clock stepped after it"), "red"))
            if ltc.get("date_mismatch"):
                L.append(("  The LTC date does not match the system date.", "yellow"))
        else:
            L.append("LTC: nothing stored")
        if st.get("ntp_age") is not None:
            L.append("NTP: synced %s ago via %s" % (fmt_age(st.get("ntp_age")), st.get("ntp_via") or "?"))
        else:
            L.append("NTP: not synced.")
        L.append("System clock: %s" % time.strftime("%A %Y-%m-%d %H:%M:%S"))
        L.append("BM TC drift: %s   BM vs LTC: %s" % (st.get("tc_drift"), st.get("tc_ltc_delta")))
        offs = st.get("tc_offsets") or {}
        if offs:
            fps = (st.get("ltc") or {}).get("fps") if isinstance(st.get("ltc"), dict) else None
            L.append("")
            L.append(("Timecode of each camera vs the Atom LTC (+ ahead, - behind):", "bold"))
            for cid, dv in sorted(offs.items()):
                mau = abs(dv["offset"]) - (dv.get("err") or 0) > 1.0 / float(fps or 25)
                L.append(("  %-12s %+.2f s  (+-%.2f)%s" % (dv.get("name") or cid, dv["offset"], dv.get("err") or 0,
                                                          "  MORE THAN A FRAME: jam it again" if mau else ""),
                          "red" if mau else "green"))
        if st.get("ltc_error"):
            L.append(("LTC error: %s" % st.get("ltc_error"), "red"))
        return L

    def time_menu(self):
        while True:
            pref = self.st().get("clock_source_pref")
            usa = lambda k: "   (in use)" if pref == k else ""
            # (chave, texto) ou Sec: despacha-se por chave e nao por posicao, para a
            # ordem do menu poder mudar sem trocar accoes.
            entradas = [
                ("status", "Status"),
                Sec("SOURCE SET"),
                ("src_ltc", "-> LTC" + usa("ltc")),
                ("src_ntp", "-> NTP" + usa("ntp")),
                ("sync_all", "SET all camera clocks now"),
                Sec("LTC"),
                ("ltc_cable", "Get (cable)"),
                ("ltc_force", "Get (cable, forced)"),
                ("ltc_ble", "Get (Bluetooth)"),
                ("ltc_clear", "Clear"),
                Sec("ZOOM F6"),
                ("f6_jam", "Send audio LTC"),
                Sec("NTP"),
                ("ntp_get", "Get"),
                ("ntp_clear", "Clear"),
            ]
            i = self.menu("TIME", [e if isinstance(e, Sec) else e[1] for e in entradas],
                          cor=COR["time"], ordem="time")
            if i is None:
                return
            k = entradas[i][0]
            if k == "status":
                self.pager("Clock sources", self.clock_lines, cor=COR["time"])
            elif k in ("src_ltc", "src_ntp"):
                fonte = k[4:]
                s, d = self.call("Source %s" % CLOCK_LABELS[fonte], "POST", "/api/time/source",
                                 {"source": fonte})
                self.done("Source %s" % CLOCK_LABELS[fonte], s, d)
            elif k == "sync_all":
                if self.confirm("Write the clock to every connected camera?"):
                    s, d = self.call("Set all camera clocks", "POST", "/api/time/cameras/sync", timeout=60)
                    extra = None
                    if s == 200:
                        extra = ["Set:    %s" % (", ".join(d.get("acertadas") or []) or "-"),
                                 "Failed: %s" % (", ".join(d.get("falhadas") or []) or "-")]
                    self.done("Set all camera clocks", s, d, extra)
            elif k in ("ltc_cable", "ltc_force", "ltc_ble"):
                via = "ble" if k == "ltc_ble" else "cable"
                lab = {"ltc_cable": "LTC Get", "ltc_force": "LTC Get (forced)",
                       "ltc_ble": "LTC Get (Bluetooth)"}[k]
                s, d = self.call(lab, "POST", "/api/time/ltc/jam", {"force": k == "ltc_force", "via": via},
                                 timeout=45 if via == "ble" else 30)
                # O Bluetooth nao tem entrada propria para o forcado: oferece-se aqui.
                if (via == "ble" and not d.get("ok") and "confirm to force" in (d.get("error") or "")
                        and self.confirm(d.get("error") + "\n\nForce anyway?")):
                    lab += " (forced)"
                    s, d = self.call(lab, "POST", "/api/time/ltc/jam", {"force": True, "via": via}, timeout=45)
                extra = None
                if s == 200 and d.get("ok"):
                    extra = ["Timecode: %s @ %s fps" % (d.get("tc"), d.get("fps")),
                             "Offset: %s s   continuity: %s" % (d.get("offset"), d.get("continuity"))]
                    if via == "cable":
                        extra.append("User bits: %s%s" % (d.get("user_bits") or "-",
                                                          " (unstable)" if d.get("user_bits") and d.get("user_bits_stable") is False else ""))
                    extra.append("Source in use now: %s" % d.get("effective"))
                if self.done(lab, s, d, extra):
                    self.offer_source("ltc")
                    if (self.st().get("f6") is not None
                            and self.confirm("Jam the Zoom F6 now (5 s of timecode)?")):
                        self.f6_jam_menu()
            elif k == "f6_jam":
                self.f6_jam_menu()
            elif k == "ltc_clear":
                if self.confirm("Clear the stored LTC reading?"):
                    s, d = self.call("Clear LTC", "POST", "/api/time/ltc/clear")
                    self.done("Clear LTC", s, d)
            elif k == "ntp_get":
                s, d = self.call("NTP Get", "POST", "/api/time/ntp/sync", timeout=45)
                extra = None
                if s == 200 and d.get("ok"):
                    extra = ["Clock now: %s" % d.get("now"), "Jump: %+.1f s" % (d.get("jump") or 0),
                             "Server: %s" % d.get("server")]
                if self.done("NTP Get", s, d, extra):
                    self.offer_source("ntp")
            elif k == "ntp_clear":
                if self.confirm("Forget NTP as a clock source? (the system clock is not changed)"):
                    s, d = self.call("Clear NTP", "POST", "/api/time/ntp/clear")
                    self.done("Clear NTP", s, d)

    def offer_source(self, k):
        """Buscar a hora nao escolhe a fonte: o bridge arranca SEMPRE sem nenhuma
        (decisao do operador), e so uma escolha explicita faz as camaras serem
        acertadas. Depois de um GET bem sucedido pergunta-se, em vez de deixar
        o estado a dizer 'unset' sem explicar porque."""
        if self.st().get("clock_source_pref"):
            return
        if self.confirm("Use %s as the clock source?\n\n"
                        "None is chosen: the cameras are not set from any clock." % CLOCK_LABELS[k]):
            s, d = self.call("Source %s" % CLOCK_LABELS[k], "POST", "/api/time/source", {"source": k})
            self.done("Source %s" % CLOCK_LABELS[k], s, d)

    # --- rede --------------------------------------------------------------
    def net_lines(self):
        st = self.st()
        L = []
        net = self.cache.get("net", 6, lambda: api("GET", "/api/net/status", timeout=15))
        if net is None:
            L.append(("Reading the Wi-Fi state ...", "dim"))
        else:
            s, d = net
            if s != 200:
                L.append(("net status: %s" % (d.get("error") or "HTTP %s" % s), "red"))
            else:
                L.append(("Uplink (internet)", "bold"))
                L.append("  network: %s   signal: %s dBm   channel: %s" % (
                    d.get("uplink_ssid") or "not connected", d.get("uplink_signal") or "-", d.get("uplink_chan") or "-"))
                L.append("  address: %s" % (d.get("uplink_ip") or "-"))
                if st.get("uplink_24ghz"):
                    L.append(("  2.4 GHz: shares the air with Bluetooth", "yellow"))
                L.append("")
                L.append(("EXT WI-FI  %s" % (d.get("ap_ssid") or ""), "bold"))
                L.append(("  %s   channel: %s" % ("UP" if d.get("ap_up") else "DOWN", d.get("ap_chan") or "-"),
                          "green" if d.get("ap_up") else "red"))
                L.append("  clients: %s   cameras: %s" % (d.get("ap_clients"), ", ".join(d.get("ap_cams") or []) or "-"))
                for o in d.get("ap_devices") or []:          # os outros, um por linha: nome, IP, MAC
                    L.append(("  %-24s %-15s %s" % (o["name"][:24], o.get("ip") or "-", o["mac"]),
                              "yellow" if o["name"] == "unknown" else ""))
        L.append("")
        ts = self.cache.get("ts", 10, lambda: api("GET", "/api/net/tailscale", timeout=15))
        L.append(("Tailscale", "bold"))
        if ts is None:
            L.append(("  reading ...", "dim"))
        else:
            s, d = ts
            if not d.get("ok"):
                L.append(("  %s" % (d.get("error") or "unavailable"), "red"))
            else:
                L.append(("  %s   %s   %s" % (d.get("state"), d.get("hostname"), ", ".join(d.get("ips") or [])),
                          "green" if d.get("online") else "red"))
                L.append("  peers online: %s of %s" % (d.get("peers_online"), d.get("peers_total")))
                for h in d.get("health") or []:
                    L.append(("  ! %s" % h, "yellow"))
        L.append("")
        L.append("Wi-Fi LED on the camera AP dongle: %s" % ("on" if st.get("led_on") else "off"))
        return L

    # --- WIRELESS (Devices): uma entrada por interface Wi-Fi do Atom ----------
    def wifi_net(self, fresco=False):
        """O /api/net/status (iw, nmcli) em cache; {} se o bridge nao o deu."""
        if fresco:
            self.cache.drop("net")
        net = self.cache.get("net", 6, lambda: api("GET", "/api/net/status", timeout=15))
        return net[1] if net and net[0] == 200 and isinstance(net[1], dict) else {}

    def wifi_entradas(self):
        """As entradas do separador WIRELESS: [(chave, texto)]. Uma por interface Wi-Fi (um AP novo aparece sozinho)."""
        d = self.wifi_net()
        ifaces = d.get("wifi_ifaces") or []
        ap_if = d.get("ap_iface") or "ps-control"
        if not any(i["iface"] == ap_if for i in ifaces):          # sem o iw (ou o AP em baixo): a do AP das camaras
            ifaces = [{"iface": ap_if, "type": "AP", "ssid": d.get("ap_ssid")}] + ifaces
        out = [("nm", "Restart NetworkManager")]
        for i in ifaces:
            nome = i.get("ssid") or (d.get("ap_ssid") if i["iface"] == ap_if else None) or i["iface"]
            papel = ("cameras' AP" if i["iface"] == ap_if else "access point" if i.get("type") == "AP"
                     else "internet" if i.get("type") == "managed" else (i.get("type") or "?"))
            out.append((("wifi", i["iface"]), "Settings %s  (%s)" % (nome, papel)))
        return out

    def wifi_settings(self, iface):
        """As definicoes de uma interface Wi-Fi. O AP das camaras: estado, aparelhos (ligados e memorizados), LED, reiniciar."""
        while True:
            d = self.wifi_net()
            ap_if = d.get("ap_iface") or "ps-control"
            info = next((i for i in d.get("wifi_ifaces") or [] if i["iface"] == iface), {"iface": iface})
            nome = info.get("ssid") or (d.get("ap_ssid") if iface == ap_if else None) or iface
            if iface == ap_if:
                agora, mem = d.get("ap_now") or [], d.get("ap_known") or []
                entradas = [("status", "Status"),
                            ("devs", "Devices  (%d connected, %d remembered)" % (len(agora), len(mem))),
                            ("led", "Wi-Fi LED  %s" % ("on" if self.st().get("led_on") else "off")),
                            ("restart", "Restart %s" % nome)]
            else:
                entradas = [("status", "Status")]
            i = self.menu("Settings %s" % nome, [e[1] for e in entradas], cor=COR["net"])
            if i is None:
                return
            k = entradas[i][0]
            if k == "status":
                self.wifi_net(fresco=True)
                self.cache.drop("ts")
                self.pager("Settings %s" % nome, lambda: self.wifi_status_lines(iface), cor=COR["net"])
            elif k == "devs":
                self.wifi_net(fresco=True)
                self.pager("%s: devices" % nome, lambda: self.wifi_devices_lines(), cor=COR["net"])
            elif k == "led":
                on = not bool(self.st().get("led_on"))
                s, r = self.call("Wi-Fi LED %s" % ("on" if on else "off"), "POST", "/api/system/led", {"on": on})
                self.done("Wi-Fi LED %s" % ("on" if on else "off"), s, r)
            elif k == "restart":
                if self.confirm("Restart %s? The Canon bodies and the gateways drop for a moment." % nome):
                    s, r = self.call("Restart %s" % nome, "POST", "/api/net/action", {"action": "ap_cameras"})
                    self.done("Restart %s" % nome, s, r)

    def wifi_status_lines(self, iface):
        d = self.wifi_net()
        st = self.st()
        ap_if = d.get("ap_iface") or "ps-control"
        info = next((i for i in d.get("wifi_ifaces") or [] if i["iface"] == iface), {"iface": iface})
        L = [("Interface %s   %s" % (iface, info.get("type") or "?"), "bold")]
        if iface == ap_if:
            L.append(("  %s   %s   channel %s" % (d.get("ap_ssid") or "-", "UP" if d.get("ap_up") else "DOWN",
                                                  d.get("ap_chan") or "-"), "green" if d.get("ap_up") else "red"))
            L.append("  connected now: %s   cameras: %s" % (d.get("ap_clients"), ", ".join(d.get("ap_cams") or []) or "-"))
            L.append("  Wi-Fi LED on the dongle: %s" % ("on" if st.get("led_on") else "off"))
            return L
        if info.get("type") == "managed":
            L.append("  network: %s   signal: %s dBm   channel: %s" % (
                d.get("uplink_ssid") or info.get("ssid") or "not connected", d.get("uplink_signal") or "-",
                d.get("uplink_chan") or info.get("channel") or "-"))
            L.append("  address: %s" % (d.get("uplink_ip") or "-"))
            if st.get("uplink_24ghz"):
                L.append(("  2.4 GHz: shares the air with Bluetooth", "yellow"))
            L.append("")
            ts = self.cache.get("ts", 10, lambda: api("GET", "/api/net/tailscale", timeout=15))
            L.append(("Tailscale", "bold"))
            if ts is None:
                L.append(("  reading ...", "dim"))
            else:
                _s, t = ts
                if not t.get("ok"):
                    L.append(("  %s" % (t.get("error") or "unavailable"), "red"))
                else:
                    L.append(("  %s   %s   %s" % (t.get("state"), t.get("hostname"), ", ".join(t.get("ips") or [])),
                              "green" if t.get("online") else "red"))
                    L.append("  peers online: %s of %s" % (t.get("peers_online"), t.get("peers_total")))
                    for h in t.get("health") or []:
                        L.append(("  ! %s" % h, "yellow"))
            return L
        L.append("  ssid %s   channel %s" % (info.get("ssid") or "-", info.get("channel") or "-"))
        return L

    def wifi_devices_lines(self):
        """Os aparelhos do AP das camaras: os ligados agora e os memorizados (reservas de DHCP e leases)."""
        d = self.wifi_net()
        agora, mem = d.get("ap_now") or [], d.get("ap_known") or []
        L = [("CONNECTED NOW  (%d)" % len(agora), "bold")]
        for o in agora:
            L.append(("  %-26s %-15s %s" % (o["name"][:26], o.get("ip") or "-", o["mac"]),
                      "green" if o.get("camera") else "yellow" if o["name"] == "unknown" else ""))
        if not agora:
            L.append(("  nothing", "dim"))
        L += ["", ("REMEMBERED, NOT CONNECTED  (%d)" % len(mem), "bold")]
        for o in mem:
            L.append(("  %-26s %-15s %s  %s" % (o["name"][:26], o.get("ip") or "-", o["mac"],
                                                 "reserved" if o.get("kind") == "reserved" else "seen before"), "dim"))
        if not mem:
            L.append(("  nothing", "dim"))
        return L

    def restart_nm(self):
        if self.confirm("Restart NetworkManager? All the Wi-Fi links (internet and the APs) drop for a few seconds."):
            s, d = self.call("Restart NetworkManager", "POST", "/api/net/action", {"action": "restart_nm"})
            self.done("Restart NetworkManager", s, d)

    # --- USB (Devices): tudo o que esta ligado por USB, menos o que e do proprio Atom ----------
    @staticmethod
    def usb_ligados():
        """Os aparelhos USB ligados (sysfs), sem hubs nem o que o Atom traz por dentro (porta "fixed"):
        [{name, vid, pid, kind, details: [...]}]."""
        base = "/sys/bus/usb/devices"

        def ler(p, n):
            try:
                with open(os.path.join(p, n)) as f:
                    return f.read().strip()
            except OSError:
                return ""
        out = []
        try:
            nomes = sorted(os.listdir(base))
        except OSError:
            return out
        for dev in nomes:
            p = os.path.join(base, dev)
            if ":" in dev or not ler(p, "idVendor"):
                continue
            if ler(p, "bDeviceClass") == "09" or ler(p, "removable") == "fixed":
                continue
            vid, pid = ler(p, "idVendor"), ler(p, "idProduct")
            drivers, extra, midi, audio = [], [], False, False
            for intf in sorted(os.listdir(base)):
                if not intf.startswith(dev + ":"):
                    continue
                ip_ = os.path.join(base, intf)
                if ler(ip_, "bInterfaceClass") == "01":         # audio: subclasse 3 e MIDI, 2 e som
                    midi, audio = midi or ler(ip_, "bInterfaceSubClass") == "03", audio or ler(ip_, "bInterfaceSubClass") == "02"
                drv = os.path.basename(os.path.realpath(os.path.join(ip_, "driver"))) if os.path.exists(os.path.join(ip_, "driver")) else ""
                if drv and drv not in drivers:
                    drivers.append(drv)
                for sub, rot in (("net", "network"), ("bluetooth", "bluetooth"), ("tty", "serial"), ("sound", "sound")):
                    try:
                        for x in os.listdir(os.path.join(ip_, sub)):
                            extra.append("%s %s" % (rot, x))
                    except OSError:
                        pass
            ds = " ".join(drivers)
            kind = ("Bluetooth" if "btusb" in ds else
                    "Wi-Fi" if any(x.startswith("network") for x in extra) and not "cdc" in ds else
                    "ESP32 gateway" if vid == "303a" else
                    "Zoom" if vid == "1686" else
                    "audio + MIDI" if "snd-usb-audio" in ds and midi and audio else
                    "MIDI" if "snd-usb-audio" in ds and midi else
                    "audio" if "snd-usb-audio" in ds else
                    "storage" if ("usb-storage" in ds or "uas" in ds) else
                    "serial" if any(x.startswith("serial") for x in extra) else
                    "input (HID)" if "usbhid" in ds else "other")
            nome = " ".join(x for x in (ler(p, "manufacturer"), ler(p, "product")) if x) or "%s:%s" % (vid, pid)
            out.append({"name": nome, "vid": vid, "pid": pid, "kind": kind, "port": dev, "speed": ler(p, "speed"),
                        "drivers": drivers, "extra": extra, "serial": bool(ler(p, "serial"))})
        return out

    def usb_all_menu(self):
        """Devices > USB devices: tudo o que esta ligado por USB (dongles Bluetooth e Wi-Fi, o Zoom, gateways, leitores...)
        e, a parte, os controladores class compliant (MIDI/HID) que o bridge usa (botoes, LEDs)."""
        while True:
            devs = self.usb_ligados()
            itens = [Sec("CONNECTED BY USB")]
            for x in devs:
                itens.append(("%-13s %-36.36s %s:%s" % (x["kind"], x["name"], x["vid"], x["pid"]), COR["cams"]))
            if not devs:
                itens.append(Sec("nothing plugged in (besides the Atom's own)"))
            ctl = self.usb_devices()
            if ctl is None:
                resumo = "offline"
            else:
                lig = sum(1 for c in ctl if c["present"])
                resumo = "%d plugged in%s" % (lig, ", %d saved" % (len(ctl) - lig) if len(ctl) > lig else "")
            itens += [Sec("USB CONTROLLERS"), ("Controllers and roles  (%s)" % resumo, COR["cams"])]
            i = self.menu("USB devices", itens, cor=COR["cams"])
            if i is None:
                return
            n = sum(1 for it in itens[:i] if not isinstance(it, Sec))
            if n >= len(devs):
                self.usb_menu()
                continue
            x = devs[n]
            self.pager(x["name"], lambda x=x: [
                (x["name"], "bold"), "",
                "Type:      %s" % x["kind"],
                "ID:        %s:%s" % (x["vid"], x["pid"]),
                "USB port:  %s   speed %s Mb/s" % (x["port"], x["speed"] or "?"),
                "Drivers:   %s" % (", ".join(x["drivers"]) or "none"),
                "Uses:      %s" % (", ".join(x["extra"]) or "-")], live=False, cor=COR["cams"])

    # --- Bluetooth (o do Atom) ----------------------------------
    def bt_lines(self):
        """O Bluetooth do Atom (adaptadores). A BMPCC ja nao passa por aqui: vai pelo C3."""
        st = self.st()
        txt, cor = bt_resumo(st)
        L = [(txt, cor), ""]
        g = next((x for x in gateways(st) if x["id"] == "c3"), None)
        L.append(("BMPCC via C3 (%s)" % (g["state"] if g else "?"), "dim"))
        L.append("")
        ad = self.cache.get("bt", 10, lambda: (200, {"out": self.bt_adapters()}))
        L.append(("Bluetooth adapters", "bold"))
        out = (ad[1].get("out") if ad else None)
        L += ["  " + x for x in (out or ["reading ..."])]
        return L

    @staticmethod
    def bt_adapters():
        try:
            r = subprocess.run(["bluetoothctl", "list"], capture_output=True, text=True, timeout=4)
            lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
            return lines or ["none found"]
        except Exception as e:
            return ["bluetoothctl not available (%s)" % e]

    def bt_menu(self):
        """O Bluetooth do Atom: desde 2026-10-12 a BMPCC esta no gateway C3 (Wi-Fi); o Bluetooth fica para o plano B
        (BMPCC directa pelo BlueZ). A BMPCC liga/desliga-se na linha C3 (GATEWAYS)."""
        while True:
            entradas = [("status", "Status"),
                        ("pair", "Pair a new device"), ("reset", "Reset Bluetooth")]
            i = self.menu("Bluetooth", [e[1] for e in entradas], cor=COR["cams"], ordem="bluetooth")
            if i is None:
                return
            k = entradas[i][0]
            if k == "status":
                self.cache.drop("bt")
                self.pager("Bluetooth", self.bt_lines, cor=COR["cams"])
            elif k == "pair":
                self.bt_pair()
            elif k == "reset":
                if self.confirm("Restart bluetoothd? Paired devices drop and reconnect by themselves."):
                    s, d = self.call("Reset Bluetooth", "POST", "/api/bluetooth/reset", timeout=90)
                    self.done("Reset Bluetooth", s, d)

    # --- logs e servidor ---------------------------------------------------
    def shoot_log_screen(self):
        """O shoot log inteiro (ultimas 300 linhas), com cursor, e editavel.
        Abre com a fonte pequena e repoe a anterior ao sair."""
        self.font_small()
        sel = top = 0
        rows, total = [], None
        self.cache.drop("shootlog")
        try:
            while True:
                r = self.cache.get("shootlog", 3, lambda: api("GET", "/api/log/shoot?limit=300", timeout=8))
                err = None
                if r is not None:
                    if r[0] == 200:
                        rows, total = r[1].get("rows") or [], r[1].get("total")
                    else:
                        err = r[1].get("error") or "HTTP %s" % r[0]
                h, w = self.scr.getmaxyx()
                view = max(1, h - 5)
                sel = max(0, min(sel, len(rows) - 1))
                if sel < top:
                    top = sel
                elif sel >= top + view:
                    top = sel - view + 1
                self.scr.erase()
                self.put(0, 1, "SHOOT LOG   %d of %s entries, newest first%s" % (
                    len(rows), total if total is not None else "?", "   (reading ...)" if r is None else ""),
                    self.attr("bold"))
                if err:
                    self.put(1, 1, "shoot log: %s" % err, self.attr("red"))
                self.put(2, 0, " %-14s %-10s %-8s %-8s %-4s %s" % ("TIME", "SOURCE", "ALIAS", "ACTION", "TAKE", "NOTE"),
                         self.attr("dim"))
                for n, x in enumerate(rows[top:top + view]):
                    i = top + n
                    style = curses.A_REVERSE if i == sel else 0
                    take = ("T%s" % x["take"]) if x.get("take") else ""
                    self.put(3 + n, 0, " " * (w - 1), style)
                    self.put(3 + n, 0, " %-14s %-10.10s %-8.8s %-8s %-4s %s" % (
                        (x.get("timestamp") or "")[5:], x.get("source", ""), x.get("alias", ""),
                        x.get("action", ""), take, x.get("note", "")), style)
                self.show_msg(h - 2)
                self.put(h - 1, 0, " up/down   Enter edit or delete   N add note   R refresh   Q back",
                         self.attr("cyan"))
                self.scr.refresh()
                k = self.scr.getch()
                if k in (ord("q"), ord("Q"), 27):
                    return
                if k in (curses.KEY_UP, ord("k")):
                    sel -= 1
                elif k in (curses.KEY_DOWN, ord("j")):
                    sel += 1
                elif k == curses.KEY_PPAGE:
                    sel -= view
                elif k == curses.KEY_NPAGE:
                    sel += view
                elif k == curses.KEY_HOME:
                    sel = 0
                elif k == curses.KEY_END:
                    sel = len(rows) - 1
                elif k in (ord("r"), ord("R")):
                    self.cache.drop("shootlog")
                elif k in (ord("n"), ord("N")):
                    t = self.prompt("Note (one line, goes on the take in progress)")
                    if t and t.strip():
                        s, d = self.call("Add note", "POST", "/api/log/shoot/note", {"note": t})
                        self.done("Add note", s, d)
                        self.cache.drop("shootlog")
                elif k in (10, 13, curses.KEY_ENTER, ord("e"), ord("E")) and rows:
                    self.shoot_edit(rows[sel])
        finally:
            self.font_restore()

    def shoot_apply(self, label, path, body):
        s, d = self.call(label, "POST", path, body)
        ok = self.done(label, s, d)
        if ok:
            self.cache.drop("shootlog")
        return ok

    def shoot_edit(self, row):
        """Uma linha do log: corrige o evento, o take, a nota, ou apaga-a. Passa
        pela API do bridge, que valida, escreve o CSV em seguranca e regista a
        alteracao no event log."""
        rid = row["id"]
        while True:
            take = ("  take %s" % row["take"]) if row.get("take") else ""
            i = self.menu("%s  %s  %s%s  %s" % ((row.get("timestamp") or "")[5:], row.get("source", ""),
                                               row.get("action", ""), take, row.get("note", "")), cor=COR["log"], items=[
                "Change action (now: %s)" % row.get("action"),
                "Change take number (now: %s; empty clears it)" % (row.get("take") or "none"),
                "Change note",
                "Delete this entry",
            ])
            if i is None:
                return
            if i == 0:
                j = self.menu("Action for this entry", list(SHOOT_EVENTS), cor=COR["log"])
                if j is not None and self.shoot_apply("Action %s" % SHOOT_EVENTS[j], "/api/log/shoot/edit",
                                                      {"id": rid, "action": SHOOT_EVENTS[j]}):
                    row["action"] = SHOOT_EVENTS[j]
            elif i == 1:
                v = self.prompt("Take number (a positive whole number; empty clears it)", row.get("take") or "")
                if v is not None and self.shoot_apply("Take", "/api/log/shoot/edit",
                                                      {"id": rid, "take": v.strip()}):
                    row["take"] = v.strip()
            elif i == 2:
                v = self.prompt("Note (one line, up to 200 characters)", row.get("note") or "")
                if v is not None and self.shoot_apply("Note", "/api/log/shoot/edit", {"id": rid, "note": v}):
                    row["note"] = " ".join(v.split())[:200]
            elif i == 3:
                if self.confirm("Delete this entry?\n\n%s  %s  %s\n\nTake numbers do not renumber by themselves." % (
                        (row.get("timestamp") or "")[5:], row.get("source", ""), row.get("action", ""))):
                    if self.shoot_apply("Delete", "/api/log/shoot/delete", {"id": rid}):
                        return

    def events_lines(self):
        r = self.cache.get("events", 3, lambda: api_text("/api/log/events.log"))
        if r is None:
            return [("reading ...", "dim")]
        s, text = r
        if s != 200:
            return [("event log: HTTP %s %s" % (s, text[:80]), "red")]
        lines = text.splitlines()
        return [("last %d of %d lines, newest first" % (min(500, len(lines)), len(lines)), "dim"), ""] \
            + lines[::-1][:500]

    def sftp_lines(self):
        s, d = api("GET", "/api/backup/sftp")
        if s != 200:
            return [("SFTP config: %s" % (d.get("error") or "HTTP %s" % s), "red")]
        if not d.get("host"):
            return ["No SFTP server configured."]
        return ["Server:   %s : %s" % (d.get("host"), d.get("port")), "User:     %s" % d.get("user"),
                "Folder:   %s" % d.get("path"),
                "Password: %s" % ("saved" if d.get("has_password") else "NONE"),
                ]

    def sftp_change(self):
        s, cur = api("GET", "/api/backup/sftp")
        cur = cur if s == 200 else {}
        host = self.prompt("SFTP server (host or address)", cur.get("host", ""))
        if not host:
            return
        port = self.prompt("Port", str(cur.get("port") or 22))
        if port is None:
            return
        user = self.prompt("User", cur.get("user", ""))
        if not user:
            return
        path = self.prompt("Remote folder (it has to exist)", cur.get("path") or ".")
        if path is None:
            return
        pw = self.prompt("Password (empty keeps the saved one%s)" % ("" if cur.get("has_password") else "; none saved"),
                         "", secret=True)
        if pw is None:
            return
        if not port.strip().isdigit():
            self.say("Port must be a number", 2)
            return
        if not self.confirm("Save %s@%s:%s (port %s)?" % (user, host, path or ".", port)):
            return
        s, d = self.call("Save SFTP server", "POST", "/api/backup/sftp",
                         {"host": host, "port": int(port), "user": user, "path": path, "password": pw})
        self.done("Save SFTP server", s, d)

    def logs_menu(self):
        while True:
            entradas = [
                Sec("LOG"),
                ("shoot", "Shoot log"),
                ("events", "Event log"),
                ("note", "Add note"),
                Sec(""),
                Sec("SFTP SERVER"),
                ("sftp_info", "Info"),
                ("sftp_edit", "Edit"),
                ("sftp_send", "Send"),
                Sec(""),
                Sec(""),
                Sec("NEW"),
                ("new_shoot", "New shoot log"),
                ("new_events", "New event log"),
            ]
            i = self.menu("Logs and server", [e if isinstance(e, Sec) else e[1] for e in entradas], cor=COR["log"], ordem="logs")
            if i is None:
                return
            k = entradas[i][0]
            if k == "shoot":
                self.shoot_log_screen()
            elif k == "events":
                self.cache.drop("events")
                self.pager("Event log", self.events_lines, cor=COR["log"])
            elif k == "note":
                t = self.prompt("Note (one line, goes on the take in progress)")
                if t and t.strip():
                    s, d = self.call("Add note", "POST", "/api/log/shoot/note", {"note": t})
                    self.done("Add note", s, d)
            elif k == "sftp_info":
                self.pager("SFTP server", self.sftp_lines, live=False, cor=COR["log"])
            elif k == "sftp_edit":
                self.sftp_change()
            elif k == "sftp_send":
                self.sftp_send()
            elif k == "new_shoot":
                if self.confirm("Archive the shoot log and start an empty one? Take numbers restart at 1.") \
                        and self.confirm("Really? This cannot be undone from here."):
                    s, d = self.call("New shoot log", "POST", "/api/log/shoot/new")
                    self.done("New shoot log", s, d, ["Archived as: %s" % d.get("archived")] if d.get("ok") else None)
            elif k == "new_events":
                if self.confirm("Archive the event log and start an empty one?"):
                    s, d = self.call("New event log", "POST", "/api/log/events/new")
                    self.done("New event log", s, d, ["Archived as: %s" % d.get("archived")] if d.get("ok") else None)

    def sftp_send(self):
        """Envia os registos e fica a mostrar a barra e o resultado (antes era preciso ir ver ao event log)."""
        s, d = self.call("Send logs", "POST", "/api/backup/send")
        if s == 409:
            self.say("A send is already running: showing it", 1)
        elif s != 200 or not d.get("ok"):
            self.done("Send logs", s, d)
            return
        self.pager("Send logs to the SFTP server", self.sftp_progress_lines, cor=COR["log"], every=0.5)

    def sftp_progress_lines(self):
        s, d = api("GET", "/api/backup/send", timeout=5)
        if s == 404:
            return [("No progress info (update the bridge); see the event log.", "yellow")]
        if s != 200:
            return [("No answer from the bridge ...", "yellow")]
        tb, db = d.get("total_bytes") or 0, d.get("done_bytes") or 0
        tf, df = d.get("total_files") or 0, d.get("done_files") or 0
        frac = (db / tb) if tb else (1.0 if not d.get("running") else 0.0)
        larg = 40
        cheio = int(round(frac * larg))
        barra = "[%s%s] %3d%%" % ("#" * cheio, "." * (larg - cheio), int(frac * 100))
        L = [(barra, "bold"),
             "files %d / %d     %s / %s" % (df, tf, _tamanho(db), _tamanho(tb)), ""]
        if d.get("running"):
            L.append(("Sending %s ..." % (d.get("file") or "(connecting)"), "yellow"))
        elif d.get("ok"):
            salt = d.get("skipped") or 0
            L.append(("DONE: %d file(s) sent%s" % (d.get("sent") or 0,
                                                  (", %d already on the server" % salt) if salt else ""), "green"))
        elif d.get("error"):
            L.append(("FAILED: %s" % d["error"], "red"))
        else:
            L.append(("Nothing sent yet.", "dim"))
        return L

    # --- sistema -----------------------------------------------------------
    def preflight_lines(self):
        st = self.st()
        r = self.cache.get("pre", 5, lambda: api("GET", "/api/preflight", timeout=15))
        L = [("System: RAM used %s (%s MB free), swap %s MB, CPU load %s, temperature %s C" % (
            ram_txt(st), _ou(st.get("mem_available_mb")), _ou(st.get("swap_used_mb")), carga_txt(st), _ou(st.get("soc_temp_c"))), "dim"), ""]
        if r is None:
            return L + [("running the checks ...", "dim")]
        s, d = r
        if s != 200:
            return L + [("preflight: %s" % (d.get("error") or "HTTP %s" % s), "red")]
        # O veredicto e o do bridge (o mesmo da pagina).
        checks = d.get("checks", [])
        ready = bool(d.get("ok"))
        L.append(("READY TO SHOOT" if ready else "NOT READY", "green" if ready else "red"))
        L.append("")
        for c in checks:
            tag, color = (("[ ok ]", "green") if c["ok"] else ("[FAIL]", "red")) if c["ok"] is not None \
                else ("[info]", "dim")
            L.append((" %s %s%s" % (tag, c["label"], (" - " + c["detail"]) if c.get("detail") else ""), color))
        return L

    def devices_info_lines(self):
        """O que as linhas dos devices ja nao mostram no dashboard: modo, bateria, tempo e definicoes."""
        st, dv = self.snap.get()[:2]
        L = []
        for r in build_rows(st, dv):
            L.append((r["label"], "bold"))
            L.append("  link: %s    battery: %s    left: %s    mode: %s" % (
                "online" if r["conn"] else "offline",
                ("%d%%" % r["batt"]) if isinstance(r["batt"], (int, float)) and r["batt"] >= 0 else "-",
                r["left"] or "-", r["mode"] or "-"))
            if r.get("alias"):
                L.append("  alias: %s" % r["alias"])
            L.append("  settings: %s" % (r["cfg"] or ("(no readings yet)" if r["conn"] else "-")))
            L.append("")
        return L or [("No devices.", "dim")]

    def versao_lines(self):
        v = self.st().get("code_version")
        eu = os.path.abspath(__file__)
        try:
            data = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.stat(eu).st_mtime))
        except OSError:
            data = "?"
        L = []
        if isinstance(v, dict) and v.get("commit"):
            L += [("Installed: %s" % v["commit"], "bold"),
                  "  %s" % (v.get("subject") or ""),
                  "  commit date %s   installed %s" % ((v.get("date") or "?")[:16].replace("T", " "), v.get("installed") or "?")]
        else:
            L += [("Installed: unknown", "yellow"),
                  "  (recorded from the next Update from GitHub)"]
        L += ["", "Console file: %s  (%s)" % (eu, data),
              "Menu orders: %s" % (_menus_ler_tudo()[1] or "none saved yet"), "",
              ]
        return L

    def sys_menu(self):
        while True:
            entradas = [
                Sec("CHECK"),
                ("pre", "Preflight"),
                ("version", "Version"),
                ("devinfo", "Devices info"),
                ("fault", "Clear fault%s" % ("  (FAULT)" if self.st().get("fault") else "")),
                Sec("UPDATE"),
                ("update", "Update from GitHub"),
                ("gwfw", "Update gateways"),
                Sec("RIG"),
                ("profiles", "Rig profiles"),
                ("slate", "Scene / reel of the next clips"),
                ("backup", "Backup / restore (USB stick)"),
                Sec("SERVICES"),
            ]
            # Ligado a verde, desligado a vermelho (o bridge sem resposta: apagado); o nome sem o estado no fim.
            sv = self.servicos()
            cor_sv = lambda k: "dim" if sv is None else ("green" if sv.get(k) else "red")
            entradas += [
                ("svc_web", "HTTP SERVER", cor_sv("web")),
                ("svc_usb", "USB CONTROLLERS", cor_sv("usb")),
                Sec("SECURITY"),
                ("pin", "Web PIN"),
                Sec("TRAINING"),
                ("demo", "Demo mode", "green" if self.st().get("demo") else "red"),
            ]
            i = self.menu("GLOBAL SETTINGS", [e if isinstance(e, Sec) else ((e[1], e[2]) if len(e) > 2 else e[1]) for e in entradas],
                          cor=COR["sys"], ordem="system")
            if i is None:
                return
            k = entradas[i][0]
            if k == "pre":
                self.cache.drop("pre")
                self.pager("Preflight", self.preflight_lines, cor=COR["sys"])
            elif k == "version":
                self.pager("Version", self.versao_lines, live=False, cor=COR["sys"])
            elif k == "devinfo":
                self.pager("Devices info", self.devices_info_lines, cor=COR["sys"])
            elif k == "fault":
                s, d = self.call("Clear fault", "POST", "/api/fault/clear")
                self.done("Clear fault", s, d)
            elif k == "update":
                self.github_update()
            elif k == "gwfw":
                self.gateways_update()
            elif k == "pin":
                self.web_pin()
            elif k == "slate":
                self.slate_menu()
            elif k == "profiles":
                self.profiles_menu()
            elif k == "backup":
                self.backup_menu()
            elif k == "demo":
                self.demo_toggle()
            elif k.startswith("svc_"):
                self.servico_toggle(k[4:], sv)

    def servicos(self):
        """{web, usb}: ligados ou nao (None se o bridge nao responde ou ainda nao tem a rota)."""
        s, d = api("GET", "/api/services", timeout=10)
        return d.get("services") if s == 200 and isinstance(d.get("services"), dict) else None

    def servico_toggle(self, k, sv):
        if sv is None:
            self.done("Services", 404, {"error": "the bridge does not answer, or update it from GitHub first"})
            return
        on = bool(sv.get(k))
        aviso = {
            "web": "Turn OFF the HTTP SERVER?\n\niPads, phones and laptops lose the page and its API. "
                   "It can only be turned back on here, in the console.",
            "usb": "Turn OFF the USB CONTROLLERS?\n\nMIDI controllers, pedals and keypads stop working. "
                   "It stays off after a restart.",
        }[k]
        if on and not self.confirm(aviso):
            return
        nomes = {"web": "HTTP SERVER", "usb": "USB CONTROLLERS"}
        s, d = self.call(nomes[k], "POST", "/api/services", {k: not on}, timeout=40)
        self.done("%s %s" % (nomes[k], "OFF" if on else "ON"), s, d)

    def demo_toggle(self):
        """Modo de treino: o bridge simula um S3 com 2 GoPro e um C3 com uma BMPCC (DEMO ...) e junta-os ao rig."""
        on = bool(self.st().get("demo"))
        if not on and not self.confirm("Start the DEMO mode?\n\nThe bridge adds simulated cameras (DEMO-1, DEMO-2, DEMO-BM) "
                                       "and their takes go to the shoot log."):
            return
        s, d = self.call("Demo mode", "POST", "/api/demo", {"on": not on}, timeout=20)
        if s == 404:
            d = {"error": "update the bridge from GitHub first"}
        self.done("Demo mode %s" % ("OFF" if on else "ON"), s, d,
                  None if on or s != 200 else ["DEMO-1, DEMO-2 (GoPro) and DEMO-BM (BMPCC) are in the device list."])
        self.snap.poll_once()

    def backup_menu(self):
        """Copia da configuracao do rig numa pen USB ligada ao Atom, e repor a partir dela (sem segredos: num Atom novo os
        gateways configuram-se outra vez pela USB, e o PIN da pagina e a password do SFTP voltam a escrever-se)."""
        while True:
            i = self.menu("BACKUP / RESTORE  (USB stick in the Atom)",
                          [("Save the configuration to the USB stick", COR["sys"]),
                           ("Restore from the USB stick ...", COR["rec"])], cor=COR["sys"])
            if i is None:
                return
            if i == 0:
                s, d = self.call("Backup", "POST", "/api/backup/usb", timeout=60)
                self.done("Backup to the USB stick", s, d, ["File on the stick: %s" % d.get("file"),
                                                  "No secrets in it: no gateway token, Wi-Fi passwords, web PIN or "
                                                  "SFTP password."] if s == 200 and isinstance(d, dict) and d.get("ok") else None)
                continue
            s, d = self.call("Backups", "GET", "/api/backup/usb", timeout=60)
            if s != 200 or not isinstance(d, dict) or not d.get("ok"):
                self.done("Backups", s, d if isinstance(d, dict) else {"error": "bridge offline"})
                continue
            fs = d.get("files") or []
            if not fs:
                self.say("No bridge-config-*.json on the USB stick", 2)
                continue
            j = self.menu("Restore which backup?", [("%s  (%d KB)" % (f["name"], max(1, f["size"] // 1024)), COR["rec"])
                                                    for f in fs], cor=COR["sys"])
            if j is None:
                continue
            if self.a_gravar():
                self.say("Recording: restore after the take", 2)
                continue
            if not self.confirm("Restore %s?\n\nThe cameras, iPhones, gateways, profiles and settings in it replace the "
                                "current ones, and the bridge restarts." % fs[j]["name"]):
                continue
            s, r = self.call("Restore", "POST", "/api/backup/restore", {"usb_file": fs[j]["name"]}, timeout=60)
            self.done("Restore from the USB stick", s, r, ["Restored: %s" % ", ".join(r.get("restored") or []),
                                                       "The bridge is restarting."]
                      if s == 200 and isinstance(r, dict) and r.get("ok") else None)

    def profiles_menu(self):
        """Perfis do rig: guardar o estado actual com um nome, aplicar (nunca a gravar) e apagar."""
        while True:
            s, d = self.call("Profiles", "GET", "/api/profiles", timeout=6)
            if s == 404:
                self.done("Profiles", s, {"error": "update the bridge from GitHub first"})
                return
            if s != 200 or not isinstance(d, dict):
                self.done("Profiles", s, d if isinstance(d, dict) else {"error": "bridge offline"})
                return
            nomes = sorted(d.get("profiles") or {})
            itens = [("Save the current rig as a profile ...", COR["sys"])] + [
                ("%s   (saved %s)" % (n, (d["profiles"][n] or {}).get("saved") or "?"), COR["cams"]) for n in nomes]
            i = self.menu("RIG PROFILES", itens, cor=COR["sys"])
            if i is None:
                return
            if i == 0:
                nome = self.prompt("Profile name (e.g. Interview 2 cameras):")
                if nome:
                    s2, r2 = self.call("Profile", "POST", "/api/profiles", {"save": nome})
                    self.done("Profile saved", s2, r2)
                continue
            nome = nomes[i - 1]
            p = d["profiles"][nome] or {}
            j = self.menu(nome, [("Apply", COR["cams"]), ("What it has", COR["sys"]), ("Delete", COR["rec"])],
                          cor=COR["sys"])
            if j == 0:
                if self.a_gravar():
                    self.say("Recording: change the profile after the take", 2)
                    continue
                s2, r2 = self.call("Profile", "POST", "/api/profiles", {"apply": nome})
                notas = (r2 or {}).get("notes") if isinstance(r2, dict) else None
                self.done("Profile %s applied" % nome, s2, r2, ["Not applied: " + n for n in notas] if notas else None)
            elif j == 1:
                sl = p.get("slate") or {}
                self.pager(nome, lambda: [
                    ("Armed for REC: %s" % (", ".join(p.get("rec_targets") or []) or "none"), "bold"),
                    "Strict REC: %s" % ("on" if p.get("strict_rec") else "off"),
                    "Slate: scene %s  reel %s" % (sl.get("scene") or "-", sl.get("reel") or "-"),
                    "C3 gateway: %s" % ("on" if p.get("c3_enabled", True) else "off"),
                    "S3 gateways: %s" % (", ".join("S3 %s %s" % (k, "on" if v else "off")
                                                   for k, v in sorted((p.get("s3_enabled") or {}).items())) or "-"),
                    "", ("Saved %s" % (p.get("saved") or "?"), "dim")], live=False, cor=COR["sys"])
            elif j == 2 and self.confirm("Delete the profile %s?" % nome):
                s2, r2 = self.call("Profile", "POST", "/api/profiles", {"delete": nome})
                self.done("Profile deleted", s2, r2)

    def slate_menu(self):
        """Cena e reel que o bridge envia, com o take seguinte, a BMPCC (principal e extra) e aos iPhones entre takes."""
        while True:
            s, d = self.call("Slate", "GET", "/api/slate", timeout=6)
            if s == 404:
                self.done("Slate", s, {"error": "update the bridge from GitHub first"})
                return
            if s != 200 or not isinstance(d, dict):
                self.done("Slate", s, d if isinstance(d, dict) else {"error": "bridge offline"})
                return
            sl_, lk = api("GET", "/api/slate/take/lock", timeout=4)
            bloqueio = not (sl_ == 200 and isinstance(lk, dict) and lk.get("enabled") is False)
            i = self.menu("Next clip: scene %s  reel %s  take %s" % (d.get("scene") or "-", d.get("reel") or "-",
                                                                    d.get("next_take") or "(take open)"),
                          [("Scene  %s" % (d.get("scene") or "-"), COR["sys"]), ("Reel  %s" % (d.get("reel") or "-"), COR["sys"]),
                           ("Who has it", COR["sys"]),
                           ("Take data lock  %s" % ("ON" if bloqueio else "off"), COR["rec"])], cor=COR["sys"])
            if i is None:
                return
            if i == 3:
                # Ligado, com a pagina de dados da take por fechar o bridge recusa um REC novo (em todos os clientes).
                s3, r3 = self.call("Take data lock", "POST", "/api/slate/take/lock", {"enabled": not bloqueio})
                self.done("Take data lock %s" % ("OFF" if bloqueio else "ON"), s3, r3)
                continue
            if i == 2:
                quem = sorted(d.get("sent") or {})
                self.pager("Slate sent", lambda: [("Cameras that already have the next clip slate:", "bold")]
                           + (["  %s" % k for k in quem] or ["  none yet (they get it between takes, within 5 s)"]),
                           live=False, cor=COR["sys"])
                continue
            campo = ("scene", "reel")[i]
            v = self.prompt("%s (empty: none):" % campo.capitalize(), str(d.get(campo) or ""))
            if v is None:
                continue
            corpo = {"scene": d.get("scene"), "reel": d.get("reel")}
            corpo[campo] = v.strip()
            s2, r2 = self.call("Slate", "POST", "/api/slate", corpo)
            self.done("Slate", s2, r2)

    def web_pin(self):
        """O PIN da pagina web para quem nao esta no Tailscale nem no Atom (iPads e telemoveis na rede das camaras,
        Wi-Fi do local). O PIN escreve-se em asteriscos e nunca volta do bridge."""
        s, d = self.call("Web PIN", "GET", "/api/security/pin", timeout=6)
        if s == 404:
            self.done("Web PIN", s, {"error": "update the bridge from GitHub first"})
            return
        if s != 200 or not isinstance(d, dict):
            self.done("Web PIN", s, d if isinstance(d, dict) else {"error": "bridge offline"})
            return
        tem = bool(d.get("pin_set"))
        i = self.menu("Web PIN  (%s)" % ("SET" if tem else "not set: devices outside Tailscale cannot open the page"),
                      [("Change the PIN" if tem else "Set a PIN", COR["sys"])] + ([("Remove the PIN", COR["rec"])] if tem else []),
                      cor=COR["sys"])
        if i is None:
            return
        if i == 1:
            if self.confirm("Remove the web PIN? Devices outside Tailscale can no longer open the page at all."):
                s, r = self.call("Web PIN", "POST", "/api/security/pin", {"pin": ""})
                self.done("Web PIN removed", s, r)
            return
        p1 = self.prompt("New PIN (4 to 12 digits):", secret=True)
        if not p1:
            return
        if not (p1.isdigit() and 4 <= len(p1) <= 12):
            self.say("The PIN must be 4 to 12 digits", 2)
            return
        p2 = self.prompt("The same PIN again:", secret=True)
        if p2 != p1:
            self.say("The two PINs differ: nothing changed", 2)
            return
        s, r = self.call("Web PIN", "POST", "/api/security/pin", {"pin": p1})
        self.done("Web PIN set", s, r, ["Open sessions were closed: phones and iPads enter the PIN again."]
                  if s == 200 and isinstance(r, dict) and r.get("ok") else None)

    def vigiar_take(self):
        """Abre a pagina de dados da take quando o bridge a pede (a ultima camara parou: /api/status take_pending), so no ecra
        principal e nunca com o ecra bloqueado. O pedido e do bridge e vale para todos os clientes (esta consola e a pagina web): qualquer um a guarda ou salta e fecha-a nos outros; com ela por fechar o bridge recusa um REC novo."""
        st, _dv, _sh, ok, _err, _at = self.snap.get()
        tp = st.get("take_pending") if ok else None
        if not tp:
            self._take_aberta_para = None
            return
        if getattr(self, "_take_aberta_para", None) == tp.get("since"):
            return                                  # ja se abriu para este pedido (e fechou: guardada, saltada ou erro)
        if os.path.exists(LOCK_FILE) or self.bloqueio.pedido:
            return                                  # ecra bloqueado: abre-se quando se desbloquear
        self._take_aberta_para = tp.get("since")
        self.take_page(pedida=True)

    def take_guardar(self, orig, val):
        """Envia ao bridge o que mudou (good e note so se escritos). (True, None) | (False, erro)."""
        corpo = {}
        for campo, _rot, _max, _aj in TAKE_CAMPOS:
            if campo in ("good", "note"):
                if val[campo]:
                    corpo[campo] = val[campo]
            elif val[campo] != orig[campo]:
                corpo[campo] = val[campo]
        s, d = api("POST", "/api/slate/take", corpo, timeout=8)         # mesmo vazio: guardar fecha a pagina em todos os clientes
        if s == 200 and isinstance(d, dict) and d.get("ok"):
            return True, None
        return False, (d.get("error") if isinstance(d, dict) else None) or "HTTP %s" % s

    def take_page(self, pedida=False):
        """Os dados da take, no fim da gravacao: nome da sessao, cena, reel, descricao do plano seguinte, bom ou mau e uma nota sobre a
        take que acabou. Tudo opcional: Enter passa ao campo seguinte (no ultimo, guarda), Esc salta sem guardar nada. Os valores da
        take anterior vem preenchidos. `pedida`: foi o bridge que a pediu (guardar ou Esc fecham-na em todos os clientes, e se um
        deles a fechar primeiro esta fecha-se sozinha); aberta com P fora de um pedido, so edita."""
        s, d = api("GET", "/api/slate/take", timeout=4)
        if s != 200 or not isinstance(d, dict) or not d.get("ok"):
            self.say("Take data: the bridge did not answer", 2)
            return
        orig = {c: str(d.get(c) or "") for c, *_ in TAKE_CAMPOS}
        orig["good"] = orig["note"] = ""
        val = dict(orig)
        sel = 0
        erro = ""
        cursor(1)
        self.scr.timeout(250)
        try:
            while True:
                if pedida and not (self.snap.get()[0].get("take_pending")):
                    self.say("Take data: filled in or skipped on another device", 1)     # a pagina web... ou comecou outra take
                    return
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                self.title_bar("TAKE %s DONE   data for take %s" % (d.get("last_take") or "-", d.get("next_take") or "-"), COR["log"])
                for i, (campo, rot, tamanho, ajuda) in enumerate(TAKE_CAMPOS):
                    y = 2 + i * 2
                    marca = i == sel
                    txt = val[campo] if campo != "good" else {"": "-", "good": "GOOD", "no-good": "NO-GOOD"}[val[campo]]
                    self.put(y, 1, "%-24s" % rot, self.attr("bold") if marca else self.attr("dim"))
                    self.put(y, 26, txt.ljust(max(1, min(tamanho or 10, w - 30))), curses.A_REVERSE if marca else 0)
                    if marca:
                        self.put(y + 1, 26, ajuda[:max(0, w - 28)], self.attr("dim"))
                if erro:
                    self.put(h - 3, 1, erro[:w - 3], self.attr("red") | curses.A_BOLD)
                self.put(h - 1, 0, " Enter next (last: save)   Up/Down field   Space good/no-good   Ctrl+U clear   Esc skip",
                         self.attr(COR["log"]))
                campo, _rot, tamanho, _aj = TAKE_CAMPOS[sel]
                if campo != "good":
                    self.scr.move(2 + sel * 2, min(w - 2, 26 + len(val[campo])))
                self.scr.refresh()
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                erro = ""
                if k in ("\x1b",):
                    if pedida:                      # saltar: fecha a pagina em todos os clientes e desbloqueia o REC
                        s2, d2 = api("POST", "/api/slate/take/skip", timeout=4)
                        if s2 != 200:
                            self.say("Take data: could not skip (HTTP %s)" % s2, 2)
                    return
                if k in ("\n", "\r", curses.KEY_ENTER):
                    if sel < len(TAKE_CAMPOS) - 1:
                        sel += 1
                        continue
                    ok, err = self.take_guardar(orig, val)
                    if ok:
                        self.say("Take data saved", 0)
                        return
                    erro = err
                elif k == curses.KEY_UP:
                    sel = max(0, sel - 1)
                elif k == curses.KEY_DOWN:
                    sel = min(len(TAKE_CAMPOS) - 1, sel + 1)
                elif campo == "good":
                    if k in (" ", curses.KEY_LEFT, curses.KEY_RIGHT):
                        ciclo = ("", "good", "no-good")
                        val["good"] = ciclo[(ciclo.index(val["good"]) + 1) % 3]
                elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    val[campo] = val[campo][:-1]
                elif k == "\x15":
                    val[campo] = ""
                elif isinstance(k, str) and k.isprintable() and len(val[campo]) < tamanho:
                    if campo == "reel" and not k.isdigit():
                        continue
                    if campo == "scene" and not (k.isascii() and k.isalnum()):
                        continue
                    val[campo] += k
        finally:
            cursor(0)
            self.scr.timeout(40)

    def a_gravar(self):
        """Alguma camara (ou o F6) esta a gravar? Para o POWER recusar logo (o bridge tambem recusa)."""
        st = self.st()
        return any(r.get("rec") for r in self.rows) or bool((st.get("f6") or {}).get("frc", {}).get("recording"))

    def power_menu(self):
        """POWER: reiniciar o bridge, reiniciar ou desligar o Atom. So aqui (sem tecla de atalho no dashboard) e nunca
        a gravar."""
        while True:
            entradas = [("restart", "Restart bridge"), ("reboot", "Reboot the Atom"),
                        ("shutdown", "Shut down the Atom")]
            i = self.menu("POWER", [e if isinstance(e, Sec) else (e[1], COR["rec"]) for e in entradas], cor=COR["rec"])
            if i is None:
                return
            k = entradas[i][0]
            if self.a_gravar():
                self.say("Recording: not now (stop the take first)", 2)
                continue
            if k == "restart":
                if self.confirm("Restart camera-bridge? bridge-ctl reconnects by itself."):
                    s, d = self.call("Restart bridge", "POST", "/api/net/action", {"action": "restart_bridge"})
                    if s == 200 and isinstance(d, dict) and d.get("ok") is not False and not d.get("error"):
                        self.ecra_energia("restart_bridge")
                    else:
                        self.done("Restart bridge", s, d)
            elif self.confirm("Really %s the Atom?" % ("reboot" if k == "reboot" else "shut down")):
                s, d = self.call(k, "POST", "/api/system/" + k)
                if s == 200 and isinstance(d, dict) and d.get("ok") is not False and not d.get("error"):
                    self.ecra_energia(k)      # a consola morre com o Atom: so se sai daqui se ele nao desligar
                else:
                    self.done(k, s, d)        # recusado (a gravar...) ou sem resposta: a pagina do erro

    def ecra_energia(self, quem, relogio=time.time):
        """Depois de aceite o pedido de reiniciar ou desligar: um ecra so com o que esta a acontecer, em vez de voltar ao menu
        com uma linha verde. `quem`: reboot | shutdown | restart_bridge. Para o Atom, fica ate ele desligar (a consola morre com
        ele); as teclas nao fazem nada, e so ao fim de 3 min, se ele nao desligou, o Esc volta ao menu. Para o bridge, fica
        ate ele voltar a responder (a consola reconecta-se)."""
        textos = {
            "reboot": ("RESTARTING THE ATOM", ["The Atom is restarting. Do not press anything.",
                                               "The console comes back by itself in about a minute."], "red"),
            "shutdown": ("SHUTTING DOWN THE ATOM", ["The Atom is shutting down. Do not press anything.",
                                                    "Wait until the screen and the lights of the Atom are off",
                                                    "before you unplug or move it."], "red"),
            "restart_bridge": ("RESTARTING THE BRIDGE", ["The bridge is restarting. The Atom stays on.",
                                                         "The console reconnects by itself in about 15 seconds."], "yellow")}
        titulo, texto, cor = textos[quem]
        anterior = getattr(self.scr, "_ms", 500)
        self.scr.timeout(250)
        t0, caiu = relogio(), False
        try:
            while True:
                dt = relogio() - t0
                if quem == "restart_bridge":
                    # So se sai quando o bridge cai e volta (ou, se nunca cair em 25 s, e porque reiniciou depressa).
                    if int(dt * 4) % 4 == 0:
                        st_, _ = api("GET", "/api/status", timeout=2)
                        if st_ == 0:
                            caiu = True
                        elif caiu or dt > 25:
                            self.say("The bridge is back", 0)
                            return
                    if dt > 120:
                        self.say("The bridge is still offline after 2 min", 2)
                        return
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                extra = []
                if quem != "restart_bridge" and dt > 60:
                    extra = ["It is taking longer than usual.", "The command was sent: keep waiting."]
                if quem != "restart_bridge" and dt > 180:
                    extra = ["The Atom did not shut down in 3 minutes.", "Esc: back to the menu (nothing was lost)."]
                roda = "|/-\\"[int(dt * 4) % 4]
                linhas = ([(titulo, self.hl(cor) | curses.A_BOLD), ("", 0), (texto[0], curses.A_BOLD)]
                          + [(x, 0) for x in texto[1:]] + [("", 0), ("%s  %d s" % (roda, int(dt)), self.attr("dim")), ("", 0)]
                          + [(x, self.attr("yellow")) for x in extra])
                y0 = max(0, (h - len(linhas)) // 2)
                for n, (txt, attr) in enumerate(linhas):
                    txt = txt[:max(0, w - 2)]
                    if n == 0:
                        txt = (" %s " % txt)
                    self.put(y0 + n, max(0, (w - len(txt)) // 2), txt, attr)
                self.scr.refresh()
                k = self.scr.getch()
                if quem != "restart_bridge" and dt > 180 and k == 27:
                    return
        finally:
            self.scr.timeout(anterior)

    def gateways_update(self):
        """GLOBAL SETTINGS > Update gateways: o firmware de todos os gateways (C3 e S3) e sempre enviado pelo Atom. Um so ecra: o
        estado de cada gateway em cima (nao se escolhe) e as accoes em baixo. A primeira accao nunca e o interruptor
        da actualizacao automatica (um Enter a mais nao o desliga), e esse pede confirmacao."""
        self.verificar_versao()
        while True:
            s, d = self.call("Checking gateways", "GET", "/api/gateways/firmware", timeout=40)
            if s == 404:
                self.done("Update gateways", s, {"error": "update the bridge from GitHub first"})
                return
            if s != 200 or not isinstance(d, dict):
                self.done("Update gateways", s, d if isinstance(d, dict) else {"error": "bridge offline"})
                return
            gws = d.get("gateways") or []
            atrasados = [g for g in gws if g["state"] == "outdated"]
            nunca = [g for g in gws if g["state"] == "unknown" and g["kind"] == "s3" and g.get("target")]
            sem_ota = [g for g in gws if g["state"] == "needs_usb"]
            itens, accoes = [Sec("GATEWAYS")], []
            for g in gws:
                itens.append(Sec("%-14s %-15s %-10s %s" % (g["name"][:14], g["host"], g["state"], (g.get("detail") or "")[:30])))
            if not gws:
                itens.append(Sec("no gateways in the bridge"))
            ult = d.get("last") or {}
            if ult.get("em"):
                itens.append(Sec("last automatic update %s: %s" % (ult["em"], "; ".join(ult.get("linhas") or [])[:60])))
            itens.append(Sec("ACTIONS"))

            def junta(txt, cor, fn):
                itens.append((txt, cor))
                accoes.append(fn)
            if atrasados:
                junta("Update %d gateway(s) now" % len(atrasados), COR["rec"], lambda: self._gw_fw_post(
                    "Update gateways", {}, "Update %d gateway(s) now? Each restarts (about 15 s)." % len(atrasados)))
            for g in nunca:
                junta("Write the Atom's firmware to %s  (never written by the Atom)" % g["name"], COR["rec"],
                      lambda g=g: self._gw_fw_post("Update %s" % g["name"], {"ids": [g["id"]]},
                                                   "Write the firmware to %s now? It restarts (about 15 s)." % g["name"]))
            for g in sem_ota:
                junta("Write %s by USB  (no OTA yet: cable on the Atom)" % g["name"], COR["rec"], self.c3_usb_flash)
            junta("Check again" if (atrasados or nunca or sem_ota) else "Check again  (nothing to update)", COR["sys"],
                  lambda: None)
            auto = bool(d.get("auto"))
            junta("Automatic update: %s  (turn %s)" % ("ON" if auto else "OFF", "off" if auto else "on"), COR["cams"],
                  lambda: self._gw_fw_auto(auto))
            i = self.menu("Gateways firmware", itens, cor=COR["sys"])
            if i is None:
                return
            n = sum(1 for it in itens[:i] if not isinstance(it, Sec))
            accoes[n]()

    def _gw_fw_post(self, nome, corpo, pergunta):
        if not self.confirm(pergunta):
            return
        s, r = self.call(nome, "POST", "/api/gateways/firmware", corpo, timeout=300)
        self.done(nome, s, r, ["%s: %s" % (x["id"], x["msg"]) for x in (r.get("results") or [])] if isinstance(r, dict) else None)

    def _gw_fw_auto(self, auto):
        if auto and not self.confirm("Turn the automatic gateway update OFF?\n\nThe gateways then only update when you ask."):
            return
        s, r = self.call("Automatic update", "POST", "/api/gateways/firmware", {"auto": not auto})
        self.done("Automatic update", s, r)

    def github_update(self):
        """GLOBAL SETTINGS > Update from GitHub: mostra o que ha de novo, pede confirmacao e corre o git pull e o
        instalador no Atom. O bridge reinicia a meio; o registo fica a vista."""
        s, d = self.call("Checking GitHub", "GET", "/api/system/update", timeout=100)
        if s == 404:
            self.done("Update from GitHub", s, {"error": "the bridge cannot update itself yet: run atualizar_atom.sh once by hand"})
            return
        if s != 200 or not isinstance(d, dict) or not d.get("ok"):
            self.done("Update from GitHub", s, d if isinstance(d, dict) else {"error": "bridge offline"})
            return
        if d.get("running"):
            self.pager("Update log", self._update_log_lines, cor=COR["sys"], every=1.0, seguir=True)
            return
        novos = d.get("new") or []
        inst = d.get("installed") or {}
        if not novos and d.get("install_pending"):
            # O git pull fez-se mas a instalacao nao acabou: o clone esta a frente do que corre
            if not self.confirm("The Atom has the latest code from GitHub (%s) but it is NOT installed:\n"
                                "running now: %s %s\n\nThe last install did not finish (see the update log).\n\n"
                                "Install it now? Bridge restarts for 1-2 min"
                                % ((d.get("current") or "?")[:60], inst.get("commit", "?"), (inst.get("subject") or "")[:50])):
                return
        elif not novos and not inst.get("commit"):
            # Atom de antes do version.json: nao se sabe o que corre. Se o pull se fez e a instalacao falhou, so
            # instalando outra vez se sai disto (o "Already up to date" deixava o Atom com o codigo velho).
            if not self.confirm("GitHub: %s\n\nThe Atom has this code but may not have installed it.\n\n"
                                "Install it again now? Bridge restarts for 1-2 min"
                                % (d.get("current") or "?")[:70]):
                return
        elif not novos:
            self.done("Update from GitHub", 200, {"ok": True},
                      ["Already up to date.", "",
                       "GitHub:    %s" % d.get("current"),
                       "Installed: %s" % ("%s  (%s)" % (inst.get("commit"), inst.get("installed")) if inst.get("commit")
                                          else "not recorded yet (it is from the next update on)")])
            return
        if novos:
            txt = ("Update from GitHub?\n"
                   "Now  %s\n\n%d new\n%s\n\nBridge restarts for 1-2 min"
                   % (d.get("current"), len(novos), "\n".join(novos[:8]) + ("\n..." if len(novos) > 8 else "")))
            if d.get("local_changes"):
                txt += "\n\nLocal changes on the Atom: the pull may refuse"
            if not self.confirm(txt):
                return
        self._upd_pedido = time.time()
        s, d = self.call("Update from GitHub", "POST", "/api/system/update", timeout=30)
        if s != 200 or not (isinstance(d, dict) and d.get("ok")):
            self._upd_pedido = 0.0
            self.done("Update from GitHub", s, d)
            return
        self.pager("Update log", self._update_log_lines, cor=COR["sys"], every=1.0, seguir=True)
        self._upd_pedido = 0.0
        self.verificar_versao()                  # o instalador pode ter trazido uma consola nova: reabre-se ja

    def _update_log_lines(self):
        s, d = api("GET", "/api/system/update?log=1", timeout=5)
        if s != 200 or not isinstance(d, dict):
            return [("Bridge restarting ...", "yellow"), "",
                    ("Esc leaves.", "dim")]
        L = [("Update running ..." if d.get("running") else "Update finished (or not running).",
              "yellow" if d.get("running") else "green"), ""]
        linhas = limpar_log(d.get("log") or [])
        # So a actualizacao em curso: do ultimo cabecalho "== <data> update from GitHub" em diante (um bridge
        # que ainda nao limpa o registo traz tambem as anteriores). Se esse cabecalho e anterior ao pedido,
        # o instalador ainda nao arrancou: nao se mostra o bloco da actualizacao velha.
        marcas = [i for i, ln in enumerate(linhas) if re.match(r"^== \d{4}-\d\d-\d\d \d\d:\d\d:\d\d update from GitHub", ln)]
        if marcas:
            ultima = linhas[marcas[-1]]
            quando = time.mktime(time.strptime(ultima[3:22], "%Y-%m-%d %H:%M:%S"))
            if self._upd_pedido and quando < self._upd_pedido - 3:
                linhas = []
            else:
                linhas = linhas[marcas[-1]:]
        elif self._upd_pedido:
            linhas = []
        L += linhas or ["starting ..."]
        if not d.get("running"):
            L += ["", ("Console reopens by itself.", "dim")]
        return L

    # --- teclas e ciclo ----------------------------------------------------
    def key(self, k):
        # Uma ordem das camaras por guardar: so as setas e o PgUp/PgDn a deixam passar.
        if self.pending_order is not None:
            if k in (ord("s"), ord("S")):
                self.order_save()
                return
            if k in (ord("u"), ord("U"), 27):
                self.order_undo()
                return
            if k not in (curses.KEY_UP, curses.KEY_DOWN, ord("k"), ord("j"), curses.KEY_PPAGE, curses.KEY_NPAGE, 9):
                self.say("Camera order changed: S save   U undo", 2)      # a tecla nao faz mais nada
                return
        if self.on_gw is not None:                   # o cursor esta numa linha dos GATEWAYS
            # As teclas de device (ligar, desligar, REC deste, nome, alvo, mover) nao se aplicam a um gateway: sem
            # isto passavam para a linha de device que ficou seleccionada (o D desligava a R6). 1-9 = a barra.
            if k in (ord(" "), ord("c"), ord("C"), ord("d"), ord("D"), ord("t"), ord("T"), ord("e"), ord("E"),
                     curses.KEY_PPAGE, curses.KEY_NPAGE, ord("<"), ord(">"), ord(","), ord("."), 9):
                # Antes nao dizia nada: na linha DEMO do gateway, o SPACE e o T pareciam nao funcionar
                g = self.gws[min(self.on_gw, len(self.gws) - 1)] if self.gws else {}
                self.say("%s is a gateway line: pick a camera row for that key" % g.get("name", "This"), 1)
                return
            if k in (curses.KEY_UP, ord("k")):
                if self.on_gw > 0:
                    self.on_gw -= 1
                else:                                # do primeiro gateway salta-se para a ultima posicao
                    self.on_gw = None
                    self.on_f6 = self.has_f6
                    self.cur = max(0, len(self.rows) - 1)
                return
            if k in (curses.KEY_DOWN, ord("j")):
                if self.on_gw < len(self.gws) - 1:
                    self.on_gw += 1
                else:                                # do ultimo gateway desce-se para a primeira camara
                    self.on_gw = None
                    self.cur = 0
                return
        if self.on_gw is not None and k in (10, 13, curses.KEY_ENTER):
            n_gw = self.on_gw

            def itens_gw():
                g = self.gws[min(n_gw, len(self.gws) - 1)] if self.gws else None
                return self.gw_items(g) if g else []
            g = self.gws[n_gw]
            self.opcoes_menu("%s gateway" % g["name"], itens_gw, COR["cams"])
            return
        if ord("1") <= k <= ord("9"):                # as opcoes da barra do device
            _nome, items = self.bar_items()
            n = k - ord("1")
            if items and n < len(items):
                items[n][1]()
            return
        if self.on_f6:                               # o cursor esta na linha do F6
            if k == ord(" "):
                self.toggle_target("f6")
                return
            if k in (10, 13, curses.KEY_ENTER):
                self.opcoes_menu("Zoom F6", lambda: ordenar_itens(self.f6_items(), "camera-f6"), COR["f6"])
                return
            if k in (ord("t"), ord("T")):
                self.act("REC F6", "POST", "/api/devices/f6/rec")
                return
            if k in (ord("e"), ord("E")):
                self.alias_edit({"id": "f6", "label": "Zoom F6", "alias": (self.dv().get("alias") or {}).get("f6", "")})
                return
            if k in (ord("c"), ord("C"), ord("d"), ord("D"), curses.KEY_PPAGE, curses.KEY_NPAGE,
                     ord("<"), ord(">"), ord(","), ord(".")):
                self.say("C / D do not apply to the F6 (Z opens its screen)", 1)
                return
        if k == 27:                                  # Esc: o menu geral (o que nao e de um device)
            self.main_menu()
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.muda_vista(1 if k == curses.KEY_RIGHT else -1)
        elif k in (curses.KEY_UP, ord("k")):
            if self.on_f6:
                self.on_f6 = False
                self.cur = max(0, len(self.rows) - 1)
            elif self.cur > 0:
                self.cur -= 1
            elif self.gws:                           # da primeira camara sobe-se para os GATEWAYS
                self.on_gw = len(self.gws) - 1
            else:                                    # sem fim: da primeira camara salta-se para a ultima posicao
                self.on_f6 = self.has_f6
                self.cur = max(0, len(self.rows) - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            if self.on_f6:                           # do F6 (a ultima posicao) volta-se ao principio
                self.on_f6 = False
                if self.gws:
                    self.on_gw = 0
                else:
                    self.cur = 0
            elif self.cur >= len(self.rows) - 1 and self.has_f6:   # a ultima camara -> o F6
                self.on_f6 = True
            elif self.cur >= len(self.rows) - 1 and self.gws:      # sem F6: da ultima camara para os GATEWAYS
                self.on_gw = 0
            else:
                self.cur = self.cur + 1 if self.cur < len(self.rows) - 1 else 0
        elif k in (curses.KEY_PPAGE, curses.KEY_NPAGE) and self.rows:
            self.move_row(-1 if k == curses.KEY_PPAGE else 1)
        elif k in (10, 13, curses.KEY_ENTER, ord("e"), ord("E")):
            if k in (ord("e"), ord("E")):
                if self.rows:
                    self.alias_edit(self.rows[self.cur])
            elif self.rows:                          # Enter: so as opcoes da camara seleccionada
                self.camera_menu(self.rows[self.cur])
        elif k in (ord("<"), ord(","), ord(">"), ord(".")) and self.rows:
            self.move_row(-1 if k in (ord("<"), ord(",")) else 1)
        elif k == ord(" ") and self.rows:
            self.toggle_target(self.rows[self.cur]["id"])
        elif k in (ord("a"), ord("A")):
            todos = [r["id"] for r in self.rows] + (["f6"] if self.st().get("f6") is not None else [])
            self.targets = list(todos)
            self.snap.alvos_locais(self.targets)
            self.act("targets", "POST", "/api/rec/targets", {"targets": todos})
        elif k in (ord("r"), ord("R")):
            self.act("REC", "POST", "/api/rec")
        elif k in (ord("g"), ord("G")):
            self.act("mark good", "POST", "/api/log/mark", {"kind": "good"})
        elif k in (ord("n"), ord("N")):
            self.act("mark no-good", "POST", "/api/log/mark", {"kind": "no-good"})
        elif k in (ord("m"), ord("M")):
            self.act("log/mark", "POST", "/api/log/mark", {"kind": "log/mark"})
        elif k in (ord("z"), ord("Z")):
            self.f6_screen()
        elif (k in (ord("c"), ord("C"), ord("d"), ord("D")) and self.rows
              and tipo_de(self.rows[self.cur]["id"]) in ("iphone", "sony")):
            r, liga = self.rows[self.cur], k in (ord("c"), ord("C"))
            if liga or self.ask("Disable %s? (y/n)" % r["label"], "yn") == "y":   # D pergunta, como nas Canon
                self.act("%s %s" % (r["label"], "enable" if liga else "disable"), "POST",
                         "/api/devices/%s/config" % r["id"], {"enabled": liga})
        elif (k in (ord("c"), ord("C"), ord("d"), ord("D")) and self.rows
              and tipo_de(self.rows[self.cur]["id"]) == "gopro"):
            self.say("C / D do not apply to a GoPro (F1 help)", 1)
        elif (k in (ord("c"), ord("C"), ord("d"), ord("D")) and self.rows
              and eh_bm_extra(self.rows[self.cur]["id"])):
            self.say("C / D do not apply to this BMPCC (F1 help)", 2)
        elif (k in (ord("c"), ord("C"), ord("d"), ord("D")) and self.rows
              and self.rows[self.cur]["id"] == "bmpcc"):
            self.say("C / D do not apply to the BMPCC (F1 help)", 2)
        elif k in (ord("c"), ord("C")) and self.rows:
            r = self.rows[self.cur]
            path = "/api/devices/%s/connect" % r["id"]
            self.act("connect %s" % r["label"], "POST", path)
        elif k in (ord("d"), ord("D")) and self.rows:
            r = self.rows[self.cur]
            if r["rec"]:
                self.say("%s is recording: stop it first" % r["label"], 2)
            elif self.ask("Disconnect %s? (y/n)" % r["label"], "yn") == "y":
                path = "/api/devices/%s/disconnect" % r["id"]
                self.act("disconnect %s" % r["label"], "POST", path)
        elif k in (ord("t"), ord("T")) and self.rows:
            self.rec_one(self.rows[self.cur])
        elif k in (ord("x"), ord("X")):
            self.act("clear fault", "POST", "/api/fault/clear")
        elif k in (ord("w"), ord("W")):
            self.wizard()
        elif k in (ord("l"), ord("L")):
            self.shoot_log_screen()
        elif k in (ord("p"), ord("P")):
            self.take_page(pedida=bool(self.st().get("take_pending")))
        elif k in (ord("?"), curses.KEY_F1):
            self.help_menu()

    def verificar_versao(self):
        """Chamada ao entrar nos menus de topo e depois do Update from GitHub: se esta consola foi substituida por uma
        versao nova (e compila), reabre-se ja. Sem isto, quem estava dentro de um menu quando o update acabava ficava com o
        codigo antigo (o mesmo ecra, os mesmos erros) ate sair para o ecra principal."""
        eu = os.path.abspath(__file__)
        antes = getattr(self, "_versao", None)
        if antes is None:
            return
        try:
            st = os.stat(eu)
            if st.st_mtime != antes and time.time() - st.st_mtime > 2:
                compile(open(eu, encoding="utf-8").read(), eu, "exec")
                raise _Reabrir()
        except (OSError, SyntaxError, ValueError):
            pass

    def loop(self):
        """Devolve "reabrir" quando o ficheiro desta consola foi substituido (Update from GitHub, ou o
        instalador): o main() volta a arrancar a versao nova. So acontece aqui, no ecra principal, sem
        menus abertos -- uma consola antiga a correr escondia as mudancas (o menu Bluetooth da BMPCC)."""
        # Ecra inteiro 4x por segundo; a linha do LTC a cada frame (40 ms = 25 fps).
        last = 0.0
        eu = os.path.abspath(__file__)
        try:
            versao = os.stat(eu).st_mtime
        except OSError:
            versao = None
        visto_em = 0.0
        visto = None
        self._versao = versao
        while True:
            if versao is not None and time.time() - visto_em > 2:
                visto_em = time.time()
                try:
                    st = os.stat(eu)
                    # Mudou ha mais de 2 s (o ficheiro ja esta todo escrito) e compila: reabre-se.
                    if st.st_mtime != versao and time.time() - st.st_mtime > 2:
                        compile(open(eu, encoding="utf-8").read(), eu, "exec")
                        return "reabrir"
                except (OSError, SyntaxError, ValueError):
                    versao = None              # nao se consegue ler, ou nao compila: fica-se com esta
            self.scr.timeout(40)       # os ecras modais deixam 250; repor aqui
            try:
                # O ecra inteiro so quando ha algo novo (dados, tecla, mensagem) ou de segundo a segundo (relogio,
                # pisca da bateria, dados velhos); entre isso so a linha do LTC, a 25 fps.
                marca = (self.snap.versao, self.msg)
                if marca != visto or time.time() - last >= 1.0:
                    self.draw()
                    last, visto = time.time(), marca
                else:
                    self.draw_tc()
            except curses.error:
                pass
            except Exception as e:
                # Um dado estranho do bridge nao pode fechar a consola a meio de uma filmagem.
                last = time.time()
                try:
                    self.put(0, 0, " SCREEN ERROR: %s " % str(e)[:100], self.attr("red") | curses.A_REVERSE)
                    self.scr.refresh()
                except Exception:
                    pass
            self.vigiar_take()
            k = self.scr.getch()
            if k in (ord("q"), ord("Q")):
                extra = "  (the camera order change is lost)" if self.pending_order is not None else ""
                if self.ask("Quit the console?%s (y/n)" % extra, "yn",
                            aviso="Something is RECORDING" if self.a_gravar() else None) == "y":
                    return
                continue
            if k != -1:
                try:
                    self.key(k)
                    # Setas em repeticao: gasta as que ja estao na fila antes de redesenhar (uma volta por seta era lento).
                    if k in (curses.KEY_UP, curses.KEY_DOWN):
                        self.scr.timeout(0)
                        for _ in range(20):
                            k2 = self.scr.getch()
                            if k2 not in (curses.KEY_UP, curses.KEY_DOWN):
                                if k2 != -1:
                                    curses.ungetch(k2)
                                break
                            self.key(k2)
                except _Reabrir:
                    return "reabrir"
                except Exception as e:
                    self.say("error: %s" % str(e)[:120], 2)
                last = 0.0                 # a seleccao (e a barra de opcoes do device) redesenham logo, nao no proximo quarto de segundo


def tty_repor():
    """Outro programa (nmtui, um shell, algo que morreu a meio) pode deixar o terminal estragado: teclado fora do modo
    unicode (KDSKBMODE), stdin sem bloqueio, eco/canonico mudados. O curses parte do que encontra, por isso repoe-se
    antes dele: sem isto a consola abria e as teclas nao faziam nada."""
    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError):
        return
    try:
        os.set_blocking(fd, True)
    except OSError:
        pass
    if fcntl is not None and re.match(r"^/dev/tty[0-9]+$", _ttyname(fd)):
        try:
            fcntl.ioctl(fd, 0x4B45, 3)                      # KDSKBMODE, K_UNICODE
        except OSError:
            pass
    try:                                # modos que outro programa de terminal pode ter deixado ligados (e o curses nao desliga)
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[?1l\x1b>\x1b[?25h")
            sys.stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        import termios
        a = termios.tcgetattr(fd)
        a[0] |= termios.ICRNL | termios.IXON                # iflag
        a[3] |= termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN   # lflag
        a[2] |= termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)               # teclas que ficaram na fila do outro programa
    except Exception:
        pass


def _ttyname(fd):
    try:
        return os.ttyname(fd)
    except OSError:
        return ""


def main(scr):
    snap = Snapshot()
    stop = threading.Event()
    snap.poll_once()
    threading.Thread(target=snap.run, args=(stop,), daemon=True).start()
    threading.Thread(target=snap.run_f6, args=(stop,), daemon=True).start()
    ui = Ui(scr, snap)
    try:
        if not ui.termos():
            return None
        return ui.loop()
    finally:
        ui.font_restore()   # nunca deixar a consola com a fonte pequena
        ui.bloqueio.parar()
        stop.set()


if __name__ == "__main__":
    iniciar_locale()
    tty_repor()
    try:
        r = curses.wrapper(main)
    except KeyboardInterrupt:
        r = None
    if r == "reabrir":
        # Versao nova instalada: arranca-a no lugar desta (mesmo terminal, mesmos argumentos).
        print("bridge-ctl: new version installed, restarting ...", flush=True)
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])

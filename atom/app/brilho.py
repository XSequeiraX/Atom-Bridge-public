#!/usr/bin/env python3
"""Brilho do ecra do Atom pelas teclas Fn+F6 (menos) e Fn+F7 (mais).

O Atom corre sem X: nao ha ninguem para tratar as teclas de brilho. Este servico le os eventos de teclado do kernel
(/dev/input/event*) e muda /sys/class/backlight/<x>/brightness. Corre como root (bridge-brilho.service).

  python3 brilho.py                servico: espera as teclas (KEY_BRIGHTNESSDOWN 224 e KEY_BRIGHTNESSUP 225)
  python3 brilho.py --estado       mostra o ecra (nome, brilho actual e maximo)
  python3 brilho.py + | -          sobe / desce um passo (para testar sem teclado)

Passos de 10% do maximo, nunca abaixo de 5% (um ecra a 0 pode ficar apagado e sem forma de o voltar a ver).
Se o kernel ou o ACPI ja mudou o brilho por si com a mesma tecla, nao se mexe outra vez (senao andava dois passos de cada vez).

A tampa (issue #19): o logind ignora-a (o instalador cria /etc/systemd/logind.conf.d/bridge-lid.conf), por isso o Atom nao
suspende e a gravacao, o Wi-Fi, o Bluetooth, o LTC e o shoot log continuam. Este servico, que ja corre como root, trata do resto:
  - fechou: bloqueia a consola (cria /run/bridge-ctl.locked, o bloqueio do Win+L e do F12; so se ha PIN, senao nao havia com
    que desbloquear) e apaga a retroiluminacao (guarda o brilho de antes);
  - abriu: repoe o brilho. NAO desbloqueia: so o PIN da pagina o faz.
"""
import fcntl
import glob
import os
import select
import signal
import struct
import sys
import time

BACKLIGHT = "/sys/class/backlight"
INPUT_GLOB = "/dev/input/event*"

EV_KEY = 1
KEY_BRIGHTNESSDOWN, KEY_BRIGHTNESSUP = 224, 225
EVENTO = struct.Struct("@llHHi")      # struct input_event: o tamanho do timeval depende de o Atom ser de 32 ou 64 bits

LOCK_FILE = "/run/bridge-ctl.locked"             # o bloqueio da consola (ver bridge-ctl.py)
PIN_FILES = ("/opt/camera-bridge/pin.json", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pin.json"))
TAMPA_SALVO = "/run/bridge-brilho-tampa"         # o brilho de antes de a tampa fechar (em /run: um reinicio repoe-no)
PROC_TAMPA = "/proc/acpi/button/lid"
SYS_INPUT = "/sys/class/input"
EVIOCGSW_8 = (2 << 30) | (8 << 16) | (ord("E") << 8) | 0x1b       # EVIOCGSW(8): o estado dos interruptores (SW_LID e o bit 0)

PASSO_PCT = 10
MINIMO_PCT = 5
ESPERA_KERNEL_S = 0.08                # se o brilho mudou sozinho nesta janela, outro (ACPI) tratou a tecla


def log(msg):
    print("brilho: " + msg, flush=True)


# ---- ecra -------------------------------------------------------------------------------------
def ecra(raiz=BACKLIGHT):
    """O caminho do primeiro backlight que existe (o da placa grafica antes do generico), ou None."""
    ds = sorted(d for d in glob.glob(os.path.join(raiz, "*")) if os.path.isfile(os.path.join(d, "max_brightness")))
    if not ds:
        return None
    pref = ("intel_backlight", "amdgpu", "nv_backlight", "acpi_video0", "radeon")
    for p in pref:
        for d in ds:
            if os.path.basename(d).startswith(p):
                return d
    return ds[0]


def _ler_int(caminho):
    try:
        with open(caminho) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def valores(dev):
    """(actual, maximo) do ecra, ou (None, None)."""
    mx = _ler_int(os.path.join(dev, "max_brightness"))
    ac = _ler_int(os.path.join(dev, "actual_brightness"))
    if ac is None:
        ac = _ler_int(os.path.join(dev, "brightness"))
    return ac, mx


def novo_valor(actual, maximo, sobe):
    """O brilho depois de um passo (10% do maximo, no minimo 1), entre 5% e 100% do maximo."""
    passo = max(1, round(maximo * PASSO_PCT / 100))
    minimo = max(1, round(maximo * MINIMO_PCT / 100))
    alvo = actual + passo if sobe else actual - passo
    return max(minimo, min(maximo, alvo))


def mudar(dev, sobe, espera=0.0):
    """Um passo. Devolve (antes, depois) ou None se nao mexeu (sem ecra, ja no limite, ou outro tratou a tecla)."""
    antes, mx = valores(dev)
    if antes is None or not mx:
        return None
    if espera:
        time.sleep(espera)
        agora, _ = valores(dev)
        if agora is not None and agora != antes:        # o kernel/ACPI ja o mudou com esta tecla
            return None
    alvo = novo_valor(antes, mx, sobe)
    if alvo == antes:
        return None
    try:
        with open(os.path.join(dev, "brightness"), "w") as f:
            f.write(str(alvo))
    except OSError as e:
        log("could not write %s: %s" % (os.path.join(dev, "brightness"), e))
        return None
    return antes, alvo


# ---- tampa ------------------------------------------------------------------------------------
def tampa_fechada(proc=PROC_TAMPA, sys_input=SYS_INPUT):
    """True se a tampa esta fechada, False se aberta, None se este portatil nao a diz. Le o /proc/acpi (quando o kernel o tem) e,
    senao, o interruptor da tampa em /dev/input (EVIOCGSW)."""
    for f in sorted(glob.glob(os.path.join(proc, "*", "state"))):
        try:
            with open(f) as fh:
                txt = fh.read().lower()
        except OSError:
            continue
        if "closed" in txt:
            return True
        if "open" in txt:
            return False
    for nome in sorted(glob.glob(os.path.join(sys_input, "input*", "name"))):
        try:
            with open(nome) as fh:
                if "lid switch" not in fh.read().lower():
                    continue
        except OSError:
            continue
        for ev in sorted(glob.glob(os.path.join(os.path.dirname(nome), "event*"))):
            try:
                fd = os.open("/dev/input/" + os.path.basename(ev), os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                buf = bytearray(8)
                fcntl.ioctl(fd, EVIOCGSW_8, buf)             # com um buffer mutavel o ioctl devolve um int e preenche o buffer
                return bool(buf[0] & 1)                      # SW_LID = 0
            except (OSError, TypeError, ValueError):
                continue
            finally:
                os.close(fd)
    return None


_tampa_erro = None


def tampa_estado():
    """tampa_fechada() sem nunca levantar: a tampa e um extra e nao pode derrubar o servico e levar as teclas do brilho com ela
    (um erro aqui, 2026-10-24, reiniciava-o de 5 em 5 s). O primeiro erro de cada tipo fica no registo."""
    global _tampa_erro
    try:
        return tampa_fechada()
    except Exception as e:                               # pylint: disable=broad-except
        if repr(e) != _tampa_erro:
            _tampa_erro = repr(e)
            log("lid: ignored error (%s)" % _tampa_erro)
        return None


def ha_pin():
    """Ha PIN da pagina? Sem ele o bloqueio nao teria com que se abrir: a tampa nao bloqueia."""
    for caminho in PIN_FILES:
        if os.path.isfile(caminho):
            return True
    return False


class Tampa:
    """O que fazer quando a tampa fecha (bloquear a consola e apagar o ecra) e quando abre (repor o brilho, sem desbloquear).
    `tick` chama-se uma vez por segundo com o estado da tampa; so age numa mudanca (e uma vez no arranque)."""

    def __init__(self, dev, lock=LOCK_FILE, salvo=TAMPA_SALVO):
        self.dev, self.lock, self.salvo = dev, lock, salvo
        self.antes = None                                    # None: ainda nao se sabe (o arranque conta como mudanca)

    def tick(self, fechada):
        if fechada is None or fechada == self.antes:
            return None
        self.antes = fechada
        if fechada:
            self.fechou()
            return "closed"
        self.abriu()
        return "open"

    def fechou(self):
        if ha_pin() and not os.path.exists(self.lock):
            try:
                fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
                log("lid closed: console locked (only the web PIN unlocks it)")
            except OSError as e:
                log("lid closed: could not lock (%s)" % e)
        elif not ha_pin():
            log("lid closed: no web PIN set, so the console is not locked")
        if self.dev and not os.path.exists(self.salvo):
            ac, _mx = valores(self.dev)
            if ac and ac > 0:                                # ja a 0 (o ACPI apagou-o): nada a guardar nem a repor
                try:
                    with open(self.salvo, "w") as f:
                        f.write(str(ac))
                    self._escrever(0)
                    log("lid closed: backlight off (was %d)" % ac)
                except OSError as e:
                    log("lid closed: could not turn the backlight off (%s)" % e)

    def abriu(self):
        self.repor()
        log("lid open%s" % (": console stays locked until the web PIN is entered" if os.path.exists(self.lock) else ""))

    def repor(self):
        """Repoe o brilho de antes de fechar a tampa (tambem ao parar o servico, para o ecra nunca ficar apagado)."""
        try:
            with open(self.salvo) as f:
                ac = int(f.read().strip())
        except (OSError, ValueError):
            return
        if self.dev:
            try:
                self._escrever(ac)
            except OSError as e:
                log("could not restore the backlight (%s)" % e)
                return
        try:
            os.remove(self.salvo)
        except OSError:
            pass

    def _escrever(self, valor):
        with open(os.path.join(self.dev, "brightness"), "w") as f:
            f.write(str(valor))


# ---- teclas -----------------------------------------------------------------------------------
def eventos(buf):
    """[(tipo, codigo, valor)] de um bloco de bytes lido de um /dev/input/eventN (resto incompleto ignorado)."""
    out = []
    n = EVENTO.size
    for i in range(0, len(buf) - n + 1, n):
        _, _, t, c, v = EVENTO.unpack_from(buf, i)
        out.append((t, c, v))
    return out


def accao(evs):
    """Para um bloco de eventos: 'mais', 'menos' ou None. Conta o premir e a repeticao (valor 1 e 2), nao o largar."""
    for t, c, v in evs:
        if t == EV_KEY and v in (1, 2):
            if c == KEY_BRIGHTNESSDOWN:
                return "menos"
            if c == KEY_BRIGHTNESSUP:
                return "mais"
    return None


def abrir_teclados():
    fds = {}
    for p in sorted(glob.glob(INPUT_GLOB)):
        try:
            fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = p
        except OSError:
            pass
    return fds


def servico():
    dev = ecra(BACKLIGHT)
    log("screen: %s" % (dev or "none (no /sys/class/backlight: the keys do nothing)"))
    tampa = Tampa(dev)
    if tampa_estado() is None:
        log("lid: this laptop does not report it (no /proc/acpi/button/lid and no Lid Switch input)")

    def parar(_sig, _frm):
        tampa.repor()                                        # o ecra nunca fica apagado por causa deste servico
        sys.exit(0)
    signal.signal(signal.SIGTERM, parar)
    fds, reabrir = abrir_teclados(), time.time() + 10
    while True:
        try:
            tampa.tick(tampa_estado())
        except Exception as e:                           # pylint: disable=broad-except
            log("lid: ignored error in the lid actions (%r)" % (e,))
        if not fds or time.time() > reabrir:
            for fd in fds:
                os.close(fd)
            fds, reabrir = abrir_teclados(), time.time() + 10
            if not fds:
                time.sleep(2)
                continue
        pronto, _, _ = select.select(list(fds), [], [], 1.0)
        for fd in pronto:
            try:
                buf = os.read(fd, EVENTO.size * 64)
            except BlockingIOError:
                continue
            except OSError:                        # o teclado saiu (USB): reabre-se no ciclo seguinte
                reabrir = 0
                continue
            a = accao(eventos(buf))
            if a and dev:
                r = mudar(dev, a == "mais", ESPERA_KERNEL_S)
                if r:
                    mx = valores(dev)[1]
                    log("%s: %d -> %d of %d (%d%%)" % (a, r[0], r[1], mx, round(r[1] * 100 / mx)))


def estado():
    dev = ecra(BACKLIGHT)
    if not dev:
        print("No backlight in %s: this screen's brightness is not controllable from Linux." % BACKLIGHT)
    else:
        ac, mx = valores(dev)
        print("screen %s: %s of %s (%s%%)" % (os.path.basename(dev), ac, mx, round(ac * 100 / mx) if ac is not None and mx else "?"))
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "--estado":
        return estado()
    if len(argv) > 1 and argv[1] in ("+", "-"):
        dev = ecra(BACKLIGHT)
        r = mudar(dev, argv[1] == "+") if dev else None
        print("%s" % (("%d -> %d" % r) if r else "unchanged"))
        return 0 if r else 1
    servico()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

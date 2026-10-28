"""A BMPCC pelo gateway ESP32-C3 (Wi-Fi), em vez do Bluetooth do Atom.

O C3 e a unica ligacao BLE a camara. No modo Bridge ele faz de proxy GATT:
  GET  /api/bm/poll?since=N&tcs=M  os pacotes da Incoming Camera Control depois de N (REC,
                                   bateria, ISO, slots, tempo restante, data...), o timecode
                                   se mudou desde M e a fase da ligacao;
  POST /api/bm/write?hex=...       qualquer comando para a Outgoing Camera Control.

Aqui os bytes entram em socketpairs (SOCK_SEQPACKET: um pacote por leitura), e o
bridge.py le-os com os MESMOS fd loops com que lia o BlueZ (camera_status_fd_loop,
timecode_fd_loop): a descodificacao de tudo continua num so sitio. Quando o C3
cai, ou a BMPCC cai no C3, os sockets fecham e os loops dao a queda como antes.

Onde esta o C3: o beacon UDP "C3GW ip=..." (porta 5172) ou "host" em c3.json.
c3.json (ao lado do bridge.py), tudo opcional:
  "enabled": false   o gateway esta desligado no bridge: nao se procura nem da alarme (a consola liga-o);
  "via": "bluez"     a BMPCC volta ao Bluetooth do Atom, sem o C3;
  "host": "..."      endereco fixo, em vez do beacon.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from vigiado import vigiado
import gravar
import fw_ota

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "c3.json")
BEACON_PORT = 5172
POLL_LIGADO = 0.1           # s entre pedidos com a BMPCC ligada ao bridge
POLL_PARADO = 0.3           # sem sessao: so o estado
QUEDA = 4.0                 # s sem resposta do C3 para dar a sessao por perdida
HTTP_TIMEOUT = 1.5
RELIGAR = 10.0              # s entre tentativas de abrir a sessao com a BMPCC pronta no C3

ao_pronto = None            # ao_pronto(): a BMPCC ficou pronta no C3 (para religar sozinho)
ao_beacon = None            # ao_beacon(ip): qualquer C3 que se anunciou (a frota de BMPCC extra usa-o)
reservado = None            # reservado(ip): este C3 e de uma BMPCC extra, nao o desta (principal)
BEACON_VELHO = 15.0         # s sem beacon do C3 actual para aceitar outro

_log = print
_lock = threading.Lock()
_beacon_host, _beacon_em = None, 0.0
vistos = {}                 # ip -> hora do ultimo beacon de qualquer C3 (a configuracao por USB confirma que o Atom o ve)
_estado = {}                # a ultima resposta do C3 (fase, modo...)
_ok_em = 0.0
_sessao = None              # {"in_w", "tc_w", "since", "tcs"}
_iniciado = False


_cfg_cache = [None, {}]          # (mtime, conteudo): o estado le o c3.json varias vezes por segundo


def _cfg():
    try:
        m = os.stat(CFG_PATH).st_mtime_ns
    except OSError:
        m = None
    if m is not None and m == _cfg_cache[0]:
        return dict(_cfg_cache[1])
    try:
        c = gravar.ler_json(CFG_PATH)
        c = c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        c = {}
    _cfg_cache[0], _cfg_cache[1] = m, c
    return dict(c)


def ativo():
    """A BMPCC passa pelo C3 (e nao pelo Bluetooth do Atom)?"""
    return _cfg().get("via", "c3") != "bluez"


def ligado():
    """O gateway esta ligado no bridge (procura-se e conta para alarmes)?"""
    return _cfg().get("enabled", True) is not False


def set_ligado(on):
    """Liga/desliga o gateway no bridge (fica em c3.json). Desligado, fecha-se a sessao."""
    c = _cfg()
    c["enabled"] = bool(on)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    gravar.trocar(tmp, CFG_PATH)
    if not on:
        desligar()
    _log("C3 gateway switched %s in the bridge" % ("ON" if on else "OFF"))


def set_via(via):
    """Por onde vai a BMPCC principal: "c3" (o gateway, por Wi-Fi) ou "bluez" (o Bluetooth do proprio Atom, plano B).
    Fica em c3.json; a sessao pelo C3 fecha-se."""
    if via not in ("c3", "bluez"):
        raise ValueError("via: c3 | bluez")
    c = _cfg()
    c["via"] = via
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    gravar.trocar(tmp, CFG_PATH)
    desligar()
    _log("BMPCC now goes through %s" % ("the C3 gateway" if via == "c3" else "the Atom Bluetooth (plan B)"))


def host():
    return (_cfg().get("host") or "").strip() or _beacon_host


def set_host(h):
    """Fixa o endereco do C3 em c3.json (vazio volta ao beacon). (ok, erro)."""
    h = str(h or "").strip()
    if h and not re.fullmatch(r"[A-Za-z0-9.\-:]{1,100}", h):
        return False, "invalid address"
    c = _cfg()
    if h:
        c["host"] = h
    else:
        c.pop("host", None)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    gravar.trocar(tmp, CFG_PATH)
    desligar()                              # outro C3: a sessao antiga fecha-se e abre-se de novo
    _log("C3 address: %s" % (h or "from the beacon"))
    return True, None


# Memoria do C3 (MicroPython livre, maior bloco livre do sistema) abaixo disto: aviso. Sem memoria o Wi-Fi ou o Bluetooth
# deixam de arrancar (2026-10-14).
MEM_BAIXA_MP = 15000
MEM_BAIXA_SIS = 6000


def memoria_baixa(mem):
    """Texto do aviso se a memoria [livre, maior bloco] do C3 esta baixa, senao None."""
    if not isinstance(mem, (list, tuple)) or len(mem) != 2:
        return None
    livre, bloco = mem
    if isinstance(livre, int) and livre < MEM_BAIXA_MP:
        return "low memory (%d KB free)" % (livre // 1024)
    if isinstance(bloco, int) and bloco < MEM_BAIXA_SIS:
        return "low memory (largest block %d KB)" % (bloco // 1024)
    return None


def estado():
    """Para a consola e a pagina. "state": off | offline | connecting | online."""
    e = dict(_estado)
    e["mem_low"] = memoria_baixa(e.get("mem"))
    alcanca = time.time() - _ok_em < QUEDA
    e.update({"host": host(), "reachable": alcanca, "session": _sessao is not None,
              "enabled": ligado(), "via_c3": ativo()})
    if not ligado():
        e["state"] = "off"
    elif not alcanca:
        e["state"] = "offline"
    elif e.get("mode") != "bridge" or e.get("phase") != "ready":
        e["state"] = "connecting"           # responde, mas no modo autonomo ou com a BMPCC ainda a ligar
    else:
        e["state"] = "online"
    return e


def _pedir(metodo, caminho):
    h = host()
    if not h:
        raise ConnectionError("C3 not found (no beacon yet; set host in c3.json)")
    # O C3 com gw_token pede o X-GW-Token em todos os POST (bm/write...); os GET (poll) nao o levam.
    req = urllib.request.Request("http://%s%s" % (h, caminho), method=metodo,
                                 data=b"" if metodo == "POST" else None,
                                 headers=fw_ota.token_cab() if metodo == "POST" else {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


# ---- sessao (os fds que o bridge.py le) ----
def ligar():
    """Abre a sessao: devolve (fd_estado, fd_timecode). Levanta excepcao se nao der."""
    global _sessao
    if not ligado():
        raise ConnectionError("the C3 gateway is switched off in the bridge (Gateways on the console)")
    s, r = _pedir("GET", "/api/bm/poll?since=-1&tcs=-1")
    if s == 409:
        raise ConnectionError("the C3 is in standalone mode: switch it to Bridge on its page")
    if s != 200:
        raise ConnectionError("C3 answered HTTP %s" % s)
    if r.get("phase") != "ready":
        raise ConnectionError("the BMPCC is not ready on the C3 (%s)" % r.get("phase"))
    desligar()
    in_r, in_w = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    tc_r, tc_w = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with _lock:
        # since=-1: o que o C3 tem na fila entra ja -- da o estado actual (ISO, slots...) sem esperar.
        _sessao = {"in_w": in_w, "tc_w": tc_w, "since": -1, "tcs": -1}
    _log("BMPCC via C3 (%s): session open" % host())
    return in_r.detach(), tc_r.detach()


def desligar():
    """Fecha a sessao: os fd loops do bridge leem EOF e saem."""
    global _sessao
    with _lock:
        s, _sessao = _sessao, None
    if s:
        for k in ("in_w", "tc_w"):
            try:
                s[k].close()
            except OSError:
                pass


def escrever(data, tentativas=3):
    """Um comando para a BMPCC (o que o bridge escrevia na Outgoing Camera Control). Uma falha de rede (o Wi-Fi a
    soluçar 1 s) repete-se ate 3 vezes: os comandos da BMPCC dizem o estado (REC=2, parar=0), repetir nao faz mal.
    Uma recusa do C3 (BMPCC nao pronta) nao se repete."""
    ultimo = None
    for n in range(tentativas):
        try:
            s, r = _pedir("POST", "/api/bm/write?hex=" + bytes(data).hex())
        except (OSError, ValueError) as e:          # URLError, timeout, ligacao recusada
            ultimo = e
            time.sleep(0.25 * (n + 1))
            continue
        if s != 200 or not r.get("ok"):
            raise ConnectionError("C3 refused the write: %s" % (r.get("error") or "HTTP %s" % s))
        return
    raise ConnectionError("C3 not answering the write (%d tries): %s" % (tentativas, ultimo))


def alcancavel():
    """O ciclo de leitura falou com o C3 ha menos de QUEDA s?"""
    return time.time() - _ok_em < QUEDA


# ---- threads ----
def _beacons():
    global _beacon_host, _beacon_em
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        u.bind(("", BEACON_PORT))
    except OSError as e:
        _log("C3: cannot listen for beacons on %d: %s" % (BEACON_PORT, e))
        return
    while True:
        try:
            m, (ip, _p) = u.recvfrom(256)
        except OSError:
            time.sleep(1)
            continue
        if not m.startswith(b"C3GW "):
            continue
        vistos[ip] = time.time()
        if ao_beacon:
            try:
                ao_beacon(ip)
            except Exception:
                pass
        if reservado and reservado(ip):
            continue
        # Com varios C3 na rede, o beacon de outro nao arrebata este: so se aceita um C3 novo
        # quando ainda nao ha nenhum ou o actual deixou de se anunciar.
        if _beacon_host and ip != _beacon_host and time.time() - _beacon_em < BEACON_VELHO:
            continue
        if ip != _beacon_host:
            _log("C3 gateway found at %s" % ip)
        _beacon_host, _beacon_em = ip, time.time()


def _e_transporte(raw):
    """True se a notificacao so traz pacotes de transporte (REC/STOP: categoria 10, parametro 1)."""
    i, achou = 0, False
    while i < len(raw) - 5:
        n = raw[i + 1]
        if n == 0 or i + n + 4 > len(raw):
            break
        if (raw[i + 4], raw[i + 5]) != (10, 1):
            return False
        achou = True
        i += n + 4
    return achou


def _so_o_ultimo_transporte(evs):
    """Leitura de raiz (sessao nova): o C3 devolve os ultimos pacotes que guardou, alguns antigos. Um REC/STOP velho la
    no meio fazia o bridge ver uma paragem ou um arranque "sozinho" que nao houve: fica so o ULTIMO transporte."""
    pacotes = [bytes.fromhex(hx) for hx in evs]
    ult = max((k for k, p in enumerate(pacotes) if _e_transporte(p)), default=None)
    return [p for k, p in enumerate(pacotes) if not _e_transporte(p) or k == ult]


def _ciclo():
    global _estado, _ok_em
    fase_antes = None
    pronto_em = 0.0
    while True:
        sess = _sessao
        if not ativo() or not ligado() or not host():
            _estado, fase_antes = {}, None
            time.sleep(1)
            continue
        q = "/api/bm/poll?since=%d&tcs=%d" % ((sess["since"], sess["tcs"]) if sess else (10 ** 9, 10 ** 9))
        try:
            s, r = _pedir("GET", q)
        except Exception:
            s, r = None, None
        agora = time.time()
        if s == 200:
            _ok_em = agora
            _estado = {k: r.get(k) for k in ("phase", "mode", "recording", "seq", "token", "mem", "rssi", "rssi_age_s", "wifi_rssi")}
            fase = r.get("phase")
            if sess and sess is _sessao:
                if fase != "ready":
                    _log("BMPCC dropped on the C3 (%s)" % fase)
                    desligar()
                else:
                    if r.get("lost"):
                        _log("C3: some BMPCC packets were lost between polls")
                    try:
                        evs = r.get("ev") or []
                        pacotes = (_so_o_ultimo_transporte(evs) if sess["since"] < 0
                                   else [bytes.fromhex(hx) for hx in evs])
                        for p in pacotes:
                            sess["in_w"].send(p)
                        if r.get("tc"):
                            sess["tc_w"].send(bytes.fromhex(r["tc"]))
                            sess["tcs"] = r.get("tc_seq", sess["tcs"])
                        sess["since"] = r.get("seq", sess["since"])
                    except OSError:
                        desligar()          # o bridge fechou o lado dele
            elif fase == "ready" and ao_pronto and (fase_antes != "ready" or agora - pronto_em >= RELIGAR):
                # Sem sessao e a BMPCC pronta no C3: o bridge liga-se sozinho (e volta a tentar).
                pronto_em = agora
                threading.Thread(target=ao_pronto, daemon=True).start()
            fase_antes = fase
        else:
            if s == 409:
                _estado = {"mode": "standalone"}      # o C3 no modo autonomo (sem o Atom)
                _ok_em = agora
            if sess and agora - _ok_em > QUEDA:
                _log("C3 not answering for %.0fs: BMPCC session lost" % QUEDA)
                desligar()
            fase_antes = None
        time.sleep(POLL_LIGADO if _sessao else POLL_PARADO)


def iniciar(log=None):
    global _log, _iniciado
    if _iniciado:
        return
    _iniciado = True
    if log:
        _log = log
    threading.Thread(target=vigiado(_beacons, "C3 beacon", _log), daemon=True, name="c3-beacon").start()
    threading.Thread(target=vigiado(_ciclo, "C3 gateway", _log), daemon=True, name="c3-bm").start()

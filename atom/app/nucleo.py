
#!/usr/bin/env python3
"""
Camera Bridge: BMPCC (C3 gateway or BlueZ), Canon EOS (CCAPI), iPhones, GoPro (S3 gateway), Zoom F6.
Web-controlled bridge with status dashboard
"""

import csv

import dbus

import json

import os

import re

import subprocess

import sys

import threading

import time

from flask import Flask, jsonify, request

import canon_ccapi
import c3_bm
import f6_ltc

import bmcam
import licencas
import sony

import gopro

import bmfleet as bmfleet_mod

from vigiado import vigiado

import f6_ctl

import frc8

import f6_card

import gravar

import claquete                 # cena, reel e take do clip seguinte em todas as camaras

import tc_vigia                 # desvio do timecode de cada camara para o LTC do Atom



# === CONFIG ===
CAMERA_ADDRESS = "CC:86:EC:2F:15:50"

OUTGOING_UUID = "5dd3465f-1aee-4299-8493-d2eca2f8e1bb"

INCOMING_UUID = "b864e140-76a0-416a-bf30-5876504537d9"

TIMECODE_UUID = "6d8f2110-86f1-41bf-9afb-451d87e976c8"

WEB_PORT = 8080


def _ap_conf():
    """O que o instalador guarda em ap.conf (KEY=valor por linha): AP_CON (o perfil do AP das cameras), AP_IF e UP_IF
    (os nomes das interfaces). Antes os nomes das interfaces eram trocados por um sed em todo o bridge.py, que tambem
    apanhava palavras iguais nos comentarios e no codigo."""
    d = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ap.conf")) as f:
            for l in f:
                k, _, v = l.strip().partition("=")
                if k and v.strip():
                    d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


_CONF = _ap_conf()

AP_IFACE = _CONF.get("AP_IF") or "ps-control"        # o dongle AR9271 do AP das cameras

# O uplink: e por aqui que o Atom chega a rede de infraestrutura e apanha NTP (o radio interno).
UPLINK_IFACE = _CONF.get("UP_IF") or "ps-uplink"

# === WHICH BLUETOOTH CONTROLLER EACH DEVICE USES ===
# No Atom o Bluetooth interno (o do cartao Intel, que partilha a antena com o uplink) esta desligado e o dongle ASUS
# USB-BT400 (radio e antena proprios) e o hci0. (No Raspberry Pi de antes o interno era o hci0 e o dongle o hci1.)
# A numeracao muda com o hardware: numa maquina nova com o Bluetooth interno ligado o dongle passa a hci1.
#
# A BMPCC fica no dongle porque perder esse link custa uma tomada.
# Set both to the same value to go back to sharing one controller.
#
# 2026-09-07: medido, e o dongle e o melhor receptor PARA ESTA CAMARA. Na mesma
# posicao, com minutos de intervalo: o dongle apanhou 67 anuncios (media -89
# dBm), o interno apanhou 10 (media -93). O interno ouve a sala tao bem ou
# melhor, mas neste link em concreto fica 4 dB abaixo e perde seis em cada sete
# pacotes -- e a BMPCC e o link cuja perda custa uma tomada.
CAMERA_ADAPTER = "hci0"          # dongle ASUS, 5C:F3:70:AA:D9:BA

# Quanto tempo esperar pelo adaptador preferido antes de desistir dele.
# O dongle so aparece depois do btusb-atrasado.service (que dorme 25s de
# proposito, senao o firmware .hcd nao carrega no arranque do Pi 4), e a
# enumeracao USB mais o patchram ainda levam segundos depois disso.
ADAPTER_WAIT_SECONDS = 20

# Preenchido no arranque com o que estava configurado, ANTES de o
# resolve_adapter lhe passar por cima. Serve para dar por um dongle que chegou
# tarde -- ver vigiar_adaptador_tardio().
ADAPTADORES_PREFERIDOS = {}


def adaptadores_presentes():
    """So os controladores a serio: /sys/class/bluetooth traz tambem hci0:64."""
    try:
        return sorted(n for n in os.listdir("/sys/class/bluetooth")
                      if re.fullmatch(r"hci\d+", n))
    except OSError:
        return []


_resolved_adapters = {}


def resolve_adapter(preferred):
    """Memoizado por nome preferido -- ver _resolve_adapter_uncached().

    Os aparelhos BLE apontam quase sempre para o mesmo adaptador ("hci1"
    nesta maquina). Sem isto, tres chamadas para o mesmo nome esperavam
    ate ADAPTER_WAIT_SECONDS*2 CADA, em serie -- ate 120s so em espera
    repetida pela mesma coisa. Visto ao vivo 2026-09-16: um dongle BT
    lento a inicializar (ou avariado) multiplicava-se por tres e ajudava
    a estourar o watchdog do systemd no arranque.
    """
    if preferred not in _resolved_adapters:
        _resolved_adapters[preferred] = _resolve_adapter_uncached(preferred)
    return _resolved_adapters[preferred]


def _resolve_adapter_uncached(preferred):
    """Return an adapter that actually exists, preferring the configured one.

    Adapter numbering is not stable across hardware changes: disabling the
    Pi's internal Bluetooth promotes the USB dongle from hci1 to hci0, which
    would otherwise silently break a hardcoded name. Bonds are unaffected —
    they live under the adapter's MAC, not its index.

    Espera pelo preferido em vez de decidir a frio. Isto corre uma unica vez,
    no arranque, e quem cair aqui para o adaptador errado fica nele para toda
    a vida do processo. E cair para o interno nao da um link pior: da link
    nenhum, porque a BMPCC so tem bond no dongle. Aconteceu a
    2026-09-10 — o bridge arrancou 42s antes do btusb e ficou o serao inteiro
    no radio interno com a camara a recusar-se a ligar.
    """
    fim = time.time() + ADAPTER_WAIT_SECONDS
    names = []
    while True:
        names = adaptadores_presentes()
        if preferred in names:
            break
        if time.time() >= fim:
            if names:
                log_event(f"Adapter {preferred} not present after {ADAPTER_WAIT_SECONDS}s — "
                          f"using {names[0]}. Anything bonded only to {preferred} "
                          f"will NOT connect.")
                return names[0]
            log_event(f"No Bluetooth adapter at all — staying on {preferred}")
            return preferred
        time.sleep(1)

    # Estar em /sys/class/bluetooth diz que o kernel ve o adaptador. Nao diz
    # que o bluetoothd o registou como Adapter1 no D-Bus -- e enquanto isso
    # nao acontecer, TUDO o que dependa do D-Bus falha com UnknownObject,
    # incluindo ler "Paired" de um aparelho que ja esta emparelhado. Visto ao
    # vivo a 2026-09-11: o hci1 esteve presente em sysfs a sessao inteira
    # (2h30) sem nunca responder no D-Bus -- um aparelho ja emparelhado nunca
    # conseguiu sequer LER se estava emparelhado, e nao havia sinal nenhum
    # disto ate se tentar ligar.
    fim2 = time.time() + ADAPTER_WAIT_SECONDS
    while adapter_mac(preferred) is None:
        if time.time() >= fim2:
            log_event(f"Adapter {preferred} is visible to the kernel but bluetoothd "
                      f"never registered it on D-Bus after {ADAPTER_WAIT_SECONDS}s — "
                      f"nothing will pair or connect on it until RESTART BLUETOOTH "
                      f"actually brings it back, or the system reboots")
            break
        time.sleep(1)
    return preferred


def adapter_mac(hci):
    """Controller address, read live so swapping the dongle needs no edit.

    Via BlueZ rather than sysfs — this kernel exposes no `address` file under
    /sys/class/bluetooth/hciX.
    """
    try:
        bus = dbus.SystemBus()
        props = dbus.Interface(bus.get_object("org.bluez", f"/org/bluez/{hci}"),
                               "org.freedesktop.DBus.Properties")
        return str(props.Get("org.bluez.Adapter1", "Address")).upper()
    except Exception:
        return None


def device_path(hci, mac):
    """BlueZ object path for a device on a specific controller."""
    return f"/org/bluez/{hci}/dev_" + mac.upper().replace(":", "_")

# === STATE ===
state = {
    "camera_connected": False,
    "camera_connecting": False,
    # Explicitly False at startup. It used to be absent, and every read used
    # .get(..., True) as the default — so on a fresh boot the UI believed the
    # BMPCC was in a reconnect loop when nothing was happening at all. Worse,
    # the badge is a toggle: with auto_reconnect "true", the first tap sent a
    # DISCONNECT, which is why connecting appeared to need two attempts.
    "_auto_reconnect": False,
    "is_recording": False,
    "timecode": "--:--:--",
    "remaining_time": "",
    "iso": 0,
    "active_slot": 0,
    "slot1_present": False,
    "slot2_present": False,
    "slot3_present": False,
    "slot1_name": "Slot 1",
    "slot2_name": "Slot 2",
    "slot3_name": "Slot 3",
    "slot1_remaining": "",
    "slot2_remaining": "",
    "slot3_remaining": "",
    "battery_percent": -1,
    "power_source": -1,
    # Ultimo REC comandado, enquanto a telemetria nao confirma. Ver
    # next_rec_direction().
    "_rec_intent": {},                 # {chave do REC: {"on", "at"}} (ver next_rec_direction)
    # Numeracao de takes do dia. Ver proxima_take().
    "_take_no": 0,
    "_take_day": None,
    "last_event": "Idle",
    "log": [],
    # Which cameras the REC button drives. A SET, not one choice:
    # with three bodies you often want two of them and not the third.
    "rec_targets": ["bmpcc"],
    # Qual dos relogios acerta as camaras.
    # Uma de CLOCK_SOURCES, ou None. Arranca SEMPRE em None: nenhuma fonte
    # escolhida, a pedido do operador (2026-09-12) -- "quando arranca, nao ha
    # fonte automatica, eu e que escolho para nao haver confusao". Nao e
    # persistido de proposito: recuperar a escolha de ontem seria pos a
    # funcionar uma fonte que ninguem escolheu hoje, que e exactamente a
    # confusao que se quer evitar.
    "clock_source_pref": None,
    "_bm_date": None,
    "_ltc_jam": None,
    "_ltc_error": None,
    "led_on": True,   # activity LED on the USB wifi dongle
    # "I drive recording only from the bridge." The protocol gives
    # no way to tell a media failure from the operator pressing the camera's
    # own REC button — both arrive as the same transport packet. This is the
    # operator supplying the missing half: with it on, a stop we did not
    # command is anomalous by definition and worth an alarm.
    "strict_rec": False,
}

camera_char_path = None

timecode_char_path = None


def log_event(msg):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    _gravar_evento(entry)
    state["log"].append(entry)
    # Single write: stdout and stderr both land in the journal under this
    # unit, so printing to both duplicated every line (and doubled the SD
    # card writes that come with it).
    print(entry, file=sys.stderr, flush=True)
    if len(state["log"]) > 50:
        state["log"] = state["log"][-50:]
    state["last_event"] = msg

# === SHOOT LOG ===
# Plain CSV of every confirmed recording start/stop across BMPCC + Canon
# bodies, independent of any single camera's own timestamp metadata — for
# lining up multicam footage in post (DaVinci Resolve etc).
# Lives next to whichever copy of this script is actually running
# (home dir or /opt), and survives service restarts since it's never
# touched by the deploy's `cp`.
SHOOT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shoot_log.csv")

# A coluna `take` foi acrescentada a 2026-09-08. Em pos ninguem fala em
# timecodes, fala em takes -- e o log so tinha start/stop com um timecode, que
# obriga quem recebe o material a construir a numeracao a mao.
# Colunas por definicao da camara: uma coluna por
# valor para ser facil de estruturar em pos. A chave e a coluna, o valor e o
# nome da definicao no estado da camara Canon.
SHOOT_SETTING_KEYS = {"av": "av", "tv": "tv", "iso": "iso",
                      "shutter_mode": "shuttermode", "movie_quality": "moviequality"}

SHOOT_SETTING_FIELDS = list(SHOOT_SETTING_KEYS)

# `alias` (antes `ref`) e `action` (antes `event`) mudaram de nome a 2026-10-23; `name` e o nome da sessao ou sequencia (a pagina de
# dados da take): quando muda, a numeracao das takes volta a 1. `clip`: o nome do ficheiro gravado (a app do iPhone, depois do STOP);
# a linha do stop recebe-o em `anotar_clip()`.
SHOOT_LOG_FIELDS = ["id", "timestamp", "source", "alias", "action", "note", "name", "take", "clip"] + SHOOT_SETTING_FIELDS

_shoot_log_lock = threading.Lock()

EVENT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.log")

EVENT_LOG_MAX = 2000          # linhas antes de se cortar pela metade


def _gravar_evento(entry):
    """Guarda a linha em disco, para o log sobreviver a um reinicio.

    O `state["log"]` sao 50 entradas em memoria e desaparecem em cada arranque
    -- incluindo o arranque que se fez POR CAUSA do problema que se estava a
    investigar, que e precisamente quando as entradas fazem falta. Aqui nao se
    faz fsync: isto e diagnostico, nao e o shoot log, e um fsync por evento
    custava mais do que vale.
    """
    try:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError:
        pass


def _aparar_event_log():
    """Corta o ficheiro pela metade quando cresce de mais.

    Cortar pela metade e nao ate ao limite de proposito: aparar uma linha de
    cada vez faria isto reescrever o ficheiro inteiro a cada evento.
    """
    try:
        with open(EVENT_LOG_PATH, encoding="utf-8") as f:
            linhas = f.readlines()
        if len(linhas) <= EVENT_LOG_MAX:
            return
        with open(EVENT_LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(linhas[-EVENT_LOG_MAX // 2:])
    except OSError:
        pass


def carregar_event_log():
    """Repoe as ultimas entradas em memoria, para a interface nao abrir vazia."""
    try:
        with open(EVENT_LOG_PATH, encoding="utf-8") as f:
            linhas = [l.rstrip("\n") for l in f.readlines()[-50:]]
    except OSError:
        return
    if linhas:
        state["log"] = linhas
        state["last_event"] = linhas[-1]


def migrar_shoot_log():
    """Poe o cabecalho novo num ficheiro escrito com o antigo.

    Sem isto, acrescentar uma coluna era desastre silencioso: as linhas novas
    passavam a ter seis valores debaixo de um cabecalho de cinco, e o ficheiro
    -- que e o que liga o material a linha temporal -- ficava desalinhado a
    partir do meio, sem nada rebentar.
    """
    try:
        with open(SHOOT_LOG_PATH, newline="") as f:
            linhas = list(csv.reader(f))
    except FileNotFoundError:
        return
    if linhas and linhas[0] == SHOOT_LOG_FIELDS:
        # Linhas escritas sem a coluna `ref` (de 2026-10-13, quando o codigo passou a viver em atom/app, ate esta
        # correccao): um valor a menos, tudo deslocado. Repoe-se a coluna vazia no sitio dela.
        curtas = [n for n, r in enumerate(linhas) if n and len(r) == len(SHOOT_LOG_FIELDS) - 1]
        if not curtas:
            return
        for n in curtas:
            linhas[n].insert(3, "")
        tmp = SHOOT_LOG_PATH + ".tmp"
        with open(tmp, "w", newline="") as f:
            csv.writer(f).writerows(linhas)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SHOOT_LOG_PATH)
        log_event(f"Shoot log repaired: {len(curtas)} rows were missing the ref column")
        return
    if not linhas:
        return
    antigos = linhas[0]
    corpo = [dict(zip(antigos, r)) for r in linhas[1:]]
    # camera->source, event->action, ref->alias: renomeados (event e ref a 2026-10-23, `action` foi o nome antigo de event). Sem
    # isto o generico `r.get(k, "")` abaixo nao encontrava as chaves antigas por baixo dos nomes novos, e as linhas ja escritas
    # perdiam a camara e a accao ao serem migradas.
    for r in corpo:
        if "source" not in r and "camera" in r:
            r["source"] = r.pop("camera")
        if "action" not in r and "event" in r:
            r["action"] = r.pop("event")
        if "alias" not in r and "ref" in r:
            r["alias"] = r.pop("ref")
    tmp = SHOOT_LOG_PATH + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SHOOT_LOG_FIELDS)
        for r in corpo:
            w.writerow([r.get(k, "") for k in SHOOT_LOG_FIELDS])
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SHOOT_LOG_PATH)
    log_event(f"Shoot log migrated: {len(corpo)} rows now have the current columns")


def take_lock():
    """O bloqueio da pagina de dados da take esta ligado (por omissao sim)? Com a pagina por fechar, o bridge recusa um REC novo."""
    return (state.get("take_info") or {}).get("lock", True) is not False


def _take_pendente_marcar(n):
    """Uma take acabou: a pagina de dados fica por preencher ate alguem a guardar ou saltar. (Chama-se com o _take_lock.)"""
    if not state.get("_take_pendente"):
        state["_take_pendente"] = {"take": int(n or 0), "since": time.time()}


def nome_da_sessao():
    """O nome da sessao ou sequencia em curso (a pagina de dados da take): vai na coluna `name` de cada linha do shoot log."""
    return str((state.get("take_info") or {}).get("name") or "")


def restaurar_contagem_takes():
    """Le do ficheiro em que take ia, para um reinicio nao recomecar do um.

    A numeracao reinicia por DIA, nao por sessao: o bridge pode ser reiniciado
    tres vezes numa manha e as takes continuam a ser as mesmas da manha.
    """
    try:
        with open(SHOOT_LOG_PATH, newline="") as f:
            linhas = list(csv.DictReader(f))
    except (FileNotFoundError, OSError):
        return
    for r in reversed(linhas):
        if not (r.get("take") or "").strip().isdigit():
            continue
        # O dia do ultimo take e a hora a que acabou: um reinicio depois da meia-noite, a meio da filmagem, continua
        # a numeracao (proxima_take so recomeca com 2 h sem takes).
        state["_take_no"] = int(r["take"])
        # O nome da sessao mudou e ainda nao houve nenhuma take com ele: a numeracao recomeca (como se ve ao fechar e abrir o bridge).
        if nome_da_sessao() and (r.get("name") or "") != nome_da_sessao():
            state["_take_no"] = 0
        state["_take_day"] = (r.get("timestamp") or "")[:10] or None
        try:
            state["_take_fim"] = time.mktime(time.strptime(r.get("timestamp") or "", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            state["_take_fim"] = 0
        return


_take_lock = threading.Lock()

_take_ativos = {}               # fonte -> hora do start, das camaras a gravar no take aberto

# O take aberto fica tambem em disco (take_aberto.json, com um "vivo" de 10 em 10 s): se o bridge reinicia a meio de
# um take (queda, actualizacao, Atom sem energia), as camaras que continuam a gravar, ao voltarem a dizer `start`, entram
# no MESMO take (antes abriam um novo) e o shoot log regista quanto tempo o bridge esteve em baixo.
TAKE_ABERTO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "take_aberto.json")

TAKE_RETOMAR_S = 180            # depois de arrancar, durante quanto tempo um `start` ainda retoma o take de antes

TAKE_RETOMAR_MAX_S = 6 * 3600   # um take aberto mais velho do que isto ja nao se retoma

_take_retomar = None            # {"take", "day", "alive", "ate"}: o take de antes do reinicio, por retomar


def _take_aberto_gravar(n):
    try:
        tmp = TAKE_ABERTO_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"take": n, "day": state.get("_take_day"), "alive": time.time()}, f)
        os.replace(tmp, TAKE_ABERTO_PATH)
    except OSError:
        pass


def _take_aberto_apagar():
    try:
        os.remove(TAKE_ABERTO_PATH)
    except OSError:
        pass


def take_aberto_ao_arrancar():
    """No arranque: havia um take aberto quando o bridge parou? Fica para retomar durante TAKE_RETOMAR_S."""
    global _take_retomar
    try:
        with open(TAKE_ABERTO_PATH) as f:
            d = json.load(f)
        n, vivo = int(d["take"]), float(d["alive"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if time.time() - vivo > TAKE_RETOMAR_MAX_S:
        _take_aberto_apagar()
        return
    _take_retomar = {"take": n, "day": d.get("day"), "alive": vivo, "ate": time.time() + TAKE_RETOMAR_S, "vistos": set(),
                     "em_baixo": max(0, time.time() - vivo)}
    log_event("The bridge stopped during take %d (%d s ago): cameras still recording rejoin it" % (n, time.time() - vivo))


def quem_grava():
    """Os nomes (os do shoot log) das camaras que estao a gravar agora."""
    out = ["BMPCC"] if state.get("is_recording") else []
    if f6.running():
        out.append("F6")
    for frota in (iphs, gofleet, bmfleet, sonys):
        out += [c.name for c in list(frota.cams.values()) if c.running()]
    out += [c.name for c in list(canon_ccapi.CAMERAS.values()) if c.a_gravar()]
    return out


def _retomar_quem_grava():
    """Depois de um reinicio a meio de um take: as camaras que o bridge ja ve a gravar entram no take de antes, cada uma
    quando aparecer (umas ligam-se antes das outras), ate acabar a janela. (As frotas nao dizem `start` ao ler o primeiro
    estado, por isso nao se pode esperar so pelo proxima_take.)"""
    r = _take_retomar
    if not r or time.time() > r["ate"]:
        return
    agora = time.time()
    with _take_lock:
        if _take_retomar is not r:
            return
        nomes = [n for n in quem_grava() if n not in r["vistos"]]
        if not nomes:
            return
        if not _take_ativos:
            state["_take_no"], state["_take_day"] = r["take"], r.get("day") or time.strftime("%Y-%m-%d")
        for n in nomes:
            _take_ativos.setdefault(n, agora)
            r["vistos"].add(n)
        _take_aberto_gravar(r["take"])
    log_event("Take %d continues after the bridge restart (%s still recording)" % (r["take"], ", ".join(nomes)))
    for n in nomes:
        _anotar_retoma(n, r["take"], r["em_baixo"])


def _anotar_retoma(source, n, em_baixo):
    """A linha do shoot log que diz que o take continuou depois de o bridge reiniciar (fora do lock do take). `em_baixo`:
    o tempo em que o bridge esteve parado (do ultimo "vivo" ate arrancar), nao ate a camara aparecer."""
    def fazer():
        log_shot(source, "fault", "bridge restarted during take %d: down %d s, still recording" % (n, em_baixo))
    threading.Thread(target=fazer, daemon=True).start()

TAKE_DIA_PAUSA_S = 2 * 3600     # a numeracao so recomeca num dia novo depois de 2 h sem takes (filmagem depois da meia-noite)


def proxima_take(event, source=""):
    """O numero de take a escrever nesta linha.

    Um take e um REC do grupo: abre-se no primeiro `start` com nada a gravar, e as outras camaras que arrancam
    juntam-se ao mesmo numero (antes cada camara abria um take: 1 REC com 4 camaras dava os takes 1 a 4). Fecha-se
    quando a ultima para (ou quando nada grava: ver fechar_take_se_parado). Uma camara que volta a dizer `start` a meio
    (religou-se) continua no mesmo take. O resto (falhas, marcas) herda o take que estiver a decorrer.
    """
    global _take_retomar
    agora = time.time()
    hoje = time.strftime("%Y-%m-%d")
    retoma = None
    with _take_lock:
        if event == "start":
            if not _take_ativos:
                state["_take_pendente"] = None                  # uma take comecou (por outra via): a pagina ja nao faz sentido
                if not (_take_retomar and agora < _take_retomar["ate"]):
                    novo_dia = state.get("_take_day") != hoje
                    if novo_dia and agora - float(state.get("_take_fim") or 0) > TAKE_DIA_PAUSA_S:
                        state["_take_no"] = 0
                    state["_take_day"] = hoje
                    state["_take_no"] = int(state.get("_take_no") or 0) + 1
                    _take_retomar = None
                    _take_aberto_gravar(state["_take_no"])
            r = _take_retomar
            if r and agora < r["ate"]:
                # O bridge reiniciou a meio deste take e esta camara continua a gravar: o mesmo numero.
                if not _take_ativos:
                    state["_take_no"], state["_take_day"] = r["take"], r.get("day") or hoje
                if source not in r["vistos"]:
                    r["vistos"].add(source)
                    retoma = (r["take"], r["em_baixo"])
                _take_aberto_gravar(r["take"])
            _take_ativos[source] = agora
        elif event == "stop":
            estava = source in _take_ativos
            _take_ativos.pop(source, None)
            if not _take_ativos:
                if estava:                                      # a ultima camara parou: a pagina de dados da take abre em todos
                    _take_pendente_marcar(state.get("_take_no"))
                state["_take_fim"] = agora
                _take_retomar = None             # o take retomado acabou: um REC a seguir e um take novo
                _take_aberto_apagar()
        n = state.get("_take_no")
    if retoma:
        log_event("Take %d continues after the bridge restart (%s still recording)" % (retoma[0], source))
        _anotar_retoma(source, *retoma)
    return str(n) if n else ""


def desvios_tc():
    """{id: {name, offset, err}}: o desvio (s, + adiantada) do timecode de cada camara ligada para o LTC do Atom. So com
    jam de LTC valido; as camaras sem medida (ainda poucas amostras) ficam de fora."""
    if ltc_secs_now() is None:
        return {}
    agora, out = time.time(), {}
    if state.get("camera_connected"):
        e = _desvio_bmpcc.estimativa(agora)
        if e:
            out["bmpcc"] = {"name": "BMPCC", "offset": e[0], "err": e[1]}
    for cid, cam in list(bmfleet.cams.items()):
        e = cam.desvio.estimativa(agora) if cam.connected() else None
        if e:
            out[cid] = {"name": cam.name, "offset": e[0], "err": e[1]}
    for cid, ip in iphs.status().items():
        if ip.get("connected") and ip.get("tc_offset") is not None:
            out[cid] = {"name": ip.get("name") or cid, "offset": ip["tc_offset"], "err": ip.get("tc_err") or 0}
    return out


def take_prevista():
    """O numero do take que o proximo REC vai abrir (None com um take aberto). Mesma regra do proxima_take."""
    agora = time.time()
    with _take_lock:
        if _take_ativos:
            return None
        n = int(state.get("_take_no") or 0)
        if state.get("_take_day") != time.strftime("%Y-%m-%d") and \
                agora - float(state.get("_take_fim") or 0) > TAKE_DIA_PAUSA_S:
            n = 0
    return n + 1


# === Claquete do clip seguinte (claquete.py) ===
# Entre takes, cada camara ligada recebe a cena, o reel e o take seguinte; so o que mudou, so uma vez por camara.
_claquete_enviado = {}          # id -> chave do que essa camara ja tem

_claquete_erro = {}             # id -> chave em que falhou (o erro regista-se uma vez)


def _claquete_bmpcc(cena, reel, take):
    for p in claquete.comandos_bm(cena, reel, take):
        write_camera(p)


def _claquete_alvos():
    """[(id, nome, enviar(cena, reel, take))] das camaras ligadas que aceitam claquete."""
    out = []
    if BMPCC_ACTIVE and state.get("camera_connected") and camera_char_path:
        out.append(("bmpcc", "BMPCC", _claquete_bmpcc))
    for cid, cam in list(bmfleet.cams.items()):
        if cam.connected():
            out.append((cid, cam.name, lambda c, r, t, cam=cam: [cam.escrever(p) for p in claquete.comandos_bm(c, r, t)]))
    for cid, cam in list(iphs.cams.items()):
        if cam.connected():
            out.append((cid, cam.name, lambda c, r, t, cam=cam: cam.set_next_slate(c, t, r)))
    return out


def _claquete_ciclo():
    while True:
        time.sleep(5)
        try:
            alvos = _claquete_alvos()
            ligados = {a[0] for a in alvos}
            for k in [k for k in _claquete_enviado if k not in ligados]:
                del _claquete_enviado[k]              # desligou: ao voltar recebe tudo outra vez
            if anything_recording():
                continue
            take = take_prevista()
            if not take:
                continue
            sl = state.get("slate") or {}
            cena, reel = sl.get("scene"), sl.get("reel")
            chave = claquete.chave(cena, reel, take)
            feitos = []
            for did, nome, enviar in alvos:
                if _claquete_enviado.get(did) == chave:
                    continue
                try:
                    enviar(cena, reel, take)
                    _claquete_enviado[did] = chave
                    _claquete_erro.pop(did, None)
                    feitos.append(nome)
                except Exception as e:                # pylint: disable=broad-except
                    if _claquete_erro.get(did) != chave:
                        _claquete_erro[did] = chave
                        log_event(f"Slate: {nome} did not take the next clip slate: {e}")
            if feitos:
                log_event("Slate for the next clip: %s take %d -> %s" % (
                    " ".join(x for x in ("scene " + cena if cena else "", "reel %s" % reel if reel else "") if x) or "",
                    take, ", ".join(feitos)))
        except Exception as e:                        # nunca deixa o fio morrer
            log_event(f"Slate: {e}")


def fechar_take_se_parado():
    """Se nada esta a gravar ha uns segundos, o take fecha-se (uma camara que caiu sem dizer `stop` nao pode deixar o
    take aberto para sempre: o REC seguinte tinha o mesmo numero)."""
    global _take_retomar
    _retomar_quem_grava()
    r = _take_retomar
    if r and time.time() > r["ate"]:
        # Ninguem voltou a gravar depois do reinicio: o take de antes fica fechado.
        _take_retomar = None
        if not _take_ativos:                    # ninguem voltou a gravar: o take de antes fica fechado
            _take_aberto_apagar()
            state["_take_fim"] = r["alive"]
    if anything_recording():
        state["_take_parado_desde"] = None
        with _take_lock:
            if _take_ativos:
                _take_aberto_gravar(state.get("_take_no"))     # "vivo": quanto tempo o bridge esteve em baixo
        return
    desde = state.get("_take_parado_desde") or time.time()
    state["_take_parado_desde"] = desde
    if time.time() - desde > 5:
        with _take_lock:
            if _take_ativos:
                _take_ativos.clear()
                state["_take_fim"] = time.time()
                _take_pendente_marcar(state.get("_take_no"))
                _take_aberto_apagar()


def settings_for_shot(source):
    """Av/Tv/ISO/... da camara que da o nome a esta linha do shoot log.

    Le o que o bridge ja tem em memoria: nenhum pedido a camara. So se a leitura
    das definicoes estiver ligada, porque com ela desligada os valores sao os
    ultimos lidos e podem estar antigos -- uma celula vazia diz a verdade ("nao
    sei") onde um valor velho mentia.
    """
    try:
        if source == "BMPCC":
            iso = state.get("iso")
            return {"iso": iso} if iso else {}
        for cam in list(canon_ccapi.CAMERAS.values()):
            if cam.name != source:
                continue
            if not canon_ccapi.settings_polling or not cam.state.get("connected"):
                return {}
            return {col: cam.state[key] for col, key in SHOOT_SETTING_KEYS.items()
                    if cam.state.get(key) is not None}
    except Exception:
        pass
    return {}


_ultimo_row_id = None


def _novo_row_id():
    """O id da proxima linha do shoot log: milissegundos desde 1970 como sempre (compativel com as linhas que ja la estao),
    mas nunca repetido -- duas linhas no mesmo milissegundo (varias camaras a confirmar juntas) ficavam com o mesmo id e
    editar/apagar uma mexia nas duas. Chama-se com o _shoot_log_lock."""
    global _ultimo_row_id
    if _ultimo_row_id is None:                  # na primeira vez, o maior id que o ficheiro ja tem (o relogio pode ter recuado)
        _ultimo_row_id = 0
        try:
            # errors="replace": um byte UTF-8 partido por um corte de energia nao pode rebentar aqui (e perder a linha
            # que se ia gravar); o que falhar a meio fica com o maior id ate ali e o relogio.
            with open(SHOOT_LOG_PATH, newline="", errors="replace") as f:
                for r in csv.DictReader(f):
                    try:
                        _ultimo_row_id = max(_ultimo_row_id, int(r.get("id") or 0))
                    except (TypeError, ValueError, OverflowError):
                        pass
        except Exception:
            pass
    _ultimo_row_id = max(int(time.time() * 1000), _ultimo_row_id + 1)
    return str(_ultimo_row_id)


def log_shot(source, event, note="", settings=None):
    """Append one row. Returns the row's id.

    Sem nota explicita guarda-se o timecode LTC do instante. E o que liga esta
    linha ao material: a hora de parede diz quando aconteceu, o timecode diz
    onde procurar nas gravacoes.
    """
    if not note:
        try:
            secs = ltc_secs_now()
            if secs is not None:
                note = "TC " + format_tc(secs, (state.get("_ltc_jam") or {}).get("fps") or 25)
        except Exception:
            pass
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    take = proxima_take(event, source)
    if settings is None and event in ("start", "stop", "fault"):
        settings = settings_for_shot(source)
    settings = settings or {}
    row_id = str(int(time.time() * 1000))              # so vale se a escrita falhar: o id verdadeiro sai dentro do lock
    try:
        with _shoot_log_lock:
            row_id = _novo_row_id()
            write_header = not os.path.exists(SHOOT_LOG_PATH)
            with open(SHOOT_LOG_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(SHOOT_LOG_FIELDS)
                w.writerow([row_id, ts, source, _ref_para(source), event, note, nome_da_sessao(), take, ""]
                           + [settings.get(k, "") for k in SHOOT_SETTING_FIELDS])
                # Force it to the card before returning. This file is the whole
                # reason the bridge exists — it is what lines multicam up in
                # post — and a take recorded but sitting in a page cache is
                # lost to the power cut that ends the shoot.
                f.flush()
                os.fsync(f.fileno())
    except Exception as e:
        log_event(f"Shoot log write failed: {e}")
    return row_id

# === FAULT FLAG (drives the 4-digit panel's blink rate) ===
# A flat battery is the failure that actually cost takes tonight — three of
# them, with no warning at all, while the bridge knew every level. Below this
# it raises the same fault the panel already flashes for.
BATTERY_WARN_PCT = 20

BATTERY_CLEAR_PCT = 26   # hysteresis, so a level hovering on the line is quiet

# Running out of card mid-take is the other way to lose one. The bodies report
# this; nothing was watching it.
CARD_WARN_SECS = 300     # 5 minutes of recording left

CARD_CLEAR_SECS = 480


def note_fault(reason, kind="fault"):
    """kind: "fault" (a take is at risk — yellow) or "warn" (act soon — orange).

    Severity has to be readable at a glance or the banner stops meaning
    anything: a flat battery you still have minutes to fix should not look
    identical to a take that just died.
    """
    # Um aviso nunca sobrepoe uma falha por dispensar. Os avisos disparam por
    # rotina -- bateria, cartao, temperatura, disco -- e sem isto o primeiro
    # deles apagava a falha antes de o operador a ver, que e exactamente o que
    # prender falhas existe para evitar. Falha sobre falha passa: fica a ultima.
    if kind == "warn" and state.get("_fault_time") and state.get("_fault_kind") == "fault":
        log_event(f"WARNING (not shown, a fault is pending): {reason}")
        return
    state["_fault_time"] = time.time()
    state["_fault_reason"] = reason
    state["_fault_kind"] = kind
    log_event(f"{'FAULT' if kind == 'fault' else 'WARNING'}: {reason}")

    # Uma falha entra tambem no shoot log. Sem isto o ficheiro que levas para
    # pos mostrava um 'stop' sem dizer que foi uma queda e nao uma decisao --
    # e e justamente essa a linha que interessa encontrar depois. Os avisos
    # ficam de fora: sao condicoes que se repetem e encheriam o ficheiro.
    if kind == "fault" and state.get("_last_logged_fault") != reason:
        state["_last_logged_fault"] = reason
        secs = ltc_secs_now()
        tc = format_tc(secs, (state.get("_ltc_jam") or {}).get("fps") or 25) if secs is not None else ""
        log_shot("SYSTEM", "fault", f"{reason}{(' | TC ' + tc) if tc else ''}")


def clear_fault():
    state["_last_logged_fault"] = None
    state["_fault_time"] = 0
    state["_fault_reason"] = ""
    state["_fault_kind"] = "fault"


def clear_warnings():
    """Limpa so o que for aviso. Uma falha so sai por accao do operador."""
    if state.get("_fault_kind") != "fault":
        clear_fault()


def check_bluez_stuck():
    """Detecta o BlueZ preso a dizer `Connected: yes` sem nenhuma ligacao real
    por baixo -- ver item 1 de ideias_por_fazer, custou uma hora a 2026-09-09.
    Nesse estado qualquer connect() fica pendurado: nao falha, nao recua, nao
    diz nada. So se detecta comparando as duas fontes que o bridge ja tem ao
    alcance -- o D-Bus (o que julgamos que esta ligado) contra `hcitool con`
    (o que o radio realmente tem em pe. NAO se corrige sozinho: reiniciar o
    bluetoothd por conta propria derruba tudo o que estiver ligado, e uma
    correccao automatica que estraga uma tomada e pior do que a avaria. So
    avisa, uma vez por condicao, para o operador carregar em RESTART
    BLUETOOTH.
    """
    warned = state.setdefault("_batt_warned", {})
    dispositivos = (
        ("bt_bmpcc", "BMPCC", CAMERA_ADAPTER, CAMERA_ADDRESS, state.get("camera_connected")),
    )
    macs_por_adaptador = {}
    for chave, nome, adapter, address, achamos_ligado in dispositivos:
        # A BMPCC pelo gateway C3 nao usa o Bluetooth do Atom: o hci0 nunca a tem, e o aviso era falso.
        if not achamos_ligado or (chave == "bt_bmpcc" and c3_bm.ativo()):
            warned.pop(chave, None)
            continue
        if adapter not in macs_por_adaptador:
            try:
                saida = _run(["hcitool", "-i", adapter, "con"], timeout=5).stdout
                macs_por_adaptador[adapter] = {
                    m.upper() for m in
                    re.findall(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", saida)}
            except Exception:
                macs_por_adaptador[adapter] = None
        macs = macs_por_adaptador[adapter]
        if macs is None:
            continue  # hcitool falhou -- nao sabemos, nao acusar
        if address.upper() in macs:
            warned.pop(chave, None)
            continue
        if not warned.get(chave):
            warned[chave] = True
            note_fault(
                f"{nome}: BlueZ says connected but the radio ({adapter}) has "
                f"no link at all — any action will hang. "
                f"Use RESTART BLUETOOTH.",
                kind="warn")


_lig_ant = {}           # camara -> (ligada, a gravar) na volta anterior

_lig_incerta = set()    # camaras que cairam a gravar e ainda nao voltaram


def vigiar_ligacoes():
    """Canons e iPhones: quando uma cai a gravar, ou volta, fica no shoot log (para a pos saber que houve um buraco
    na ligacao a meio do take). A BMPCC, as BMPCC extra e as GoPro fazem-no no seu proprio codigo."""
    vistas = []
    for cid, cam in list(canon_ccapi.CAMERAS.items()):
        vistas.append((cid, cam.name, bool(cam.state.get("connected")), bool(cam.state.get("recording"))))
    for cid, cam in list(iphs.cams.items()) + list(sonys.cams.items()):
        vistas.append((cid, cam.name, bool(cam.connected()), bool(cam.running())))
    for cid, nome, lig, rec in vistas:
        antes = _lig_ant.get(cid)
        _lig_ant[cid] = (lig, rec)
        if antes is None:
            continue
        if antes[0] and not lig and (antes[1] or rec) and cid not in _lig_incerta:
            _lig_incerta.add(cid)
            log_shot(nome, "fault", "link lost while recording")
            note_fault("%s dropped while recording" % nome)
        elif not antes[0] and lig and cid in _lig_incerta:
            _lig_incerta.discard(cid)
            log_shot(nome, "fault" if rec else "stop",
                     "link back: still recording" if rec else "stopped while the link was down")


BRIDGE_RSS_AVISO_MB = 500     # o bridge parado anda pelos 55 MB (medido sem Bluetooth): 500 e uma fuga


def check_health():
    """Raise a warning once per condition, not every ten seconds."""
    warned = state.setdefault("_batt_warned", {})

    def check(key, name, pct, on_ac=False):
        if pct is None or pct < 0 or on_ac:
            warned.pop(key, None)
            return
        if pct <= BATTERY_WARN_PCT:
            if not warned.get(key):
                warned[key] = True
                note_fault(f"{name}: battery {pct}%", kind="warn")
        elif pct >= BATTERY_CLEAR_PCT:
            warned.pop(key, None)

    if state.get("camera_connected"):
        check("bmpcc", "BMPCC", state.get("battery_percent"),
              state.get("power_source") == 1)
    else:
        warned.pop("bmpcc", None)
    for cid, cam in list(canon_ccapi.CAMERAS.items()):
        if cam.state["connected"]:
            check(cid, cam.name, cam.state["battery_percent"])
        else:
            warned.pop(cid, None)
    for cid, cam in list(bmfleet.cams.items()):
        if cam.connected() and cam.battery is not None:
            check(cid, cam.name, cam.battery)
        else:
            warned.pop(cid, None)
    for cid, so in sonys.status().items():
        check(cid, so["name"], so.get("battery"))      # desligada: battery None, o aviso sai

    # Card space. The Canons report seconds left in movie mode; the BMPCC's
    # figure is per slot and we watch the one actually being recorded to.
    def check_card(key, name, secs):
        if secs is None or secs <= 0:
            warned.pop(key, None)
            return
        if secs <= CARD_WARN_SECS:
            if not warned.get(key):
                warned[key] = True
                note_fault(f"{name}: card has {secs // 60}min left", kind="warn")
        elif secs >= CARD_CLEAR_SECS:
            warned.pop(key, None)

    for cid, cam in list(canon_ccapi.CAMERAS.items()):
        check_card(f"card_{cid}", cam.name,
                   cam.state.get("remaining_time") if cam.state["connected"] else None)
    for cid, so in sonys.status().items():
        check_card(f"card_{cid}", so["name"], so.get("remaining_s"))
    if state.get("camera_connected"):
        act = state.get("active_slot") or 1
        check_card("card_bmpcc", "BMPCC", state.get(f"_slot{act}_secs"))
    else:
        warned.pop("card_bmpcc", None)

    try:
        st = os.statvfs("/")
        free_mb = st.f_bavail * st.f_frsize // (1024 * 1024)
        if free_mb < 300:
            if not warned.get("disk"):
                warned["disk"] = True
                note_fault(f"System disk: {free_mb}MB free", kind="warn")
        elif free_mb > 600:
            warned.pop("disk", None)
    except Exception:
        pass

    # A memoria do proprio bridge: sem MemoryMax no servico (matava-o a meio de uma take), uma fuga ve-se aqui
    # antes de o Atom comecar a trocar para o disco e os REC atrasarem.
    try:
        with open("/proc/self/status") as f:
            rss_mb = next(int(l.split()[1]) for l in f if l.startswith("VmRSS:")) // 1024
        if rss_mb > BRIDGE_RSS_AVISO_MB:
            if not warned.get("rss"):
                warned["rss"] = True
                note_fault(f"Bridge memory {rss_mb}MB: restart the bridge between takes", kind="warn")
        elif rss_mb < BRIDGE_RSS_AVISO_MB * 0.8:
            warned.pop("rss", None)
    except (OSError, StopIteration, ValueError):
        pass

    # O proprio Atom: carregador desligado e bateria fraca (o bridge morre com ele, a meio de um take).
    try:
        pw = _read_power()
        bat = pw.get("battery") or {}
        pct, mins = bat.get("percent"), bat.get("minutes")
        if pw.get("ac") is False:
            if not warned.get("atom_ac"):
                warned["atom_ac"] = True
                note_fault("Atom: charger unplugged, on battery%s" % (" %d%%" % pct if pct is not None else ""), kind="warn")
            fraca = (pct is not None and pct <= 20) or (mins is not None and mins <= 20)
            if fraca and not warned.get("atom_low"):
                warned["atom_low"] = True
                note_fault("Atom battery low: %s%%%s" % (pct if pct is not None else "?",
                                                         " (~%d min)" % mins if mins is not None else ""))
        elif pw.get("ac"):
            warned.pop("atom_ac", None)
            warned.pop("atom_low", None)
    except Exception:
        pass

    # Timecode de cada camara contra o LTC do Atom: mais de um frame de desvio (descontado o erro da medida) e aviso.
    fps = (state.get("_ltc_jam") or {}).get("fps") or 25
    for cid, dv in desvios_tc().items():
        mau = tc_vigia.desvio_a_mais(dv["offset"], dv["err"], fps)
        if mau and not warned.get("tc_" + cid):
            warned["tc_" + cid] = True
            note_fault("%s: timecode %+.2f s off the Atom LTC (+-%.2f s): jam it again" % (
                dv["name"], dv["offset"], dv["err"]), kind="warn")
        elif not mau:
            warned.pop("tc_" + cid, None)

    # Memoria dos C3 (o principal e os das BMPCC extra): baixa, o Wi-Fi ou o Bluetooth deixam de arrancar.
    _c3e = c3_bm.estado()
    memorias = [("c3", "C3", _c3e.get("mem") if _c3e.get("reachable") else None)] + \
        [("c3_" + cid, "C3 of %s" % cam.name, cam.mem) for cid, cam in list(bmfleet.cams.items())]
    for chave, nome, mem in memorias:
        txt = c3_bm.memoria_baixa(mem)
        if txt and not warned.get("mem_" + chave):
            warned["mem_" + chave] = True
            note_fault(f"{nome}: {txt}", kind="warn")
        elif not txt:
            warned.pop("mem_" + chave, None)

    check_bluez_stuck()



def _on_canon_recording_change(name, recording):
    log_shot(name, "start" if recording else "stop")
    if recording:
        # Uma take nova limpa avisos, mas NAO a falha anterior: o operador tem
        # de a ver e dispensar, senao arriscava-se a nunca saber que existiu.
        clear_warnings()

canon_ccapi.on_recording_change = _on_canon_recording_change

canon_ccapi.on_fault = note_fault


# Thermal states, in the order they bite. Anything that restricts or disables
# recording is a fault; a plain warning is still time to act.
def on_canon_temperature(name, status):
    bad = any(k in status for k in ("restriction", "disable", "frameratedown"))
    note_fault(f"{name}: temperatura — {status}", kind="fault" if bad else "warn")

canon_ccapi.on_temperature = on_canon_temperature

# "Is anyone actually looking?" — the web UI polls /api/status and
# /api/devices every 1-1.5s, so a recent poll means a phone is open in
# front of someone. No poll for a while means the rig is idle and the Canon
# bodies can be left alone.
UI_WATCH_WINDOW = 12


def ui_is_watching():
    return (time.time() - state.get("_last_ui_poll", 0)) < UI_WATCH_WINDOW

canon_ccapi.activity_hint = ui_is_watching

# === TIME SOURCE PRIORITY: BMPCC timecode > NTP > nothing ===
# The BMPCC is fed LTC by a Deity TC-1, so whenever that is running it is the
# clock the entire shoot will be cut against — which makes it a better
# reference than NTP for stamping the Canon bodies, not a worse one. It also
# works at a venue with no internet, which is exactly where NTP leaves you
# with nothing (there is no RTC on this box).
#
# LTC carries no date, only time of day, so the date still comes from the
# system clock. And a frozen timecode is not a clock: we require it to have
# actually CHANGED recently, not merely to have arrived.
TC_FRESH_SECONDS = 5

# A timecode generator can be jammed to time of day, or free-running from an
# arbitrary start. Only the first is a clock. When NTP is available we can tell
# them apart by comparing; if they disagree by more than this, the timecode is
# not wall-clock time and must not be written into any camera.
TC_AGREE_SECONDS = 180

# LTC lido directamente do gerador por entrada de audio USB. E a fonte
# principal: ao contrario do timecode da BMPCC, nao exige que a camara esteja
# ligada por BLE, e permite acertar as Canon, que nao tem entrada de timecode.
# Depois do jam o gerador pode sair da sala; o desvio guardado e que manda.
# O relogio de sistema e disciplinado por NTP (e o Atom tem RTC), por isso
# nao foge enquanto houver rede. Sem rede, convem voltar a fazer jam.
#
# Mesmo limiar para as tres fontes com fetch manual (LTC/NTP/PHONE) desde
# 2026-09-12, pedido do operador para uniformizar o aviso "isto e velho,
# vai buscar outra vez" em qualquer botao de fonte -- so o aviso visual, nao
# a janela de confianca que decide se se pode escrever numa camara
# (PHONE_TRUST_HOURS, abaixo, continua as 12h que sempre foram).
STALE_WARNING_HOURS = 6


def jam_clock_stepped():
    """O relogio saltou desde o jam? Entao o desvio guardado nao vale.

    O jam guarda a DIFERENCA para o relogio do Atom. Se o NTP corrigir a hora a
    meio, essa diferenca passa a apontar para o sitio errado -- foi o que
    aconteceu quando o relogio deste Pi foi corrigido em 1h14m. Comparamos o
    afastamento entre o relogio de parede e o monotonico, que so muda quando a
    hora e mesmo alterada.

    Num arranque novo o monotonico volta a zero e nao da para comparar, por isso
    nesse caso nao invalidamos: a hora de parede continua ancorada ao NTP.
    """
    return jam_invalid_reason() is not None


def _boot_id():
    """Identificador do arranque actual, ou None se nao der para ler.

    E o que distingue "o relogio saltou" de "o Pi reiniciou". A comparacao com
    o monotonico nao chega: so denuncia o reinicio enquanto o uptime for MENOR
    que o monotonico guardado no jam, e passado esse ponto um reinicio passa a
    parecer um salto de relogio. Visto a serio 2026-09-07 -- jam com mono=508,
    Pi com 3000s de uptime, e o jam foi dado como saltado sem nada ter saltado.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


# NTP manda desde 2026-10-07 e pode dar saltos pequenos ao relogio (0,7 s ao
# liga-lo). O TC-1 nao mexeu, por isso o jam nao se deita fora: o desvio
# guardado acompanha o salto. Parede - monotonico so muda quando a hora e
# alterada (os ajustes lentos do NTP mexem nos dois por igual), por isso
# abaixo disto nao ha salto nenhum. Acima de 2 s continua a invalidar: ai
# pode ser suspensao ou um relogio acertado a mao, e nao se sabe o que foi.
JAM_REANCHOR_SECONDS = 0.02

_jam_lock = threading.Lock()


def _jam_reanchor(j):
    with _jam_lock:
        # Recalcular dentro do lock: duas threads a ver o mesmo salto
        # descontavam-no duas vezes.
        passo = (time.time() - time.monotonic()) - j["clock_ref"]
        if abs(passo) <= JAM_REANCHOR_SECONDS or abs(passo) > 2.0:
            return
        j["offset"] = (j["offset"] - passo + 43200) % 86400 - 43200
        j["at"] += passo
        j["clock_ref"] += passo
    save_settings()
    log_event(f"Clock stepped {passo:+.3f}s (NTP) — LTC jam re-anchored, "
              f"offset now {j['offset']:+.3f}s")


# LTC sem NTP: o Atom tem relogio de hardware (RTC), ao contrario do Pi.
RTC_PATH = "/sys/class/rtc/rtc0/since_epoch"

TIMESYNC_CLOCK = "/var/lib/systemd/timesync/clock"      # o timesyncd toca-lhe ao desligar: a ultima hora boa


def rtc_trustworthy(desde=None):
    """O relogio de hardware serve para manter um jam depois de um reinicio sem NTP? (True/False)

    `desde`: a hora (time.time()) do jam; o relogio de agora nao pode estar atras dela."""
    agora = time.time()
    try:
        with open(RTC_PATH) as f:
            rtc = int(f.read().strip())
    except (OSError, ValueError):
        return False                                  # sem RTC (ou ilegivel): como no Pi
    if time.gmtime(rtc).tm_year < 2025 or abs(rtc - agora) > 5:
        return False                                  # pilha gasta, ou RTC em hora local
    if desde is not None and agora < desde:
        return False                                  # o relogio esta atras do jam: esta errado
    try:
        if agora < os.path.getmtime(TIMESYNC_CLOCK) - 2:
            return False                              # atras da ultima hora boa que se guardou
    except OSError:
        pass
    return True


def jam_invalid_reason():
    """Porque e que o desvio guardado no jam nao serve, ou None se servir."""
    j = state.get("_ltc_jam")
    if not j or "clock_ref" not in j:
        return None
    # Jams antigos nao trazem boot_id; para esses fica so o teste do monotonico,
    # que e o que havia antes.
    booted = j.get("boot_id")
    if time.monotonic() < j.get("mono", 0) or (booted and booted != _boot_id()):
        # Reiniciou: o monotonico voltou a zero e nao ha base de comparacao.
        # O jam so continua a valer se a hora de parede for de confianca. Sem RTC
        # valido e sem rede, o sistema arranca com uma data estimada -- no Pi de antes
        # arrancou 1h14m errado. Extrapolar o jam a partir
        # dessa hora daria um tempo decorrido errado, ou ate negativo.
        ok, _ = canon_ccapi.clock_is_trustworthy()
        if not ok and rtc_trustworthy(j.get("at")):
            # Sem NTP (INT WI-FI em baixo), o relogio de hardware do Atom segura o jam, a +-1 s (o RTC so
            # guarda segundos): fica "aproximado" ate se fazer GET LTC outra vez.
            ok = True
            j["approx"] = "rtc"
        if not ok:
            return "no trusted clock since reboot"
        # O jam passa a ser deste arranque: um salto do relogio a seguir volta a ser detectado.
        j["boot_id"], j["mono"] = _boot_id(), time.monotonic()
        j["clock_ref"] = time.time() - time.monotonic()
        return None
    passo = (time.time() - time.monotonic()) - j["clock_ref"]
    if abs(passo) > 2.0:
        return "the clock stepped after the LTC was read"
    if abs(passo) > JAM_REANCHOR_SECONDS:
        _jam_reanchor(j)
    return None


def ltc_secs_at(when=None):
    """Segundos do dia em LTC num dado instante, ou None se nao houver jam.

    A fraccao interessa: e dela que sai o numero do frame -- no contador da UI
    e no timecode que se regista para a claquete.
    """
    j = state.get("_ltc_jam")
    if not j or jam_clock_stepped():
        return None
    t = time.time() if when is None else when
    lt = time.localtime(t)
    return (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
            + (t % 1) + j["offset"]) % 86400


def ltc_secs_now():
    return ltc_secs_at()


def format_tc(secs, fps):
    n = int(secs * fps + 1e-6) % (86400 * fps)
    return "%02d:%02d:%02d:%02d" % (n // (fps * 3600), n // (fps * 60) % 60,
                                    n // fps % 60, n % fps)


def ltc_epoch_now():
    """Instante absoluto vindo do LTC, ou None se o jam nao trouxe data.

    Quando o gerador manda a data nos user bits, o jam deixa de precisar do
    relogio do Atom para saber o DIA: ancora-se no instante absoluto do jam e
    extrapola dai. Isto importa porque, sem RTC valido e sem rede, o sistema
    arranca com uma data estimada, e escrever o dia
    errado nos metadados e o erro que mais estraga a organizacao em pos.
    """
    j = state.get("_ltc_jam")
    if not j or jam_clock_stepped():
        return None
    d, secs = j.get("date"), j.get("secs")
    if not d or secs is None:
        return None
    hh, mm, ss = int(secs // 3600), int(secs % 3600 // 60), int(secs % 60)
    try:
        base = time.mktime((d[0], d[1], d[2], hh, mm, ss, 0, 0, -1)) + (secs % 1)
    except (ValueError, OverflowError):
        return None
    return base + (time.time() - j["at"])


def ltc_time_of_day():
    """time.struct_time derivada do jam, ou None se nunca foi feito jam."""
    e = ltc_epoch_now()
    if e is not None:
        return time.localtime(e)          # data E hora, ambas do gerador
    # Sem data nos user bits: hora do LTC com a data do sistema, como antes.
    secs = ltc_secs_now()
    if secs is None:
        return None
    lt = time.localtime()
    hh, mm, ss = int(secs // 3600), int(secs % 3600 // 60), int(secs % 60)
    return time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday,
                             hh, mm, ss, lt.tm_wday, lt.tm_yday, lt.tm_isdst))


def timecode_time_of_day():
    """time.struct_time from the BMPCC's live timecode, or None."""
    if not state.get("camera_connected"):
        return None
    if time.time() - state.get("_last_tc_change", 0) > TC_FRESH_SECONDS:
        return None
    parts = (state.get("timecode") or "").split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    hh, mm, ss = (int(p) for p in parts)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    if (hh, mm, ss) == (0, 0, 0):
        return None      # free-run counter not yet jammed to time of day
    now = time.localtime()
    cand = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday,
                             hh, mm, ss, now.tm_wday, now.tm_yday, now.tm_isdst))
    # Cross-check against NTP when we have it. A free-running generator reads a
    # plausible-looking time that is nothing to do with the wall clock — seen
    # for real: a TC-1 counting from midnight said 00:37 at 09:01, and the
    # cameras would have been set to yesterday.
    ok, _ = canon_ccapi.clock_is_trustworthy()
    if ok:
        drift = abs(time.mktime(cand) - time.time())
        drift = min(drift, 86400 - drift)      # wrap at midnight
        if drift > TC_AGREE_SECONDS:
            state["_tc_drift"] = int(drift)
            return None
    state["_tc_drift"] = 0
    return cand

CLOCK_SOURCE_LABELS = {"bm": "BM TC", "ltc": "LTC",
                       "ntp": "NTP", "iphone": "iPhone"}

NTP_MARKER = "/run/systemd/timesync/synchronized"


def _fonte(ok, desde=None):
    """Uma entrada de clock_sources(): tenho? desde quando? ja e velha?"""
    if not ok:
        return {"ok": False, "age": None, "stale": False}
    if desde is None:
        return {"ok": True, "age": None, "stale": False}
    idade = max(0, int(time.time() - desde))
    return {"ok": True, "age": idade,
            "stale": idade > STALE_WARNING_HOURS * 3600}


def clock_sources():
    """Estado das quatro fontes, independente de qual esta escolhida.

    Independente de proposito: a interface acende o interior de todas as que
    tem valor e a cercadura so na que esta em uso. Sem isto, fazer GET LTC com
    o NTP escolhido nao dava sinal nenhum de que a leitura tinha resultado.
    """
    jam = state.get("_ltc_jam")
    tel = state.get("_phone_adopted_at")
    return {
        "bm": _fonte(timecode_time_of_day() is not None),
        "ltc": _fonte(ltc_time_of_day() is not None,
                      jam.get("at") if jam else None),
        "ntp": _fonte(os.path.exists(NTP_MARKER), ntp_synced_at()),
        "iphone": _fonte(bool(tel) and time.time() < canon_ccapi.manual_trust_until,
                         tel),
    }


def preferred_time_source():
    """A hora a escrever nas camaras, ou None para nao escrever nada.

    So as fontes de timecode dao aqui uma hora; as de parede devolvem None
    porque nesse caso o canon_ccapi usa o relogio do proprio Atom. Quem decide
    se HA fonte e o selected_source_ok() -- sem ele, uma fonte de timecode
    indisponivel caia em silencio para a hora de parede, que e exactamente o
    fallback automatico que o operador nao quer.
    """
    pref = state.get("clock_source_pref")
    if pref == "bm":
        return timecode_time_of_day()
    if pref == "ltc":
        return ltc_time_of_day()
    return None                      # ntp / iphone: relogio de parede do Atom


def selected_source_ok():
    """(ha fonte?, etiqueta) para a fonte escolhida. E o portao das escritas.

    O sync_datetime() das Canon e o sync_bmpcc_clock() chamam isto em vez do
    clock_is_trustworthy() precisamente para nao haver fallback: escolher
    CLK->BM e a camara cair nao pode passar a escrever a hora de parede nas
    camaras sem dizer nada a ninguem.
    """
    pref = state.get("clock_source_pref")
    if not pref:
        return False, "none picked"
    fonte = clock_sources().get(pref) or {"ok": False}
    return bool(fonte["ok"]), CLOCK_SOURCE_LABELS.get(pref, pref)


canon_ccapi.time_source_hook = preferred_time_source

canon_ccapi.clock_gate_hook = selected_source_ok

_ssid_cache = {"v": None, "at": 0.0}


def uplink_ssid(ttl=60):
    """SSID do uplink, com cache.

    O /api/net/status evita isto de proposito por ser um shell-out caro a cada
    segundo. Aqui basta-nos saber por que rede e que o NTP entrou, e isso nao
    muda de segundo a segundo -- uma leitura por minuto chega.
    """
    # uplink_ssid
    # Sem rede na ultima leitura: tenta de novo daqui a 5 s. No arranque o bridge sobe antes do
    # Wi-Fi, e guardar "sem rede" um minuto deixava o dashboard com o INT WI-FI em "none" ja ligado.
    now = time.time()
    if now - _ssid_cache["at"] < (ttl if _ssid_cache["v"] else min(ttl, 5)):
        return _ssid_cache["v"]
    v, out = None, ""
    try:
        # O iw vem com o bridge (install_atom.sh); o iwgetid (wireless-tools) pode nao estar instalado.
        out = _run(["iw", "dev", UPLINK_IFACE, "link"], timeout=4).stdout or ""
        v = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                  if l.strip().startswith("SSID:")), None) or None
    except Exception:
        out = ""
    if v is None and not out.strip().startswith("Not connected"):
        try:
            v = (_run(["iwgetid", UPLINK_IFACE, "-r"], timeout=4).stdout or "").strip() or None
        except Exception:
            pass
    _ssid_cache.update(v=v, at=now)
    return v


_wlan_cache = {"v": (None, None), "at": 0.0}


def wlan_radio(ttl=30):
    """(modo, MHz) do radio da ps-uplink, com cache. Modo e "managed" ou "ap".

    Serve o aviso de banda, que vive no /api/status e portanto corre a cada
    segundo -- dai a cache. Um radio de 2,4 GHz divide o ar com o Bluetooth: a
    ps-uplink no canal 6 a centimetros do dongle derrubava a BMPCC de 16 em 16
    segundos, e passar para 5 GHz aguentou-a cinco minutos seguidos. E uma
    condicao que se quer ver antes de comecar, nao depois de perder uma tomada.

    Le-se o `info` e nao o `link` de proposito: quando a ps-uplink sobe como AP
    PS_Setup deixa de haver ligacao para o `link` mostrar, e era exactamente
    assim que um AP de 2,4 GHz no MESMO silicio do Bluetooth passava
    despercebido -- o pior caso a ficar calado. O `info` da o tipo e o canal
    nos dois modos.
    """
    now = time.time()
    if now - _wlan_cache["at"] < ttl:
        return _wlan_cache["v"]
    mode, mhz = None, None
    try:
        r = _run(["iw", "dev", UPLINK_IFACE, "info"], timeout=4).stdout or ""
        for line in r.splitlines():
            line = line.strip()
            if line.startswith("type "):
                mode = line.split(None, 1)[1].strip().lower()
            elif line.startswith("channel "):
                # "channel 44 (5220 MHz), width: 80 MHz, center1: 5210 MHz"
                for tok in line.replace("(", " ").replace(")", " ").split():
                    if tok.isdigit() and int(tok) > 1000:
                        mhz = int(tok)
                        break
    except Exception:
        pass
    if mhz is None and _wlan_cache["v"][1] is not None:
        # Uma leitura falhada NAO apaga uma boa. O `iw` tem 4s de limite e
        # falha de vez em quando; guardar (None, None) escondia o aviso de
        # 2,4 GHz durante 30 segundos por causa de um solucao, com o operador a
        # continuar na mesma rede. Um aviso que se apaga sozinho e pior do que
        # nenhum, porque ensina a nao confiar nele.
        _wlan_cache["at"] = now - ttl // 2      # tentar de novo mais cedo
        return _wlan_cache["v"]
    _wlan_cache["v"] = (mode, mhz)
    _wlan_cache["at"] = now
    return _wlan_cache["v"]


def ntp_synced_at():
    """Quando o systemd-timesyncd sincronizou pela ultima vez, ou None."""
    try:
        return os.stat("/run/systemd/timesync/synchronized").st_mtime
    except Exception:
        return None


def effective_clock_source():
    """A fonte em uso agora, no vocabulario que a interface ja fala.

    Devolve os nomes antigos (bmtc/ltc/ntp/phone/tc-none/tc-stepped/none) de
    proposito: sao os que o contador, as etiquetas, o /view e o shoot log
    leem. Mudar a SELECCAO para quatro fontes explicitas nao e razao para
    renomear tudo isso ao mesmo tempo.
    """
    pref = state.get("clock_source_pref")
    if not pref:
        # Estado de arranque: ninguem escolheu ainda. E diferente de "escolhi
        # e nao ha" -- este nao e uma avaria, e um passo que falta dar.
        return "unset"
    ok = (clock_sources().get(pref) or {}).get("ok")
    if pref == "bm":
        return "bmtc" if ok else "tc-none"
    if pref == "ltc":
        if ok:
            return "ltc"
        return "tc-stepped" if jam_clock_stepped() else "tc-none"
    if pref == "iphone":
        return "phone" if ok else "none"
    return "ntp" if ok else "none"

canon_ccapi.time_source_label = effective_clock_source


def notify_watchdog():
    """Tell systemd we are still alive.

    Restart=always only catches a process that DIES. One that wedges — a BLE
    call that never returns, a deadlock — keeps the unit "active" for ever
    while nothing works, which on a shoot is worse than a crash because
    nothing recovers. With WatchdogSec set, missing these pings gets us killed
    and restarted.
    """
    sock = os.environ.get("NOTIFY_SOCKET")
    if not sock:
        return
    try:
        import socket as _sock
        addr = "\0" + sock[1:] if sock.startswith("@") else sock
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as sk:
            sk.connect(addr)
            sk.sendall(b"WATCHDOG=1")
    except Exception:
        pass


# === PERSISTED OPERATOR CHOICES ===
# rec_target, the clock source and the settings-polling switch all used to
# reset on every start — and with Restart=always, a crash mid-shoot silently
# changed which cameras REC drives. Written atomically because this box
# can lose power at any moment.
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# A escolha de fonte de relogio NAO entra aqui: cada arranque comeca sem
# fonte nenhuma e e o operador que escolhe. Ver "clock_source_pref".
PERSISTED_KEYS = ("_ltc_jam", "rec_targets", "led_on", "strict_rec", "slate", "web_remote", "take_info")

_settings_lock = threading.Lock()                # varias rotas gravam as definicoes ao mesmo tempo (cada uma com o seu .tmp)


def save_settings():
    try:
        with _settings_lock:
            data = {k: state.get(k) for k in PERSISTED_KEYS}
            data["settings_polling"] = canon_ccapi.settings_polling
            tmp = gravar.tmp_unico(SETTINGS_PATH)
            try:
                with open(tmp, "w") as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                gravar.trocar(tmp, SETTINGS_PATH)  # com o .bak do anterior (ler_dict cai nele se o ficheiro vier estragado)
            finally:
                try:
                    os.remove(tmp)                 # so resta se algo falhou antes do replace
                except OSError:
                    pass
    except Exception as e:
        log_event(f"Could not save settings: {e}")


def ler_dict(caminho):
    """O JSON de `caminho` como dict. Se o ficheiro faltar, estiver estragado ou for outra coisa (uma lista, um texto...),
    o do .bak; senao None. Um ficheiro de configuracao com o tipo errado nunca pode deitar o bridge abaixo no arranque
    (o servico reiniciava-se em ciclo)."""
    for p in (caminho, caminho + ".bak"):
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(d, dict):
            return d
    return None


# O tipo que cada chave do settings.json tem de ter; o que vier de outro tipo ignora-se (fica o valor por defeito).
_SETTINGS_TIPOS = {"_ltc_jam": dict, "rec_targets": list, "led_on": bool, "strict_rec": bool, "slate": dict,
                   "web_remote": bool, "take_info": dict}


def load_settings():
    data = ler_dict(SETTINGS_PATH)
    if data is None:
        return
    for k in PERSISTED_KEYS:
        v = data.get(k)
        if v is not None and isinstance(v, _SETTINGS_TIPOS[k]):
            if k == "rec_targets":
                v = [t for t in v if isinstance(t, str)]
            state[k] = v
    # A escolha de fonte de relogio nao se le daqui: cada arranque comeca sem
    # fonte, e e o operador que escolhe. Ficheiros de settings antigos ainda
    # trazem a chave e sao simplesmente ignorados -- ela nao esta nos
    # PERSISTED_KEYS, por isso nunca volta a ser escrita.
    # Older settings files stored a single rec_target; carry it forward.
    if data.get("rec_targets") is None and isinstance(data.get("rec_target"), str) and data.get("rec_target"):
        old_t = data["rec_target"]
        state["rec_targets"] = (["bmpcc", *canon_ccapi.CAMERAS.keys()]
                                if old_t == "all" else [old_t])
    if "settings_polling" in data:
        canon_ccapi.settings_polling = bool(data["settings_polling"])
    apply_led(bool(state.get("led_on", True)))
    log_event(f"Settings restored: rec targets={','.join(state.get('rec_targets') or [])}, "
              f"clock=none picked yet, "
              f"LED={'on' if state.get('led_on', True) else 'off'}")


# === ATOM HEALTH ===
def _read_power():
    """Carregador e bateria do portatil, de /sys/class/power_supply."""
    import glob

    def ler(p, conv=str):
        try:
            with open(p) as f:
                return conv(f.read().strip())
        except Exception:
            return None

    ac, bat = None, None
    for d in sorted(glob.glob("/sys/class/power_supply/*")):
        tipo = ler(d + "/type")
        if tipo == "Mains":
            ac = (ler(d + "/online", int) == 1) if ac is None else (ac or ler(d + "/online", int) == 1)
        elif tipo == "Battery" and bat is None and ler(d + "/present", int) != 0:
            def um(*nomes, d=d):
                # O primeiro que existir (um 0 conta: bateria vazia e um valor).
                for n in nomes:
                    v = ler(d + "/" + n, int)
                    if v is not None:
                        return v
                return None
            # energy_* (uWh) ou charge_* (uAh), conforme o firmware da bateria.
            agora = um("energy_now", "charge_now")
            cheio = um("energy_full", "charge_full")
            novo = um("energy_full_design", "charge_full_design")
            ritmo = um("power_now", "current_now")
            pct = ler(d + "/capacity", int)
            if pct is None and agora and cheio:
                pct = int(round(100.0 * agora / cheio))
            estado = ler(d + "/status") or "Unknown"
            minutos = None
            if ritmo and agora is not None and cheio:
                if estado == "Discharging":
                    minutos = int(60.0 * agora / ritmo)
                elif estado == "Charging":
                    minutos = int(60.0 * max(0, cheio - agora) / ritmo)
            bat = {"percent": pct, "status": estado,
                   "health": int(round(100.0 * cheio / novo)) if cheio and novo else None,
                   "minutes": minutos}
    return {"ac": ac, "battery": bat}


def decode_bcd_timecode(data):
    """Decode BCD timecode from camera notification packet"""
    if len(data) < 12:
        return "--:--:--:--"
    # Timecode is in bytes 8-11 of the 12-byte packet
    # byte 8 = frames (BCD), byte 9 = seconds (BCD), byte 10 = minutes (BCD), byte 11 = hours (BCD)
    ss = ((data[9] >> 4) & 0x0F) * 10 + (data[9] & 0x0F)
    mm = ((data[10] >> 4) & 0x0F) * 10 + (data[10] & 0x0F)
    hh = ((data[11] >> 4) & 0x0F) * 10 + (data[11] & 0x0F)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"

_desvio_bmpcc = tc_vigia.Desvio()


def timecode_fd_loop(fd):
    """Read timecode notifications from file descriptor"""
    import os
    import select
    last_tc = ""
    _desvio_bmpcc.limpar()                  # ligacao nova: as amostras antigas nao valem
    while state["camera_connected"]:
        try:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                raw = os.read(fd, 64)
                if raw:
                    tc = decode_bcd_timecode(raw)
                    if tc != last_tc:
                        state["timecode"] = tc
                        state["_last_tc_change"] = time.time()
                        last_tc = tc
                        _desvio_bmpcc.tc_mudou(tc, state["_last_tc_change"], ltc_secs_at(state["_last_tc_change"]))
                    state["_last_tc_time"] = time.time()
                else:
                    # fd closed by remote
                    break
        except Exception as e:
            print(f"TC fd error: {e}")
            break
    try:
        os.close(fd)
    except OSError:
        pass
    # Signal connection lost if we were supposed to be connected
    if state["camera_connected"]:
        log_event("Timecode stream lost — camera may have disconnected")
        _handle_camera_drop()


def start_timecode_polling():
    """Acquire timecode notifications via D-Bus AcquireNotify"""
    global timecode_char_path
    timecode_char_path = find_char_path_by_uuid(TIMECODE_UUID, CAMERA_ADAPTER)
    if not timecode_char_path:
        log_event("Timecode characteristic not found")
        return

    char = None
    try:
        bus = dbus.SystemBus()
        char = dbus.Interface(
            bus.get_object("org.bluez", timecode_char_path),
            "org.bluez.GattCharacteristic1"
        )
        fd, mtu = char.AcquireNotify({})
        fd = fd.take()  # Get the underlying file descriptor number
        _register_notify_fd(fd)
        log_event(f"Timecode acquired (fd={fd}, mtu={mtu})")
        t = threading.Thread(target=timecode_fd_loop, args=(fd,), daemon=True)
        t.start()
    except Exception as e:
        log_event(f"Timecode acquire error: {e}")
        # Fallback: try StartNotify — but only if we actually got as far as
        # building the characteristic proxy. Calling it unconditionally raised
        # NameError here whenever the failure came from get_object/Interface,
        # masking the real error.
        if char is None:
            return
        try:
            char.StartNotify()
            log_event("Timecode: fell back to StartNotify (may not update)")
        except Exception as e2:
            log_event(f"Timecode StartNotify also failed: {e2}")


# === CAMERA (D-Bus) ===
def find_char_path_by_uuid(uuid, hci=None):
    """Find a GATT characteristic path by UUID via D-Bus.

    Scoped to one controller when `hci` is given — with two adapters present
    an unscoped search can return an object belonging to the wrong radio.
    """
    bus = dbus.SystemBus()
    manager = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager"
    )
    prefix = f"/org/bluez/{hci}/" if hci else None
    for path, interfaces in manager.GetManagedObjects().items():
        if prefix and not str(path).startswith(prefix):
            continue
        if "org.bluez.GattCharacteristic1" in interfaces:
            props = interfaces["org.bluez.GattCharacteristic1"]
            if str(props.get("UUID", "")).lower() == uuid.lower():
                return path
    return None


def camera_status_fd_loop(fd):
    """Read incoming camera control notifications to track recording state and remaining time"""
    import os
    import select
    import struct
    while state["camera_connected"]:
        try:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                raw = os.read(fd, 256)
                if not raw:
                    log_event("Camera status fd: empty read, connection lost")
                    break
                if len(raw) >= 9:
                    state["_last_status_time"] = time.time()
                    i = 0
                    while i < len(raw) - 4:
                        length = raw[i+1]
                        if length == 0 or i + length + 4 > len(raw):
                            break
                        category = raw[i+4]
                        parameter = raw[i+5] if i+5 < len(raw) else 0

                        # Categoria 7.0 = relogio de tempo real. Sabemos ESCREVER
                        # aqui (e como se acerta a camara); se ela tambem emite o
                        # valor, ficamos a saber a data que ela tem. Nao esta
                        # confirmado que emita -- se nao emitir, isto nunca
                        # dispara e nada se perde.
                        if category == 7 and parameter == 0 and i + 15 < len(raw):
                            try:
                                _bcd = lambda v: (v >> 4) * 10 + (v & 0xF)
                                dt = struct.unpack_from('<i', raw, i + 12)[0]
                                yyyy = _bcd((dt >> 24) & 0xFF) * 100 + _bcd((dt >> 16) & 0xFF)
                                mo, dd = _bcd((dt >> 8) & 0xFF), _bcd(dt & 0xFF)
                                if 2000 <= yyyy <= 2099 and 1 <= mo <= 12 and 1 <= dd <= 31:
                                    state["_bm_date"] = "%04d-%02d-%02d" % (yyyy, mo, dd)
                            except Exception:
                                pass

                        # Category 10 = Transport
                        if category == 10:
                            if parameter == 1 and i+8 < len(raw):
                                transport_value = raw[i+8]
                                if state.get("_bm_rec_incerto") and transport_value in (0, 2):
                                    # Voltou depois de cair a gravar: o mesmo take continua (sem start novo), ou
                                    # parou enquanto nao se via.
                                    state["_bm_rec_incerto"] = None
                                    state["is_recording"] = transport_value == 2
                                    log_shot("BMPCC", "fault" if transport_value == 2 else "stop",
                                             "link back: still recording" if transport_value == 2
                                             else "stopped while the link was down")
                                elif transport_value == 2:
                                    if not state["is_recording"]:
                                        state["is_recording"] = True
                                        log_event("Camera status: RECORDING")
                                        log_shot("BMPCC", "start")
                                        clear_warnings()
                                elif transport_value == 0:
                                    if state["is_recording"]:
                                        state["is_recording"] = False
                                        commanded = (time.time() - state.get("_stop_commanded_at", 0)) < BMPCC_STOP_JANELA_S
                                        log_event("Camera status: STOPPED" if commanded
                                                  else "Camera status: STOPPED (not by the bridge!)")
                                        # Antes tinha o seu proprio valor
                                        # ("stop-unexpected"); fundido em
                                        # "fault" a 2026-09-17. Sem nota
                                        # propria, para nao perder o
                                        # auto-preenchimento do timecode
                                        # (log_shot() so o faz se note="").
                                        log_shot("BMPCC", "stop" if commanded else "fault")
                                        # Only alarms in strict mode, where the
                                        # operator has declared they drive recording
                                        # solely from here — otherwise this fires
                                        # every time they touch the camera's own REC
                                        # button, and an alarm that cries wolf gets
                                        # ignored. The shoot log marks the take
                                        # either way.
                                        if not commanded and state.get("strict_rec"):
                                            note_fault("BMPCC stopped recording by itself")
                                # Parse storage slot info from transport mode
                                if i+13 < len(raw):
                                    # Layout per the Blackmagic SDI Camera Control
                                    # Protocol, Media 10.1 "Transport mode":
                                    #   [0] mode  [1] speed  [2] flags
                                    #   [3] slot 1 medium  [4] slot 2 medium
                                    # This used to read the media bytes one
                                    # position late, and decoded the flags with
                                    # invented bits (0x40 as disk 1, 0x10 as
                                    # disk 2 — 0x10 is not defined at all).
                                    flags = raw[i+10]
                                    state["_transport_flags"] = flags
                                    # Slot count comes from the payload length,
                                    # not a guess: media bytes start at data[3]
                                    # and run to the end. The 6K has three.
                                    payload_len = max(0, length - 4)
                                    n_slots = min(3, max(0, payload_len - 3))
                                    media = [raw[i + 11 + k] for k in range(n_slots)
                                             if i + 11 + k < len(raw)]
                                    # Active masks per slot. 0x10 is Disk3Active —
                                    # absent from Blackmagic's own SDI PDF, but it
                                    # is what the Pocket bodies actually send, and
                                    # this code used to read it as "slot 2".
                                    ACTIVE_MASKS = (0x20, 0x40, 0x10)
                                    old_slot = state["active_slot"]
                                    active = [k + 1 for k in range(len(media))
                                              if flags & ACTIVE_MASKS[k]]
                                    state["active_slot"] = active[0] if active else (old_slot or 1)
                                    # Clear remaining time on slot change
                                    if state["active_slot"] != old_slot and old_slot > 0:
                                        act = state["active_slot"]
                                        state["remaining_time"] = state.get(
                                            f"slot{act}_remaining", "")
                                    # ActiveStorageMedium: 0=CFast, 1=SD,
                                    # 2=SSD Recorder, 3=USB
                                    medium_names = {0: "CFast", 1: "SD", 2: "SSD", 3: "USB-C"}
                                    for k in range(3):
                                        key = f"slot{k+1}"
                                        if k < len(media):
                                            state[f"{key}_name"] = medium_names.get(
                                                media[k], f"Slot{k+1}({media[k]})")
                                            # Presence comes from its own packet;
                                            # do not guess it from the media type,
                                            # which is the slot's KIND and is
                                            # reported whether or not a card is in.
                                            pass
                                        else:
                                            state[f"{key}_present"] = False
                                    # Only log when the storage picture actually
                                    # changes — the camera sends transport status
                                    # continuously, and logging every packet spammed
                                    # the journal (and the SD card) all through a take.
                                    storage_line = "Storage: " + " | ".join(
                                        f"{state.get(f'slot{k+1}_name', '?')}"
                                        f"{'*' if state['active_slot'] == k + 1 else ''}"
                                        for k in range(len(media)))
                                    if storage_line != state.get("_last_storage_line"):
                                        state["_last_storage_line"] = storage_line
                                        log_event(storage_line)

                        # Category 9 = Media
                        elif category == 9:
                            # Parameter 1 = Slot presence
                            if parameter == 1 and i+9 < len(raw):
                                state["slot1_present"] = raw[i+8] > 0
                                state["slot2_present"] = raw[i+9] > 0
                                if i+10 < len(raw):
                                    state["slot3_present"] = raw[i+10] > 0
                            # Parameter 2 = Remaining record time for both slots
                            elif parameter == 2:
                                if i+13 < len(raw):
                                    # Read as many slots as the packet carries,
                                    # not two: the 6K has three, and the active
                                    # one is often the third (USB-C SSD). The
                                    # remaining-time readout used to be for a
                                    # card you were not recording to.
                                    # The payload starts at i+8 and is one int16
                                    # per slot. This used to start at i+10, so
                                    # every value landed under the previous
                                    # slot's name — which is why an SSD on
                                    # USB-C showed its time labelled "SD".
                                    # Verified against the camera: with 2:14 left
                                    # the packet reads [0, 0, 134, 0].
                                    n = min(3, max(0, (length - 4) // 2))
                                    secs = []
                                    for k in range(n):
                                        off = i + 8 + 2 * k
                                        if off + 1 < len(raw):
                                            secs.append(struct.unpack_from('<h', raw, off)[0])
                                    slot1_secs = secs[0] if len(secs) > 0 else 0
                                    slot2_secs = secs[1] if len(secs) > 1 else 0
                                    slot3_secs = secs[2] if len(secs) > 2 else 0

                                    def fmt_time(s):
                                        if s <= 0:
                                            return ""
                                        h = s // 3600
                                        m = (s % 3600) // 60
                                        sec = s % 60
                                        if h > 0:
                                            return f"{h}h {m:02d}m"
                                        return f"{m}m {sec:02d}s"

                                    state["slot1_remaining"] = fmt_time(slot1_secs)
                                    state["slot2_remaining"] = fmt_time(slot2_secs)
                                    state["slot3_remaining"] = fmt_time(slot3_secs)
                                    # Raw seconds too: the formatted string is
                                    # for the eye, the number is for the alarm.
                                    state["_slot1_secs"] = slot1_secs
                                    state["_slot2_secs"] = slot2_secs
                                    state["_slot3_secs"] = slot3_secs
                                    act = state.get("active_slot") or 1
                                    state["remaining_time"] = state.get(
                                        f"slot{act}_remaining", state["slot1_remaining"])

                        # Category 1 = Video
                        elif category == 1:
                            if parameter == 14 and i+11 < len(raw):
                                iso_val = struct.unpack_from('<i', raw, i+8)[0]
                                if iso_val > 0:
                                    state["iso"] = iso_val

                        # Category 12 = System/Metadata
                        elif category == 12:
                            payload = raw[i+8:i+length+4]
                            
                            # p=1: [battery_level 0-255, power_source, flags]
                            # power_source: 0=Battery, 1=AC
                            if parameter == 1 and len(payload) >= 2:
                                batt_raw = payload[0]
                                batt_pct = round(batt_raw / 255 * 100)
                                power_src = payload[1]  # 1=AC, 0=Battery
                                old_pct = state.get("battery_percent", -1)
                                old_src = state.get("power_source", -1)
                                state["battery_percent"] = batt_pct
                                state["power_source"] = power_src
                                if batt_pct != old_pct or power_src != old_src:
                                    src_name = "AC" if power_src == 1 else "Battery"
                                    log_event(f"Power: {batt_pct}% ({src_name})")

                        i += length + 4
        except Exception as e:
            print(f"Camera status fd error: {e}")
            break
    try:
        os.close(fd)
    except OSError:
        pass
    if state["camera_connected"]:
        log_event("Camera status stream lost")
        _handle_camera_drop()


def start_camera_status_monitor():
    """Acquire notifications on incoming camera control characteristic"""
    incoming_path = find_char_path_by_uuid(INCOMING_UUID, CAMERA_ADAPTER)
    if not incoming_path:
        log_event("Incoming camera control characteristic not found!")
        return

    try:
        bus = dbus.SystemBus()
        char = dbus.Interface(
            bus.get_object("org.bluez", incoming_path),
            "org.bluez.GattCharacteristic1"
        )
        fd, mtu = char.AcquireNotify({})
        fd = fd.take()
        _register_notify_fd(fd)
        log_event(f"Camera status monitor started (fd={fd}, mtu={mtu})")
        t = threading.Thread(target=camera_status_fd_loop, args=(fd,), daemon=True)
        t.start()
    except Exception as e:
        log_event(f"Camera status monitor error: {e}")


def connect_camera_dbus():
    global camera_char_path
    if c3_bm.ativo():
        return _connect_camera_c3()
    if not BMPCC_ACTIVE:
        return False                  # BMPCC retirada

    # Connect over D-Bus on the device's path under CAMERA_ADAPTER, rather than
    # shelling out to `bluetoothctl connect`, which always uses whichever
    # controller BlueZ considers default — with two adapters that is the wrong
    # radio half the time. This also fails fast and clearly when the camera has
    # never been paired on this particular controller.
    path = device_path(CAMERA_ADAPTER, CAMERA_ADDRESS)
    log_event(f"Connecting camera on {CAMERA_ADAPTER}...")
    try:
        bus = dbus.SystemBus()
        dev = dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Device1")
        props = dbus.Interface(bus.get_object("org.bluez", path),
                               "org.freedesktop.DBus.Properties")

        def already_connected():
            try:
                return bool(props.Get("org.bluez.Device1", "Connected"))
            except Exception:
                return False

        # O BlueZ mantem o link vivo quando o bridge reinicia. Nesse caso o
        # Connect() rebenta com "In Progress" e antes desistiamos as tres
        # tentativas, deixando a camara ligada no radio mas inalcancavel aqui --
        # e a unica saida era desligar e ligar a mao. Se o link ja esta de pe
        # nao ha nada a ligar: vai-se direito ao GATT.
        last = None
        if _camera_cancel.is_set():
            log_event("Camera: connection cancelled before it started")
            return False
        if already_connected():
            log_event("Camera already up in BlueZ — reusing the link")
        else:
            # The first attempt after the camera has been idle often fails while
            # it wakes, and a second one straight after succeeds — which is why
            # this used to need cancelling and reconnecting by hand. Retry here
            # instead of making the operator do it.
            # Connect() sem esperar pela resposta, e depois sondamos a
            # propriedade Connected. A chamada sincrona bloqueava ate 25s a
            # espera que o BlueZ desistisse, e nesse intervalo o cancelamento
            # do operador nao era visto -- carregar no badio por engano custava
            # mais de um minuto a desfazer. Assim o cancelamento e atendido em
            # menos de um segundo, e a propriedade e a fonte de verdade de
            # qualquer maneira: e ela que diz se o link subiu.
            # O Connect() vai numa thread propria, SINCRONO. Antes chamava-se
            # com ignore_reply=True e sondava-se a propriedade: mantinha o
            # cancelamento rapido, mas deitava fora o erro do BlueZ -- e sempre
            # que ele recusava, aqui so se via a propriedade nao mudar e
            # dizia-se "camera did not respond", que e a unica coisa que
            # seguramente NAO tinha acontecido. Visto vezes sem conta a 8 de
            # Setembro: tres tentativas de 22s a falhar enquanto um
            # `bluetoothctl connect` a seguir pegava de imediato.
            #
            # Assim tem-se as duas coisas: a chamada bloqueia na thread e diz o
            # que o BlueZ respondeu, e este ciclo continua livre para atender o
            # cancelamento em menos de um segundo.
            chamada = {}

            def _chamar_connect():
                try:
                    dev.Connect(timeout=CONNECT_ATTEMPT_SECONDS + 5)
                    chamada["ok"] = True
                except Exception as exc:                    # pylint: disable=broad-except
                    chamada["erro"] = exc

            for attempt in (1, 2, 3):
                if _camera_cancel.is_set():
                    log_event("Camera: connection cancelled")
                    return False
                chamada.clear()
                t = threading.Thread(target=_chamar_connect, daemon=True)
                t.start()
                deadline = time.time() + CONNECT_ATTEMPT_SECONDS
                while time.time() < deadline:
                    if _camera_cancel.is_set():
                        log_event("Camera: connection cancelled")
                        return False
                    if already_connected():
                        last = None
                        break
                    if "erro" in chamada:
                        # Ja sabemos porque falhou; nao ha nada a ganhar em
                        # esperar o resto dos 22 segundos.
                        last = chamada["erro"]
                        break
                    time.sleep(0.25)
                if already_connected():
                    last = None
                    break
                last = last or RuntimeError("camera did not respond")
                if attempt < 3:
                    log_event(f"Camera: attempt {attempt} failed ({last}), retrying...")
                    if _camera_cancel.wait(2):
                        log_event("Camera: connection cancelled")
                        return False
                    # Nao arrancar outra chamada por cima de uma que ainda
                    # corre: duas Connect() em voo dao "In Progress" e a
                    # segunda estraga a primeira.
                    t.join(timeout=5)
        if last is not None:
            raise last
    except dbus.exceptions.DBusException as e:
        name = e.get_dbus_name()
        if "UnknownObject" in name or "DoesNotExist" in name:
            log_event(f"Camera not known to {CAMERA_ADAPTER} — pair it on that "
                      f"controller first (select {adapter_mac(CAMERA_ADAPTER)})")
        else:
            log_event(f"Camera connection failed: {e.get_dbus_message()}")
        return False
    except Exception as e:
        log_event(f"Camera connection failed: {e}")
        return False

    if _camera_cancel.wait(2):
        log_event("Camera: connection cancelled after connecting")
        return False

    camera_char_path = find_char_path_by_uuid(OUTGOING_UUID, CAMERA_ADAPTER)

    if not camera_char_path:
        log_event("Camera characteristic not found via D-Bus")
        return False

    state["camera_connected"] = True
    # Reset watchdog activity timestamps so a reconnect after a long drop
    # doesn't get judged stale against data from before the reconnect.
    state["_last_tc_time"] = time.time()
    state["_last_status_time"] = time.time()
    log_event("Camera connected and ready")
    # Start timecode polling
    try:
        start_timecode_polling()
    except Exception as e:
        log_event(f"Timecode setup error: {e}")
    # Start camera status monitor to track recording state
    try:
        start_camera_status_monitor()
    except Exception as e:
        log_event(f"Camera status monitor error: {e}")
    return True


# === BMPCC pelo gateway C3 ===
def _connect_camera_c3():
    global camera_char_path
    if not globals().get("BMPCC_ACTIVE", True):
        return False
    log_event(f"Connecting camera via the C3 gateway ({c3_bm.host() or 'not found yet'})...")
    try:
        fd_in, fd_tc = c3_bm.ligar()
    except Exception as e:
        log_event(f"Camera via C3 failed: {e}")
        return False
    camera_char_path = "c3"            # os testes "not camera_char_path" continuam a valer
    state["camera_connected"] = True
    state["_last_tc_time"] = time.time()
    state["_last_status_time"] = time.time()
    _register_notify_fd(fd_in)
    _register_notify_fd(fd_tc)
    threading.Thread(target=timecode_fd_loop, args=(fd_tc,), daemon=True).start()
    threading.Thread(target=camera_status_fd_loop, args=(fd_in,), daemon=True).start()
    log_event("Camera connected and ready (via C3)")
    return True


def _camera_c3_disconnect():
    state["camera_connected"] = False       # os fd loops saem em <=1s
    c3_bm.desligar()
    time.sleep(1.2)
    n = _close_camera_notify_fds()
    if n:
        log_event(f"BMPCC: closed {n} C3 session stream(s)")


def _c3_pronto():
    # A BMPCC liga-se sozinha pelo C3, como as GoPro pelo S3 (sem o "c" da consola).
    with _reconnect_lock:
        if state["camera_connected"] or state["camera_connecting"]:
            return
        state["camera_connecting"] = True
    try:
        connect_camera_dbus()
    finally:
        state["camera_connecting"] = False


c3_bm.ao_pronto = _c3_pronto

c3_bm.iniciar(log_event)


def write_camera(data):
    if c3_bm.ativo():
        try:
            c3_bm.escrever(data)
        except Exception as e:
            log_event(f"BLE write (via C3) failed: {e}")
            # So e queda se o C3 tambem deixou de responder as leituras; senao o comando falhou mas a ligacao esta
            # viva (o operador ve o erro e repete), sem anunciar uma queda que nao houve.
            if not c3_bm.alcancavel():
                _handle_camera_drop()
            raise
        return
    try:
        bus = dbus.SystemBus()
        char = dbus.Interface(
            bus.get_object("org.bluez", camera_char_path),
            "org.bluez.GattCharacteristic1"
        )
        char.WriteValue([dbus.Byte(b) for b in data], {})
    except Exception as e:
        log_event(f"BLE write failed: {e}")
        # So e queda se o link caiu (o BlueZ diz que nao esta ligado, ou a camara esta calada); uma escrita recusada
        # com o link vivo (a claquete, por exemplo) nao pode desligar a camara a meio de nada.
        msg = str(e)
        if camera_link_is_stale() or any(k in msg for k in ("NotConnected", "Not connected", "UnknownObject",
                                                           "Disconnected", "not available")):
            _handle_camera_drop()
        raise

# Quanto tempo se acredita no ultimo comando em vez da telemetria. Generoso de
# proposito: mediu-se 15s entre um STOP comandado e a BMPCC confirmar.
REC_INTENT_TTL = 20

# Um STOP da BMPCC visto ate estes segundos depois de comandado e do bridge. Eram 5 s, mas a camara chegou a demorar 15 s
# a confirmar (ver acima): o STOP comandado ficava registado como "not by the bridge" (falha no shoot log, e alarme no
# modo estrito) so por ela ter sido lenta.
BMPCC_STOP_JANELA_S = 15

_rec_group_lock = threading.Lock()


class _rec_parado:
    """`with _rec_parado() as livre:` -- segura o lock do REC de grupo durante uma remocao de um device, para nenhum REC/STOP
    arrancar a meio. `livre` e False se um REC nao largou o lock a tempo (a rota responde 409)."""
    def __init__(self, espera=3.0):
        self.espera, self.livre = espera, False

    def __enter__(self):
        self.livre = _rec_group_lock.acquire(timeout=self.espera)
        return self.livre

    def __exit__(self, *_):
        if self.livre:
            _rec_group_lock.release()
        return False


def next_rec_direction(observed, chave=None):
    """Para onde vai a proxima pressao de REC, dado o que a telemetria diz.

    A telemetria chega atrasada -- o `is_recording` da BMPCC so muda quando
    aparece a notificacao BLE. Enquanto ela nao apanhar o ultimo comando, e o
    comando que vale: sem isto a segunda pressao lia "parado", concluia
    "entao arranca", mandava START outra vez, e a paragem nunca acontecia. Era
    esse o "ou nao a desliga, ou nao a arranca".

    `chave` diz a que REC a intencao pertence (o de grupo, ou o de um device): a de um device nao vale para outro.
    Antes era uma so para tudo, e ligar uma GoPro logo depois de outra era lido como "a segunda ja esta a gravar" --
    o REC dela virava STOP.

    Devolve (quer_gravar, telemetria_ainda_atrasada).
    """
    # Uma intencao por REC (a chave): o REC de um device a meio nao apaga a do grupo, e vice-versa -- com uma so, um
    # toque num device entre dois REC do grupo fazia o segundo ler a telemetria atrasada e arrancar outra vez.
    intencoes = state.setdefault("_rec_intent", {})
    pending = intencoes.get(chave)
    stale = bool(pending and time.time() - pending["at"] < REC_INTENT_TTL
                 and bool(observed) != pending["on"])
    if pending and not stale:
        # A telemetria apanhou (ou o prazo passou): volta a mandar ela, para
        # que carregar no botao da propria camara continue a ser respeitado.
        intencoes.pop(chave, None)
    base = pending["on"] if stale else bool(observed)
    return (not base), stale


def note_rec_intent(want_recording, chave=None):
    """Regista o que acabamos de comandar, para a proxima pressao partir dai."""
    state.setdefault("_rec_intent", {})[chave] = {"on": bool(want_recording), "at": time.time()}
    for k in [k for k, v in state["_rec_intent"].items() if time.time() - v["at"] >= REC_INTENT_TTL]:
        del state["_rec_intent"][k]             # as velhas nao ficam a acumular (um device apagado deixava a sua)


def get_toggle_record_command(start_recording):
    # Remember that WE asked for a stop. A stop that arrives over BLE without
    # one of these behind it was the camera's own doing — dropped frames, a
    # card that cannot keep up, a full disk — and that is a lost take, not a
    # normal end. Marking it here covers every call site at once.
    if not start_recording:
        state["_stop_commanded_at"] = time.time()
    data = bytearray([255, 5, 0, 0, 10, 1, 1, 0, 0, 0, 0, 0])
    if start_recording:
        data[8] = 2
    else:
        data[8] = 0
    return data


def _bcd(text):
    """'20260906' -> 0x20260906. BCD packs each decimal digit into a nibble."""
    return int(text, 16)

# Os fds que o AcquireNotify nos deu. Enquanto um destes estiver aberto o BlueZ
# mantem a sessao GATT viva, e o Disconnect() nao desmonta a ligacao: a camara
# ficava pendurada, com o badge a dizer desligado e o radio ainda ligado, e so
# um restart do bluetoothd a soltava.
_camera_notify_fds = []


def _register_notify_fd(fd):
    _camera_notify_fds.append(fd)


def _close_camera_notify_fds():
    """Fecha e esquece os fds de notificacao. Devolve quantos fechou."""
    global _camera_notify_fds
    fds, _camera_notify_fds = _camera_notify_fds, []
    for fd in fds:
        try:
            os.close(fd)
        except Exception:
            pass
    return len(fds)


def _camera_dbus_disconnect():
    if c3_bm.ativo():
        return _camera_c3_disconnect()
    """Desliga no controlador onde a camara vive, a serio.

    Ordem importa: baixa-se a flag para os fd loops sairem, esperam-se os <=1s
    do select deles, fecham-se os fds e so entao se pede o Disconnect. Fazer o
    Disconnect com os fds ainda abertos nao desmontava nada.
    """
    state["camera_connected"] = False       # os fd loops saem em <=1s
    time.sleep(1.2)
    n = _close_camera_notify_fds()
    if n:
        log_event(f"BMPCC: closed {n} notification session(s)")

    path = device_path(CAMERA_ADAPTER, CAMERA_ADDRESS)
    bus = None
    try:
        bus = dbus.SystemBus()
        dbus.Interface(bus.get_object("org.bluez", path),
                       "org.bluez.Device1").Disconnect()
    except Exception as e:
        # Antes isto era engolido em silencio, e era esse silencio que escondia
        # o problema: a UI dizia desligado e o radio continuava ligado.
        log_event(f"BMPCC: Disconnect failed: {e}")
    if bus is None:
        return
    try:
        props = dbus.Interface(bus.get_object("org.bluez", path),
                               "org.freedesktop.DBus.Properties")
        if bool(props.Get("org.bluez.Device1", "Connected")):
            log_event("BMPCC: still connected in BlueZ after Disconnect — "
                      "use RESTART BLUETOOTH if it persists")
    except Exception:
        pass


def disconnect_camera():
    global camera_char_path
    _camera_dbus_disconnect()
    camera_char_path = None
    state["camera_connected"] = False
    state["is_recording"] = False
    state["timecode"] = "--:--:--"
    state["remaining_time"] = ""
    state["_last_storage_line"] = None
    state["iso"] = 0
    state["active_slot"] = 0
    state["slot1_present"] = False
    state["slot2_present"] = False
    state["slot3_present"] = False
    log_event("Camera disconnected")

# === AUTO-RECONNECT ===
_reconnect_lock = threading.Lock()

# Permite abortar uma tentativa de ligacao a meio. Com a BMPCC desligada cada
# tentativa fica pendurada ate o BlueZ desistir, e sao tres; sem isto, o segundo
# toque no badge so era atendido no fim de tudo -- que e como se perde a
# paciencia com um botao.
_camera_cancel = threading.Event()

# Quanto esperar por cada tentativa antes de a dar por perdida. O BlueZ desiste
# sozinho por volta dos 25s; abaixo disto cortamos-lhe a paciencia sem ganho.
CONNECT_ATTEMPT_SECONDS = 22

# Historico de quedas, para responder a "esta a piorar?" sem tocar no radio.
# A alternativa obvia -- ler o sinal -- exige uma varredura, e uma varredura
# monopoliza o controlador e derruba as ligacoes que se esta a medir: aconteceu
# duas vezes a 8 de Setembro, por diagnostico meu. Isto conta o que ja passa
# pelo bridge e nao custa um simbolo de radio.
QUEDAS_JANELA = 3600


def registar_queda(qual):
    hist = state.setdefault("_quedas", {})
    agora = time.time()
    lista = [t for t in hist.get(qual, []) if agora - t < QUEDAS_JANELA]
    lista.append(agora)
    hist[qual] = lista[-50:]


def instantaneo_condicoes():
    """As condicoes de radio que decidem se os links aguentam.

    Existe porque a 8 de Setembro se perderam horas a responder a "o que e que
    mudou?" -- e a resposta so apareceu depois de eu andar a repetir medicoes a
    mao. Nenhuma destas coisas dava erro: eram escolhas silenciosas com
    consequencias. Registadas quando mudam, a pergunta passa a responder-se
    lendo o log.
    """
    modo, mhz = wlan_radio()
    partes = [f"uplink {uplink_ssid() or modo or '?'}"
              + (f" {mhz}MHz" if mhz else " down")]
    try:
        info = _run(["iw", "dev", AP_IFACE, "info"], timeout=6).stdout
        canal = next((l.strip().split()[1] for l in info.splitlines()
                      if l.strip().startswith("channel")), "?")
        partes.append(f"AP ch{canal}" if "type AP" in info else "AP down")
    except Exception:
        partes.append("AP ?")
    n = len(adaptadores_presentes())
    partes.append(f"{n} BT adapter" + ("s" if n != 1 else "")
                  + f" (BM {CAMERA_ADAPTER})")
    try:
        with open("/sys/kernel/debug/bluetooth/hci0/supervision_timeout",
                  encoding="ascii") as f:
            partes.append(f"supervision {int(f.read().strip()) * 10}ms")
    except (OSError, ValueError):
        pass
    return " | ".join(partes)


def vigiar_adaptador_tardio():
    """O adaptador preferido apareceu depois de termos desistido dele?

    resolve_adapter() corre uma unica vez, no arranque. Quem ligar o dongle com
    o Pi ja a trabalhar fica com o bridge no radio interno para o resto da vida
    do processo -- e em silencio: a BMPCC, que so tem bond no dongle, nunca mais
    liga. Reatribuir por baixo dos fios que ja estao a correr e que nao da:
    ha threads BLE agarradas ao adaptador antigo. Diz-se ao operador, que
    reinicia o servico quando lhe der jeito.
    """
    presentes = adaptadores_presentes()
    em_uso = {"BMPCC": CAMERA_ADAPTER}
    em_falta = sorted(f"{quem} wants {quer}, using {em_uso[quem]}"
                      for quem, quer in ADAPTADORES_PREFERIDOS.items()
                      if quer in presentes and quer != em_uso[quem])
    if not em_falta:
        state["_aviso_adaptador"] = None
        return
    aviso = "; ".join(em_falta)
    if state.get("_aviso_adaptador") == aviso:
        return
    state["_aviso_adaptador"] = aviso
    log_event(f"Preferred BLE adapter is back but this process is not using it "
              f"({aviso}) — restart the bridge to pick it up")


def vigiar_condicoes():
    """Regista o instantaneo no arranque e sempre que ele muda."""
    novo = instantaneo_condicoes()
    if novo != state.get("_condicoes"):
        anterior = state.get("_condicoes")
        state["_condicoes"] = novo
        log_event(("Radio conditions: " if anterior is None else "Radio conditions CHANGED: ")
                  + novo)
    vigiar_adaptador_tardio()


_queda_lock = threading.Lock()


def _handle_camera_drop():
    """Called when camera connection is detected as lost. Tres threads podem dar pela mesma queda ao mesmo tempo (o
    estado, o timecode, o vigia): so a primeira conta (antes podiam ficar duas linhas "link lost" e duas religacoes)."""
    with _queda_lock:
        if not state.get("camera_connected"):
            return
        state["camera_connected"] = False
        _handle_camera_drop_1()


def _handle_camera_drop_1():
    global camera_char_path
    was_recording = state.get("is_recording")
    registar_queda("bmpcc")
    camera_char_path = None
    state["is_recording"] = False
    state["timecode"] = "--:--:--"
    state["remaining_time"] = ""
    state["_last_storage_line"] = None
    if was_recording:
        # A camara deve continuar a gravar: fica "incerto" (conta como a gravar) ate voltar e dizer o estado.
        state["_bm_rec_incerto"] = time.time()
        log_shot("BMPCC", "fault", "link lost while recording")
        note_fault("BMPCC dropped while recording")
    # Don't reset ISO/slot — they'll refresh on reconnect
    if state.get("_auto_reconnect", False):
        log_event("Camera dropped — will auto-reconnect in 5s...")
        threading.Timer(5.0, _try_camera_reconnect).start()


def _try_camera_reconnect():
    """Background attempt to reconnect camera"""
    if state["camera_connected"] or state["camera_connecting"]:
        return
    if not state.get("_auto_reconnect", False):
        return                            # o operador desligou de proposito
    # Claim the attempt under the lock, then release it before doing any of
    # the slow work below. bluetoothctl can burn ~15s and connect_camera_dbus()
    # sleeps another 2s on top; holding _reconnect_lock across all of that made
    # the UI's Connect button — which takes the same lock — hang for the whole
    # reconnect. The camera_connecting flag is what actually serialises
    # attempts, so the lock only needs to cover setting it.
    with _reconnect_lock:
        if state["camera_connected"] or state["camera_connecting"]:
            return
        state["camera_connecting"] = True
    log_event("Auto-reconnecting camera...")
    try:
        # First disconnect cleanly, on the right controller.
        _camera_dbus_disconnect()
        time.sleep(2)
        if connect_camera_dbus():
            log_event("Camera auto-reconnected!")
        else:
            log_event("Auto-reconnect failed — retrying in 10s...")
            threading.Timer(10.0, _try_camera_reconnect).start()
    except Exception as e:
        log_event(f"Auto-reconnect error: {e}")
        threading.Timer(10.0, _try_camera_reconnect).start()
    finally:
        state["camera_connecting"] = False

# Quanto silencio da camara chega para dar o link por morto. Ela manda
# notificacoes continuamente -- mediram-se 1 a 10 por segundo --, por isso o
# silencio dela e um sinal muito mais rapido do que o do controlador.
CAMERA_SILENCE_SECONDS = 5


def camera_link_is_stale(now=None):
    """A camara esta calada ha tempo de mais para o link estar vivo?

    Existe porque o supervision timeout esta em 20s (a 5s, o valor por omissao, os links fracos caiam de poucos em
    poucos minutos com Connection Timeout), o que deixaria a camara a demorar 20s a ser dada por perdida. Esta funcao
    devolve essa rapidez sem voltar aos 5s.
    """
    if not state["camera_connected"]:
        return False
    last = max(state.get("_last_tc_time", 0), state.get("_last_status_time", 0))
    if last <= 0:
        return False          # ainda nao chegou nada; disso trata a ligacao
    return ((time.time() if now is None else now) - last) > CAMERA_SILENCE_SECONDS


def _camera_watchdog():
    """Background watchdog — detect stale connections.

    Tica a cada segundo por causa da camara. Antes ticava de 10 em 10 com
    limiar de 15s, o que dava ate 25s para detectar -- mais lento que o proprio
    controlador, e portanto inutil como rede de seguranca. As tarefas pesadas
    continuam de 10 em 10 segundos, que e a cadencia de que precisam.
    """
    tick = 0
    while True:
        time.sleep(1)
        tick += 1
        if camera_link_is_stale():
            log_event(f"Watchdog: no camera data for {CAMERA_SILENCE_SECONDS}s "
                      f"— connection stale")
            _handle_camera_drop()
        try:
            vigiar_ligacoes()
        except Exception as e:                  # nunca para o ciclo do watchdog
            log_event(f"Link watch: {e}")
        if tick % 60 == 0:
            vigiar_condicoes()
        if tick % 10:
            continue
        notify_watchdog()
        fechar_take_se_parado()
        check_health()

# === WEB SERVER ===
app = Flask(__name__)

import acesso                                   # PIN para quem nao esta no Tailscale nem no Atom (acesso.py)


WEB_DESLIGADA = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                 "<body style='background:#111;color:#ccc;font:18px system-ui;text-align:center;padding-top:20vh'>"
                 "<h2>Camera Bridge</h2><p>The web page is turned off.</p>"
                 "<p>Turn it on in the console: GLOBAL SETTINGS &gt; Services.</p>")


def web_remota():
    """A pagina e a API para os outros aparelhos (iPads, telemoveis, portateis, Tailscale). Desligada, so o Atom."""
    return state.get("web_remote", True) is not False


def _do_proprio_atom(ip):
    import ipaddress
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    return a.is_loopback or bool(getattr(a, "ipv4_mapped", None) and a.ipv4_mapped.is_loopback)


@app.before_request
def _guarda_acesso():
    """Quem esta no Atom ou no Tailscale entra como antes; os outros so com a sessao do PIN (acesso.py). Um POST de um
    browser tem de vir da propria pagina (Origin), seja de onde for."""
    from flask import redirect
    if request.method not in ("GET", "HEAD", "OPTIONS") and not acesso.origem_ok(request.headers.get("Origin"),
                                                                                   request.host):
        return jsonify({"ok": False, "error": "request from another site refused"}), 403
    ip = request.remote_addr or ""
    if not web_remota() and not _do_proprio_atom(ip):
        # Pagina desligada na consola: so o proprio Atom (consola, USB) fala com o bridge. Resposta
        # minima, sem sessao nem pagina: um browser que ficou aberto custa quase nada.
        # O cabecalho diz a uma pagina que ficou aberta para recarregar: fica com o aviso, que nao pergunta nada
        if request.path.startswith("/api/"):
            return (jsonify({"ok": False, "error": "the web page is off (console: GLOBAL SETTINGS > Services)"}), 503,
                    {"X-Bridge-Web-Off": "1"})
        return WEB_DESLIGADA, 503, dict(NO_CACHE, **{"X-Bridge-Web-Off": "1"})
    if acesso.de_confianca(ip):
        return None
    if not acesso.limite(ip):
        return jsonify({"ok": False, "error": "too many requests"}), 429
    p = request.path
    if p in acesso.LIVRES or any(p.startswith(x) for x in acesso.LIVRES if x.endswith("/")):
        return None
    if acesso.sessao_valida(request.cookies.get(acesso.COOKIE)):
        return None
    if p.startswith("/api/"):
        return jsonify({"ok": False, "error": "PIN required", "login": "/login"}), 401
    from urllib.parse import quote
    return redirect("/login?next=" + quote(request.full_path.rstrip("?"), safe="/?=&"))


def _body():
    """O corpo JSON do pedido como dict: {} se nao ha corpo, ou se for outra coisa (lista, texto, numero)."""
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _rec_do_operador_cancela():
    global _take_retomar
    _take_retomar = None
    _take_aberto_apagar()


@app.after_request
def _resposta_com_ok(resp):
    """O contrato da API: todo o POST e todo o erro de /api/* devolvem um objecto JSON com "ok" (true/false) e, se falhou,
    "error". Quem escreve uma rota pode esquecer-se do "ok": aqui acrescenta-se, para o cliente nunca ter de adivinhar."""
    try:
        if (request.path.startswith("/api/") and resp.mimetype == "application/json" and not resp.direct_passthrough
                and (request.method == "POST" or resp.status_code >= 400)):
            corpo = resp.get_json(silent=True)
            if isinstance(corpo, dict) and "ok" not in corpo:
                corpo["ok"] = resp.status_code < 400
                resp.set_data(json.dumps(corpo))
    except Exception:                                   # pylint: disable=broad-except
        pass
    return resp


@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(500)
def _erro_em_json(e):
    """Em /api/* um erro que nenhuma rota tratou (rota que nao existe, metodo errado) tambem e JSON com "ok" e "error"."""
    codigo = getattr(e, "code", 500) or 500
    if request.path.startswith("/api/"):
        texto = {404: "no such route", 405: "method not allowed"}.get(codigo, "internal error")
        return jsonify({"ok": False, "error": texto}), codigo
    return e

# Sem cabecalhos de cache, o Safari guarda a pagina por conta propria com
# heuristica -- sobretudo adicionada ao ecra principal -- e passa a servir uma
# versao antiga sem avisar. Numa interface de controlo isso e inaceitavel: o
# operador ve botoes que ja nao existem e alteracoes que parecem nao ter sido
# feitas. Os dados vem todos por /api/, por isso o HTML nao ganha nada em ser
# guardado.
NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0"}


# === Firmware de todos os gateways, sempre pelo Atom ===
# O Atom e quem actualiza os gateways: o firmware dos C3 vem do repositorio (que o instalador deixou em fw/c3) e o
# dos S3 e o .bin guardado no Atom. Nada se grava num gateway a partir de outro sitio. Os gateways ligados no bridge
# (o C3 principal, os C3 das BMPCC extra e os S3) sao comparados com o que o Atom traz e, quando nada grava, os que
# estao atrasados actualizam-se um a um (o gateway reinicia; a BMPCC e as GoPro voltam sozinhas).
GW_FW_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gw_fw.json")

_gw_fw_lock = threading.Lock()

_gw_fw_falhou = {}          # (tipo, host) -> alvo em que falhou: nao se insiste ate o alvo mudar

_gw_fw_ultimo = {"em": None, "linhas": []}

# Pedido "Update from GitHub" de um gateway: o Atom actualiza-se (git pull + instalador, o bridge reinicia) e, ao
# voltar, grava esses gateways (mesmo com a actualizacao automatica desligada). {"ids": [...] | null (todos), "em": t}
GW_FW_PENDENTE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gw_fw_pendente.json")


def _gw_fw_auto():
    try:
        with open(GW_FW_CFG) as f:
            return json.load(f).get("auto", False) is True
    except (OSError, ValueError, AttributeError):
        return False                # por defeito desligada: um gateway so reinicia quando o operador pede


def _gw_fw_alvos():
    """Os gateways que o bridge conhece e tem ligados: [{id, kind, name, host}]."""
    out = []
    if c3_bm.ligado() and c3_bm.host():
        out.append({"id": "c3", "kind": "c3", "name": "C3", "host": c3_bm.host()})
    for cid, cam in sorted(bmfleet.cams.items()):
        if cam.host:
            out.append({"id": cid, "kind": "c3", "name": f"C3 of {cam.name}", "host": cam.host})
    for g in gofleet.gateways():
        if g["enabled"] and g["host"]:
            out.append({"id": f"s3-{g['idx']}", "kind": "s3", "name": g["name"], "host": g["host"]})
    return out


def _gw_fw_estado_de(a):
    import fw_ota
    if a["kind"] == "c3":
        e = fw_ota.c3_estado(a["host"])
        if not e.get("reachable"):
            return {"state": "unreachable", "detail": e.get("error") or "not answering", "target": ""}
        if e.get("error"):
            return {"state": "needs_usb", "detail": e["error"], "target": ""}
        alvo = ",".join(sorted(e.get("available", {}).values()))
        if e.get("changed"):
            v_c3, v_bin = fw_ota.c3_versao(e.get("fw")), e.get("bin_version")
            versoes = "  (runs v%s, the Atom has v%s)" % (v_c3, v_bin) if v_c3 is not None and v_bin is not None and v_c3 != v_bin else ""
            return {"state": "outdated", "detail": "to send: " + ", ".join(e["changed"]) + versoes, "target": alvo,
                    "fw": e.get("fw")}
        return {"state": "ok", "detail": "up to date", "target": alvo, "fw": e.get("fw")}
    e = fw_ota.s3_estado(a["host"])
    if not e.get("reachable"):
        return {"state": "unreachable", "detail": "not answering", "target": ""}
    if not e.get("bin"):
        return {"state": "unknown", "detail": "no .bin on the Atom", "target": "", "fw": e.get("fw")}
    alvo = e["bin"]["md5"]
    if e.get("outdated") is None:
        return {"state": "unknown", "detail": "the Atom never wrote a .bin to it", "target": alvo, "fw": e.get("fw")}
    if e["outdated"] and e.get("by") == "version":
        detalhe = "runs v%s, the Atom has v%s" % (fw_ota.s3_versao(e.get("fw")), (e.get("bin") or {}).get("version"))
    else:
        detalhe = "a newer .bin is on the Atom" if e["outdated"] else "up to date"
    return {"state": "outdated" if e["outdated"] else "ok", "detail": detalhe, "target": alvo, "fw": e.get("fw")}


def _gw_fw_lista():
    """O estado de cada gateway, consultados em paralelo (cada um e um pedido de rede)."""
    from concurrent.futures import ThreadPoolExecutor
    alvos = _gw_fw_alvos()
    if not alvos:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(alvos))) as ex:
        estados = list(ex.map(_gw_fw_estado_de, alvos))
    return [{**a, **e} for a, e in zip(alvos, estados)]


def _gw_fw_atualizar(itens, quem="operator"):
    """Actualiza os `itens` atrasados, um de cada vez. Devolve [{id, ok, msg}]."""
    import fw_ota
    out = []
    for it in itens:
        if anything_recording():
            out.append({"id": it["id"], "ok": False, "msg": "recording: not updating now"})
            break
        if it["kind"] == "c3":
            ok, d = fw_ota.c3_atualizar(it["host"], log_event)
        else:
            ok, d = fw_ota.s3_atualizar(it["host"], log_event)
        msg = "updated" if ok else (d.get("error") or "failed")
        if not ok:
            _gw_fw_falhou[(it["kind"], it["host"])] = it.get("target")
        else:
            _gw_fw_falhou.pop((it["kind"], it["host"]), None)
        log_event(f"Gateway firmware ({quem}): {it['name']} {it['host']}: {msg}")
        out.append({"id": it["id"], "ok": ok, "msg": msg})
        time.sleep(2)               # o gateway reinicia: nao se acumulam reinicios em simultaneo
    return out


def _gw_fw_pendentes():
    """Os gateways pedidos com "Update from GitHub": depois do Atom se actualizar, grava-os (um de cada vez). O pedido
    vale 2 h; com algo a gravar, ou um gateway pedido ainda sem resposta (a reiniciar), espera pela volta seguinte.
    True enquanto houver pedido."""
    try:
        with open(GW_FW_PENDENTE) as f:
            p = json.load(f)
        if not isinstance(p, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return False
    try:
        em = float(p.get("em") or 0)
    except (TypeError, ValueError):
        em = 0.0
    if time.time() - em > 7200:
        os.remove(GW_FW_PENDENTE)
        return False
    if anything_recording() or not _gw_fw_lock.acquire(blocking=False):
        return True
    try:
        ids = p.get("ids")
        lista = [x for x in _gw_fw_lista() if ids is None or x["id"] in ids]
        itens = [x for x in lista if x["state"] == "outdated"
                 or (x["state"] == "unknown" and x["kind"] == "s3" and x.get("target"))]
        res = _gw_fw_atualizar(itens, "after the GitHub update") if itens else []
        if res:
            _gw_fw_ultimo.update({"em": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "linhas": [f"{r['id']}: {r['msg']}" for r in res]})
        if not any(x["state"] == "unreachable" for x in lista):    # uma falha fica no registo; nao se insiste
            os.remove(GW_FW_PENDENTE)
            return False
        return True
    finally:
        _gw_fw_lock.release()


def _gw_fw_ciclo():
    """Depois do arranque (e de meia em meia hora): se nada grava, actualiza os gateways atrasados. Um gateway em
    que a actualizacao falhou so se tenta de novo quando o firmware do Atom mudar."""
    time.sleep(90)
    while True:
        pendente = False
        try:
            pendente = _gw_fw_pendentes()
        except Exception as e:           # nunca deixa o fio morrer
            log_event(f"Gateway firmware (after the GitHub update): {e}")
        try:
            # Automatica (se ligada): so com o rig parado ha 10 min (nao entre takes seguidos).
            sossego = time.time() - float(state.get("_take_fim") or 0) > 600 and not _take_ativos
            if _gw_fw_auto() and sossego and not anything_recording() and _gw_fw_lock.acquire(blocking=False):
                try:
                    itens = [x for x in _gw_fw_lista() if x["state"] == "outdated"
                             and _gw_fw_falhou.get((x["kind"], x["host"])) != x.get("target")]
                    if itens:
                        res = _gw_fw_atualizar(itens, "automatic")
                        _gw_fw_ultimo.update({"em": time.strftime("%Y-%m-%d %H:%M:%S"),
                                              "linhas": [f"{r['id']}: {r['msg']}" for r in res]})
                finally:
                    _gw_fw_lock.release()
        except Exception as e:           # nunca deixa o fio morrer
            log_event(f"Gateway firmware (automatic): {e}")
        time.sleep(60 if pendente else 1800)

_alvos_lock = threading.Lock()


def _alvos_tirar(ids):
    """Tira um id (ou varios) dos alvos de REC, sob o lock dos alvos (senao uma lista lida antes de outra rota gravar a sua
    desfazia essa mudanca)."""
    ids = [ids] if isinstance(ids, str) else list(ids)
    with _alvos_lock:
        state["rec_targets"] = [t for t in (state.get("rec_targets") or []) if t not in ids]

# Zoom F6: o gerador do jam de LTC (f6_ltc.py) e o F6Control (f6_ctl.py,
# FRC-8) logo abaixo. Arranca no __main__.
_f6_gen = f6_ltc.Generator(ltc_secs_at,
                           lambda: (state.get("_ltc_jam") or {}).get("fps") or 25,
                           log_event)


def _f6_mudou_sozinho(a_gravar):
    """O F6 comecou ou parou sem ordem do bridge: botao no proprio F6, cartao
    cheio, pilhas. Fica no shoot log como as camaras (o start abre take)."""
    log_event(f"F6 {'started' if a_gravar else 'stopped'} by itself "
              f"(not commanded by the bridge)")
    log_shot("F6", "start" if a_gravar else "stop")


# F6: FRC-8 por USB-MIDI (frc8.py) + jam de LTC a pedido (f6_ltc.py). Ver f6_ctl.py.
f6 = f6_ctl.F6Control(_f6_gen, frc8.Link(log_event), log_event, _f6_mudou_sozinho)


def _f6_shot(evento, nota):
    """Avisos e notas do F6 no shoot log, com o TC do instante."""
    secs = ltc_secs_now()
    tc = format_tc(secs, (state.get("_ltc_jam") or {}).get("fps") or 25) if secs is not None else ""
    log_shot("F6", evento, (f"{tc} | " if tc else "") + nota)


f6.shot = _f6_shot

f6.on_fault = note_fault

# iPhones com a app Blackmagic Camera (bmcam.py): REC pela REST API de cada app.
iphs = bmcam.Fleet(log_event, os.path.join(os.path.dirname(os.path.abspath(__file__)), "iphones.json"),
                   ltc_secs=ltc_secs_at,
                   fps=lambda: (state.get("_ltc_jam") or {}).get("fps") or 25)


def _iph_mudou_sozinho(cam, a_gravar):
    """Um iPhone comecou ou parou sem o bridge (o REC da propria app...)."""
    log_shot(cam.name, "start" if a_gravar else "stop")
    if a_gravar:
        cam.take_no = state.get("_take_no")


iphs.on_unexpected = _iph_mudou_sozinho


def _iph_shot(cam, evento, nota):
    """Notas do iPhone (desvio do TC...) no shoot log, na take que esta a decorrer."""
    log_shot(cam.name, evento, nota)


iphs.shot = _iph_shot

iphs.on_fault = lambda cam, txt: note_fault(txt)

iphs.on_clip = lambda cam, nome: anotar_clip(cam.name, cam.take_no, nome)

# GoPro pelo gateway ESP32-S3 (gopro.py): mesma interface que os iPhones, mesmos avisos.
gofleet = gopro.GoproFleet(log_event, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gopros.json"))

# O F6 tambem pode vir pelo gateway S3 no modo F6 (selector do firmware): mesmo endereco, mesmo token.
f6.link.gw_f6 = gofleet.f6_gateway

f6.link.gw_cab = gopro.fw_ota.token_cab          # o token dos gateways (o gopro.py ja o usa)

gofleet.on_unexpected = _iph_mudou_sozinho

gofleet.shot = _iph_shot

gofleet.on_lost = lambda cam: note_fault("%s dropped while recording" % cam.name)

# Sony (sony.py): pelo Camera Remote SDK, que cada um descarrega da Sony (o ajudante sony/sony_ctl compila-se contra
# ele). Sem SDK a frota fica parada e diz porque; o resto do rig funciona igual.
sonys = sony.Fleet(log_event, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sonys.json"))

sonys.on_unexpected = _iph_mudou_sozinho

sonys.shot = _iph_shot

sonys.on_fault = lambda cam, txt: note_fault(txt)
sonys.termos_ok = lambda: licencas.estado()["accepted"]

# BMPCC extra, cada uma no seu C3 (bmfleet.py): a principal ("bmpcc") continua a ser a de cima.
bmfleet = bmfleet_mod.Fleet(log_event, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bmpccs.json"))

bmfleet.ltc_secs = ltc_secs_at

bmfleet.on_unexpected = _iph_mudou_sozinho

bmfleet.shot = _iph_shot

bmfleet.on_lost = lambda cam: note_fault("%s dropped while recording" % cam.name)

bmfleet.shot = _iph_shot

c3_bm.ao_beacon = bmfleet.beacon

c3_bm.reservado = lambda ip: ip in bmfleet.hosts()

# --- Aparelhos acrescentados/retirados pelo operador -------------
# As Canon de origem (canon_ccapi.CAMERAS) e a BMPCC (CAMERA_ADDRESS) vem do codigo;
# o devices.json guarda o que o operador mudou: Canon acrescentadas, Canon de origem
# retiradas, e a BMPCC trocada. E lido uma vez, aqui, no arranque.
DEVICES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")

_dev_lock = threading.Lock()

_CANON_ORIGEM = dict(canon_ccapi.CAMERAS)          # id -> objecto, antes de retirar nada


_dev_cache = [None, None]                           # (mtime, dados)


_DEV_TIPOS = {"canons": dict, "saved": dict, "saved_bmpcc": dict, "alias": dict, "bmpcc": dict,
              "removed": list, "forgotten": list, "order": list}


def _dev_sanear(d):
    """O devices.json so com chaves do tipo certo (e os dicionarios de aparelhos so com valores dict): o resto largase."""
    out = {}
    for k, v in d.items():
        t = _DEV_TIPOS.get(k)
        if t is not None and not isinstance(v, t):
            continue
        if k in ("canons", "saved", "saved_bmpcc"):
            v = {a: b for a, b in v.items() if isinstance(b, dict)}
        elif k in ("removed", "forgotten"):
            v = [x for x in v if isinstance(x, str)]
        out[k] = v
    return out


def _dev_ler():
    """O devices.json (uma copia nova de cada vez; relido so se o ficheiro mudou)."""
    try:
        m = os.stat(DEVICES_JSON).st_mtime_ns
    except OSError:
        return {}
    if _dev_cache[0] != m:
        d = ler_dict(DEVICES_JSON)                      # dict, ou o do .bak; um tipo errado nao chega ao resto do codigo
        if d is None:
            return {}
        _dev_cache[:] = [m, _dev_sanear(d)]
    return json.loads(json.dumps(_dev_cache[1]))


def _dev_gravar(d):
    tmp = gravar.tmp_unico(DEVICES_JSON)
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    gravar.trocar(tmp, DEVICES_JSON)
    _dev_cache[0] = None


BMPCC_ACTIVE = True                                 # False: retirada (fica guardada)


def _dev_aplicar():
    global CAMERA_ADDRESS, BMPCC_ACTIVE
    d = _dev_ler()
    for cid in d.get("removed") or []:
        canon_ccapi.CAMERAS.pop(cid, None)
    for cid, c in (d.get("canons") or {}).items():
        try:
            canon_ccapi.CAMERAS[cid] = canon_ccapi.CanonCamera(
                c["name"], c["ip"], port=int(c.get("port") or 8080), use_https=bool(c.get("https")))
        except Exception as e:
            print(f"devices.json: Canon {cid} ignored: {e!r}")
    b = d.get("bmpcc") or {}
    if b.get("address") and isinstance(b["address"], str):
        CAMERA_ADDRESS = b["address"]
    BMPCC_ACTIVE = b.get("active", True) is not False


_dev_aplicar()


def _dev_canon_removidos(d):
    """As Canon retiradas: id -> (objecto de origem | dict guardado)."""
    out = {cid: _CANON_ORIGEM[cid] for cid in (d.get("removed") or [])
           if cid in _CANON_ORIGEM and cid not in (d.get("forgotten") or [])}
    out.update({cid: c for cid, c in (d.get("saved") or {}).items()})
    return out


def _dev_resolve(cid):
    """(tipo, handle, estado) do device `cid`; estado = "ativo" | "removido"; (None, None, None) se o id nao existe (nem na
    lista dos retirados). O handle e o objecto da camara (canon, iphone, gopro, bmpcc_extra), o proprio F6, ou, num device
    retirado, a sua configuracao guardada. A BMPCC principal tem o id fixo "bmpcc" (retirada: estado "removido")."""
    if not isinstance(cid, str) or not cid:
        return None, None, None
    if cid == "f6":
        return "f6", f6, "ativo"
    if cid == "bmpcc":
        return "bmpcc", None, ("ativo" if BMPCC_ACTIVE else "removido")
    cam = canon_ccapi.CAMERAS.get(cid)
    if cam is not None:
        return "canon", cam, "ativo"
    for tipo, frota in (("iphone", iphs), ("gopro", gofleet), ("bmpcc_extra", bmfleet), ("sony", sonys)):
        cam = frota.get(cid)
        if cam is not None:
            return tipo, cam, "ativo"
    d = _dev_ler()
    c = _dev_canon_removidos(d).get(cid)
    if c is not None:
        return "canon", c, "removido"
    if cid in (d.get("saved_bmpcc") or {}):
        return "bmpcc", d["saved_bmpcc"][cid], "removido"
    c = _dev_iphones_tirados_cfg().get(cid)
    if c is not None:
        return "iphone", c, "removido"
    c = sonys.removed_list().get(cid)
    if c is not None:
        return "sony", c, "removido"
    return None, None, None


def _dev_iphones_tirados_cfg():
    try:
        return iphs.removed_list()
    except Exception:
        return {}


def _ref_para(source):
    """O nome curto de referencia da camara que da o nome a esta linha do shoot log."""
    try:
        alias = _dev_ler().get("alias") or {}
        if not alias:
            return ""
        if source == "BMPCC":
            dev = "bmpcc"
        elif source == "F6":
            dev = "f6"
        else:
            dev = (next((k for k, c in list(canon_ccapi.CAMERAS.items()) if c.name == source), None)
                   or next((k for k, c in iphs.cams.items() if c.name == source), None)
                   or next((k for k, c in gofleet.cams.items() if c.name == source), None)
                   or next((k for k, c in sonys.cams.items() if c.name == source), None)
                   or next((k for k, c in list(bmfleet.cams.items()) if c.name == source), None))
        return alias.get(dev, "") if dev else ""
    except Exception:
        return ""


def _bmpcc_livre():
    """None se a BMPCC se pode mexer; senao, porque nao."""
    if state.get("camera_connected") or state.get("camera_connecting") or state.get("is_recording"):
        return "disconnect the BMPCC first (Bluetooth > Disconnect)"
    return None


def _bmpcc_guardar(d):
    """Poe a BMPCC em servico na lista guardada (com o nome de referencia dela)."""
    if not BMPCC_ACTIVE or not CAMERA_ADDRESS:
        return
    saved = d.setdefault("saved_bmpcc", {})
    if any(v.get("address") == CAMERA_ADDRESS for v in saved.values()):
        return
    chave = "b" + CAMERA_ADDRESS.replace(":", "").lower()[-6:]
    n = 2
    while chave in saved or _dev_resolve(chave)[0] is not None:       # o id e unico em toda a API
        chave, n = "b" + CAMERA_ADDRESS.replace(":", "").lower()[-6:] + str(n), n + 1
    saved[chave] = {"address": CAMERA_ADDRESS, "alias": (d.get("alias") or {}).get("bmpcc", "")}


def _bmpcc_activar(d, addr, alias):
    """Esta BMPCC passa a ser a de servico (a anterior, se havia, ja ficou guardada)."""
    global CAMERA_ADDRESS, BMPCC_ACTIVE
    for k in [k for k, v in (d.get("saved_bmpcc") or {}).items() if v.get("address") == addr]:
        d["saved_bmpcc"].pop(k)
    d["bmpcc"] = {"address": addr, "active": True}
    if alias:
        d.setdefault("alias", {})["bmpcc"] = alias
    else:
        (d.get("alias") or {}).pop("bmpcc", None)
    CAMERA_ADDRESS, BMPCC_ACTIVE = addr, True


def _bmpcc_gravar(d):
    try:
        _dev_gravar(d)
    except OSError as e:
        return jsonify({"ok": False, "error": f"could not save devices.json: {e}"}), 500
    return None


def _remove_bmpcc():
    """Retira a BMPCC desta sessao: fica guardada, sem ligacao nem alvo de REC."""
    global BMPCC_ACTIVE
    if not BMPCC_ACTIVE:
        return jsonify({"ok": False, "error": "no BMPCC in service"}), 409
    porque = _bmpcc_livre()
    if porque:
        return jsonify({"ok": False, "error": porque}), 409
    with _dev_lock:
        d = _dev_ler()
        _bmpcc_guardar(d)
        d["bmpcc"] = {"address": CAMERA_ADDRESS, "active": False}
        (d.get("alias") or {}).pop("bmpcc", None)
        erro = _bmpcc_gravar(d)
        if erro:
            return erro
        BMPCC_ACTIVE = False
        state["_auto_reconnect"] = False
        try:
            _alvos_tirar("bmpcc")
        except Exception:
            pass
    log_event("Devices: BMPCC removed (kept in the saved list)")
    return jsonify({"ok": True})


# --- fim dos aparelhos

f6_card.player.configure(f6_ltc.DEVICE, _f6_gen.pause)


def bm_rec_incerto():
    """A BMPCC caiu a gravar e ainda nao voltou (no maximo 30 min): conta como a gravar."""
    t = state.get("_bm_rec_incerto")
    return bool(t and time.time() - t < 1800)


def anything_recording():
    return bool(state.get("is_recording")) or bm_rec_incerto() or f6.running() or iphs.any_running() or gofleet.any_running() or bmfleet.any_running() or sonys.any_running() or any(
        c.a_gravar() for c in list(canon_ccapi.CAMERAS.values()))


def _run(cmd, timeout=45):
    """Corre um comando. Um programa que nao existe (o tailscale num Atom sem ele) ou que passa do tempo nao levanta
    excepcao (a rota rebentava com 500): volta como falha normal (127 / 124) com o motivo em stderr."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "%s is not installed" % cmd[0])
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "%s did not answer in %d s" % (cmd[0], timeout))
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 126, "", "%s: %s" % (cmd[0], e))


def _dongle_led():
    """Path of the USB wifi dongle's activity LED, or None.

    Looked up rather than hardcoded: the phy number moves with the hardware
    present (it was phy1 with the internal wifi enabled).
    """
    import glob
    for path in sorted(glob.glob("/sys/class/leds/ath9k_htc-phy*")):
        return path
    return None


def apply_led(on):
    """The dongle blinks on every packet, which is a distraction on a dark set.
    'none' with zero brightness kills it; the throughput trigger restores it."""
    led = _dongle_led()
    if not led:
        return False
    try:
        phy = os.path.basename(led).split("-", 1)[1]
        with open(f"{led}/trigger", "w") as f:
            f.write(f"{phy}tpt" if on else "none")
        if not on:
            with open(f"{led}/brightness", "w") as f:
                f.write("0")
        return True
    except Exception as e:
        log_event(f"LED: {e}")
        return False


def _marcar(especie, nota=""):
    """good / no-good / mark na take que acabou de parar: a linha do shoot log com o timecode, e o F6 e os iPhones recebem a marca."""
    secs = ltc_secs_now()
    if secs is not None:
        tc = format_tc(secs, (state.get("_ltc_jam") or {}).get("fps") or 25)
    else:
        tc = time.strftime("%H:%M:%S")
    log_shot("MARK", especie, f"{tc}" + (f" | {nota}" if nota else ""))
    if "f6" in (state.get("rec_targets") or []):
        f6.on_mark(especie)          # Zoom F6: teclas USER do FRC-8
    for _cam in iphs.in_targets(state.get("rec_targets") or []):
        _cam.on_mark(especie)        # iPhones: goodTake na claquete do ultimo clip
    return tc


BMPCC_CONFIRMA_S = 5.0


def _rec_alvo(c):
    """O estado para onde o alvo vai (o que se lhe pediu, enquanto confirma), ou o que se ve. Um STOP de grupo logo a
    seguir a um START que ainda nao foi confirmado nao pode saltar essa camara por ela parecer parada."""
    f = getattr(c, "alvo", None)
    return f() if f else c.running()


_bmpcc_cmd_n = 0                        # sobe a cada REC/STOP da BMPCC: a confirmacao de um comando ultrapassado desiste


def _bmpcc_confirmar(on, n=None):
    """Depois do REC/STOP da BMPCC: a camara tem de o confirmar (pacote de transporte). Sem isso, antes nada avisava:
    a take seguia sem a BMPCC e so se via depois."""
    # O STOP pode demorar ate BMPCC_STOP_JANELA_S a ser confirmado (mediu-se 15 s): so o START usa a janela curta.
    janela = BMPCC_CONFIRMA_S if on else max(BMPCC_CONFIRMA_S, BMPCC_STOP_JANELA_S)
    fim = time.time() + janela
    while time.time() < fim:
        if bool(state.get("is_recording")) == on or not state.get("camera_connected"):
            return                      # confirmou, ou caiu (a queda ja da o seu aviso)
        if n is not None and n != _bmpcc_cmd_n:
            return                      # REC->STOP rapido: o comando mais novo tem a sua propria confirmacao
        time.sleep(0.1)
    if n is not None and n != _bmpcc_cmd_n:
        return
    note_fault("BMPCC did not confirm %s in %.0f s" % ("REC" if on else "STOP", janela))


def _a_gravar_ids():
    """Os ids de tudo o que esta a gravar agora (armado ou nao)."""
    ids = {cid for cid, c in list(canon_ccapi.CAMERAS.items()) if c.state["connected"] and c.state["recording"]}
    if state.get("camera_connected") and state.get("is_recording"):
        ids.add("bmpcc")
    if f6.running():
        ids.add("f6")
    for frota in (iphs, gofleet, bmfleet, sonys):
        for cid in frota.ids():
            c = frota.get(cid)
            if c is not None and _rec_alvo(c):          # alvo, nao so running: um START por confirmar ja conta
                ids.add(cid)
    return ids


def _do_rec_group(targets, grupo=False):
    """Bring the recording-capable targets into the same state in one go.
    Camaras fora do conjunto ficam exactamente como estao -- excepto no REC de grupo (`grupo`, o botao REC dos alvos),
    que a parar para tudo o que grava, armado ou nao (ver abaixo). O REC de um so device nunca mexe nos outros.
    """
    targets = set(targets or [])
    # "Se alguma coisa grava, o toque seguinte para tudo" vale tambem para o que se desarmou a meio da take: antes, com
    # as camaras a gravar desarmadas, o REC ja nao as via, tentava ARRANCAR as armadas (ou dizia "None of the selected
    # cameras is ready") e as desarmadas ficavam a gravar sem STOP de grupo.
    chave = "grupo" if grupo else ",".join(sorted(targets))      # a intencao do REC e deste REC, nao de outro
    fora = (_a_gravar_ids() - targets) if grupo else set()
    if fora:
        log_event("REC: also stopping %s (recording but not armed)" % ", ".join(sorted(fora)))
        targets |= fora
    canon_targets = [c for cid, c in list(canon_ccapi.CAMERAS.items())
                     if c.state["connected"] and cid in targets]
    canon_video = [c for c in canon_targets if c.state["movie_mode"]]
    for c in canon_targets:
        if c not in canon_video:
            log_event(f"REC: {c.name} skipped — not in video mode: switch the camera to video")
    bmpcc_connected = ("bmpcc" in targets and state["camera_connected"]
                       and camera_char_path)
    # F6: pronto (jam valido e saida a tocar), ou a gravar -- parar tem de
    # funcionar mesmo que o jam se tenha perdido entretanto.
    f6_on = "f6" in targets and (f6.ready()[0] or f6.running())
    if "f6" in targets and not f6_on:
        log_event(f"REC: F6 skipped — {f6.ready()[1]}")
    iph_alvo = iphs.in_targets(targets) + gofleet.in_targets(targets) + bmfleet.in_targets(targets) + sonys.in_targets(targets)
    iph_on = [c for c in iph_alvo if c.ready()[0] or c.running()]
    for c in iph_alvo:
        if c not in iph_on:
            log_event(f"REC: {c.name} skipped — {c.ready()[1]}")
    if not bmpcc_connected and not canon_video and not f6_on and not iph_on:
        return False, {"error": "None of the selected cameras is ready to record"}

    recording_states = [c.state["recording"] for c in canon_video]
    if bmpcc_connected:
        recording_states.append(state["is_recording"])
    if f6_on:
        recording_states.append(f6.alvo())
    recording_states.extend(_rec_alvo(c) for c in iph_on)
    observed = any(recording_states)
    # "If ANYTHING is rolling, the next press stops everything." The obvious
    # alternative — start unless ALL are already recording — reads fine until
    # one camera fails to start: the states go mixed, and the operator's next
    # press, meant to stop the take, starts the stragglers instead. Seen for
    # real in testing, with an R6 II left rolling. Stopping must always be one
    # press away; retrying a camera that failed is what its own REC button is
    # for.
    want_recording, telemetria_atrasada = (
        next_rec_direction(observed, chave) if (bmpcc_connected or canon_video or f6_on or bool(iph_on))
        else (False, False))
    # F6 nos alvos e sem nenhuma pista armada: a take nao arranca (so o START; parar passa sempre).
    # _f6_sem_pistas_bloqueia
    if (want_recording and "f6" in targets and hasattr(f6, "armed_any")
            and f6.ready()[0] and f6.armed_any() is False):
        state.setdefault("_rec_intent", {}).pop(chave, None)
        log_event("REC refused: the F6 is a REC target with no track armed")
        return False, {"error": "F6: no track armed. Arm a track on the F6, or take the F6 out of the REC targets"}

    # Um REC/STOP do operador e um take novo: o take de antes do reinicio ja nao se retoma. So aqui, quando o pedido vai
    # mesmo ser tratado (um 404/409 antes de chegar a este ponto nao o desfaz).
    if _take_retomar:
        _rec_do_operador_cancela()

    # Fire every body concurrently. Each Canon toggle is one or two blocking
    # CCAPI round-trips (moviemode + recbutton, 6s timeout each), so doing them
    # in a serial loop staggered the start of each camera by seconds — bad for
    # multicam. The BMPCC's BLE write happens on this thread in parallel with
    # them; it still lands first because BLE is far quicker than CCAPI, but the
    # Canons no longer queue up behind each other.
    acted = []
    threads = []
    for cam in canon_video:
        if telemetria_atrasada or cam.state["recording"] != want_recording:
            def toggle(c=cam):
                try:
                    # Explicito: o toggle decidia pelo estado em cache, que pode estar atrasado, e invertia o pedido
                    # (um STOP que chegava com o START ainda a correr ligava a camara outra vez).
                    c.start_recording() if want_recording else c.stop_recording()
                    # O stop_recording() engole os erros (so regista a falha): o estado e que diz se a camara aceitou.
                    # O START nao: um primeiro envio falhado fica a ser repetido em segundo plano.
                    if want_recording or (c.state["connected"] and not c.state["recording"]):
                        acted.append(c.name)
                    else:
                        log_event(f"REC ALL: {c.name} did not stop")
                except Exception as e:
                    log_event(f"REC ALL: {c.name} error: {e}")
            t = threading.Thread(target=toggle, daemon=True)
            t.start()
            threads.append(t)


    # iPhones, GoPro e BMPCC extra: arrancam ja, ao mesmo tempo que as Canons (antes iam depois da BMPCC e do F6 e
    # cada um podia prender o REC 10 s). Cada thread regista a sua linha no shoot log quando a camara confirma, mesmo
    # que seja depois de o pedido ter respondido (uma GoPro confirma o STOP em ate ~6 s): nada fica por registar.
    def _iph_cmd(c):
        try:
            ok_i, err_i = c.set_running(want_recording)
        except Exception as e:                      # pylint: disable=broad-except
            ok_i, err_i = False, str(e)
        if ok_i:
            acted.append(c.name)
            log_shot(c.name, "start" if want_recording else "stop")
            if want_recording:
                c.take_no = state.get("_take_no")
        elif str(err_i).startswith("superseded"):
            # Ultrapassado por um REC/STOP mais recente (cliques rapidos): o comando mais novo fala por si.
            log_event(f"REC ALL: {c.name} {'start' if want_recording else 'stop'} superseded by a newer command")
        else:
            log_event(f"REC ALL: {c.name} not {'started' if want_recording else 'stopped'} — {err_i}")
            # Antes so ficava no registo de eventos: uma camara que nao arrancou no take nao dava aviso nenhum.
            note_fault(f"{c.name} did not {'start' if want_recording else 'stop'}: {err_i}")
    for c in [c for c in iph_on if _rec_alvo(c) != want_recording]:
        t = threading.Thread(target=_iph_cmd, args=(c,), daemon=True)
        t.start()
        threads.append(t)

    if bmpcc_connected and (telemetria_atrasada
                            or state["is_recording"] != want_recording):
        global _bmpcc_cmd_n
        _bmpcc_cmd_n += 1
        n_bm = _bmpcc_cmd_n

        def _bmpcc_cmd():
            try:
                write_camera(get_toggle_record_command(want_recording))
                acted.append("BMPCC")
                threading.Thread(target=_bmpcc_confirmar, args=(want_recording, n_bm), daemon=True).start()
            except Exception as e:                  # pylint: disable=broad-except
                log_event(f"REC ALL: BMPCC write error: {e}")
                note_fault(f"BMPCC did not {'start' if want_recording else 'stop'}: {e}")
        t = threading.Thread(target=_bmpcc_cmd, daemon=True)
        t.start()
        threads.append(t)

    # O estado do F6 e o que mandamos (nao ha telemetria dele): exacto. Tambem em paralelo: o set_running do F6 e
    # uma conversa com o gravador e nao pode atrasar o resto.
    if f6_on and f6.alvo() != want_recording:
        def _f6_cmd():
            try:
                ok_f6, err_f6 = f6.set_running(want_recording)
            except Exception as e:                  # pylint: disable=broad-except
                ok_f6, err_f6 = False, str(e)
            if ok_f6:
                acted.append("F6")
                # Como as camaras: o start abre take no shoot log.
                log_shot("F6", "start" if want_recording else "stop")
            else:
                log_event(f"REC ALL: F6 not {'started' if want_recording else 'stopped'} — {err_f6}")
                note_fault(f"F6 did not {'start' if want_recording else 'stop'}: {err_f6}")
        t = threading.Thread(target=_f6_cmd, daemon=True)
        t.start()
        threads.append(t)

    # Bounded wait — a camera that is slow to answer keeps working in the
    # background (canon_ccapi retries on its own timers) rather than holding
    # this HTTP response open; the UI's status poll picks up the result.
    deadline = time.monotonic() + 5
    for t in threads:
        t.join(timeout=max(0, deadline - time.monotonic()))

    # So se alguem aceitou o comando (ou ainda esta a tratar dele): um STOP que falhou em todas tem de continuar a ser um
    # STOP no toque seguinte, em vez de a intencao "ja parou" o transformar em START.
    if acted or any(t.is_alive() for t in threads):
        note_rec_intent(want_recording, chave)
    log_event(f"REC ALL: {'START' if want_recording else 'STOP'} ({', '.join(acted) or 'nothing changed'})")
    return True, {"ok": True, "recording": want_recording, "cameras": acted}


def _gp_gw(d):
    """O gateway S3 a que o pedido se refere (campo gw). None = omitido (o primeiro que existir); 0 = invalido."""
    v = (d or {}).get("gw")
    if v in (None, ""):
        return None
    try:
        if isinstance(v, bool) or (isinstance(v, float) and v != int(v)):  # true, 1.5...: nao e um numero de gateway
            return 0
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


def _gp_gw_ok(d):
    """(n, None) se o gw do pedido serve; senao (None, resposta de erro): 400 se o valor e invalido (nao se cai no
    primeiro gateway em silencio), 404 se o numero e valido mas nao existe. gw omitido = o primeiro que existir."""
    n = _gp_gw(d)
    if n is not None and n < 1:
        return None, (jsonify({"ok": False, "error": "gw: invalid"}), 400)
    if gofleet.gw(n) is None:
        return None, (jsonify({"ok": False, "error": "no such gateway"}), 404)
    return n, None


def anotar_clip(source, take, clip):
    """O nome do ficheiro gravado vai para a linha `stop` desse take e dessa camara (coluna `clip`).

    So se a linha ainda estiver sem clip: um STOP repetido ou um nome ja anotado nao mudam nada. Se a linha nao existir
    (a camara parou sem o bridge a ter registado), fica so no registo de eventos.
    """
    take = str(take or "").strip()

    def mutate(rows):
        for r in reversed(rows):
            if r["source"] == source and r["action"] == "stop" and r["take"] == take:
                if r["clip"]:
                    return None
                r["clip"] = clip
                return rows, "clip %s on take %s (%s)" % (clip, take, source)
        return None

    if not take or not clip:
        return False
    desc, erro = _rewrite_shoot_log(mutate)
    if erro:
        log_event("Clip name not written for %s take %s (%s): %s" % (source, take, clip, erro))
        return False
    log_event(desc)
    return True


def _rewrite_shoot_log(mutate):
    """Le o CSV, aplica `mutate` as linhas e reescreve-o inteiro, em seguranca.

    Escreve-se para um ficheiro ao lado e so depois se troca o nome, com fsync
    dos dois e do directorio. Reescrever por cima seria arriscar o unico
    registo que liga o material a linha temporal: uma falha de energia a meio
    deixaria o ficheiro truncado, e o log e a razao de este bridge existir.

    `mutate` recebe a lista de linhas e devolve (novas_linhas, descricao) ou
    None se nao houver nada a mudar.
    """
    with _shoot_log_lock:
        try:
            with open(SHOOT_LOG_PATH, newline="") as f:
                rows = [{k: r.get(k, "") for k in SHOOT_LOG_FIELDS}
                        for r in csv.DictReader(f)]
        except FileNotFoundError:
            return None, "shoot log is empty"
        out = mutate(rows)
        if out is None:
            return None, "entry not found"
        novas, descricao = out
        tmp = SHOOT_LOG_PATH + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(SHOOT_LOG_FIELDS)
            for r in novas:
                w.writerow([r[k] for k in SHOOT_LOG_FIELDS])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SHOOT_LOG_PATH)
        try:
            d = os.open(os.path.dirname(SHOOT_LOG_PATH), os.O_RDONLY)
            os.fsync(d)
            os.close(d)
        except OSError:
            pass
        return descricao, None


def _nota_no_log(texto, take="", fonte="NOTE"):
    """Escreve uma nota no shoot log (com o timecode do instante); com `take`, arquiva-a nessa take. Devolve o id da linha."""
    tc = ""
    try:
        secs = ltc_secs_now()
        if secs is not None:
            tc = format_tc(secs, (state.get("_ltc_jam") or {}).get("fps") or 25)
    except Exception:
        pass
    rid = log_shot(fonte, "note", (f"{tc} | " if tc else "") + texto)
    if take and rid:
        def mutate(rows):
            for r in rows:
                if r["id"] == rid:
                    r["take"] = take
                    return rows, f"note filed under take {take}"
            return None

        _rewrite_shoot_log(mutate)
    return rid



def _resolve_bluetooth_adapters():
    """Resolve a BMPCC e actualiza os globals -- corre em fundo,
    ver a nota no arranque em __main__ sobre porque isto ja nao bloqueia
    o Flask nem o watchdog.
    """
    global CAMERA_ADAPTER
    # A numeracao dos adaptadores nao e estavel: desactivar o Bluetooth
    # interno promove o dongle USB de hci1 para hci0. Guardar aqui, ANTES
    # de resolve_adapter() passar por cima dos globals, evita partir em
    # silencio quando o hardware muda.
    ADAPTADORES_PREFERIDOS.update({"BMPCC": CAMERA_ADAPTER})
    CAMERA_ADAPTER = resolve_adapter(CAMERA_ADAPTER)
    log_event(f"Bluetooth: BMPCC on {CAMERA_ADAPTER}")


# === Modo de treino (simulador.py) ===
# Liga, dentro do proprio bridge, um S3 com 2 GoPro e um C3 com uma BMPCC simulados (no Atom, 127.0.0.1) e junta-os
# ao rig como "DEMO ...": a consola, a pagina e o iPad funcionam como com equipamento, para treinar ou mostrar. Desligar
# tira-os (e tira-os dos alvos de REC). Nao sobrevive a um reinicio: no arranque, o que tiver ficado sai.
DEMO_S3_PORTA, DEMO_C3_PORTA = 8170, 8172


def _demo_hosts():
    return "127.0.0.1:%d" % DEMO_S3_PORTA, "127.0.0.1:%d" % DEMO_C3_PORTA


def _demo_tirar_do_rig():
    """Tira do rig os gateways e as camaras de treino (os desta vez ou os que ficaram de um arranque anterior)."""
    s3, c3 = _demo_hosts()
    ids = []
    for n, g in list(gofleet.gws.items()):
        if g.host == s3:
            ids += [cid for cid, c in list(gofleet.cams.items()) if c.fleet is g]
            gofleet.remove_gateway(n)
            gofleet.memoria.pop(s3, None)
    for cid, cam in list(bmfleet.cams.items()):
        if cam.host == c3:
            bmfleet.remove(cid)
            bmfleet.memoria.pop(c3, None)
            ids.append(cid)
    gofleet.save()
    bmfleet.save()
    if ids:
        _alvos_tirar(ids)
        save_settings()


def main():
    """Arranque (chamado pelo bridge.py)."""
    log_event("Camera Bridge starting...")
    load_settings()
    carregar_event_log()
    _aparar_event_log()
    migrar_shoot_log()
    restaurar_contagem_takes()
    take_aberto_ao_arrancar()
    _demo_tirar_do_rig()                    # o modo de treino nao sobrevive a um reinicio
    vigiar_condicoes()
    # O watchdog (heartbeat ao systemd a cada 10s) e o Flask arrancam JA,
    # antes do Bluetooth estar resolvido -- nao depois. resolve_adapter()
    # pode legitimamente demorar ate ADAPTER_WAIT_SECONDS*2 (40s) por
    # adaptador (o dongle so aparece depois do btusb-atrasado.service, que
    # dorme 25s de proposito), e o WatchdogSec do systemd e 45s. Bloquear
    # o arranque nisto tirava o bridge INTEIRO do ar -- incluindo o
    # controlo das Canon por wifi, que nao depende disto nada -- e podia
    # estourar o watchdog antes de ele sequer comecar a bater. Visto ao
    # vivo 2026-09-16: o dongle BT falhou a inicializar (cabo de extensao
    # USB) e o bridge ficou preso num ciclo de crash, incapaz de servir a
    # pagina a ninguem. Resolver o Bluetooth em fundo, numa thread propria,
    # corrige isto: o UI e as Canon ficam operacionais de imediato, o
    # Bluetooth junta-se quando (e se) estiver pronto.
    threading.Thread(target=vigiado(_camera_watchdog, "Watchdog", log_event, 2.0), daemon=True).start()
    threading.Thread(target=vigiado(canon_ccapi.watchdog, "Canon watchdog", log_event), daemon=True).start()
    threading.Thread(target=_resolve_bluetooth_adapters, daemon=True).start()
    f6.start()
    iphs.start()
    gofleet.start()
    sonys.start()
    bmfleet.start()
    threading.Thread(target=_gw_fw_ciclo, daemon=True, name="gw-fw").start()
    threading.Thread(target=_claquete_ciclo, daemon=True, name="claquete").start()
    # threaded=True is required once the Canon live view MJPEG stream is
    # open — that request stays connected for as long as the viewer has it
    # on screen, and would otherwise block every other request (status
    # polling, REC button, ...) on this single-threaded dev server.
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)


# As rotas por area: registam-se no app ao importar. Vem no fim, quando tudo o que usam do nucleo ja existe.
import rotas_paginas  # noqa: E402,F401
import rotas_aparelhos  # noqa: E402,F401
import rotas_gateways  # noqa: E402,F401
import rotas_relogio  # noqa: E402,F401
import rotas_registos  # noqa: E402,F401
import rotas_claquete  # noqa: E402,F401
import rotas_sistema  # noqa: E402,F401

import bluetooth, time, json, os, machine, gc
import netcfg
from micropython import const

# O mesmo ficheiro serve o Stamp-S3A (com LED RGB) e a XIAO ESP32-C3 (sem LED
# de estado; e o G38 nem existe no C3).
IS_S3 = "ESP32S3" in os.uname().machine

# Em campo nao ha ninguem a ler a USB: prints sem leitor podem encravar o
# CDC (micropython#18000). Ligar so para depurar via mpremote.
DEBUG = False

def dprint(*a):
    if DEBUG:
        print(*a)

# ---- diagnostico em flash: causa de cada arranque + "vivo aos N s" ----
# Distingue falta de energia (causa=ligar, ultimo vivo ~ quando morreu) de
# crash (watchdog/hard). Le-se com: mpremote fs cat :diag.log
DIAG_FILE = "diag.log"
ALIVE_FILE = "alive.txt"
ALIVE_EVERY_MS = 30000
_CAUSES = {machine.PWRON_RESET: "power-on", machine.HARD_RESET: "hard",
           machine.WDT_RESET: "watchdog", machine.DEEPSLEEP_RESET: "deepsleep",
           machine.SOFT_RESET: "soft"}

def _diag_boot():
    try:
        prev = open(ALIVE_FILE).read().strip()
    except Exception:
        prev = "?"
    try:
        lines = open(DIAG_FILE).read().splitlines()[-19:]
    except Exception:
        lines = []
    cause = machine.reset_cause()
    lines.append("boot cause=%s(%d) last_alive_before=[%s]" % (_CAUSES.get(cause, "?"), cause, prev))
    with open(DIAG_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(ALIVE_FILE, "w") as f:
        f.write("0s")

_diag_boot()
_last_alive = time.ticks_ms()

# ---- watchdog e firmware novo em teste (OTA: boot.py) ----
# Ha sempre um watchdog (30 s), alimentado no ciclo principal: um ciclo encravado reinicia o C3. No ESP32 so ha um:
# pedir outro reconfigura o mesmo (o do boot.py, em teste, passa a 30 s). Com o fw_trial o firmware fica dado por
# bom com o Wi-Fi ligado 60 s. Se em 5 min nao ligar: procura as redes; se nenhuma das guardadas esta ao alcance (o
# Atom desligado, por exemplo) a culpa nao e do firmware e fica dado por bom; se alguma esta ao alcance e mesmo assim
# nao ligou (ou a procura falha), reinicia (conta como arranque falhado e o boot.py acaba por repor o anterior).
WDT_MS = 30000
FW_TRIAL = "fw_trial"
try:
    os.stat(FW_TRIAL)
    _fw_trial = True
except OSError:
    _fw_trial = False
_wdt = machine.WDT(timeout=WDT_MS)
_boot_ms = time.ticks_ms()

def _fw_aceitar(porque):
    global _fw_trial
    try:
        os.remove(FW_TRIAL)
    except OSError:
        pass
    _fw_trial = False
    _diag("OTA: new firmware accepted (%s)" % porque)

def _redes_ausentes():
    """True se nenhuma das redes guardadas aparece numa procura. False se alguma aparece ou se a procura falha."""
    nets = [n[0] for n in netcfg.cfg["nets"]]
    if not nets:
        return True
    try:
        try:
            netcfg._sta.disconnect()    # a meio de uma tentativa o ESP32 recusa procurar; o netcfg volta a tentar depois
        except Exception:
            pass
        _wdt.feed()
        vistas = [r[0].decode("utf-8", "ignore") for r in netcfg._sta.scan()]
        _wdt.feed()
    except Exception:
        return False
    return not any(n in vistas for n in nets)

def fw_watch():
    _wdt.feed()
    if not _fw_trial:
        return
    up = time.ticks_diff(time.ticks_ms(), _boot_ms)
    if netcfg.state == "sta_up" and up > 60000:
        _fw_aceitar("Wi-Fi up")
    elif up > 300000:
        if _redes_ausentes():
            _fw_aceitar("no saved network in range: not the firmware's fault")
        else:
            _diag("OTA: new firmware did not join a network that is in range: restarting")
            machine.reset()

def diag_alive():
    global _last_alive
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_alive) < ALIVE_EVERY_MS:
        return
    _last_alive = now
    try:
        with open(ALIVE_FILE, "w") as f:
            f.write("%ds fiz=%s bm=%s" % (now // 1000, fiz["phase"], bm["phase"]))
    except Exception:
        pass

# ---- identidades (de camera_bridge.py) ----
FIZ_MAC = bytes([0xF4, 0x4E, 0xFD, 0x00, 0x41, 0x77])
FIZ_NOTIFY_UUID = bluetooth.UUID("69400002-b5a3-f393-e0a9-e50e24dcca99")

BM_MAC = bytes([0xCC, 0x86, 0xEC, 0x2F, 0x15, 0x50])     # a de sempre; a camera escolhida fica em bm.json
BM_FILE = "bm.json"
# Blackmagic Camera Service: as BM anunciam-no (em little-endian no anuncio). Serve para as por no topo da lista.
BM_SERVICE_ADV = bytes(reversed(bytes.fromhex("291d567a6d7511e68b7786f30ca893d3")))
BM_OUTGOING_UUID = bluetooth.UUID("5dd3465f-1aee-4299-8493-d2eca2f8e1bb")
BM_INCOMING_UUID = bluetooth.UUID("b864e140-76a0-416a-bf30-5876504537d9")
# A BM mostra este nome no menu Bluetooth Setup (UTF-8, max 32 bytes).
BM_DEVICE_NAME_UUID = bluetooth.UUID("ffac0c52-c9fb-41a0-b063-cc76282eb89c")
# Timecode da BM (notifica). No modo Bridge vai para o Atom com o resto.
BM_TIMECODE_UUID = bluetooth.UUID("6d8f2110-86f1-41bf-9afb-451d87e976c8")
DEVICE_NAME = "FIZ Bridge" if IS_S3 else "FIZ Bridge C3"

def rec_command(start):
    data = bytearray([255, 5, 0, 0, 10, 1, 1, 0, 0, 0, 0, 0])
    data[8] = 2 if start else 0
    return bytes(data)

FIZ_RETRY_MIN = 5000
FIZ_RETRY_MAX = 60000
BM_RETRY_MIN = 3000
BM_RETRY_MAX = 30000
SCAN_WINDOW_MS = 4000
CONNECT_TIMEOUT_MS = 8000
ENCRYPT_TIMEOUT_MS = 15000
DISCOVER_TIMEOUT_MS = 10000
# Cada tentativa de cifra falhada faz a BM mostrar um codigo no ecra. Com o
# bond perdido, insistir encheria o ecra de pedidos a meio de uma filmagem.
BM_MAX_ENC_FAILS = 3

_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)
_IRQ_PERIPHERAL_CONNECT = const(7)
_IRQ_PERIPHERAL_DISCONNECT = const(8)
_IRQ_GATTC_SERVICE_RESULT = const(9)
_IRQ_GATTC_SERVICE_DONE = const(10)
_IRQ_GATTC_CHARACTERISTIC_RESULT = const(11)
_IRQ_GATTC_CHARACTERISTIC_DONE = const(12)
_IRQ_GATTC_WRITE_DONE = const(17)
_IRQ_GATTC_NOTIFY = const(18)
_IRQ_GATTC_INDICATE = const(19)

_PROP_NOTIFY = const(0x10)
_PROP_INDICATE = const(0x20)
_IRQ_ENCRYPTION_UPDATE = const(28)
_IRQ_GET_SECRET = const(29)
_IRQ_SET_SECRET = const(30)
_IRQ_PASSKEY_ACTION = const(31)

# ---- bonds (chaves de emparelhamento), persistidas na flash ----
SECRETS_FILE = "ble_secrets.json"

def load_secrets():
    try:
        with open(SECRETS_FILE) as f:
            return {(t, bytes.fromhex(k)): bytes.fromhex(v) for t, k, v in json.load(f)}
    except Exception:
        return {}

def save_secrets():
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([[k[0], k[1].hex(), v.hex()] for k, v in secrets.items()], f)
    os.rename(tmp, SECRETS_FILE)

secrets = load_secrets()
dprint("saved bonds:", len(secrets))

def load_bm_mac():
    try:
        with open(BM_FILE) as f:
            m = bytes.fromhex(json.load(f)["mac"])
        return m if len(m) == 6 else BM_MAC
    except Exception:
        return BM_MAC

def save_bm_mac(mac):
    tmp = BM_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mac": mac.hex()}, f)
    os.rename(tmp, BM_FILE)

def _diag(txt):
    try:
        with open(DIAG_FILE, "a") as f:
            f.write(txt + "\n")
    except Exception:
        pass

# O Bluetooth liga-se ANTES do Wi-Fi e com a memoria arrumada: o controlador BLE do C3 precisa de um bloco grande
# da memoria do sistema e, com o Wi-Fi ja ligado, pode nao o encontrar ("BLE_INIT: hci inits failed", e o MicroPython
# rebenta). Se falhar mesmo assim, tenta-se com o Wi-Fi desligado (o netcfg liga-o depois).
gc.collect()
ble = bluetooth.BLE()
BLE_ERRO = ""                       # a ultima falha a ligar o Bluetooth (vai no /api/status e na pagina do C3)

def _mem_num():
    """(livre do MicroPython, maior bloco livre da memoria do sistema): o que o Wi-Fi e o Bluetooth precisam."""
    try:
        import esp32
        return gc.mem_free(), max((h[2] for h in esp32.idf_heap_info(esp32.HEAP_DATA)), default=0)
    except Exception:
        return gc.mem_free(), None

# A memoria vai no /api/status e no /api/bm/poll (o Atom avisa quando desce): mede-se de 5 em 5 s no ciclo principal.
MEM_EVERY_MS = 5000
mem_now = (None, None)
_mem_ms = None

def mem_tick():
    global mem_now, _mem_ms
    agora = time.ticks_ms()
    if _mem_ms is None or time.ticks_diff(agora, _mem_ms) >= MEM_EVERY_MS:
        _mem_ms = agora
        mem_now = _mem_num()

def _mem():
    """Memoria livre: a do MicroPython e o maior bloco livre da memoria do sistema (o que o Bluetooth precisa)."""
    try:
        import esp32
        maior = max((h[2] for h in esp32.idf_heap_info(esp32.HEAP_DATA)), default=0)
        return "mp %d, sys max %d" % (gc.mem_free(), maior)
    except Exception:
        return "mp %d" % gc.mem_free()

# Ligar o Bluetooth pode falhar (OSError EIO, visto no MicroPython 1.29 do C3). Tenta-se algumas vezes; se nao der,
# o programa segue sem Bluetooth: o Wi-Fi arranca na mesma (o Atom ve o C3 e o erro) e reinicia-se daqui a 2 min.
# Se o arranque anterior morreu a ligar o Bluetooth (o MicroPython do C3 rebenta - Guru Meditation - quando o controlador
# BLE nao arranca: "BLE_INIT: hci inits failed"), este arranque salta o Bluetooth: o Wi-Fi sobe, o Atom ve o C3
# (bm=ble_error) e o erro fica no diag.log. Tenta-se outra vez no arranque seguinte.
BLE_TRY = "ble_try"
BLE_SALTOS = "ble_skips"            # arranques seguidos sem Bluetooth (para nao reiniciar sem fim)
BLE_OK = False

def _ler_int(nome):
    try:
        return int(open(nome).read().strip() or "0")
    except Exception:
        return 0

def _escrever(nome, txt):
    try:
        with open(nome, "w") as f:
            f.write(txt)
    except Exception:
        pass

def _apagar(nome):
    try:
        os.remove(nome)
    except OSError:
        pass

# O marcador so conta se o arranque anterior acabou num crash: um corte de energia (ligar de novo) a meio do arranque
# do Bluetooth nao e um crash e nao pode deixar o C3 sem Bluetooth.
try:
    os.stat(BLE_TRY)
    _ble_saltar = machine.reset_cause() != machine.PWRON_RESET
except OSError:
    _ble_saltar = False
_apagar(BLE_TRY)
if _ble_saltar:
    _diag("previous boot crashed starting Bluetooth: this one starts without it")
try:
    BLE_OK = bool(ble.active())        # ja ligado (um boot.py da versao 16 a 18 ligava-o)
except Exception:
    BLE_OK = False
for _t in range(0 if (_ble_saltar or BLE_OK) else 4):
    if _t >= 1:                         # da 2.a tentativa em diante, com o Wi-Fi desligado e a memoria arrumada
        try:
            netcfg._sta.active(False)
            netcfg._ap.active(False)
        except Exception:
            pass
        gc.collect()
    _escrever(BLE_TRY, "1")             # so a volta do ble.active: e ai que o MicroPython do C3 pode rebentar
    try:
        ble.active(True)
        BLE_OK = True
        _apagar(BLE_TRY)
        if _t >= 1:
            _diag("BLE started on try %d (with Wi-Fi off)" % (_t + 1))
        break
    except OSError as e:
        _apagar(BLE_TRY)
        BLE_ERRO = "%s (try %d, %s)" % (e, _t + 1, _mem())
        _diag("BLE start failed: " + BLE_ERRO)
        try:
            ble.active(False)
        except Exception:
            pass
        time.sleep_ms(500 * (_t + 1))
if _ble_saltar:
    BLE_ERRO = "the previous boot crashed starting Bluetooth (%s)" % _mem()
# Sem Bluetooth: volta-se a tentar com um reinicio (2 min; ou 5 min se este arranque saltou o Bluetooth), no maximo 3
# vezes seguidas - depois fica sem Bluetooth (o Wi-Fi continua: o Atom ve o C3 com bm=ble_error).
_ble_saltos = 0 if BLE_OK else _ler_int(BLE_SALTOS) + 1
if BLE_OK:
    _apagar(BLE_SALTOS)
else:
    _escrever(BLE_SALTOS, str(_ble_saltos))
_ble_reiniciar_ms = None if (BLE_OK or _ble_saltos > 3) else (300000 if _ble_saltar else 120000)
# KEYBOARD_ONLY: a BM e DisplayOnly e exige codigo (Passkey Entry). Qualquer
# outra combinacao cai em Just Works e a BM recusa. Emparelhar: /api/cam/pair e
# /api/cam/code (a consola do Atom faz isso), ou pair_bm_kbd.py pela USB.
if BLE_OK:
    ble.config(bond=True, mitm=True, le_secure=False, io=2)
    ble.config(gap_name=DEVICE_NAME)

# fases: idle -> queued -> connecting -> [encrypting] -> discovering -> ready
#        broken (faltam caracteristicas), gave_up (BM sem bond valido)
fiz = {"name": "FIZ", "mac": FIZ_MAC, "notify_uuid": FIZ_NOTIFY_UUID, "write_uuid": None,
       "need_bond": False, "retry_min": FIZ_RETRY_MIN, "retry_max": FIZ_RETRY_MAX}
bm = {"name": "BM", "mac": load_bm_mac(), "notify_uuid": BM_INCOMING_UUID, "write_uuid": BM_OUTGOING_UUID,
      "need_bond": True, "retry_min": BM_RETRY_MIN, "retry_max": BM_RETRY_MAX}
all_links = (fiz, bm)
for _l in all_links:
    _l.update({"addr_type": None, "conn_handle": None, "notify_handle": None,
               "write_handle": None, "phase": "idle", "next_attempt": 0,
               "retry": _l["retry_min"], "enc_fails": 0, "phase_since": 0, "notify_props": 0,
               "name_handle": None, "tc_handle": None, "tc_props": 0,
               "_wq": [], "_wbusy": False, "_wsince": 0,
               "_svcs": [], "_svc_queue": [], "rssi": None, "rssi_ms": None})

links_by_conn = {}
scanning = False
connect_queue = []
connecting_link = None
connect_deadline = 0
# Scan, ligacao e descoberta GATT sao exclusivos para a pilha inteira, nao
# por ligacao -- pedir dois ao mesmo tempo da OSError(16) (visto ao vivo).
gatt_busy = False
gatt_queue = []

recording = False  # estado local; corrigido quando a BM notifica o transporte

# ---- proxy para o Atom (modo Bridge) ----
# Tudo o que a BM notifica (Incoming Camera Control: REC, bateria, ISO, slots,
# tempo restante, data...) fica numa fila curta que o Atom vai buscando por
# /api/bm/poll; o timecode so interessa o ultimo. O Atom descodifica como fazia
# com o Bluetooth dele -- aqui so se transportam bytes.
BM_RING = 32
bm_ev = []         # [(seq, bytes)]
bm_seq = 0
bm_tc = None
bm_tc_seq = 0

def bm_push(data):
    global bm_seq
    bm_seq += 1
    bm_ev.append((bm_seq, data))
    if len(bm_ev) > BM_RING:
        bm_ev.pop(0)

# Escritas GATT com resposta: uma de cada vez por ligacao (duas em simultaneo
# sao recusadas pela pilha). A fila serve o CCCD, o nome, o REC e o Atom.
WRITE_TIMEOUT_MS = 2000
# A fila tem limite (o Atom pode mandar mais depressa do que a BM aceita). O REC passa a frente; cheia, sai a
# escrita mais antiga do Atom que nao seja REC. Devolve False quando nao cabe (o Atom recebe o erro).
WQ_MAX = 8

def is_rec(data):
    return len(data) > 8 and data[0] == 255 and data[4] == 10 and data[5] == 1

def gatt_write(link, handle, data):
    q = link["_wq"]
    if len(q) >= WQ_MAX:
        for i in range(len(q)):
            # so as do Atom (pela caracteristica de escrita): o CCCD e o nome ficam
            if q[i][0] == link["write_handle"] and not is_rec(q[i][1]):
                q.pop(i)
                break
        else:
            return False
    if is_rec(data):
        i = 0
        while i < len(q) and is_rec(q[i][1]):      # atras dos REC ja na fila: a ordem entre eles mantem-se
            i += 1
        q.insert(i, (handle, data))
    else:
        q.append((handle, data))
    write_next(link)
    return True

def write_next(link):
    if link["_wbusy"] or not link["_wq"] or link["conn_handle"] is None:
        return
    handle, data = link["_wq"].pop(0)
    try:
        ble.gattc_write(link["conn_handle"], handle, data, 1)
        link["_wbusy"] = True
        link["_wsince"] = time.ticks_ms()
    except Exception as e:
        dprint(link["name"], "write failed:", e)
        link["_wbusy"] = False
        write_next(link)

def set_phase(link, phase):
    link["phase"] = phase
    link["phase_since"] = time.ticks_ms()

def by_mac(addr):
    addr = bytes(addr)
    if addr == FIZ_MAC:
        return fiz
    if addr == bm["mac"]:
        return bm
    return None

def wants_scan(link):
    return (link["conn_handle"] is None and link["phase"] == "idle"
            and time.ticks_diff(time.ticks_ms(), link["next_attempt"]) >= 0)

def schedule_retry(link):
    set_phase(link, "idle")
    link["next_attempt"] = time.ticks_add(time.ticks_ms(), link["retry"])
    link["retry"] = min(link["retry"] * 2, link["retry_max"])

def fiz_tick():
    """O FIZ so conta no modo autonomo: no Bridge quem grava e o Atom, o C3 nem procura o FIZ (e larga-o se estava
    ligado). Fase "off" enquanto estiver no Bridge; ao voltar ao autonomo procura-o logo."""
    if netcfg.bridge_mode():
        if fiz["conn_handle"] is not None:
            drop(fiz)
        elif fiz["phase"] in ("idle", "queued") and fiz is not connecting_link:
            if fiz in connect_queue:
                connect_queue.remove(fiz)
            set_phase(fiz, "off")
    elif fiz["phase"] == "off":
        fiz["retry"], fiz["next_attempt"], fiz["addr_type"] = fiz["retry_min"], time.ticks_ms(), None
        set_phase(fiz, "idle")

def start_scan_if_needed():
    global scanning
    if scanning or connecting_link is not None or gatt_busy:
        return
    if not any(wants_scan(l) for l in all_links):
        return
    scanning = True
    try:
        ble.gap_scan(SCAN_WINDOW_MS, 30000, 30000, True)
    except Exception as e:
        dprint("gap_scan failed:", e)
        scanning = False

def try_next_connect():
    global connecting_link, connect_deadline
    while connecting_link is None and connect_queue:
        link = connect_queue.pop(0)
        if link["conn_handle"] is not None:
            continue
        connecting_link = link
        connect_deadline = time.ticks_add(time.ticks_ms(), CONNECT_TIMEOUT_MS)
        set_phase(link, "connecting")
        dprint("connecting to", link["name"])
        try:
            ble.gap_connect(link["addr_type"], link["mac"])
        except Exception as e:
            dprint("gap_connect failed:", e)
            connecting_link = None
            schedule_retry(link)

def start_discovery(link):
    global gatt_busy
    if gatt_busy:
        gatt_queue.append(link)
        return
    gatt_busy = True
    set_phase(link, "discovering")
    link["_svcs"] = []
    try:
        ble.gattc_discover_services(link["conn_handle"])
    except Exception as e:
        dprint("discover failed:", e)
        discovery_failed(link)

def release_gatt():
    global gatt_busy
    gatt_busy = False
    while gatt_queue:
        nxt = gatt_queue.pop(0)
        if nxt["conn_handle"] is not None:
            start_discovery(nxt)
            return

def drop(link):
    if link["conn_handle"] is not None:
        try:
            ble.gap_disconnect(link["conn_handle"])
        except Exception:
            pass

def discover_next_service(link):
    if not link["_svc_queue"]:
        finish_discovery(link)
        return
    s, e = link["_svc_queue"].pop(0)
    try:
        ble.gattc_discover_characteristics(link["conn_handle"], s, e)
    except Exception as ex:
        dprint("discover characteristics failed:", ex)
        discovery_failed(link)

def discovery_failed(link):
    # Sai de "discovering" antes de largar: o DISCONNECT ja nao liberta a pilha outra vez (podia ser de outra ligacao).
    set_phase(link, "idle")
    release_gatt()
    drop(link)

def finish_discovery(link):
    ok = link["notify_handle"] is not None and (link["write_uuid"] is None or link["write_handle"] is not None)
    if not ok:
        dprint(link["name"], "characteristics missing")
        set_phase(link, "broken")
        release_gatt()
        drop(link)
        return
    # A BM pode entregar o estado por indicacao em vez de notificacao --
    # subscrever o que a caracteristica anuncia, senao o CCCD fica mudo.
    props = link["notify_props"]
    cccd = (1 if props & _PROP_NOTIFY else 0) | (2 if props & _PROP_INDICATE else 0)
    dprint(link["name"], "props=0x%02x cccd=%d" % (props, cccd))
    # Pela fila: CCCD do estado, CCCD do timecode, nome -- um de cada vez.
    link["_wq"], link["_wbusy"] = [], False
    gatt_write(link, link["notify_handle"] + 1, bytes([cccd or 1, 0]))
    if link["tc_handle"] is not None:
        tp = link["tc_props"]
        tcc = (1 if tp & _PROP_NOTIFY else 0) | (2 if tp & _PROP_INDICATE else 0)
        gatt_write(link, link["tc_handle"] + 1, bytes([tcc or 1, 0]))
    if link["name_handle"] is not None:
        gatt_write(link, link["name_handle"], DEVICE_NAME.encode())
    set_phase(link, "ready")
    link["retry"] = link["retry_min"]
    dprint(link["name"], "READY")
    release_gatt()

def on_notify(link, data):
    global recording
    if link is fiz:
        try:
            msg = data.decode("ascii").strip()
        except Exception:
            return
        dprint("FIZ:", msg)
        if msg == "AT+REC" and not netcfg.bridge_mode():
            # O FIZ e so do modo autonomo: dispara a BMPCC deste C3 (no Bridge quem grava e o Atom).
            send_rec(not recording)
        return
    bm_push(data)
    if len(data) > 8 and data[4] == 10 and data[5] == 1:
        real = data[8] == 2
        if real != recording:
            dprint("BM says: %s" % ("RECORDING" if real else "STOPPED"))
            recording = real

def send_rec(start):
    global recording
    if bm["phase"] != "ready":
        dprint("BM not ready -- REC ignored")
        return
    gatt_write(bm, bm["write_handle"], rec_command(start))
    recording = start
    dprint("-> BM:", "RECORD" if start else "STOP")

# Erros dentro do IRQ vao para o diag.log, no maximo 1 a cada 10 s e 10 por arranque (a flash nao aguenta mais).
_irq_err_ms = None
_irq_err_n = 0

def bt_irq(event, data):
    global _irq_err_ms, _irq_err_n
    try:
        return _bt_irq(event, data)
    except Exception as e:
        dprint("IRQ ERROR", event, e)
        now = time.ticks_ms()
        if _irq_err_n < 10 and (_irq_err_ms is None or time.ticks_diff(now, _irq_err_ms) > 10000):
            _irq_err_ms = now
            _irq_err_n += 1
            _diag("BLE IRQ error %d: %r" % (event, e))

def _bt_irq(event, data):
    global scanning, connecting_link, bm_tc, bm_tc_seq

    if event == _IRQ_SCAN_RESULT:
        addr_type, addr, _, rssi, adv = data
        link = by_mac(addr)
        if link is not None:
            # O sinal da camara no ultimo anuncio visto (com a ligacao feita ela deixa de anunciar, e o MicroPython nao
            # da o sinal de uma ligacao): serve para escolher onde pousar o C3.
            link["rssi"], link["rssi_ms"] = rssi, time.ticks_ms()
            if wants_scan(link):
                link["addr_type"] = addr_type
        if cam["scan"] == "scanning":
            cam_seen(addr_type, bytes(addr), rssi, bytes(adv))

    elif event == _IRQ_SCAN_DONE:
        scanning = False
        if cam["scan"] == "scanning":
            cam["scan"] = "done"
        for link in all_links:
            if link["conn_handle"] is not None or link["phase"] != "idle":
                continue
            if link["addr_type"] is not None:
                set_phase(link, "queued")
                connect_queue.append(link)
            else:
                schedule_retry(link)
        try_next_connect()

    elif event == _IRQ_PERIPHERAL_CONNECT:
        conn_handle, _, addr = data
        link = by_mac(addr)
        if link is None:
            return
        if link is connecting_link:
            connecting_link = None
        link["conn_handle"] = conn_handle
        links_by_conn[conn_handle] = link
        dprint(link["name"], "connected")
        if link["need_bond"]:
            set_phase(link, "encrypting")
            if cam["pair"] == "waiting":
                cam["pair"] = "need_code"      # a BM mostra agora o codigo no ecra
            try:
                ble.gap_pair(conn_handle)
            except Exception as e:
                dprint("gap_pair failed:", e)
                drop(link)
        else:
            start_discovery(link)
        try_next_connect()

    elif event == _IRQ_PERIPHERAL_DISCONNECT:
        conn_handle, _, addr = data
        link = links_by_conn.pop(conn_handle, None)
        if link is None:
            # Ligacao que falhou antes de estabelecer: o MicroPython entrega o
            # endereco a zeros, por isso atribui-se a tentativa em curso.
            if connecting_link is not None:
                failed = connecting_link
                connecting_link = None
                schedule_retry(failed)
                try_next_connect()
            return
        dprint(link["name"], "dropped")
        was_discovering = link["phase"] == "discovering"
        link["conn_handle"] = None
        link["notify_handle"] = None
        link["write_handle"] = None
        link["name_handle"] = None
        link["tc_handle"] = None
        link["_wq"], link["_wbusy"] = [], False
        if link in gatt_queue:
            gatt_queue.remove(link)
        elif was_discovering and gatt_busy:
            release_gatt()
        if link["phase"] != "gave_up":
            schedule_retry(link)

    elif event == _IRQ_ENCRYPTION_UPDATE:
        conn_handle, encrypted, _, bonded, _ = data
        link = links_by_conn.get(conn_handle)
        if link is None or link["phase"] != "encrypting":
            return
        if encrypted:
            link["enc_fails"] = 0
            dprint(link["name"], "encrypted (bond=%d)" % bonded)
            if cam["pair"] in ("waiting", "need_code", "checking"):
                cam["pair"], cam["error"] = "ok", None
            start_discovery(link)
        elif cam["pair"] in ("waiting", "need_code", "checking"):
            cam["pair"], cam["error"] = "failed", "the camera refused the pairing (wrong code?)"
            drop(link)
        else:
            link["enc_fails"] += 1
            dprint(link["name"], "encryption failed (%d/%d)" % (link["enc_fails"], BM_MAX_ENC_FAILS))
            if link["enc_fails"] >= BM_MAX_ENC_FAILS:
                set_phase(link, "gave_up")
                dprint("!!! BM refuses the saved bond -- giving up until restart. Pair again (Atom: GATEWAYS > C3 > Pair camera)")
            drop(link)

    elif event == _IRQ_GATTC_SERVICE_RESULT:
        link = links_by_conn.get(data[0])
        if link is not None:
            link["_svcs"].append((data[1], data[2]))

    elif event == _IRQ_GATTC_SERVICE_DONE:
        link = links_by_conn.get(data[0])
        if link is not None and link["phase"] == "discovering":    # nao a que ja deu timeout
            link["_svc_queue"] = list(link["_svcs"])
            discover_next_service(link)

    elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
        conn_handle, _, value_handle, properties, uuid = data
        link = links_by_conn.get(conn_handle)
        if link is None:
            return
        if uuid == link["notify_uuid"]:
            link["notify_handle"] = value_handle
            link["notify_props"] = properties
        elif link["write_uuid"] is not None and uuid == link["write_uuid"]:
            link["write_handle"] = value_handle
        elif link is bm and uuid == BM_DEVICE_NAME_UUID:
            link["name_handle"] = value_handle
        elif link is bm and uuid == BM_TIMECODE_UUID:
            link["tc_handle"] = value_handle
            link["tc_props"] = properties

    elif event == _IRQ_GATTC_WRITE_DONE:
        conn_handle, value_handle, status = data
        link = links_by_conn.get(conn_handle)
        if link is not None:
            if status:
                dprint(link["name"], "write 0x%x refused (status %d)" % (value_handle, status))
            link["_wbusy"] = False
            write_next(link)

    elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
        link = links_by_conn.get(data[0])
        if link is not None and link["phase"] == "discovering":
            discover_next_service(link)

    elif event == _IRQ_GATTC_NOTIFY or event == _IRQ_GATTC_INDICATE:
        conn_handle, value_handle, notify_data = data
        link = links_by_conn.get(conn_handle)
        if link is None:
            return
        if link is bm and value_handle == bm["tc_handle"]:
            bm_tc = bytes(notify_data)
            bm_tc_seq += 1
        elif value_handle == link["notify_handle"]:
            on_notify(link, bytes(notify_data))

    elif event == _IRQ_GET_SECRET:
        sec_type, index, key = data
        if key is None:
            i = 0
            for (t, k), v in secrets.items():
                if t == sec_type:
                    if i == index:
                        return v
                    i += 1
            return None
        return secrets.get((sec_type, bytes(key)), None)

    elif event == _IRQ_PASSKEY_ACTION:
        conn_handle, action, _passkey = data
        if action == 2 and cam["pair"] == "waiting":
            cam["pair"] = "need_code"
        elif action == 4:                      # comparacao numerica: aceitar
            ble.gap_passkey(conn_handle, action, 1)

    elif event == _IRQ_SET_SECRET:
        sec_type, key, value = data
        k = (sec_type, bytes(key))
        if value is None:
            secrets.pop(k, None)
        else:
            secrets[k] = bytes(value)
        save_secrets()
        return True

# ---- LED de estado (Stamp-S3A: dados em G21, alimentacao em G38) ----
LED_RED = (40, 0, 0)
LED_YELLOW = (40, 25, 0)
LED_GREEN = (0, 40, 0)
LED_OFF = (0, 0, 0)

_np = None
if IS_S3:
    import neopixel
    machine.Pin(38, machine.Pin.OUT, value=1)
    _np = neopixel.NeoPixel(machine.Pin(21), 1)
_led_shown = None

def update_led():
    global _led_shown
    if _np is None:
        return
    if bm["phase"] == "gave_up":
        color = LED_RED if (time.ticks_ms() // 400) % 2 else LED_OFF
    else:
        ready = (fiz["phase"] == "ready") + (bm["phase"] == "ready")
        color = (LED_RED, LED_YELLOW, LED_GREEN)[ready]
    if color != _led_shown:
        _np[0] = color
        _np.write()
        _led_shown = color

def check_timeouts():
    global connecting_link
    now = time.ticks_ms()
    if connecting_link is not None and time.ticks_diff(now, connect_deadline) >= 0:
        dprint(connecting_link["name"], "connect timeout")
        try:
            ble.gap_connect(None)
        except Exception:
            pass
        schedule_retry(connecting_link)
        connecting_link = None
        try_next_connect()
    for link in all_links:
        lim = PAIR_CODE_MS if (link is bm and cam["pair"] in ("need_code", "checking")) else ENCRYPT_TIMEOUT_MS
        if link["phase"] == "encrypting" and time.ticks_diff(now, link["phase_since"]) > lim:
            dprint(link["name"], "encryption timeout")
            if link is bm and cam["pair"] in ("need_code", "checking"):
                cam["pair"], cam["error"] = "failed", "no code entered in time"
            set_phase(link, "idle")
            drop(link)
        # Uma descoberta GATT sem fim prendia a pilha (scan, ligacoes, a outra descoberta) para sempre.
        if link["phase"] == "discovering" and time.ticks_diff(now, link["phase_since"]) > DISCOVER_TIMEOUT_MS:
            _diag("%s: GATT discovery timeout" % link["name"])
            discovery_failed(link)
        # Uma escrita sem resposta nao pode prender a fila para sempre.
        if link["_wbusy"] and time.ticks_diff(now, link["_wsince"]) > WRITE_TIMEOUT_MS:
            dprint(link["name"], "write not answered -- queue moves on")
            link["_wbusy"] = False
            write_next(link)

# ---- escolher e emparelhar a camera (pela API: a consola do Atom) ----
# scan:  idle | wanted | scanning | done          pair: idle | waiting | need_code | checking | ok | failed
PAIR_CODE_MS = 90000       # tempo para ler o codigo na BM e o escrever na consola
PAIR_TIMEOUT_MS = 150000
CAM_SCAN_MS = 6000
cam = {"scan": "idle", "found": {}, "pair": "idle", "since": 0, "error": None}

def _adv_fields(adv):
    i, out = 0, []
    while i + 1 < len(adv):
        n = adv[i]
        if n == 0 or i + 1 + n > len(adv):
            break
        out.append((adv[i + 1], adv[i + 2:i + 1 + n]))
        i += 1 + n
    return out

def cam_seen(addr_type, addr, rssi, adv):
    k = addr.hex()
    d = cam["found"].get(k)
    if d is None:
        if len(cam["found"]) >= 30:
            return
        d = cam["found"][k] = {"mac": k, "t": addr_type, "name": "", "rssi": rssi, "bm": False}
    d["rssi"] = max(d["rssi"], rssi)
    for typ, val in _adv_fields(adv):
        if typ in (0x08, 0x09) and val:
            try:
                d["name"] = val.decode()
            except Exception:
                pass
        elif typ in (0x06, 0x07) and BM_SERVICE_ADV in val:
            d["bm"] = True

def cam_scan_start():
    cam["found"] = {}
    # Ja ha um scan a correr (o das ligacoes): aproveita-se; senao, pede-se um ao ciclo principal.
    cam["scan"] = "scanning" if scanning else "wanted"

def cam_tick():
    global scanning
    now = time.ticks_ms()
    if cam["scan"] == "wanted" and not scanning and connecting_link is None and not gatt_busy:
        try:
            ble.gap_scan(CAM_SCAN_MS, 30000, 30000, True)
            scanning = True
            cam["scan"] = "scanning"
        except Exception as e:
            dprint("camera scan failed:", e)
    if cam["pair"] in ("waiting", "need_code", "checking") and time.ticks_diff(now, cam["since"]) > PAIR_TIMEOUT_MS:
        cam["pair"], cam["error"] = "failed", "the camera did not answer"

def cam_pair(mac, addr_type):
    """Passa a usar esta BM e emparelha: apaga o bond antigo, liga e espera pelo codigo."""
    global connecting_link
    save_bm_mac(mac)
    secrets.clear()                     # so a BM usa bond; com o antigo a cifra falhava antes de pedir codigo
    save_secrets()
    bm["mac"], bm["addr_type"] = mac, addr_type
    bm["enc_fails"], bm["retry"], bm["next_attempt"] = 0, bm["retry_min"], time.ticks_ms()
    cam["pair"], cam["since"], cam["error"] = "waiting", time.ticks_ms(), None
    if bm["conn_handle"] is not None:
        drop(bm)                        # ao cair volta a tentar, ja com a camera nova
    elif connecting_link is bm:         # a meio de ligar a antiga: cancela e recomeca
        try:
            ble.gap_connect(None)
        except Exception:
            pass
        connecting_link = None
        set_phase(bm, "idle")
    elif bm["phase"] in ("gave_up", "broken", "queued"):
        if bm in connect_queue:
            connect_queue.remove(bm)
        set_phase(bm, "idle")

def cam_code(code):
    if cam["pair"] not in ("need_code", "checking") or bm["conn_handle"] is None:
        return False
    ble.gap_passkey(bm["conn_handle"], 2, code)
    cam["pair"] = "checking"
    return True

def cam_estado():
    return {"ok": True, "mac": bm["mac"].hex(), "phase": bm["phase"], "pair": cam["pair"],
            "error": cam["error"], "scan": cam["scan"]}

def net_cam(method, path, q):
    """/api/cam...: (estado HTTP, dict) ou None."""
    if path == "/api/cam":
        return "200 OK", cam_estado()
    # Procurar, emparelhar ou cancelar mexe na ligacao Bluetooth da BMPCC (o pair apaga o emparelhamento e desliga-a):
    # a meio de um take faria o bridge perder a camara.
    if method == "POST" and path in ("/api/cam/scan", "/api/cam/pair", "/api/cam/cancel") and recording:
        return "409 Conflict", {"ok": False, "error": "the BMPCC is recording"}
    if path == "/api/cam/scan":
        if method == "POST":
            cam_scan_start()
        lst = sorted(cam["found"].values(), key=lambda d: (not d["bm"], not d["name"], -d["rssi"]))
        return "200 OK", {"ok": True, "scan": cam["scan"], "found": lst}
    if method != "POST":
        return "405 Method Not Allowed", {"ok": False, "error": "POST"}
    if path == "/api/cam/pair":
        try:
            mac = bytes.fromhex(q.get("mac", "").replace(":", ""))
            t = int(q.get("t", "0"))
        except Exception:
            mac, t = b"", 0
        if len(mac) != 6:
            return "400 Bad Request", {"ok": False, "error": "mac"}
        cam_pair(mac, t)
        return "200 OK", cam_estado()
    if path == "/api/cam/code":
        c = q.get("c", "")
        if len(c) != 6 or not c.isdigit():
            return "400 Bad Request", {"ok": False, "error": "6 digits"}
        if not cam_code(int(c)):
            return "409 Conflict", {"ok": False, "error": "the camera is not asking for a code"}
        return "200 OK", cam_estado()
    if path == "/api/cam/cancel":
        if cam["pair"] in ("waiting", "need_code", "checking"):
            cam["pair"] = "idle"
            drop(bm)
        return "200 OK", cam_estado()
    return None

def _rssi_bm():
    """O sinal da BMPCC (dBm) e ha quantos s foi visto; (None, None) se nunca."""
    if bm["rssi_ms"] is None:
        return None, None
    return bm["rssi"], time.ticks_diff(time.ticks_ms(), bm["rssi_ms"]) // 1000

def _wifi_rssi():
    """O sinal (dBm) do Wi-Fi do C3 ate ao AP, ou None sem ligacao (ou se o MicroPython nao o der)."""
    try:
        if netcfg._sta.isconnected():
            return netcfg._sta.status("rssi")
    except Exception:
        pass
    return None

def net_status():
    r, idade = _rssi_bm()
    return {"fw": "fiz_bm_c3 26", "wifi_rssi": _wifi_rssi(), "mem_free": mem_now[0], "mem_max_block": mem_now[1], "bm_rssi": r, "bm_rssi_age_s": idade, "ble_error": BLE_ERRO, "fiz": fiz["phase"] if BLE_OK or fiz["phase"] == "off" else "ble_error", "bm": bm["phase"] if BLE_OK else "ble_error", "recording": recording,
            "bm_mac": bm["mac"].hex(), "pair": cam["pair"]}

def net_rec(on):
    if bm["phase"] != "ready":
        return False
    send_rec(on)
    return True

def net_bm_poll(since, tcs):
    """Para o Atom: os pacotes da BM depois de `since` e o timecode se mudou desde `tcs`."""
    r, idade = _rssi_bm()
    d = {"phase": bm["phase"], "seq": bm_seq, "ev": [b.hex() for s, b in bm_ev if s > since],
         "lost": bool(bm_ev) and since >= 0 and bm_ev[0][0] > since + 1,
         "recording": recording, "mem": [mem_now[0], mem_now[1]], "rssi": r, "rssi_age_s": idade,
         "wifi_rssi": _wifi_rssi()}
    if bm_tc is not None and bm_tc_seq != tcs:
        d["tc"] = bm_tc.hex()
        d["tc_seq"] = bm_tc_seq
    return d

def net_bm_write(data):
    """Do Atom: qualquer comando para a BM (Outgoing Camera Control) -- REC, ISO, WB, relogio..."""
    if bm["phase"] != "ready" or bm["write_handle"] is None:
        return False
    return gatt_write(bm, bm["write_handle"], data)

if BLE_OK:
    ble.irq(bt_irq)
dprint("boot -- FIZ + BM")

# Wi-Fi (rede guardada / AP de configuracao / API do Bridge). Se falhar, a ponte BLE corre na mesma.
_net_ok = True
try:
    netcfg.init(net_status, net_rec, net_bm_poll, net_bm_write, net_cam, wdt=_wdt)
except Exception as e:
    _net_ok = False
    dprint("netcfg failed:", e)

# Memoria: recolhe-se de 2 em 2 s, ou logo que fique pouca (fragmentada, a pilha BLE deixa de conseguir alocar).
GC_EVERY_MS = 2000
GC_LOW = 16000
_gc_ms = time.ticks_ms()

try:
    while True:
        fw_watch()
        mem_tick()
        fiz_tick()                      # mesmo sem Bluetooth: no modo Bridge o FIZ fica "off" (nao "a procurar")
        if BLE_OK:
            cam_tick()
            start_scan_if_needed()
            check_timeouts()
        elif _ble_reiniciar_ms is not None and time.ticks_diff(time.ticks_ms(), _boot_ms) > _ble_reiniciar_ms:
            _diag("no Bluetooth: restarting to try again (%d)" % _ble_saltos)
            machine.reset()
        update_led()
        diag_alive()
        if _net_ok:
            try:
                netcfg.poll()
            except Exception as e:
                dprint("netcfg poll:", e)
        if time.ticks_diff(time.ticks_ms(), _gc_ms) > GC_EVERY_MS or gc.mem_free() < GC_LOW:
            gc.collect()
            _gc_ms = time.ticks_ms()
        time.sleep_ms(50)
except KeyboardInterrupt:
    # Ctrl-C pela USB (mpremote: gravar ficheiros, pair_bm_kbd.py): o watchdog nao se desliga no ESP32, por isso
    # alarga-se a 1 h, senao reiniciava o C3 a meio da copia. Um reset volta ao normal.
    try:
        machine.WDT(timeout=3600000)
    except Exception:
        pass
    raise

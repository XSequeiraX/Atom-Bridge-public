"""Configuracao inicial e actualizacao dos gateways (C3 e S3) por USB, sempre a partir do Atom.

Um gateway novo (placa em branco) ou antigo (sem OTA) liga-se por USB ao Atom e este faz tudo: identifica a placa, grava o
firmware e deixa-lhe o Wi-Fi da rede das cameras. Depois disso actualiza-se por Wi-Fi (fw_ota.py).

  S3 (ESP32-S3, Arduino): esptool. Placa em branco: apaga e grava a imagem completa (fw/s3/gopro_gw.merged.bin) a partir
      do 0x0. Placa que ja tem firmware: so a aplicacao (fw/s3/gopro_gw.bin) no 0x10000, depois de apagar o otadata (para
      arrancar mesmo dela e nao da copia antiga); a memoria do S3 (Wi-Fi e GoPro) fica. O Wi-Fi vai pela porta serie
      (comando WIFISET), sem passar por linhas de comandos nem pelo registo.
  C3 (ESP32-C3, MicroPython): sem MicroPython, grava-o com o esptool (fw/c3/micropython.bin; se faltar, descarrega-o de
      micropython.org); depois o mpremote copia boot.py, main.py, app.mpy e netcfg.mpy e, se pedido, o wifi.json.

As passwords nunca aparecem em linhas de comandos, no registo nem nas respostas: seguem por ficheiro temporario 0600 ou pela
porta serie.
"""
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import fw_ota

S3_APP = fw_ota.S3_BIN
S3_MERGED = os.path.join(fw_ota.FW_DIR, "s3", "gopro_gw.merged.bin")
# Comando de bolso do F6 (M5Stack M5StickS3, _esp32_tools/m5stick_f6_remote): so a aplicacao ou a imagem completa; o nome e a palavra-passe
# do Wi-Fi do S3 do F6 vao numa particao propria ("cfg", 0x310000, 128 bytes) que o Atom escreve ao gravar.
M5_APP = os.path.join(fw_ota.FW_DIR, "m5stick", "f6_remote_only_program.bin")
M5_MERGED = os.path.join(fw_ota.FW_DIR, "m5stick", "f6_remote_complete.bin")
M5_APP_OFFSET = 0x10000
M5_CFG_OFFSET = 0x310000
M5_HOST = "192.168.4.1"
# Versao fixa do MicroPython do C3: com a 1.29 o Bluetooth nao liga no C3 (ble.active -> OSError EIO). Mudar so depois
# de experimentar a nova num C3 (Bluetooth com a BMPCC).
MP_VERSAO = "1.24.1"
MP_RUINS = ("1.29",)        # versoes com que o gateway nao funciona: o Atom propoe reinstalar a fixa
C3_MP = os.path.join(fw_ota.C3_DIR, "micropython-v%s.bin" % MP_VERSAO)
MP_PAGE = "https://micropython.org/download/ESP32_GENERIC_C3/"
MP_BASE = "https://micropython.org"
S3_APP_OFFSET = "0x10000"
PORTA_ESPERA = 20.0
HEARTBEAT = 5.0               # de quanto em quanto se diz que o esptool continua a trabalhar (apagar nao escreve nada)


def particao(dados, tipo, subtipo=None, nomes=None):
    """(offset, tamanho) da primeira particao com este tipo/subtipo (ou nome) na tabela do 0x8000 de uma imagem completa."""
    tabela = dados[0x8000:0x8C00]
    for i in range(0, len(tabela) - 31, 32):
        e = tabela[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        nome = e[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        if e[2] == tipo and (subtipo is None or e[3] == subtipo) and (nomes is None or nome in nomes):
            return int.from_bytes(e[4:8], "little"), int.from_bytes(e[8:12], "little")
    return None


def particao_ficheiros(imagem):
    """(offset, tamanho) da particao de ficheiros (vfs) na tabela de particoes da imagem do MicroPython (gravada no 0x0, a
    tabela esta no 0x8000), ou None."""
    try:
        with open(imagem, "rb") as f:
            dados = f.read(0x9000)
    except OSError:
        return None
    return particao(dados, 0x01, nomes=("vfs", "ffat", "spiffs", "littlefs"))


def esptool_cmd():
    """O comando do esptool: GW_ESPTOOL (testes), o modulo python, ou o executavel. None se nao ha."""
    if os.environ.get("GW_ESPTOOL"):
        return [os.environ["GW_ESPTOOL"]]
    for n in ("esptool.py", "esptool"):                 # o oficial, no ambiente proprio (o do Debian vem sem stubs)
        c = os.path.join(fw_ota.GW_TOOLS, n)
        if os.access(c, os.X_OK):
            return [c]
    try:
        import importlib.util
        if importlib.util.find_spec("esptool"):
            return [sys.executable, "-m", "esptool"]
    except (ImportError, ValueError):
        pass
    exe = shutil.which("esptool") or shutil.which("esptool.py")
    return [exe] if exe else None


def _sem_stub(out):
    return "stub_flasher" in (out or "") or ("No such file" in (out or "") and "esptool/targ" in (out or ""))


def _esptool(porta, args, timeout=120, log=None):
    """O esptool numa porta. Se o esptool instalado vier sem stubs (o do Debian), repete com --no-stub (o carregador do
    proprio chip: mais lento, e sem leitura da flash)."""
    et = esptool_cmd()
    rc, out = _correr(et + ["--port", porta] + list(args), timeout, log)
    if rc and _sem_stub(out):
        if log:
            log("  this esptool has no stub (Debian package): retrying with --no-stub")
        rc, out = _correr(et + ["--no-stub", "--port", porta] + list(args), timeout, log)
    return rc, out


def _correr(cmd, timeout=120, log=None):
    """(returncode, saida) sem levantar excepcoes. Com `log`, vai dizendo o progresso (o esptool escreve "(37 %)")."""
    if log is None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except OSError as e:
            return 127, str(e)
    try:
        # PYTHONUNBUFFERED: o esptool e python; sem isto a saida dele so chegava no fim, toda de uma vez.
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=dict(os.environ, PYTHONUNBUFFERED="1"), start_new_session=True)
    except OSError as e:
        return 127, str(e)
    fila = queue.Queue()

    def ler():
        while True:
            pedaco = p.stdout.read1(256) if hasattr(p.stdout, "read1") else p.stdout.read(256)
            fila.put(pedaco)
            if not pedaco:
                return
    threading.Thread(target=ler, daemon=True).start()
    t0 = time.time()
    fim, saida, buf, ultimo, ultimo_sinal = t0 + timeout, [], b"", -1, t0
    while True:
        agora = time.time()
        if agora > fim:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
            return 124, "".join(saida) + "\ntimeout after %d s" % timeout
        try:
            pedaco = fila.get(timeout=min(HEARTBEAT, max(0.05, fim - agora)))
        except queue.Empty:
            if time.time() - ultimo_sinal >= HEARTBEAT:       # esptool calado (apagar demora): diz-se que continua
                ultimo_sinal = time.time()
                log("  still working ... %d s" % (ultimo_sinal - t0))
            continue
        if not pedaco:
            break
        ultimo_sinal = time.time()
        buf += pedaco
        while b"\r" in buf or b"\n" in buf:
            i = min(x for x in (buf.find(b"\r"), buf.find(b"\n")) if x >= 0)
            linha, buf = buf[:i].decode("utf-8", "replace").strip(), buf[i + 1:]
            if not linha:
                continue
            saida.append(linha + "\n")
            m = re.search(r"\((\d+) ?%\)", linha)
            if m:
                pct = int(m.group(1)) // 10 * 10
                if pct != ultimo:
                    ultimo = pct
                    log("  writing %d%%" % pct)
            elif re.search(r"Connecting|Chip is|Chip type|Erasing|Hash of data verified|Leaving|Hard resetting|rror", linha):
                log("  " + linha[:100])
    p.wait()
    return p.returncode, "".join(saida)


def ferramentas():
    return {"esptool": esptool_cmd() is not None, "mpremote": bool(fw_ota.ferramenta("mpremote")), "pyserial": _tem_serial()}


def _tem_serial():
    try:
        import serial  # noqa: F401  pylint: disable=import-outside-toplevel,unused-import
        return True
    except ImportError:
        return False


def imagens():
    """O que o Atom tem para gravar."""
    def tem(c):
        return os.path.isfile(c)
    return {"s3_app": tem(S3_APP), "s3_full": tem(S3_MERGED), "m5_app": tem(M5_APP), "m5_full": tem(M5_MERGED),
            "c3_micropython": tem(C3_MP),
            "c3_files": all(tem(os.path.join(fw_ota.C3_DIR, n)) for n in fw_ota.C3_USB_FILES)}


# ---- identificacao -------------------------------------------------------------------------------------------------
def identificar(porta):
    """'c3' | 's3' | None, e a saida do esptool."""
    et = esptool_cmd()
    if not et:
        return None, "esptool is not installed"
    rc, out = _esptool(porta, ["chip_id"], 60)
    m = re.search(r"ESP32-(C3|S3)", out)
    return (m.group(1).lower() if m else None), out


def tem_micropython(porta, tentativas=2, log=None):
    """O C3 responde com MicroPython? Repete (a porta acabou de renumerar depois do esptool): um falso "nao" apagava o
    Wi-Fi e a camara emparelhada de um C3 que ja funcionava. Sem soft reset (resume) e com tempo limite firme."""
    mp = fw_ota.ferramenta("mpremote")
    if not mp:
        return False
    for n in range(tentativas):
        rc, out = fw_ota.correr_limitado([mp, "connect", porta, "resume", "exec", "print('GW_MP_OK')"], 12)
        if rc == 0 and "GW_MP_OK" in out:
            return True
        if log:
            log("  no answer from MicroPython (attempt %d/%d): %s" % (n + 1, tentativas, _ultima(out)[:80]))
        if n + 1 < tentativas:
            time.sleep(2)
    return False


# ---- Wi-Fi das cameras (o do AP do Atom) -----------------------------------------------------------------------------
def _ap_if():
    """O nome da interface do AP das cameras (ap.conf AP_IF, escrito pelo instalador)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ap.conf")) as f:
            for l in f:
                if l.startswith("AP_IF=") and l[6:].strip():
                    return l[6:].strip()
    except OSError:
        pass
    return "ps-control"


def ap_wifi(iface=None):
    """(ssid, psk) do AP das cameras, lido do NetworkManager (so o Atom o sabe). (None, None) se nao houver."""
    iface = iface or _ap_if()
    rc, out = _correr(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], 10)
    if rc:
        return None, None
    for linha in out.splitlines():
        if not linha.endswith(":802-11-wireless"):
            continue
        nome = linha[:-len(":802-11-wireless")].replace("\\:", ":")
        rc, v = _correr(["nmcli", "-s", "-g", "connection.interface-name,802-11-wireless.mode,802-11-wireless.ssid,"
                         "802-11-wireless-security.psk", "connection", "show", nome], 10)
        partes = v.strip().split("\n")
        if rc == 0 and len(partes) >= 3 and partes[0] == iface and partes[1] == "ap" and partes[2]:
            return partes[2], (partes[3] if len(partes) > 3 else "")
    return None, None


# ---- MicroPython do C3 -----------------------------------------------------------------------------------------------
def micropython_url(html, versao=None):
    """O .bin da versao `versao` (por omissao MP_VERSAO) da pagina de transferencias do MicroPython para o C3."""
    versao = versao or MP_VERSAO
    for m in re.finditer(r'/resources/firmware/ESP32_GENERIC_C3-\d{8}-v(\d+\.\d+\.\d+)\.bin', html or ""):
        if m.group(1) == versao:
            return MP_BASE + m.group(0)
    return None


def c3_mp_versao(porta):
    """A versao do MicroPython no C3 (ex. '1.24.1'), ou None."""
    mp = fw_ota.ferramenta("mpremote")
    if not mp:
        return None
    rc, out = fw_ota.correr_limitado([mp, "connect", porta, "resume", "exec", "import os;print('MPV',os.uname().release)"], 15)
    m = re.search(r"MPV (\S+)", out or "")
    return m.group(1) if rc == 0 and m else None


def baixar_micropython(log=print):
    """Descarrega o MicroPython do C3 para fw/c3/micropython.bin (precisa de Internet). (ok, erro)."""
    try:
        with urllib.request.urlopen(MP_PAGE, timeout=20) as r:
            url = micropython_url(r.read().decode("utf-8", "replace"))
        if not url:
            return False, "could not find MicroPython v%s on micropython.org: put it in %s" % (MP_VERSAO, C3_MP)
        with urllib.request.urlopen(url, timeout=120) as r:
            dados = r.read()
    except Exception as e:                      # pylint: disable=broad-except
        return False, "no Internet for the MicroPython image (%s): put it in %s" % (str(e)[:80], C3_MP)
    if len(dados) < 500000 or dados[:1] != b"\xe9":
        return False, "the downloaded file is not an ESP32 image"
    os.makedirs(os.path.dirname(C3_MP), exist_ok=True)
    tmp = C3_MP + ".tmp"
    with open(tmp, "wb") as f:
        f.write(dados)
    os.replace(tmp, C3_MP)
    log("MicroPython for the C3 downloaded (%s)" % url.rsplit("/", 1)[-1])
    return True, None


# ---- S3 ------------------------------------------------------------------------------------------------------------------
def s3_ja_tem_firmware(porta):
    """True / False, ou None se nao se conseguiu ler a flash (nao e o mesmo que estar em branco)."""
    with tempfile.TemporaryDirectory() as d:
        alvo = os.path.join(d, "app.bin")
        rc, _ = _esptool(porta, ["read_flash", S3_APP_OFFSET, "4", alvo], 60)
        try:
            with open(alvo, "rb") as f:
                primeiro = f.read(1)
        except OSError:
            return None
        if rc:
            return None
        return primeiro == b"\xe9"


def app_valida(d):
    """Imagem de aplicacao ESP32 inteira? Cabecalho 0xE9 e, se a imagem traz o SHA-256 no fim, confere-o."""
    if len(d) < 4096 or d[:1] != b"\xe9":
        return False
    if d[23] == 1:
        return hashlib.sha256(d[:-32]).digest() == d[-32:]
    return True


def s3_imagens_validas():
    """Erro (texto) se o firmware do S3 que o Atom tem estiver estragado, ou None. Gravar uma imagem estragada deixa o S3
    sem arrancar (sem rede e mudo na USB)."""
    try:
        with open(S3_APP, "rb") as f:
            app = f.read()
        with open(S3_MERGED, "rb") as f:
            completa = f.read()
    except OSError as e:
        return "S3 firmware missing on the Atom (%s): update the bridge from GitHub" % str(e)[:80]
    if not app_valida(app):
        return "the S3 firmware on the Atom is damaged (checksum): update the bridge from GitHub"
    if completa[0x8000:0x8002] != b"\xaa\x50" or completa[0x10000:0x10000 + len(app)] != app:
        return "the full S3 image on the Atom is damaged: update the bridge from GitHub"
    return None


def s3_gravar(porta, modo, log=print):
    """modo: 'update' (so a aplicacao; Wi-Fi e GoPro ficam) | 'setup' (apaga tudo e grava a imagem completa)."""
    err = s3_imagens_validas()
    if err:
        return False, err
    # Grava-se sempre a imagem completa (bootloader + tabela de particoes + otadata + aplicacao), aparada do 0xFF do fim: so
    # a aplicacao, em cima de um bootloader/tabela de outro firmware, pode nao arrancar. No 'update' salta-se a zona das
    # definicoes (NVS: Wi-Fi e GoPro emparelhadas); no 'setup' tambem e escrita (fica vazia).
    with open(S3_MERGED, "rb") as f:
        util = f.read().rstrip(b"\xff")
    util += b"\xff" * (-len(util) % 4096)
    nvs = particao(util, 0x01, 0x02) if modo == "update" else None
    with tempfile.TemporaryDirectory() as d:
        partes = [(0, util)] if not nvs else [(0, util[:nvs[0]]), (nvs[0] + nvs[1], util[nvs[0] + nvs[1]:])]
        args = ["write_flash"]
        for n, (off, dados) in enumerate(partes):
            nome = os.path.join(d, "s3_%d.bin" % n)
            with open(nome, "wb") as f:
                f.write(dados)
            args += [hex(off), nome]
        log("Writing the S3 firmware (%d KB: bootloader + partitions + application%s) ..."
            % (sum(len(x) for _o, x in partes) // 1024, "; Wi-Fi and cameras kept" if nvs else ""))
        rc, out = _esptool(porta, args, 300, log)
    if rc:
        return False, "esptool: " + _ultima(out)
    log("S3 firmware written by USB (%s)" % modo)
    return True, None


def m5_cfg_blob(ssid, psk, host=M5_HOST):
    """Os 128 bytes da particao "cfg" da M5StickS3 (formato no cabecalho do m5stick_f6_remote.ino): 0-3 "F6RC", 4 versao 1,
    5-7 tamanhos (ssid, psk, host), 8-39 ssid, 40-103 psk, 104-119 host, 126-127 soma de 16 bits dos bytes 0-125."""
    s, p, h = ssid.encode(), psk.encode(), host.encode()
    b = bytearray(128)
    b[0:4] = b"F6RC"
    b[4], b[5], b[6], b[7] = 1, len(s), len(p), len(h)
    b[8:8 + len(s)] = s
    b[40:40 + len(p)] = p
    b[104:104 + len(h)] = h
    soma = sum(b[:126]) & 0xFFFF
    b[126], b[127] = soma & 0xFF, soma >> 8
    return bytes(b)


def m5_imagens_validas(modo):
    """Erro (texto) se o firmware da M5StickS3 que o Atom tem estiver em falta ou estragado, ou None."""
    if modo == "wifi":
        return None
    try:
        with open(M5_APP, "rb") as f:
            app = f.read()
        completa = None
        if modo == "setup":
            with open(M5_MERGED, "rb") as f:
                completa = f.read()
    except OSError as e:
        return "M5StickS3 firmware missing on the Atom (%s): update the bridge from GitHub" % str(e)[:80]
    if not app_valida(app):
        return "the M5StickS3 firmware on the Atom is damaged (checksum): update the bridge from GitHub"
    if completa is not None:
        cfg = particao(completa, 0x01, 0x40, ("cfg",))
        if (completa[0x8000:0x8002] != b"\xaa\x50" or completa[M5_APP_OFFSET:M5_APP_OFFSET + len(app)] != app
                or not cfg or cfg[0] != M5_CFG_OFFSET):
            return "the full M5StickS3 image on the Atom is damaged: update the bridge from GitHub"
    return None


def m5_gravar(porta, modo, wifi, log=print):
    """modo: 'setup' (imagem completa + Wi-Fi) | 'update' (so a aplicacao; o Wi-Fi guardado fica) | 'wifi' (so o Wi-Fi).
    Fixa o chip (--chip esp32s3): nao se identifica a placa antes (o chip_id reinicia-a e saia do modo de gravacao). A palavra-passe
    vai por ficheiro temporario 0600, nunca na linha de comandos nem no registo. (ok, erro)"""
    err = m5_imagens_validas(modo)
    if err:
        return False, err
    with tempfile.TemporaryDirectory() as d:
        args = ["--chip", "esp32s3", "write_flash"]
        if modo == "setup":
            with open(M5_MERGED, "rb") as f:
                util = f.read().rstrip(b"\xff")
            util += b"\xff" * (-len(util) % 4096)
            completa = os.path.join(d, "m5_complete.bin")
            with open(completa, "wb") as f:
                f.write(util)
            args += ["0x0", completa]
            log("Writing the M5StickS3 firmware (%d KB: bootloader + partitions + application) ..." % (len(util) // 1024))
        elif modo == "update":
            args += [hex(M5_APP_OFFSET), M5_APP]
            log("Writing the M5StickS3 application (its Wi-Fi is kept) ...")
        if modo in ("setup", "wifi"):
            cfg = os.path.join(d, "m5_wifi.cfg")
            fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(m5_cfg_blob(wifi["ssid"], wifi["psk"]))
            args += [hex(M5_CFG_OFFSET), cfg]
            log("Writing its Wi-Fi: network %s" % wifi["ssid"])
        rc, out = _esptool(porta, args, 300, log)
    if rc:
        return False, "esptool: " + _ultima(out)
    log("M5StickS3 written by USB (%s)" % modo)
    return True, None


def m5_configurar(porta, wifi, modo, usar_ap, log):
    """O comando de bolso do F6 numa M5StickS3 ligada por USB ao Atom. (ok, dict sem segredos)."""
    if modo not in ("setup", "update", "wifi"):
        return False, {"error": "M5StickS3: the mode is setup, update or wifi"}
    if usar_ap:
        return False, {"error": "the M5StickS3 joins the Wi-Fi of the F6's S3 (F6-REMOTE-xxxx), not the cameras' network"}
    if not esptool_cmd():
        return False, {"error": "esptool is not installed on the Atom (pip install esptool --break-system-packages)"}
    if modo in ("setup", "wifi"):
        ssid, psk = (wifi or {}).get("ssid") or "", (wifi or {}).get("psk") or ""
        if not 1 <= len(ssid.encode()) <= 32:
            return False, {"error": "the Wi-Fi name (F6-REMOTE-xxxx) must have 1 to 32 characters"}
        if not 8 <= len(psk.encode()) <= 63:
            return False, {"error": "the F6 standalone Wi-Fi password has 8 to 63 characters"}
    ok, err = m5_gravar(porta, modo, wifi, log)
    res = {"kind": "m5stick", "port": porta, "mode": modo}
    if not ok:
        return False, {**res, "error": err}
    res["wifi"] = modo != "update"
    res["note"] = ("done: unplug the M5StickS3 and plug it in again (or press its power button). It joins the Wi-Fi of the F6's S3 "
                   "(F6 standalone) by itself")
    return True, res


def _ultima(texto):
    linhas = [l.strip() for l in (texto or "").splitlines() if l.strip()]
    return (linhas[-1] if linhas else "no output")[:200]


def esperar_porta(antes, tempo=PORTA_ESPERA, portas=None):
    """A porta Espressif depois do reinicio (o nome pode mudar). `portas` devolve a lista actual."""
    portas = portas or fw_ota.c3_usb_portas
    fim = time.time() + tempo
    while time.time() < fim:
        cand = [p["port"] for p in portas() if p["espressif"]]
        if antes in cand:
            return antes
        if cand:
            return cand[0]
        time.sleep(0.5)
    return None


def _token_s3():
    """O token dos gateways (o mesmo dos C3), para o S3 o guardar pela USB. None se nao ha."""
    try:
        with open(fw_ota.gw_token_path()) as f:
            t = f.read().strip()
        return t if re.fullmatch(r"[0-9a-fA-F]{16,64}", t) else None
    except (OSError, TypeError):
        return None


def s3_wifi_serie(porta, ssid, psk, log=None, tempo=40.0, diagnostico=True, estado=None):
    """Envia ao S3 pela porta serie o token dos gateways (TOKENSET) e o Wi-Fi (WIFISET; ssid None: so o token).
    (ok, erro). A password e o token nao saem daqui. `estado` (dict) recebe "token": True | False (firmware sem token).
    Logo a seguir a gravar, o S3 reinicia e a USB dele desaparece e volta: abre-se a porta (e volta-se a procurar, o nome
    pode mudar) e envia-se ate ele confirmar, durante `tempo` s."""
    estado = {} if estado is None else estado
    token = _token_s3()
    ssid, psk = ssid or "", psk or ""
    if any(c in ssid + psk for c in "|\r\n"):
        return False, "the Wi-Fi name and password cannot contain '|' or line breaks (for the S3)"
    # Espacos no inicio ou no fim: o WIFISET de firmware antigo corta-os (a password ficava mais curta e o S3 nao ligava).
    # Vai em hex (WIFISETX); um firmware que o nao conhece responde "ERR STATUS ..." e nao se grava nada errado.
    em_hex = bool(ssid) and (ssid != ssid.strip() or psk != psk.strip())
    try:
        import serial  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False, "pyserial is not installed on the Atom (apt install python3-serial)"
    log = log or (lambda _m: None)
    fim = time.time() + tempo
    ultimo_erro, tentativa, ouviu = "no answer", 0, False
    while time.time() < fim:
        tentativa += 1
        p = esperar_porta(porta, 5) or porta
        try:
            # A porta fica aberta e o comando repete-se: abrir/fechar a USB a cada tentativa pode reiniciar o S3 (DTR/RTS)
            # e o comando chegava sempre durante o arranque. O que o S3 responde vai para o registo (diagnostico); o
            # comando em si (com a password) nunca e registado e o firmware nao o repete.
            with serial.Serial(p, 115200, timeout=0.3, write_timeout=2) as s:
                time.sleep(1.0)
                envio, vistas, ultimo = 0.0, 0, ""
                while time.time() < fim:
                    # Primeiro o token (o S3 guarda-o e continua), depois o Wi-Fi (o S3 guarda-o e reinicia).
                    falta_token = token and "token" not in estado
                    if not falta_token and not ssid:
                        return True, None
                    if time.time() - envio > 4:
                        s.write(b"\n")
                        if falta_token:
                            ultimo, cmd = "token", "TOKENSET %s\n" % token
                        elif em_hex:
                            ultimo, cmd = "wifix", "WIFISETX %s|%s\n" % (ssid.encode().hex(), psk.encode().hex())
                        else:
                            ultimo, cmd = "wifi", "WIFISET %s|%s\n" % (ssid, psk)
                        s.write(cmd.encode())
                        s.flush()
                        envio = time.time()
                    linha = s.readline().decode("utf-8", "replace").strip()
                    if not linha:
                        continue
                    ouviu = True
                    if falta_token and linha.startswith("OK token saved"):
                        estado["token"] = True
                        log("  S3: gateway token saved")
                        envio = 0.0
                        continue
                    if falta_token and (linha.startswith("ERR STATUS") or linha.startswith("ERR TOKENSET")):
                        estado["token"] = False          # firmware antigo, sem token: segue sem ele
                        log("  S3: this firmware has no gateway token (update it): it stays open on the network")
                        envio = 0.0
                        continue
                    if linha.startswith("OK wifi saved"):
                        log("  S3: " + linha[:80])
                        return True, None
                    if linha.startswith("ERR WIFISET"):
                        return False, linha[:160]
                    if ultimo == "wifix" and not falta_token and linha.startswith("ERR STATUS"):
                        return False, ("this S3 firmware cannot take a Wi-Fi name or password with a space at the start or the end: "
                                       "update the S3 firmware first, or use a network without them")
                    if vistas < 12 and not linha.startswith("ERR STATUS"):
                        vistas += 1
                        log("  S3: " + linha[:80])
                ultimo_erro = "the S3 answered but did not confirm" if ouviu else "nothing received from the S3"
        except (OSError, ValueError) as e:     # SerialException e OSError: a porta a desaparecer/voltar
            ultimo_erro = str(e)[:120]
        log("  the S3 is not ready yet (attempt %d): %s" % (tentativa, ultimo_erro[:70]))
        time.sleep(2)
    if not ouviu:
        if diagnostico and any("DOWNLOAD" in l for l in s3_arranque(esperar_porta(porta, 5) or porta, log)):
            return False, S3_PRESO
        ultimo_erro = ("nothing received from the S3 by USB: it may not be running the new firmware; unplug it and plug "
                       "it again, then retry")
    return False, ("the S3 did not take the Wi-Fi (%s). The firmware is written: set the Wi-Fi again "
                   "(Setup / update by USB) or join its network GOPRO-S3-XXXX" % ultimo_erro)


def s3_verificar_wifi(porta, ssid, log, tempo=45.0):
    """Depois do WIFISET: pergunta ao S3, pela USB (comando WIFI), se ligou ao Wi-Fi. Ele reinicia e tenta cada rede
    15 s; se nenhuma ligar abre o AP GOPRO-S3-XXXX. Devolve o dict que ele responde (state: up | ap | trying; nunca a
    password) ou None se nao respondeu. Nunca faz falhar a gravacao: so diz no registo como ficou."""
    try:
        import serial  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    log("Asking the S3 by USB whether it joined the Wi-Fi ...")
    fim, d = time.time() + tempo, None
    while time.time() < fim:
        p = esperar_porta(porta, 5) or porta
        try:
            with serial.Serial(p, 115200, timeout=0.5, write_timeout=2) as s:
                time.sleep(0.5)
                while time.time() < fim:
                    s.write(b"\nWIFI\n")
                    s.flush()
                    t1 = time.time() + 3
                    while time.time() < t1:
                        linha = s.readline().decode("utf-8", "replace").strip()
                        if linha.startswith("{") and '"state"' in linha:
                            try:
                                d = json.loads(linha)
                            except ValueError:
                                continue
                            if d.get("state") in ("up", "ap"):
                                break
                    if d and d.get("state") in ("up", "ap"):
                        break
                if d and d.get("state") in ("up", "ap"):
                    break
        except (OSError, ValueError):               # a USB a desaparecer/voltar com o reinicio
            time.sleep(2)
    if d is None:
        log("  the S3 did not answer by USB: cannot tell whether it joined the Wi-Fi")
    elif d.get("state") == "up":
        log("  the S3 joined %s, address %s" % (d.get("ssid") or ssid, d.get("ip")))
    elif d.get("state") == "ap":
        guardadas = d.get("saved") or []
        if guardadas:
            log("  the S3 has %s saved but did NOT join it: it opened its own network %s (%s)"
                % (", ".join(guardadas), d.get("ap") or "GOPRO-S3-XXXX", d.get("ip") or "192.168.4.1"))
            log("  is EXT WI-FI on the air? It tries the saved networks again by itself every 3 min")
        else:
            log("  the S3 has NO Wi-Fi saved (the Wi-Fi did not stick): it opened its own network %s (%s)"
                % (d.get("ap") or "GOPRO-S3-XXXX", d.get("ip") or "192.168.4.1"))
    else:
        log("  the S3 is still trying to join the Wi-Fi")
    return d


S3_PRESO = "the S3 stayed in flashing mode"


def esperar_religar(porta, log, tempo=120.0, nome="S3"):
    """Espera que o operador desligue e volte a ligar o S3 (so assim ele sai do modo de gravacao). A porta nova, ou None."""
    fim, saiu, aviso = time.time() + tempo, False, 0.0
    while time.time() < fim:
        cand = [p["port"] for p in fw_ota.c3_usb_portas() if p["espressif"]]
        if not cand:
            if not saiu:
                log("  unplugged: now plug it in again")
            saiu = True
        elif saiu:
            time.sleep(3)                       # deixa o firmware arrancar
            return esperar_porta(porta, 5)
        if time.time() - aviso > 15:
            aviso = time.time()
            if not saiu:
                log("  waiting: unplug the %s from the USB (%d s left)" % (nome, fim - time.time()))
        time.sleep(0.5)
    return None


def s3_arranque(porta, log):
    """Diagnostico: reinicia o S3 pela USB (RTS) e mostra o que ele escreve a arrancar (a ROM diz o modo de arranque:
    "SPI_FAST_FLASH_BOOT" = corre o firmware, "DOWNLOAD" = ficou no modo de gravacao; e erros da flash/imagem)."""
    try:
        import serial  # pylint: disable=import-outside-toplevel
    except ImportError:
        return []
    log("Restarting the S3 to see how it boots ...")
    linhas, fim = [], time.time() + 8
    try:
        with serial.Serial(porta, 115200, timeout=0.3, write_timeout=2) as s:
            s.dtr = False
            s.rts = True
            time.sleep(0.1)
            s.rts = False
            buf = b""
            while time.time() < fim and len(linhas) < 14:
                try:
                    buf += s.read(256)
                except (OSError, ValueError):
                    break                       # a USB caiu com o reinicio
                while b"\n" in buf and len(linhas) < 14:
                    l, buf = buf.split(b"\n", 1)
                    t = "".join(c for c in l.decode("ascii", "replace") if c.isprintable()).strip()
                    if t:
                        linhas.append(t)
    except (OSError, ValueError) as e:
        log("  could not open %s: %s" % (porta, str(e)[:80]))
        return []
    if not linhas:
        log("  the S3 wrote nothing while booting")
    for t in linhas:
        log("  boot: " + t[:90])
    return linhas


ERRO_MP_RUIM = ("The C3 runs MicroPython %%s, where its Bluetooth does not start. Confirm the erase to install MicroPython "
                "%s: the Atom sets its Wi-Fi again, and the camera has to be paired again (GATEWAYS > C3 > Pair camera)." % MP_VERSAO)


# ---- C3 --------------------------------------------------------------------------------------------------------------------
ERRO_APAGAR = ("%s did not answer as a working gateway (blank, or an old firmware that does not let go of the USB). Unplug it and plug "
               "it again and retry; or confirm the erase to install everything from scratch: the Atom sets its Wi-Fi again "
               "(cameras' network), and the camera has to be paired again (GATEWAYS > C3 > Pair camera).")


def c3_gravar(porta, wifi, log=print, apagar=False, de_raiz=False):
    """Garante o MicroPython no C3, copia os ficheiros do repositorio e (se `wifi`) o wifi.json. (ok, erro).
    Sem MicroPython so grava (e apaga) com `apagar`: a confirmacao do operador."""
    mp = fw_ota.ferramenta("mpremote")
    if not mp:
        return False, "mpremote is not installed on the Atom (pip install mpremote --break-system-packages)"
    log("Checking whether the C3 already runs MicroPython ...")
    tem = False if de_raiz and apagar else tem_micropython(porta, log=log)
    if de_raiz and apagar:
        log("  erase and install from scratch (chosen)")
    versao = c3_mp_versao(porta) if tem else None
    if versao:
        log("  MicroPython %s on the C3" % versao)
    ruim = bool(versao) and any(versao.startswith(v) for v in MP_RUINS)
    if not tem or ruim:
        if not apagar:
            return False, (ERRO_MP_RUIM % versao) if ruim else ERRO_APAGAR % "The C3"
        log("Installing MicroPython %s on the board" % MP_VERSAO)
        if not os.path.isfile(C3_MP):
            log("Downloading the MicroPython image ...")
            ok, err = baixar_micropython(log)
            if not ok:
                return False, err
        # Nao se apaga o chip inteiro: no C3 pela USB nativa demora mais de 2 minutos e a ligacao cai ("Packet content
        # transfer stopped"). Grava-se o MicroPython (o esptool apaga so o que escreve) e, no mesmo comando, 64 KB de 0xFF no
        # inicio da area de ficheiros: o sistema de ficheiros antigo (e o programa velho que nao largava a USB) deixa de
        # existir e o MicroPython formata-o no arranque.
        # Primeiro tenta-se apagar o chip todo (com o esptool proprio, com stub, sao uns 20 s): restos de outro firmware
        # (definicoes do Wi-Fi/Bluetooth de outra versao) podem rebentar o MicroPython. Se falhar, segue como antes.
        log("Erasing the whole C3 flash (about 30 s) ...")
        rc_e, out_e = _esptool(porta, ["erase_flash"], 150, log)
        apagado = rc_e == 0
        if not apagado:
            log("  full erase failed (%s): clearing the old areas instead" % _ultima(out_e)[:80])
        time.sleep(2)                           # o esptool reinicia o C3: a porta pode mudar de nome
        porta = esperar_porta(porta, 15) or porta
        # Sem o apagar total limpam-se, no mesmo comando, as zonas que o MicroPython nao reescreve: as definicoes (NVS) e
        # os dados do radio (phy_init) - restos do firmware anterior aqui fazem o Bluetooth nao arrancar ("BLE_INIT: hci
        # inits failed" e Guru Meditation) - e o inicio da area de ficheiros (o programa velho).
        args = ["write_flash", "0x0", C3_MP]
        limpar = []
        if not apagado:
            with open(C3_MP, "rb") as f:
                img = f.read(0x9000)
            for nome, zona in (("settings (NVS)", particao(img, 0x01, 0x02)), ("radio data", particao(img, 0x01, 0x01)),
                               ("files", particao_ficheiros(C3_MP))):
                if zona:
                    limpar.append((nome, zona[0], min(0x10000, zona[1])))
        tmps = []
        try:
            for nome, off, tam in limpar:
                fd, t = tempfile.mkstemp(prefix="c3ff")
                tmps.append(t)
                with os.fdopen(fd, "wb") as f:
                    f.write(b"\xff" * tam)
                args += [hex(off), t]
            log("Writing MicroPython %s%s ..." % (MP_VERSAO, "" if not limpar else
                                                  " and clearing the old " + ", ".join(n for n, _o, _t in limpar)))
            rc, out = _esptool(porta, args, 300, log)
        finally:
            for t in tmps:
                try:
                    os.remove(t)
                except OSError:
                    pass
        if rc:
            return False, "esptool: " + _ultima(out)
        log("MicroPython written to the C3")
        nova = esperar_porta(porta)
        if not nova:
            return False, "the C3 did not come back on USB after MicroPython: unplug it and plug it again, then retry"
        porta = nova
        time.sleep(3)
    tmp = None
    try:
        extra = []
        if wifi and wifi.get("ssid"):
            fd, tmp = tempfile.mkstemp(prefix="gwwifi")
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w") as f:
                json.dump({"nets": [[wifi["ssid"], wifi.get("psk") or ""]], "mode": "bridge"}, f)
            extra = [(tmp, "wifi.json")]
        # O token da OTA (o C3 so aceita firmware por Wi-Fi de quem o tiver): vai pela USB, nunca pela rede.
        token = fw_ota.gw_token_path() if hasattr(fw_ota, "gw_token_path") else None
        if token:
            extra.append((token, "gw_token"))
        ok, d = fw_ota.c3_usb_gravar(porta, log, extra_files=extra)
        return (True, None) if ok else (False, d.get("error") or "mpremote failed")
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


C3_DIAG_CODE = r"""
import network, json
s = network.WLAN(network.STA_IF)
a = network.WLAN(network.AP_IF)
import os
d = {"mpv": os.uname().release, "sta": s.isconnected(), "ip": s.ifconfig()[0], "st": s.status(), "ap": a.active()}
try:
    d["ssid"] = s.config("ssid") if s.isconnected() else ""
except Exception:
    d["ssid"] = ""
try:
    d["apname"] = a.config("essid") if a.active() else ""
except Exception:
    d["apname"] = ""
try:
    c = json.load(open("wifi.json"))
    d["nets"] = [n[0] for n in c.get("nets", [])]
    d["mode"] = c.get("mode")
except Exception:
    d["nets"] = None
try:
    d["diag"] = open("diag.log").read().splitlines()[-5:]
except Exception:
    d["diag"] = []
try:
    d["alive"] = open("alive.txt").read().strip()
except Exception:
    d["alive"] = "?"
import sys
m = sys.modules.get("netcfg")
d["net"] = getattr(m, "state", "?") if m else None
print("DIAG " + json.dumps(d))
"""


def c3_diagnostico(porta, log):
    """Pergunta ao C3, pela USB, como esta o Wi-Fi dele (rede, IP, modo; nunca a password) e reinicia-o. dict ou None."""
    mp = fw_ota.ferramenta("mpremote")
    if not mp:
        return None
    p = esperar_porta(porta, 5) or porta
    log("Asking the C3 by USB how its Wi-Fi is ...")
    rc, out = fw_ota.correr_limitado([mp, "connect", p, "resume", "exec", C3_DIAG_CODE], 20)
    d = None
    for l in out.splitlines():
        if l.startswith("DIAG "):
            try:
                d = json.loads(l[5:])
            except ValueError:
                pass
    if d is None:
        log("  the C3 did not answer (%s)" % _ultima(out)[:80])
    else:
        log("  MicroPython %s on the C3" % d.get("mpv"))
        if d.get("nets") is None:
            log("  the C3 has NO Wi-Fi saved (no wifi.json): set it again with a Wi-Fi network")
        else:
            log("  saved networks: %s; mode: %s" % (", ".join(d["nets"]) or "none", d.get("mode")))
        if d.get("sta"):
            log("  connected to %s, address %s" % (d.get("ssid") or "?", d.get("ip")))
        elif d.get("ap"):
            log("  NOT connected: it opened its own network %s (192.168.4.1)" % (d.get("apname") or "FIZ-C3-XXXX"))
        else:
            log("  NOT connected (Wi-Fi status %s): wrong password, or the network is out of reach" % d.get("st"))
        for t in d.get("diag") or []:
            log("  diag: " + str(t)[:80])
        log("  program alive: %s; Wi-Fi part: %s" % (str(d.get("alive"))[:60], "NOT started (the program stopped before it)"
                                                      if d.get("net") is None else d.get("net")))
    if not c3_arranque(p, log):
        fw_ota.correr_limitado([mp, "connect", p, "reset"], 15)    # o exec parou o programa: volta a arrancar
    return d


def c3_arranque(porta, log, tempo=25.0):
    """Reinicia o C3 (reset a serio, como ligar: um "soft reboot" com o Bluetooth activo rebenta o C3 - Guru Meditation)
    e mostra, a medida que chega, o que ele escreve a arrancar: se o main.py rebenta, aparece aqui o erro (Traceback).
    Corre numa thread com prazo: uma porta USB presa nunca prende o trabalho. False se nao se conseguiu."""
    try:
        import serial  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False
    log("Restarting the C3 and watching it for %d s ..." % tempo)
    linhas, res = [], {"ok": False}

    def ver():
        try:
            with serial.Serial(porta, 115200, timeout=0.3, write_timeout=2) as s:
                s.write(b"\r\x03\x03")            # para o que estiver a correr (fica na REPL)
                time.sleep(0.5)
                s.write(b"import machine; machine.reset()\r")
                time.sleep(0.3)
        except Exception:                       # pylint: disable=broad-except
            pass                                # a porta cai com o reset: normal
        time.sleep(0.3)
        fim, buf, s = time.time() + tempo, b"", None
        while time.time() < fim and len(linhas) < 25:
            if s is None:                       # a USB do C3 volta logo depois do reset: apanha-se o arranque
                try:
                    s = serial.Serial(esperar_porta(porta, 0.5) or porta, 115200, timeout=0.3, write_timeout=2)
                    res["ok"] = True
                except Exception:               # pylint: disable=broad-except
                    time.sleep(0.1)
                    continue
            try:
                buf += s.read(256)
            except Exception:                   # pylint: disable=broad-except
                s = None
                continue
            while b"\n" in buf and len(linhas) < 25:
                l, buf = buf.split(b"\n", 1)
                t = "".join(c for c in l.decode("utf-8", "replace") if c.isprintable()).strip()
                if t and t != ">>>":
                    linhas.append(t)
                    log("  C3: " + t[:90])
        if s is not None:
            s.close()

    t = threading.Thread(target=ver, daemon=True)
    t.start()
    t.join(tempo + 8)
    if t.is_alive():
        log("  the C3 USB port is stuck (no answer)")
        return False
    if not res["ok"]:
        log("  the C3 did not come back on USB after the restart")
        return False
    if any("Traceback" in x or "Error" in x for x in linhas):
        log("  the C3 program STOPPED with an error (above)")
    elif any(x.startswith(">>>") or x.endswith(">>>") for x in linhas):
        log("  the C3 program ended (it is at the prompt)")
    else:
        log("  no error: the C3 program is running")
    return True


# ---- ponto de entrada ------------------------------------------------------------------------------------------------------
def estado():
    ssid, _psk = ap_wifi()
    return {"ports": fw_ota.c3_usb_portas(), "tools": ferramentas(), "images": imagens(),
            "ap_wifi": bool(ssid), "ap_ssid": ssid}


def configurar(porta=None, tipo="auto", wifi=None, usar_ap=False, modo="auto", log=print, apagar=False):
    """Identifica a placa na porta USB e grava-lhe o firmware (e o Wi-Fi). (ok, dict sem segredos).
    `apagar`: o operador confirmou que se pode apagar a placa (sem isso, uma placa que nao responde como um gateway
    que funciona nao se apaga: um erro transitorio nao pode levar o Wi-Fi e a camara emparelhada)."""
    portas = fw_ota.c3_usb_portas()
    if porta is None:
        cand = [p for p in portas if p["espressif"]] or portas
        if not cand:
            return False, {"error": "no USB serial port: plug the gateway into the Atom by USB"}
        if len(cand) > 1:
            return False, {"error": "more than one USB serial port (%s): say which one" % ", ".join(p["port"] for p in cand),
                           "ports": cand}
        porta = cand[0]["port"]
    elif porta not in [p["port"] for p in portas]:
        return False, {"error": "%s is not a USB serial port of the Atom" % porta}
    if tipo == "m5stick":                      # a M5StickS3 tambem e um ESP32-S3: so se distingue de um gateway porque se escolhe
        return m5_configurar(porta, wifi, modo, usar_ap, log)
    if usar_ap and not wifi:
        ssid, psk = ap_wifi()
        if not ssid:
            return False, {"error": "could not read the cameras' Wi-Fi from the Atom: type the network name and password"}
        wifi = {"ssid": ssid, "psk": psk}
    if not esptool_cmd():
        return False, {"error": "esptool is not installed on the Atom (pip install esptool --break-system-packages)"}
    if wifi and any(c in (wifi.get("ssid") or "") + (wifi.get("psk") or "") for c in "|\r\n") and tipo != "c3":
        return False, {"error": "the Wi-Fi name and password cannot contain '|' or line breaks (for the S3)"}
    if wifi is not None and (not wifi.get("ssid") or len(wifi["ssid"]) > 32 or len(wifi.get("psk") or "") > 63
                             or 0 < len(wifi.get("psk") or "") < 8):
        return False, {"error": "Wi-Fi: name 1 to 32 characters; password empty (open network) or 8 to 63"}
    log("Port %s: identifying the board ..." % porta)
    chip, out = identificar(porta)
    if chip:
        log("Board: ESP32-%s (%s gateway)" % (chip.upper(), chip.upper()))
        for l in out.splitlines():
            if re.match(r"\s*(Chip is|Features|USB mode|Crystal)", l):
                log("  " + l.strip()[:90])
        # O esptool reinicia a placa: espera a porta voltar (o nome pode mudar) e o programa arrancar.
        nova = esperar_porta(porta, 15)
        if nova:
            porta = nova
        time.sleep(2)
    if tipo in ("c3", "s3") and chip and chip != tipo:
        return False, {"error": "the board on %s is an ESP32-%s, not a %s gateway" % (porta, chip.upper(), tipo.upper())}
    if not chip:
        return False, {"error": "could not identify the board on %s (%s). Unplug it and plug it again, or hold BOOT while "
                       "plugging it. If the port is busy: sudo systemctl stop ModemManager" % (porta, _ultima(out))}
    res = {"chip": chip, "port": porta}
    if chip == "c3":
        ok, err = c3_gravar(porta, wifi, log, apagar, de_raiz=modo == "setup")
        if not ok:
            return False, {**res, "error": err, "needs_erase": err == ERRO_APAGAR % "The C3"
                                   or err.startswith("The C3 runs MicroPython")}
        res.update({"kind": "c3", "wifi": bool(wifi), "note": "the C3 restarts; it joins the Wi-Fi and the Atom finds it by itself (about 30 s)"})
        return True, res
    # S3
    if modo == "auto":
        log("Checking whether the S3 already has firmware ...")
        tem = s3_ja_tem_firmware(porta)
        modo = "update" if tem else "setup"
        log("S3: %s" % ("already has firmware: updating the application only (Wi-Fi and GoPro are kept)" if tem
                        else "could not read its flash: cannot tell whether it has firmware" if tem is None
                        else "blank: full setup"))
        if modo == "setup" and not apagar:
            return False, {**res, "error": ERRO_APAGAR % "The S3", "needs_erase": True}
    ok, err = s3_gravar(porta, modo, log)
    if not ok:
        return False, {**res, "error": err}
    res.update({"kind": "s3", "mode": modo})
    est = {}
    if not (wifi and wifi.get("ssid")) and _token_s3():
        # Sem Wi-Fi para enviar: o token vai na mesma (o S3 continua a correr, nao reinicia por isto).
        log("Waiting for the S3 to restart ...")
        time.sleep(3)
        nova = esperar_porta(porta)
        if nova:
            log("Sending the gateway token to the S3 ...")
            s3_wifi_serie(nova, None, None, log, tempo=30, diagnostico=False, estado=est)
        if not est.get("token"):
            log("  WARNING: the S3 did not take the gateway token: it stays open on the network (set it up again by USB)")
        res["token"] = bool(est.get("token"))
    if wifi and wifi.get("ssid"):
        log("Waiting for the S3 to restart ...")
        time.sleep(3)                          # a USB do S3 desaparece no reinicio: nao se apanha a porta velha
        nova = esperar_porta(porta)
        if not nova:
            return False, {**res, "error": "the S3 did not come back on USB: the firmware is written; set the Wi-Fi again"}
        log("Sending the Wi-Fi to the S3 ...")
        ok, err = s3_wifi_serie(nova, wifi["ssid"], wifi.get("psk") or "", log, estado=est)
        if not ok and err == S3_PRESO:
            # O reinicio pela USB deixou-o no modo de gravacao (o GPIO0 lido em baixo): so desligar a alimentacao o tira de la.
            log(">>> UNPLUG the S3 from the USB and plug it in again <<<")
            nova = esperar_religar(nova, log)
            if not nova:
                return False, {**res, "error": "the S3 was not unplugged and plugged in again: the firmware is written; "
                               "unplug it, plug it again and set the Wi-Fi (Setup / update by USB)"}
            log("Sending the Wi-Fi to the S3 ...")
            ok, err = s3_wifi_serie(nova, wifi["ssid"], wifi.get("psk") or "", log, diagnostico=False, estado=est)
        if not ok:
            return False, {**res, "error": err}
        res["wifi"] = True
        res["token"] = bool(est.get("token"))
        try:
            diag = s3_verificar_wifi(nova, wifi["ssid"], log)
        except Exception as e:                  # pylint: disable=broad-except
            diag = None                          # a verificacao nunca estraga uma gravacao que correu bem
            log("  could not check the S3 on the Wi-Fi: %s" % str(e)[:80])
        res["joined"] = None if diag is None else diag.get("state") == "up"
        if res["joined"] is False and diag.get("state") == "ap":
            res["wifi_warning"] = "the S3 did not join the Wi-Fi (it opened %s)" % (diag.get("ap") or "GOPRO-S3-XXXX")
    res["note"] = ("the S3 restarts and joins the Wi-Fi; add it in Devices > Add an S3 gateway (its address shows as found)"
                   if res.get("wifi") else "no Wi-Fi was set: unplug the S3 and plug it in again (so it starts the new firmware); it "
                   "opens its own network GOPRO-S3-XXXX (192.168.4.1)")
    if res.get("wifi_warning"):
        res["note"] = ("WARNING: " + res["wifi_warning"] + ". The Wi-Fi is saved: check that EXT WI-FI is on the air; it tries "
                       "again by itself every 3 min, or join its network and add the Wi-Fi in its page (192.168.4.1).")
    return True, res


if __name__ == "__main__":                      # sudo python3 /opt/camera-bridge/gw_usb.py [--ap] [porta]   |   --fetch-mp
    if "--fetch-mp" in sys.argv:                # o instalador: deixa o MicroPython do C3 no Atom, se houver Internet
        if os.path.isfile(C3_MP):
            sys.exit(0)
        ok_mp, err_mp = baixar_micropython()
        print("MicroPython do C3:", "ok" if ok_mp else err_mp)
        sys.exit(0 if ok_mp else 1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ok, d = configurar(args[0] if args else None, usar_ap="--ap" in sys.argv)
    print(json.dumps(d, indent=1))
    sys.exit(0 if ok else 1)

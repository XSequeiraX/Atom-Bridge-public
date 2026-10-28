"""Firmware dos gateways por Wi-Fi (OTA), a partir do Atom (2026-10-13).

C3 (MicroPython): os ficheiros vem do repositorio (c3_fiz_bm: main.py e o programa ja compilado, app.mpy e netcfg.mpy,
que o GitHub compila; compilar no C3 deixava-o sem memoria para o Wi-Fi e o Bluetooth), que o instalador copia para
fw/c3/ ao lado deste ficheiro. So se enviam os que mudaram (o C3 diz o SHA-256 de cada um em /api/fw); depois
/api/fw/apply: o C3 guarda os actuais em .bak e reinicia em teste (boot.py repoe-os se o novo nao arrancar).

S3 (Arduino, gateway das GoPro): o .bin vem do repositorio (o GitHub compila-o para atom/app/fw/s3/gopro_gw.bin; o firmware
nao leva segredos, o Wi-Fi guarda-se no proprio S3) e o instalador deixa-o em fw/s3/. Um .bin compilado a mao tambem pode
chegar por /api/gateways/s3/firmware. O S3 recebe-o em /api/fw (multipart, com o MD5), grava-o na outra particao e reinicia.
/api/fw/rollback no S3 volta ao anterior. `escrito.json` regista o que o Atom gravou em cada S3.
"""
import hashlib
import json
import glob
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

AQUI = os.path.dirname(os.path.abspath(__file__))
FW_DIR = os.path.join(AQUI, "fw")
C3_DIR = os.path.join(FW_DIR, "c3")
C3_FILES = ("main.py", "app.mpy", "netcfg.mpy")
S3_BIN = os.path.join(FW_DIR, "s3", "gopro_gw.bin")
S3_ESCRITO = os.path.join(FW_DIR, "s3", "escrito.json")     # host -> md5 do ultimo .bin que o Atom lhe gravou


def _pedido(url, data=None, ctype=None, timeout=10, method=None, headers=None):
    """(status HTTP, JSON). 0 quando nao responde."""
    cab = dict(headers or {})
    if ctype:
        cab["Content-Type"] = ctype
    metodo = method or ("POST" if data is not None else "GET")
    if metodo != "POST":
        cab.pop("X-GW-Token", None)        # o token so vai em pedidos que mudam alguma coisa (nunca num GET de sondagem)
    req = urllib.request.Request(url, data=data, method=metodo, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:                  # pylint: disable=broad-except
        return 0, {"error": str(e)}


def _sha(caminho):
    with open(caminho, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# -- C3 -------------------------------------------------------------------------------------------------------------
# O token da OTA dos C3: o C3 so aceita firmware por Wi-Fi de quem o tiver (cabecalho X-GW-Token). Vive fora da pasta
# da app (sobrevive as actualizacoes) e vai para o C3 pela USB, na configuracao inicial; nunca pela rede nem para o registo.
GW_TOKEN_PATH = "/var/lib/camera-bridge/gw_token"


def gw_token_path():
    """O ficheiro do token (criado na primeira vez, 0600). None se nao se conseguiu criar."""
    caminho = GW_TOKEN_PATH if os.access(os.path.dirname(GW_TOKEN_PATH), os.W_OK) or os.path.isfile(GW_TOKEN_PATH) \
        else os.path.join(AQUI, "gw_token")
    if os.path.isfile(caminho):
        return caminho
    try:
        import secrets
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        tmp = caminho + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(16))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, caminho)
        return caminho
    except OSError:
        return None


def token_cab():
    """O cabecalho com o token dos gateways ({} se nao ha). Vai so nos pedidos que MUDAM alguma coisa (POST) aos C3 (firmware)
    e aos S3, a um aparelho ja conhecido como gateway: nunca num GET (o firmware nao os protege) nem num broadcast."""
    p = gw_token_path()
    try:
        with open(p) as f:
            return {"X-GW-Token": f.read().strip()}
    except (OSError, TypeError):
        return {}


_token_cab = token_cab


def c3_versao(texto):
    """O numero de versao de "fiz_bm_c3 24" (o que o C3 diz no /api/status) ou do app.mpy (que traz esse texto), ou None."""
    if isinstance(texto, bytes):
        m = re.findall(rb"fiz_bm_c3 (\d+)", texto)
        return max(int(x) for x in m) if m else None
    m = re.search(r"fiz_bm_c3 (\d+)", str(texto or ""))
    return int(m.group(1)) if m else None


def c3_estado(host):
    """O que esta no C3 e o que o repositorio traz: quais ficheiros mudaram (pelo SHA-256 que o proprio C3 diz ter de cada
    um, por isso vale para um C3 gravado por USB). `bin_version`: a versao do app.mpy que o Atom traz."""
    disp = {n: _sha(os.path.join(C3_DIR, n)) for n in C3_FILES if os.path.isfile(os.path.join(C3_DIR, n))}
    s, d = _pedido("http://%s/api/fw" % host, timeout=5) if host else (0, {"error": "no C3 address"})
    out = {"available": disp, "host": host, "reachable": s == 200}
    try:
        with open(os.path.join(C3_DIR, "app.mpy"), "rb") as f:
            out["bin_version"] = c3_versao(f.read())
    except OSError:
        out["bin_version"] = None
    if s == 200 and "app.mpy" not in (d.get("files") or {}) and "main.py" in (d.get("files") or {}):
        # Firmware de antes da 20 (o codigo-fonte no C3): o netcfg dele nao aceita os .mpy por Wi-Fi.
        out.update({"fw": d.get("fw"), "error": "C3 firmware older than fiz_bm_c3 20: update it once by USB "
                    "(the program now goes precompiled)"})
    elif s == 200:
        inst = d.get("files") or {}
        out.update({"fw": d.get("fw"), "installed": inst, "trial": d.get("trial"), "rollback": d.get("rollback"),
                    "changed": [n for n in disp if inst.get(n) != disp[n]]})
    elif s == 404:
        out["error"] = "C3 firmware without OTA: flash it once by USB"
    else:
        out["error"] = d.get("error") or "C3 not answering (HTTP %s)" % s
    return out


def c3_atualizar(host, log=print):
    """Envia os ficheiros que mudaram e aplica (o C3 reinicia). (ok, dict)."""
    e = c3_estado(host)
    if not e.get("reachable"):
        return False, e
    if not e["available"]:
        return False, {"error": "no C3 firmware in %s (update the bridge from GitHub)" % C3_DIR}
    if not e["changed"]:
        return True, {"ok": True, "changed": [], "note": "already up to date"}
    for n in e["changed"]:
        with open(os.path.join(C3_DIR, n), "rb") as f:
            dados = f.read()
        s, d = _pedido("http://%s/api/fw/put?name=%s&sha=%s" % (host, n, hashlib.sha256(dados).hexdigest()),
                       data=dados, ctype="application/octet-stream", timeout=30, headers=_token_cab())
        if s != 200:
            return False, {"error": "%s: %s" % (n, d.get("error") or "HTTP %s" % s)}
        log("C3 firmware: %s sent (%d bytes)" % (n, len(dados)))
    s, d = _pedido("http://%s/api/fw/apply" % host, data=b"", timeout=10, headers=_token_cab())
    if s != 200:
        return False, {"error": "apply: %s" % (d.get("error") or "HTTP %s" % s)}
    log("C3 firmware: %s applied, the C3 restarts" % ", ".join(e["changed"]))
    return True, {"ok": True, "changed": e["changed"]}


def c3_reverter(host):
    s, d = _pedido("http://%s/api/fw/rollback" % host, data=b"", timeout=10, headers=_token_cab())
    return s == 200, d


# -- C3 por USB (so quando o firmware do C3 ainda nao tem OTA) -------------------------------------------------------
# O C3 ligado por cabo ao Atom grava-se daqui com o mpremote: boot.py, main.py, app.mpy e netcfg.mpy do repositorio (fw/c3,
# C3_USB_FILES). So esses: a rede (wifi.json) e o emparelhamento da camara (ble_secrets.json) ficam como estao. Depois disto o
# C3 tem OTA e actualiza-se sempre por Wi-Fi.
C3_USB_FILES = ("boot.py",) + C3_FILES
# As ferramentas dos gateways (esptool, mpremote) num ambiente proprio, instalado pelo instalador: o esptool do Debian vem
# sem os "stubs" (o programa que o esptool envia ao chip para ler e gravar) e falha.
GW_TOOLS = "/opt/gw-tools/bin"


def ferramenta(nome):
    """O caminho da ferramenta: a do ambiente proprio (/opt/gw-tools) se existir, senao a do sistema. None se nao ha."""
    for n in ((nome, nome + ".py") if not nome.endswith(".py") else (nome,)):
        c = os.path.join(GW_TOOLS, n)
        if os.access(c, os.X_OK):
            return c
    return shutil.which(nome)
# O que corre no C3 para conferir os ficheiros: o SHA-256 de cada um, lido aos bocados (a RAM do C3 e pouca). A indentacao
# conta (e MicroPython): uma linha fora do ciclo imprimia so o ultimo ficheiro.
C3_SHA_CODE = "\n".join([
    "import hashlib,binascii",
    "for n in %r:" % (C3_USB_FILES,),
    " h=hashlib.sha256()",
    " f=open(n,'rb')",
    " while True:",
    "  b=f.read(512)",
    "  if not b:",
    "   break",
    "  h.update(b)",
    " f.close()",
    " print('SHA',n,binascii.hexlify(h.digest()).decode())",
])
C3_RM_TRIAL_CODE = "import os\ntry:\n os.remove('fw_trial')\nexcept OSError:\n pass"
# Depois de copiar os .mpy: o codigo-fonte antigo sai (o MicroPython prefere o .py ao .mpy) e as copias do OTA
# antigo tambem (o boot.py repunha-as).
C3_LIMPAR_CODE = "\n".join([
    "import os",
    "for n in ('app.py','netcfg.py','main.py.bak','netcfg.py.bak','main.py.new','netcfg.py.new','app.mpy.bak',"
    "'netcfg.mpy.bak','app.mpy.new','netcfg.mpy.new'):",
    " try:",
    "  os.remove(n)",
    " except OSError:",
    "  pass",
])
_usb_lock = threading.Lock()


def c3_usb_portas(sys_tty="/sys/class/tty", dev="/dev"):
    """As portas serie USB que podem ser o C3: [{port, vid, pid, espressif}], as da Espressif (303a) primeiro."""
    out = []
    for caminho in sorted(glob.glob(os.path.join(sys_tty, "ttyACM*")) + glob.glob(os.path.join(sys_tty, "ttyUSB*"))):
        nome = os.path.basename(caminho)
        vid = pid = ""
        d = os.path.realpath(os.path.join(caminho, "device"))
        for _ in range(4):                      # o idVendor esta no dispositivo USB, acima da interface
            try:
                with open(os.path.join(d, "idVendor")) as f:
                    vid = f.read().strip()
                with open(os.path.join(d, "idProduct")) as f:
                    pid = f.read().strip()
                break
            except OSError:
                d = os.path.dirname(d)
        out.append({"port": os.path.join(dev, nome), "vid": vid, "pid": pid, "espressif": vid == "303a"})
    return sorted(out, key=lambda x: (not x["espressif"], x["port"]))


def c3_usb_estado(**kw):
    """O que ha para gravar e por onde: portas, mpremote, ficheiros do repositorio."""
    em_falta = [n for n in C3_USB_FILES if not os.path.isfile(os.path.join(C3_DIR, n))]
    return {"ports": c3_usb_portas(**kw), "mpremote": bool(ferramenta("mpremote")), "missing_files": em_falta}


def correr_limitado(cmd, timeout):
    """Corre `cmd` e devolve (returncode, saida) em no maximo `timeout` (+5) s, aconteca o que acontecer. Um mpremote preso
    numa porta USB que nao responde pode nem morrer logo (o fecho da porta fica pendurado no kernel): mata-se o grupo e, se
    nao acabar em 5 s, abandona-se, em vez de ficar a espera para sempre."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    except OSError as ex:
        return 127, str(ex)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
        return 124, (out or "") + "\ntimeout: no answer in %d s" % timeout


def _mpremote(exe, porta, args, timeout):
    """Um comando do mpremote (cada passo a sua vez: o erro diz onde parou). (returncode, saida).
    `resume`: sem soft reset (que voltava a correr o boot.py do C3; se o firmware arranca dali, nunca respondia)."""
    return correr_limitado([exe, "connect", porta, "resume"] + list(args), timeout)


def _dica_usb(saida):
    low = (saida or "").lower()
    if "busy" in low or "ermission" in saida:
        return " (the port is in use: stop ModemManager with 'sudo systemctl stop ModemManager', or run as root)"
    if "could not" in low or "imeout" in low or "no device" in low:
        return " (the C3 may be stuck: unplug and plug it again, or hold BOOT while plugging it)"
    return ""


def c3_usb_gravar(porta=None, log=print, mpremote="mpremote", extra_files=None, **kw):
    """Grava o firmware do repositorio no C3 por USB, um passo de cada vez, e confere o SHA-256 de cada ficheiro no proprio
    C3. `extra_files`: [(origem, nome no C3)], ex. o wifi.json da configuracao inicial. (ok, dict). O C3 reinicia no fim."""
    e = c3_usb_estado(**kw)
    exe = ferramenta(mpremote) if os.path.basename(mpremote) == mpremote else mpremote
    if not exe:
        return False, {"error": "mpremote is not installed on the Atom (pip install mpremote --break-system-packages)"}
    if e["missing_files"]:
        return False, {"error": "no C3 firmware in %s (update the bridge from GitHub): missing %s" % (C3_DIR, ", ".join(e["missing_files"]))}
    if porta is None:
        cand = [p for p in e["ports"] if p["espressif"]] or e["ports"]
        if not cand:
            return False, {"error": "no USB serial port: is the C3 plugged into the Atom by USB?"}
        if len(cand) > 1:
            return False, {"error": "more than one USB serial port (%s): say which one" % ", ".join(p["port"] for p in cand),
                           "ports": cand}
        porta = cand[0]["port"]
    elif porta not in [p["port"] for p in e["ports"]]:
        return False, {"error": "%s is not a USB serial port of the Atom" % porta}
    if not _usb_lock.acquire(blocking=False):
        return False, {"error": "a USB flash is already running"}
    try:
        # Apaga o fw_trial de uma actualizacao OTA a meio, para o boot.py novo nao o contar como arranque falhado.
        passos = [("Stopping the C3 program", ["exec", C3_RM_TRIAL_CODE], 40)]
        for n in C3_USB_FILES:
            passos.append(("Copying %s" % n, ["fs", "cp", os.path.join(C3_DIR, n), ":" + n], 120))
        passos.append(("Removing the old source files", ["exec", C3_LIMPAR_CODE], 40))
        for origem, nome in (extra_files or []):
            passos.append(("Writing %s" % nome, ["fs", "cp", origem, ":" + nome], 60))
        passos.append(("Checking the files on the C3", ["exec", C3_SHA_CODE], 60))
        passos.append(("Restarting the C3", ["reset"], 30))
        shas = {}
        for i_passo, (titulo, args, tempo) in enumerate(passos):
            log(titulo + " ...")
            rc, saida = _mpremote(exe, porta, args, tempo)
            for n in range(2 if i_passo == 0 else 0):   # o primeiro passo apanha o C3 acabado de reiniciar: repete
                if not rc:
                    break
                log("  the C3 is not ready yet, retrying (%s)" % (saida.strip().splitlines() or ["?"])[-1][:60])
                time.sleep(3)
                rc, saida = _mpremote(exe, porta, args, tempo)
            if rc and args[0] != "reset":
                ultima = (saida.strip().splitlines() or ["?"])[-1][:200]
                return False, {"error": "%s failed: %s%s" % (titulo, ultima, _dica_usb(saida)), "step": titulo}
            for linha in saida.splitlines():
                partes = linha.split()
                if len(partes) == 3 and partes[0] == "SHA":
                    shas[partes[1]] = partes[2]
        mal = [n for n in C3_USB_FILES if n in shas and shas[n] != _sha(os.path.join(C3_DIR, n))]
        if mal:
            return False, {"error": "the C3 did not keep %s (checksum differs): try again" % ", ".join(mal), "step": "check"}
        nao_lidos = [n for n in C3_USB_FILES if n not in shas]
        log("C3 firmware written by USB on %s: %s" % (porta, ", ".join(C3_USB_FILES)))
        nota = "the C3 restarts; it rejoins the Wi-Fi by itself (about 20 s)"
        if nao_lidos:                       # o C3 nao devolveu o checksum de alguns: copiou-os, mas nao se confirmou
            nota += " (copied, but the checksum of %s was not read back)" % ", ".join(nao_lidos)
        return True, {"ok": True, "port": porta, "files": list(C3_USB_FILES), "note": nota}
    finally:
        _usb_lock.release()


# -- S3 -------------------------------------------------------------------------------------------------------------
def _s3_escrito():
    try:
        with open(S3_ESCRITO) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _s3_registar(host, md5):
    d = _s3_escrito()
    d[host] = md5
    os.makedirs(os.path.dirname(S3_ESCRITO), exist_ok=True)
    tmp = S3_ESCRITO + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, S3_ESCRITO)


def s3_versao(texto):
    """O numero de versao de "gopro_gw 14" (o que o S3 diz no /api/status) ou de um .bin (que traz esse texto), ou None."""
    if isinstance(texto, bytes):
        m = re.findall(rb"gopro_gw (\d+)", texto)
        return max(int(x) for x in m) if m else None
    m = re.search(r"gopro_gw (\d+)", str(texto or ""))
    return int(m.group(1)) if m else None


def s3_estado(host):
    """O .bin que o Atom guarda e se este S3 ja o tem. A versao que o S3 diz ("gopro_gw 13") compara-se com a do .bin
    ("gopro_gw 14"): vale para um S3 gravado por USB, de que o Atom nunca registou nada. Com a mesma versao, e se o Atom lhe
    gravou um .bin, compara-se o md5 (um .bin refeito com a mesma versao). `outdated` e None so quando nao ha como saber."""
    out = {"host": host, "bin": None, "outdated": None}
    s, d = _pedido("http://%s/api/status" % host, timeout=5) if host else (0, {})
    out["reachable"] = s == 200
    out["fw"] = d.get("fw") if s == 200 else None
    if os.path.isfile(S3_BIN):
        with open(S3_BIN, "rb") as f:
            b = f.read()
        out["bin"] = {"size": len(b), "md5": hashlib.md5(b).hexdigest(), "mtime": int(os.path.getmtime(S3_BIN)),
                      "version": s3_versao(b)}
        antes = _s3_escrito().get(host)
        out["written_md5"] = antes
        v_bin, v_s3 = out["bin"]["version"], s3_versao(out["fw"])
        if v_bin is not None and v_s3 is not None and v_bin != v_s3:
            out["outdated"] = v_bin > v_s3                # o S3 mais novo do que o .bin do Atom nao e "atrasado"
            out["by"] = "version"
        elif v_bin is not None and v_s3 is not None:      # a mesma versao
            out["outdated"] = (antes != out["bin"]["md5"]) if antes else False
            out["by"] = "md5" if antes else "version"
        else:
            out["outdated"] = (antes != out["bin"]["md5"]) if antes else None
            out["by"] = "md5"
    return out


def _s3_app_valida(d):
    """Imagem de aplicacao ESP32 inteira? Cabecalho 0xE9 e, se traz o SHA-256 no fim, confere-o."""
    if len(d) < 4096 or d[:1] != b"\xe9":
        return False
    return hashlib.sha256(d[:-32]).digest() == d[-32:] if d[23] == 1 else True


def s3_atualizar(host, log=print):
    """Grava no S3 o .bin guardado (multipart, campo "fw", com o MD5). O S3 reinicia. (ok, dict)."""
    if not host:
        return False, {"error": "no S3 address"}
    if not os.path.isfile(S3_BIN):
        return False, {"error": "no S3 firmware on the Atom: update the bridge from GitHub"}
    with open(S3_BIN, "rb") as f:
        b = f.read()
    if not _s3_app_valida(b):           # um .bin estragado (git, disco) deixava o S3 sem arrancar
        return False, {"error": "the S3 firmware on the Atom is damaged (checksum): update the bridge from GitHub"}
    md5 = hashlib.md5(b).hexdigest()
    fronteira = uuid.uuid4().hex
    corpo = (("--%s\r\nContent-Disposition: form-data; name=\"fw\"; filename=\"gopro_gw.bin\"\r\n"
              "Content-Type: application/octet-stream\r\n\r\n" % fronteira).encode() + b
             + ("\r\n--%s--\r\n" % fronteira).encode())
    s, d = _pedido("http://%s/api/fw?md5=%s" % (host, md5), data=corpo,
                   ctype="multipart/form-data; boundary=%s" % fronteira, timeout=120, headers=_token_cab())
    if s != 200:
        if s == 404:
            d = {"error": "S3 firmware without OTA: flash it once by USB"}
        return False, {"error": d.get("error") or "HTTP %s" % s}
    _s3_registar(host, md5)
    pendente = bool(isinstance(d, dict) and d.get("pending_restart"))      # gravou, mas ha uma camara a gravar: so reinicia no proximo arranque
    log("S3 firmware: %d bytes written, %s" % (len(b), "the S3 restarts at its next boot (a camera is recording)" if pendente else "the S3 restarts"))
    r = {"ok": True, "size": len(b), "md5": md5}
    if pendente:
        r["pending_restart"] = True
    return True, r


def s3_reverter(host):
    s, d = _pedido("http://%s/api/fw/rollback" % host, data=b"", timeout=10, headers=_token_cab())
    return s == 200, d


if __name__ == "__main__":                      # sudo python3 /opt/camera-bridge/fw_ota.py usb [/dev/ttyACM0]
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "usb":
        ok, d = c3_usb_gravar(sys.argv[2] if len(sys.argv) > 2 else None)
        print(json.dumps(d, indent=1))
        sys.exit(0 if ok else 1)
    sys.exit("use: fw_ota.py usb [port]")

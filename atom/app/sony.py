"""As camaras Sony (Alpha, FX, ZV...) pelo Camera Remote SDK da Sony (2026-10-27).

O SDK e uma biblioteca C++ fechada, com licenca propria (sony/AVISO-SONY.md): o repositorio leva so o nosso ajudante ja
compilado (sony/sony_ctl, de sony/sony_ctl.cpp) com as bibliotecas da Sony de que ele precisa ao lado, como a licenca
deixa distribuir; os headers e o resto do SDK nao. Os termos da Sony aceitam-se uma vez, com os outros avisos de licenca
(licencas.py), antes de usar a consola ou a pagina: so se acrescenta uma camara com eles aceites (termos_ok).

Este modulo arranca o ajudante e fala com ele por linhas (comandos no stdin, eventos JSON no stdout). Cada camara e um
alvo de REC com id proprio (sony1, sony2...) e a mesma interface das outras frotas (bmcam, gopro): ready, running, alvo,
set_running, status. Liga por Wi-Fi (a camara entra no AP das camaras do Atom, como as Canon: sem gateway) ou por USB.

sonys.json: {"sonys": {id: {name, model, via: "ip"|"usb", host, serial, ssh, user, password, enabled}}, "removed": {...}}
"""
import json
import os
import re
import subprocess
import threading
import time

import gravar

AQUI = os.path.dirname(os.path.abspath(__file__))
SONY_CTL = os.path.join(AQUI, "sony", "sony_ctl")
CONFIRM = 6.0            # s para a camara confirmar REC/STOP (RecordingState)
RESPOSTA = 4.0           # s para o ajudante responder a um comando
RELIGAR = 10.0           # s entre tentativas de ligar uma camara que nao respondeu
ESTADO_VELHO = 8.0       # sem estado ha mais do que isto: os valores lidos ja nao contam

# O que cada modelo suporta para o controlo remoto (API Reference do SDK 2.02.00, Compatibility): por onde liga e se
# exige autenticacao SSH. Um modelo que nao esta aqui mas que o SDK instalado conhece funciona na mesma (o ajudante le a
# lista do SDK): so nao ha estas indicacoes para a consola e a pagina.
MODELOS = {
    "ILX-LR1": {"usb": True, "lan": True, "wifi": True, "ssh": False},
    "ILCE-1M2": {"usb": True, "lan": True, "wifi": True, "ssh": False},
    "ILCE-1": {"usb": True, "lan": True, "wifi": True, "ssh": False},
    "ILCE-9M3": {"usb": True, "lan": True, "wifi": True, "ssh": False},
    "ILCE-9M2": {"usb": True, "lan": True, "wifi": False, "ssh": False},
    "ILCE-7RM6": {"usb": True, "lan": False, "wifi": True, "ssh": True},
    "ILCE-7RM5": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ILCE-7RM4A": {"usb": True, "lan": False, "wifi": False, "ssh": False},
    "ILCE-7RM4": {"usb": True, "lan": False, "wifi": False, "ssh": False},
    "ILCE-7CR": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ILCE-7SM3": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ILCE-7M5": {"usb": True, "lan": False, "wifi": True, "ssh": True},
    "ILCE-7M4": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ILCE-7CM2": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ILCE-7C": {"usb": True, "lan": False, "wifi": False, "ssh": False},
    "ILCE-6700": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "MPC-2610": {"usb": False, "lan": True, "wifi": True, "ssh": True},
    "ILME-FX6": {"usb": False, "lan": True, "wifi": True, "ssh": True},        # firmware 3.00 ou mais
    "ILME-FX3A": {"usb": True, "lan": True, "wifi": True, "ssh": False},
    "ILME-FX3": {"usb": True, "lan": True, "wifi": True, "ssh": False},        # firmware 2.00 ou mais
    "ILME-FX2": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ILME-FX30": {"usb": True, "lan": True, "wifi": True, "ssh": False},
    "PXW-Z300": {"usb": False, "lan": True, "wifi": True, "ssh": True},
    "PXW-Z200": {"usb": False, "lan": True, "wifi": True, "ssh": True},
    "HXR-NX800": {"usb": False, "lan": True, "wifi": True, "ssh": True},
    "BRC-AM7": {"usb": False, "lan": True, "wifi": False, "ssh": True},
    "ILME-FR7": {"usb": False, "lan": True, "wifi": False, "ssh": True},
    "ZV-E1": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "ZV-E10M2": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "DSC-RX1RM3": {"usb": True, "lan": False, "wifi": True, "ssh": False},
    "DSC-RX0M2": {"usb": True, "lan": False, "wifi": False, "ssh": False},     # firmware 3.00 ou mais
}

TERMOS_FALTA = "accept the license notices first (they show when the console starts and on the web page)"

# O nome comercial, para a consola e a pagina mostrarem "A7 IV" ao lado de "ILCE-7M4".
NOMES = {
    "ILCE-1M2": "Alpha 1 II", "ILCE-1": "Alpha 1", "ILCE-9M3": "A9 III", "ILCE-9M2": "A9 II", "ILCE-7RM6": "A7R VI",
    "ILCE-7RM5": "A7R V", "ILCE-7RM4A": "A7R IVA", "ILCE-7RM4": "A7R IV", "ILCE-7CR": "A7CR", "ILCE-7SM3": "A7S III",
    "ILCE-7M5": "A7 V", "ILCE-7M4": "A7 IV", "ILCE-7CM2": "A7C II", "ILCE-7C": "A7C", "ILCE-6700": "A6700",
    "MPC-2610": "VENICE 2", "ILME-FX6": "FX6", "ILME-FX3A": "FX3A", "ILME-FX3": "FX3", "ILME-FX2": "FX2",
    "ILME-FX30": "FX30", "PXW-Z300": "Z300", "PXW-Z200": "Z200", "HXR-NX800": "NX800", "ILME-FR7": "FR7",
    "ZV-E10M2": "ZV-E10 II", "DSC-RX1RM3": "RX1R III", "DSC-RX0M2": "RX0 II",
}
for _m, _c in MODELOS.items():
    _c["label"] = NOMES.get(_m, _m)

# Os codigos de erro do SDK (CrError.h) que aparecem a quem esta a filmar, em palavras.
ERROS = {
    0x8202: "camera not reachable at that address (on the camera Wi-Fi? Remote Shooting on?)",
    0x8207: "disconnected",
    0x8208: "connection timed out",
    0x8209: "reconnection timed out",
    0x820A: "camera refused the connection (pair it: Network > Remote Shoot Function > Pairing, or check SSH)",
    0x820B: "camera busy (another computer connected to it?)",
    0x8210: "a session is already open on this camera",
    0x8212: "this camera does not use SSH: turn SSH off for it in the bridge",
    0x8213: "SSH user or password missing",
    0x8214: "SSH: could not reach the camera",
    0x8215: "SSH: the camera key changed",
    0x8216: "SSH: wrong user or password (camera: Network > Access Authen. Settings)",
}


def erro_txt(code):
    try:
        n = int(str(code), 16)
    except (TypeError, ValueError):
        return str(code)
    return ERROS.get(n) or "SDK error 0x%04X" % n


def sdk_txt(v):
    """A versao do SDK (GetSDKVersion: major<<24 | minor<<16 | patch<<8) como a Sony a escreve: 2.02.00."""
    try:
        n = int(str(v), 16)
    except (TypeError, ValueError):
        return str(v) if v else None
    return "%d.%02d.%02d" % (n >> 24, (n >> 16) & 0xFF, (n >> 8) & 0xFF)


def iso_txt(v):
    if v is None:
        return None
    iso = v & 0xFFFFFF
    return "AUTO" if iso == 0xFFFFFF else str(iso)


def fnumber_txt(v):
    if v is None or v >= 0xFFFD:
        return None
    return ("%.1f" % (v / 100.0)).rstrip("0").rstrip(".")


def shutter_txt(v):
    if v is None or v == 0xFFFFFFFF:
        return None
    if v == 0:
        return "BULB"
    num, den = v >> 16, v & 0xFFFF
    if not den:
        return None
    if num == 1:
        return "1/%d" % den
    return ('%g"' % (num / float(den)))


class SonyCam:
    """Uma camara Sony: alvo de REC do bridge, com a mesma interface das outras frotas."""

    def __init__(self, cid, cfg, frota):
        self.id, self.cfg, self.frota = cid, cfg, frota
        self.on_unexpected = None          # on_unexpected(a_gravar): comecou/parou sem o bridge
        self.shot = None                   # shot(evento, nota): linha no shoot log
        self.on_fault = None               # on_fault(texto)
        self._st = {}                      # o ultimo estado do ajudante (valores em bruto do SDK)
        self._st_at = 0.0
        self._ligada = False
        self._tentou = 0.0                 # ultima tentativa de ligar
        self._expect = None                # para onde o ultimo comando a mandou (enquanto confirma)
        self._cmd_t = 0.0
        self._cmd_n = 0
        self.take_no = None
        self.error = None
        self.fingerprint = None
        self._rec_since = None

    @property
    def name(self):
        return self.cfg.get("name") or self.id

    @property
    def modelo(self):
        return self.cfg.get("model") or ""

    def log(self, msg):
        self.frota.log("%s: %s" % (self.name, msg))

    # --- estado -------------------------------------------------------------------------
    def connected(self):
        return bool(self.cfg.get("enabled", True)) and self._ligada and time.time() - self._st_at < ESTADO_VELHO

    def ready(self):
        if not self.cfg.get("enabled", True):
            return False, "%s disabled in the bridge" % self.name
        if not self.frota.disponivel():
            return False, "%s: %s" % (self.name, self.frota.motivo)
        if not self.connected():
            return False, "%s not connected%s" % (self.name, (": " + self.error) if self.error else "")
        return True, None

    def _rec(self):
        return self._st.get("rec")

    def running(self):
        return self.connected() and self._rec() == 1

    def alvo(self):
        if self._expect is not None and time.monotonic() - self._cmd_t < CONFIRM:
            return self._expect
        return self.running()

    def _novo_estado(self, d):
        antes = self._st.get("rec")
        self._st = d
        self._st_at = time.time()
        agora = d.get("rec")
        if agora == 1 and antes != 1:
            self._rec_since = time.time()
        elif agora != 1:
            self._rec_since = None
        if antes in (0, 1) and agora in (0, 1) and antes != agora:
            self._mudou(agora == 1)
        if agora == 2 and antes != 2:
            txt = "%s: recording FAILED (camera says Recording Failed)" % self.name
            self.log(txt)
            for fn, args in ((self.shot, ("fault", txt)), (self.on_fault, (txt,))):
                if fn:
                    fn(*args)

    def _mudou(self, a_gravar):
        if self._expect is not None and a_gravar == self._expect:
            return                                  # comandado pelo bridge: o _confirmar trata
        self._expect = a_gravar
        self.log("%s by itself (not commanded by the bridge)" % ("started" if a_gravar else "stopped"))
        if self.on_unexpected:
            try:
                self.on_unexpected(a_gravar)
            except Exception as e:                  # pylint: disable=broad-except
                self.log("callback: %r" % e)

    # --- REC ----------------------------------------------------------------------------
    def set_running(self, on):
        if on:
            ok, why = self.ready()
            if not ok:
                return False, why
        elif not self.connected():
            return False, "%s not connected" % self.name
        self._expect = bool(on)
        self._cmd_n += 1
        n = self._cmd_n
        self._cmd_t = t0 = time.monotonic()
        r = self.frota.pedir("rec %s %d" % (self.id, 1 if on else 0), self.id, "rec_on" if on else "rec_off")
        if not r or not r.get("ok"):
            self._expect = None
            return False, "%s refused %s (%s)" % (self.name, "REC" if on else "STOP",
                                                  erro_txt(r.get("code")) if r else "no answer from sony_ctl")
        threading.Thread(target=self._confirmar, args=(bool(on), t0, n), daemon=True).start()
        return True, None

    def _confirmar(self, on, t0, n):
        fim = time.monotonic() + CONFIRM
        while time.monotonic() < fim:
            if n != self._cmd_n:
                return
            if self.running() == on:
                self.log("confirmed %s in %.2f s" % ("REC" if on else "STOP", time.monotonic() - t0))
                return
            self.frota.mandar("read %s" % self.id)
            time.sleep(0.3)
        if n != self._cmd_n:
            return
        txt = "%s did NOT %s (the camera accepted the command but did not change state in %.0f s)" % (
            self.name, "start recording" if on else "stop recording", CONFIRM)
        self.log(txt)
        for fn, args in ((self.shot, ("fault", txt)), (self.on_fault, (txt,))):
            if fn:
                try:
                    fn(*args)
                except Exception as e:              # pylint: disable=broad-except
                    self.log("callback: %r" % e)

    def set_timecode(self, tc):
        """Escreve o Time Code Preset (HH:MM:SS:FF) na camara parada. (ok, erro)."""
        if not self.connected():
            return False, "%s not connected" % self.name
        if self.running():
            return False, "%s is recording" % self.name
        if not re.match(r"^\d{2}:\d{2}:\d{2}:\d{2}$", str(tc)):
            return False, "timecode: HH:MM:SS:FF"
        r = self.frota.pedir("tc %s %s" % (self.id, tc), self.id, "tc")
        if not r or not r.get("ok"):
            return False, erro_txt(r.get("code")) if r else "no answer from sony_ctl"
        return True, None

    def on_mark(self, kind):
        pass                                        # sem claquete/metadados de take pelo SDK: so o shoot log

    def status(self):
        st = self._st if self.connected() else {}
        val = lambda k, mau: None if st.get(k) in (None, mau) else st.get(k)
        bat = val("battery_pct", 0xFF)
        if bat is not None and bat > 100:
            bat = None
        restante = val("slot1_s", 0xFFFFFFFF)
        if not restante:
            restante = val("slot2_s", 0xFFFFFFFF) or restante
        caps = MODELOS.get(self.modelo, {})
        ok, why = self.ready()
        return {
            "id": self.id, "type": "sony", "name": self.name, "model": self.modelo, "via": self.cfg.get("via"),
            "host": self.cfg.get("host"), "serial": self.cfg.get("serial"), "ssh": bool(self.cfg.get("ssh")),
            "enabled": bool(self.cfg.get("enabled", True)), "connected": self.connected(), "ready": ok, "why": why,
            "running": self.running(), "rec_since": self._rec_since if self.running() else None,
            "pending": None if self._expect is None or time.monotonic() - self._cmd_t >= CONFIRM
            or self._expect == self.running() else ("REC" if self._expect else "STOP"),
            "battery": bat, "battery_min": val("battery_min", 0xFFFFFFFF),
            "battery_v": (round(st["battery_mv"] / 1000.0, 2) if val("battery_mv", 0xFFFFFFFF) is not None else None),
            "remaining_s": restante, "iso": iso_txt(st.get("iso")), "fnumber": fnumber_txt(st.get("fnumber")),
            "shutter": shutter_txt(st.get("shutter")), "rec_failed": st.get("rec") == 2,
            "take_no": self.take_no, "error": None if self.connected() else self.error,
            "caps": caps, "fingerprint": self.fingerprint,
        }


class Fleet:
    """As camaras Sony do bridge e o processo do ajudante (sony_ctl)."""

    def __init__(self, log, cfg_path, comando=None):
        self.log, self.cfg_path = log, cfg_path
        self.comando = comando                     # [programa, args...]; None: o sony_ctl instalado
        self.on_unexpected = None                  # on_unexpected(cam, a_gravar)
        self.shot = None                           # shot(cam, evento, nota)
        self.on_fault = None                       # on_fault(cam, texto)
        self.termos_ok = None                      # termos_ok() -> bool: os avisos de licenca aceites (licencas.py)
        self._lock = threading.Lock()
        self._out_lock = threading.Lock()
        self._proc = None
        self._esperas = {}                         # (id, cmd) -> [Event, resultado]
        self._arrancou = False
        self.motivo = "starting"
        self.sdk = None
        self.modelos_sdk = []
        self.encontradas = []
        lista = None
        try:
            lista = gravar.ler_json(cfg_path)
        except (OSError, ValueError):
            pass
        if not isinstance(lista, dict) or not isinstance(lista.get("sonys"), dict):
            lista = {"sonys": {}}
        self._cfg = lista
        self.cams = {}
        for cid, c in sorted(lista["sonys"].items()):
            self._criar(cid, c)

    # --- frota (a mesma interface das outras) ---------------------------------------------
    def _criar(self, cid, c):
        c.setdefault("enabled", True)
        cam = SonyCam(cid, c, self)
        cam.on_unexpected = lambda a, cam=cam: self.on_unexpected and self.on_unexpected(cam, a)
        cam.shot = lambda ev, nota, cam=cam: self.shot and self.shot(cam, ev, nota)
        cam.on_fault = lambda txt, cam=cam: self.on_fault and self.on_fault(cam, txt)
        self.cams[cid] = cam
        return cam

    def save(self):
        try:
            tmp = self.cfg_path + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:     # pode ter passwords SSH
                json.dump(self._cfg, f, indent=1)
            gravar.trocar(tmp, self.cfg_path)
        except OSError as e:
            self.log("Sony: could not save %s: %s" % (self.cfg_path, e))

    def ids(self):
        return list(self.cams)

    def get(self, cid):
        return self.cams.get(cid)

    def in_targets(self, targets):
        return [c for cid, c in self.cams.items() if cid in (targets or [])]

    def any_running(self):
        return any(c.running() for c in self.cams.values())

    def status(self):
        return {cid: c.status() for cid, c in self.cams.items()}

    def disponivel(self):
        return self._proc is not None and self._proc.poll() is None and self.sdk is not None

    def resumo(self):
        """Para o /api/system/sony_sdk e a consola: o ajudante esta a correr, porque nao, e o que o SDK conhece."""
        return {"available": self.disponivel(), "why": None if self.disponivel() else self.motivo, "sdk": self.sdk,
                "terms_accepted": self.termos_ok() if self.termos_ok else True,
                "models": sorted(set(self.modelos_sdk) or set(MODELOS)), "caps": MODELOS, "found": self.encontradas}

    def validar(self, model, via, host=None, serial=None, ssh=False, user=None, password=None):
        model = str(model or "").strip().upper()
        if not re.match(r"^[A-Z0-9]+(-[A-Z0-9]+)+$", model):
            return None, "model: e.g. ILCE-7M4"
        if self.modelos_sdk and model not in self.modelos_sdk:
            return None, "%s is not in the installed Sony SDK (update the SDK)" % model
        caps = MODELOS.get(model)
        c = {"model": model, "via": via, "ssh": bool(ssh)}
        if via == "ip":
            host = str(host or "").strip()
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) or any(int(x) > 255 for x in host.split(".")):
                return None, "host: an IPv4 address (the camera's, on the camera Wi-Fi)"
            if caps and not (caps["wifi"] or caps["lan"]):
                return None, "%s has no network remote control (USB only)" % model
            c["host"] = host
        elif via == "usb":
            serial = str(serial or "").strip()
            if not re.match(r"^[A-Za-z0-9]{1,12}$", serial):
                return None, "serial: the camera's USB serial number (12 characters)"
            if caps and not caps["usb"]:
                return None, "%s has no USB remote control" % model
            c["serial"] = serial
        else:
            return None, "via: ip | usb"
        if caps and caps["ssh"] and via == "ip":
            c["ssh"] = True                          # estes modelos so ligam pela rede com SSH
        if c["ssh"]:
            if not user or not password or re.search(r"\s", str(user) + str(password)):
                return None, "SSH needs the camera's user and password (Network > Access Authen. Settings), no spaces"
            c["user"], c["password"] = str(user), str(password)
        return c, None

    def add(self, name, model, via, host=None, serial=None, ssh=False, user=None, password=None):
        if self.termos_ok is not None and not self.termos_ok():
            return False, TERMOS_FALTA
        c, err = self.validar(model, via, host, serial, ssh, user, password)
        if err:
            return False, err
        with self._lock:
            for outro in list(self.cams.values()) + [SonyCam(k, v, self) for k, v in (self._cfg.get("removed") or {}).items()]:
                if (c.get("host") and outro.cfg.get("host") == c.get("host")) or \
                        (c.get("serial") and outro.cfg.get("serial") == c.get("serial")):
                    return False, "%s already has that address" % outro.name
            n = 1
            while "sony%d" % n in self.cams or "sony%d" % n in (self._cfg.get("removed") or {}):
                n += 1
            cid = "sony%d" % n
            c["name"] = (str(name).strip()[:20] if name and str(name).strip() else "%s %d" % (NOMES.get(c["model"], c["model"]), n))
            c["enabled"] = True
            self._cfg["sonys"][cid] = c
            cam = self._criar(cid, c)
            self.save()
        self.log("Sony: %s added (%s %s)" % (cam.name, c["model"], c.get("host") or "USB " + c.get("serial", "")))
        self._registar(cam)
        return True, cid

    def configure(self, cid, **k):
        cam = self.cams.get(cid)
        if not cam:
            return False, "no Sony %s" % cid
        if cam.running():
            return False, "%s is recording" % cam.name
        novo = dict(cam.cfg)
        novo.update({a: b for a, b in k.items() if b is not None})
        if "name" in k and k["name"] is not None:
            novo["name"] = str(k["name"]).strip()[:20] or cam.name
        c, err = self.validar(novo.get("model"), novo.get("via"), novo.get("host"), novo.get("serial"), novo.get("ssh"),
                              novo.get("user"), novo.get("password"))
        if err:
            return False, err
        c["name"], c["enabled"] = novo.get("name") or cam.name, bool(novo.get("enabled", True))
        with self._lock:
            cam.cfg.clear()
            cam.cfg.update(c)
            self.save()
        self.mandar("del %s" % cid)
        cam._ligada = False
        self._registar(cam)
        return True, None

    def remove(self, cid):
        with self._lock:
            cam = self.cams.get(cid)
            if not cam:
                return False, "no Sony %s" % cid
            if cam.running():
                return False, "%s is recording: stop it first" % cam.name
            del self.cams[cid]
            self._cfg.setdefault("removed", {})[cid] = self._cfg["sonys"].pop(cid, None) or dict(cam.cfg)
            self.save()
        self.mandar("del %s" % cid)
        self.log("Sony: %s removed (kept in the saved list)" % cam.name)
        return True, None

    def removed_list(self):
        with self._lock:
            return {k: {kk: vv for kk, vv in v.items() if kk != "password"}
                    for k, v in (self._cfg.get("removed") or {}).items() if isinstance(v, dict)}

    def forget(self, cid):
        with self._lock:
            c = (self._cfg.get("removed") or {}).pop(cid, None)
            if not isinstance(c, dict):
                return False, "no saved Sony %s" % cid
            self.save()
        self.log("Sony: %s forgotten" % (c.get("name") or cid))
        return True, None

    def restore(self, cid):
        with self._lock:
            c = (self._cfg.get("removed") or {}).get(cid)
            if not isinstance(c, dict):
                return False, "no saved Sony %s" % cid
            novo, n = cid, 1
            while novo in self.cams:
                while "sony%d" % n in self.cams or "sony%d" % n in self._cfg["removed"]:
                    n += 1
                novo = "sony%d" % n
            self._cfg["removed"].pop(cid)
            self._cfg["sonys"][novo] = c
            cam = self._criar(novo, c)
            self.save()
        self.log("Sony: %s restored" % cam.name)
        self._registar(cam)
        return True, novo

    def procurar(self):
        """Procura camaras (USB e a rede local) pelo SDK: lista em resumo()["found"] (leva uns 3 s)."""
        self.mandar("enum")

    # --- o ajudante ------------------------------------------------------------------------
    def start(self):
        if self._arrancou:
            return
        self._arrancou = True
        threading.Thread(target=self._ciclo, daemon=True, name="sony-ctl").start()
        threading.Thread(target=self._religar, daemon=True, name="sony-religar").start()

    def _cmd(self):
        if self.comando:
            return self.comando
        if not os.path.isfile(SONY_CTL) or not os.access(SONY_CTL, os.X_OK):
            return None
        return [SONY_CTL]

    def _ciclo(self):
        avisado = False
        while True:
            cmd = self._cmd()
            if not cmd:
                self.motivo = "Sony helper missing in %s (update the bridge from GitHub)" % os.path.dirname(SONY_CTL)
                if not avisado and self.cams:
                    self.log("Sony: %s" % self.motivo)
                avisado = True
                time.sleep(30)
                continue
            try:
                # O sony_ctl corre na pasta dele: e la que estao a libCr_Core.so e a CrAdapter/ que o SDK carrega.
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                              text=True, bufsize=1, cwd=None if self.comando else os.path.dirname(cmd[0]))
            except OSError as e:
                self.motivo = "sony_ctl did not start: %s" % e
                time.sleep(30)
                continue
            self.motivo = "sony_ctl starting"
            for linha in self._proc.stdout:
                try:
                    ev = json.loads(linha)
                except ValueError:
                    continue
                try:
                    self._evento(ev)
                except Exception as e:              # pylint: disable=broad-except
                    self.log("Sony: event %r: %r" % (ev.get("ev"), e))
            self._proc.wait()
            self.sdk = None
            self.motivo = "sony_ctl stopped (code %s): restarting" % self._proc.returncode
            self.log("Sony: %s" % self.motivo)
            for cam in list(self.cams.values()):
                cam._ligada = False
            time.sleep(5)

    def mandar(self, linha):
        p = self._proc
        if p is None or p.poll() is not None:
            return False
        try:
            with self._out_lock:
                p.stdin.write(linha + "\n")
                p.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def pedir(self, linha, cid, cmd, timeout=RESPOSTA):
        """Manda e espera pelo evento "result" desse (id, comando). O resultado, ou None."""
        ev = threading.Event()
        caixa = [ev, None]
        with self._lock:
            self._esperas[(cid, cmd)] = caixa
        if not self.mandar(linha):
            with self._lock:
                self._esperas.pop((cid, cmd), None)
            return None
        ev.wait(timeout)
        with self._lock:
            self._esperas.pop((cid, cmd), None)
        return caixa[1]

    def _registar(self, cam):
        """Diz ao ajudante que camara e esta (add) e liga-a, se o ajudante estiver a correr."""
        if not self.disponivel() or not cam.cfg.get("enabled", True) or cam.id not in self.cams:
            return
        c = cam.cfg
        if c.get("via") == "usb":
            linha = "add %s %s usb %s" % (cam.id, c["model"], c["serial"])
        else:
            linha = "add %s %s ip %s" % (cam.id, c["model"], c["host"])
            if c.get("ssh"):
                linha += " ssh %s %s" % (c["user"], c["password"])
        r = self.pedir(linha, cam.id, "add")
        if r and r.get("ok"):
            cam._tentou = time.time()
            self.mandar("connect %s" % cam.id)
        else:
            cam.error = (r.get("error") or erro_txt(r.get("code"))) if r else "sony_ctl did not answer"

    def _religar(self):
        """As camaras que ainda nao ligaram (desligadas, fora do Wi-Fi): tenta-se de RELIGAR em RELIGAR s. Depois de
        ligada uma vez, o proprio SDK volta a ligar uma camara que cai (CrReconnecting_ON)."""
        while True:
            time.sleep(2)
            if not self.disponivel():
                continue
            for cam in list(self.cams.values()):
                if cam.cfg.get("enabled", True) and not cam._ligada and getattr(cam, "_falhou", False) \
                        and time.time() - cam._tentou > RELIGAR:
                    cam._falhou = False
                    cam._tentou = time.time()
                    self.mandar("disconnect %s" % cam.id)       # solta a tentativa anterior antes de outra
                    self.mandar("connect %s" % cam.id)

    def _evento(self, ev):
        tipo, cid = ev.get("ev"), ev.get("id")
        cam = self.cams.get(cid) if cid else None
        if tipo == "ready":
            self.sdk = sdk_txt(ev.get("sdk"))
            self.motivo = None
            self.log("Sony: sony_ctl ready (SDK %s)" % self.sdk)
            self.mandar("models")
            for c in list(self.cams.values()):
                threading.Thread(target=self._registar, args=(c,), daemon=True).start()
        elif tipo == "models":
            self.modelos_sdk = list(ev.get("models") or [])
        elif tipo == "found":
            self.encontradas = list(ev.get("cameras") or [])
        elif tipo == "result":
            with self._lock:
                caixa = self._esperas.get((cid, ev.get("cmd")))
            if caixa:
                caixa[1] = ev
                caixa[0].set()
            if ev.get("cmd") == "connect" and cam and not ev.get("ok"):
                cam.error = erro_txt(ev.get("code"))
                cam._falhou = True
        elif cam is None:
            return
        elif tipo == "connected":
            if not cam._ligada:
                self.log("Sony: %s connected" % cam.name)
            cam._ligada, cam.error = True, None
            cam._st_at = time.time()
        elif tipo in ("disconnected", "reconnecting"):
            if cam._ligada:
                self.log("Sony: %s %s" % (cam.name, "lost, reconnecting" if tipo == "reconnecting" else "disconnected"))
            cam._ligada = False
            if tipo == "disconnected":
                cam.error = erro_txt(ev.get("code"))
                cam._falhou = True
        elif tipo == "error":
            novo = erro_txt(ev.get("code"))
            if novo != cam.error:                   # a mesma falha a cada nova tentativa so se regista uma vez
                self.log("Sony: %s: %s" % (cam.name, novo))
            cam.error = novo
            cam._falhou = True
        elif tipo == "fingerprint":
            cam.fingerprint = ev.get("fp")
        elif tipo == "state":
            cam._ligada = True
            cam._novo_estado({k: v for k, v in ev.items() if k not in ("ev", "id")})

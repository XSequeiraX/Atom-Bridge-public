"""GoPro (HERO8...) como alvos de REC do bridge, por um gateway ESP32-S3 (gopro_gw).

O gateway fala BLE (Open GoPro) com ate 3 GoPro e expoe HTTP na rede das cameras:
  GET /api/status, POST /api/rec?id=gp1&on=1, POST /api/scan, GET /api/scan,
  POST /api/add?mac=..&type=..&name=.., POST /api/remove?id=gp1.
O bridge nao fala BLE com elas: so com o gateway, que pode ficar longe do Atom.

Cada GoPro (gp1, gp2, gp3) tem a mesma interface que um iPhone (bmcam.BMCam), por isso
o REC do bridge trata-as sem mudancas: id, name, ready(), running(), set_running(on),
take_no, on_mark(), status(). O endereco do gateway fica em gopros.json; se mudar (DHCP),
o aviso UDP do gateway (porta 5170) corrige-o.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from vigiado import vigiado
import gravar
import fw_ota

POLL_SECONDS = 2.0
BEACON_PORT = 5170
RESET_PORT = 5171           # "GPGW-RESET" por UDP reinicia o gateway quando o HTTP dele nao responde
TIMEOUT = 3.0
REC_TIMEOUT = 8.0           # espera pelo resultado do comando no estado do gateway (o STOP ate ~6 s)


def _http(host, metodo, caminho, args=None, timeout=TIMEOUT):
    """(status http, dict | None). Os argumentos vao na query (o gateway le-os de la)."""
    url = "http://%s%s" % (host, caminho)
    if args:
        url += "?" + urllib.parse.urlencode(args)
    # O token do Atom vai so nos pedidos que mudam alguma coisa (POST: REC, add/remove, resets...); os GET do firmware nao o
    # pedem, e assim nunca vai a um aparelho que ainda nao se sabe ser um gateway (parece_c3, o estado de um endereco novo).
    cab = fw_ota.token_cab() if metodo == "POST" else {}
    req = urllib.request.Request(url, method=metodo, data=b"" if metodo == "POST" else None, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except ValueError:
            return e.code, None
    except (OSError, ValueError):
        return 0, None


class GoPro:
    """Uma GoPro do gateway, com a interface de um alvo de REC."""

    def __init__(self, fleet, cid, dados, lid=None):
        # `fleet` e o gateway desta GoPro. `id` e o id no bridge (unico entre gateways); `lid` e o que o
        # proprio gateway lhe deu (gp1..gp3) e o que se lhe manda nos pedidos.
        self.fleet, self.id, self.lid = fleet, cid, lid or cid
        self.d = dict(dados)
        self.take_no = None
        self.pending = None            # "starting" / "stopping" enquanto espera a confirmacao da camara
        self._expect = None            # o que o bridge mandou por ultimo (para detectar mudancas sozinhas)
        self._expect_at = 0.0
        self._rec_t0 = None
        # REC/STOP da mesma GoPro passam um de cada vez: o gateway so aceita um comando por camara ("another command
        # is still running"), e um STOP logo a seguir a um START perdia-se. Cada pedido tem um numero; o que foi
        # ultrapassado por outro mais recente nao se envia (ou larga a espera).
        self._cmd_lock = threading.Lock()
        self._pedido_n = 0

    @property
    def name(self):
        return self.d.get("name") or self.id

    def connected(self):
        return bool(self.d.get("connected")) and self.fleet.gateway_ok

    def ready(self):
        if not self.fleet.gateway_ok:
            return False, "%s: the GoPro gateway is not answering" % self.name
        if not self.d.get("connected"):
            return False, "%s is not connected to the gateway (%s)" % (self.name, self.d.get("error") or "no Bluetooth link")
        return True, ""

    def running(self):
        # Caiu a gravar: conta como a gravar (a GoPro continua a rodar) ate voltar e dizer o estado, ou 30 min.
        incerto = getattr(self, "rec_incerto", None)
        return bool(self.d.get("recording")) or bool(incerto and time.time() - incerto < 1800)

    def alvo(self):
        """O estado para onde a camara vai: com um REC/STOP em curso e o que se pediu, nao o que ainda se ve. O REC
        de grupo decide por aqui, senao um STOP logo a seguir a um START (que a GoPro so confirma ao fim de segundos)
        achava a camara parada e nao lhe mandava nada: ela acabava a gravar."""
        return {"starting": True, "stopping": False}.get(self.pending, self.running())

    def set_running(self, on):
        """Grava / para. O gateway poe o comando na fila e responde logo; o resultado vem no estado
        dele (cmd.seq / cmd.state), que se consulta ate acabar. Se nao confirmar a tempo, confere-se
        o estado real da camara antes de dar o comando por falhado. (ok, erro)."""
        on = bool(on)
        self._expect, self._expect_at = on, time.time()
        self._pedido_n += 1
        meu = self._pedido_n
        self.pending = "starting" if on else "stopping"
        try:
            # Se ha um comando em curso nesta camara, larga-lhe a espera (fica "superseded") e espera pela vez.
            with self._cmd_lock:
                if meu != self._pedido_n:
                    return False, "superseded by a newer command"
                return self._set_running(on, meu)
        finally:
            if meu == self._pedido_n:
                self.pending = None

    def _set_running(self, on, meu):
        t_ini = time.time()
        while True:
            s, r = _http(self.fleet.host, "POST", "/api/rec", {"id": self.lid, "on": 1 if on else 0}, timeout=4)
            if s == 200 and isinstance(r, dict) and r.get("ok"):
                break
            erro = (r or {}).get("error") if isinstance(r, dict) else None
            # O comando anterior (largado por este) ainda corre no gateway: espera que acabe em vez de falhar.
            if s == 409 and erro and "still running" in erro and time.time() - t_ini < REC_TIMEOUT:
                if meu != self._pedido_n:
                    return False, "superseded by a newer command"
                time.sleep(0.2)
                continue
            return False, erro or ("the gateway did not answer" if s == 0 else "HTTP %s" % s)
        seq, t0, msg = r.get("seq"), time.time(), None
        while time.time() - t0 < REC_TIMEOUT:
            time.sleep(0.15)
            self.fleet.poll_once(max_idade=0.1)
            cmd = self.d.get("cmd") or {}
            if cmd.get("seq") == seq and cmd.get("state") == "done":
                msg = cmd.get("msg")
                if cmd.get("ok"):
                    self._rec_t0 = time.time() if on else None
                    self.d["recording"] = on
                    self.fleet.log("GoPro %s confirmed %s (%.1f s)" % (self.name, "REC" if on else "STOP", time.time() - t0))
                    return True, None
                break
            if meu != self._pedido_n:
                return False, "superseded by a newer command"
        # sem confirmacao do gateway: o estado real da camara decide
        time.sleep(1.0)
        self.fleet.poll_once()
        if self.fleet.gateway_ok and self.connected() and self.running() == on:
            self.fleet.log("GoPro %s did %s (confirmed afterwards)" % (self.name, "REC" if on else "STOP"))
            return True, None
        return False, msg or "the GoPro did not confirm in time"

    def check_clock(self):
        """Hora da camara (pedida por BLE ao gateway, resolucao de 1 s) contra a do bridge.
        (ok, dict | erro). Serve para ver se o QR do Labs acertou o relogio."""
        ok, why = self.ready()
        if not ok:
            return False, why
        fino = self._check_clock_fino()
        if fino is not None:                 # None: o gateway nao tem a medida fina (firmware antigo)
            return fino
        seq0 = (self.d.get("clock") or {}).get("seq", 0)
        s, r = _http(self.fleet.host, "POST", "/api/clock", {"id": self.lid}, timeout=4)
        if s != 200:
            return False, ((r or {}).get("error") if isinstance(r, dict) else None) or "the gateway did not answer"
        t0, ck = time.time(), None
        while time.time() - t0 < 6:
            time.sleep(0.25)
            self.fleet.poll_once()
            c = self.d.get("clock") or {}
            if c.get("seq", 0) > seq0:
                ck = c
                break
        if not ck:
            return False, "the camera did not answer the clock request"
        # a hora da camara, mais o tempo que passou desde que chegou ao gateway, e meio segundo
        # (ela trunca os segundos), contra a hora local do bridge neste instante
        base = time.mktime((ck["y"], ck["mo"], ck["d"], ck["h"], ck["mi"], ck["s"], 0, 0, -1))
        cam_now = base + ck.get("age_ms", 0) / 1000.0 + 0.5
        off = cam_now - time.time()
        txt = "%s: camera clock is %+.1f s vs the bridge (+-1 s)" % (self.name, off)
        self.fleet.log(txt)
        return True, {"offset": round(off, 1), "err": 1.0, "camera": "%04d-%02d-%02d %02d:%02d:%02d" % (
            ck["y"], ck["mo"], ck["d"], ck["h"], ck["mi"], ck["s"]), "text": txt}

    def _check_clock_fino(self):
        """A fase do relogio da camara a ~0,1 s: o gateway pede a hora em cadeia ate o segundo mudar, e a hora
        do bridge nesse instante vem do relogio do gateway (tempo de resposta incluido). None se nao houver."""
        s, r = _http(self.fleet.host, "POST", "/api/clock_fine", {"id": self.lid}, timeout=4)
        if s == 404 or (s == 200 and not isinstance(r, dict)):
            return None
        if s != 200:
            return False, ((r or {}).get("error") if isinstance(r, dict) else None) or "the gateway did not answer"
        seq, fim = r.get("seq"), time.time() + 9
        while time.time() < fim:
            time.sleep(0.2)
            s2, st = _http(self.fleet.host, "GET", "/api/status")
            t_recv = time.time()
            if s2 != 200 or not isinstance(st, dict):
                continue
            cam = next((c for c in st.get("cams") or [] if c.get("id") == self.lid), None)
            f = (cam or {}).get("fine") or {}
            if f.get("seq") != seq or f.get("state") != "done":
                continue
            if not f.get("ok"):
                if f.get("reason"):
                    return False, "clock measurement %s: try again" % f["reason"]
                return False, "the camera clock did not tick within 4 s (is the camera awake?)"
            flip_gw = (f["lo"] + f["hi"]) / 2.0                      # ms do gateway em que o segundo mudou
            flip_t = t_recv - (f["gw_now"] - flip_gw) / 1000.0       # a mesma coisa na hora do bridge
            base = time.mktime((f["y"], f["mo"], f["d"], f["h"], f["mi"], f["s"], 0, 0, -1))   # a camara marca isto no instante
            off = base - flip_t                                      # + : camara adiantada; - : atrasada
            err = (f["hi"] - f["lo"]) / 2000.0 + 0.04
            lead = self.fleet.lead_ms
            sug = int(round(lead - off * 1000))
            quando = "behind" if off < 0 else "ahead of"
            txt = "%s: camera clock is %.2f s %s the bridge (+-%.2f s)" % (self.name, abs(off), quando, err)
            self.fleet.log(txt)
            return True, {"offset": round(off, 2), "err": round(err, 2), "fine": True, "lead_ms": lead, "suggest_lead_ms": sug,
                          "camera": "%04d-%02d-%02d %02d:%02d:%02d.000 at the flip" % (f["y"], f["mo"], f["d"], f["h"], f["mi"], f["s"]),
                          "text": txt}
        return False, "the gateway did not finish the clock measurement"

    def on_mark(self, especie):
        return None                     # sem claquete: as marcas ficam so no shoot log

    def status(self):
        d = dict(self.d)
        ok, why = self.ready()
        rec_s = d.get("rec_s")
        lig = self.connected()
        return {
            "id": self.id, "name": self.name, "mac": d.get("mac"), "gw": self.fleet.idx,
            "gateway": self.fleet.host, "gateway_ok": self.fleet.gateway_ok,
            "connected": self.connected(), "ready": ok, "why": why,
            "running": self.running(), "pending": self.pending,
            "rec_since": (time.time() - rec_s) if self.running() and rec_s is not None else self._rec_t0,
            # desligada: sem bateria nem cartao (eram os da ultima leitura)
            "battery": d.get("battery") if lig else None, "remaining_s": d.get("remaining_s") if lig else None,
            "rssi": d.get("rssi") if lig else None,                  # sinal Bluetooth da GoPro no S3 (dBm)
            "take_no": self.take_no, "error": None if lig else (d.get("error") or why),
        }


def parece_c3(host):
    """True se o aparelho nesse endereco responde como um gateway C3 (e nao como um S3): evita juntar um C3 como S3."""
    s, r = _http(host, "GET", "/api/status", timeout=2)
    return s == 200 and isinstance(r, dict) and ("bm" in r or str(r.get("fw") or "").startswith("fiz_bm"))


class Gateway:
    """Um gateway ESP32-S3 e as GoPro que ele tem. O bridge pode ter varios (cada um com o seu alcance BLE)."""

    def __init__(self, hub, idx, host="", enabled=True, name=""):
        self.hub, self.idx = hub, idx
        self.host, self.enabled, self.name = host, enabled, name
        self.cams = {}
        self.gateway_ok = False
        self.gateway_info = {}
        self.modo = None                # "gopro" | "f6" (o selector do firmware; None = ainda nao se sabe)
        self.solo = False               # o S3 esta no F6 autonomo (f6solo): faz de comando sem o Atom
        self._lock = threading.Lock()
        self._vivo = False
        # O estado do gateway pede-se de dois lados ao mesmo tempo (o ciclo de 1 s e a confirmacao de um REC): uma
        # resposta pedida antes de outra que ja se aplicou e velha, e aplicada depois punha a GoPro outra vez como
        # estava ("parou sozinha" e logo "comecou sozinha" no shoot log, a seguir a um REC/STOP do bridge).
        self._ordem_lock = threading.Lock()
        self._aplicar_lock = threading.Lock()
        self._pedido = 0
        self._aplicado = 0
        # Varias GoPro a confirmar um REC ALL ao mesmo tempo pediam o estado cada uma, 6x/s por camara: um S3 que
        # aguenta 1 pedido de cada vez via a fila crescer. Agora o pedido em curso serve a todas.
        self._poll_lock = threading.Lock()
        self._poll_t = 0.0

    # ---- o que as GoPro usam ----
    @property
    def lead_ms(self):
        return self.hub.lead_ms

    def log(self, txt):
        self.hub.log(txt if self.idx == 1 else "[S3 %d] %s" % (self.idx, txt))

    def gid(self, local):
        """O id no bridge: o do primeiro gateway fica como sempre (gp1..gp3); os outros contam por dezenas
        (gp11..gp13, gp21..gp23), para nao haver dois gp1."""
        if self.idx == 1:
            return local
        m = re.fullmatch(r"gp(\d+)", str(local))
        return "gp%d" % ((self.idx - 1) * 10 + int(m.group(1))) if m else "%s_%d" % (local, self.idx)

    def label(self):
        return self.name or ("S3" if self.idx == 1 else "S3 %d" % self.idx)

    # ---- consulta ----
    def start(self):
        if self._vivo:
            return
        self._vivo = True
        threading.Thread(target=vigiado(self._poll_loop, "GoPro gateway %d" % self.idx, self.log), daemon=True).start()

    def stop(self):
        self._vivo = False

    def _poll_loop(self):
        while self._vivo:
            self.poll_once(max_idade=POLL_SECONDS / 2)
            time.sleep(POLL_SECONDS)

    def _limpa(self):
        self.gateway_ok = False
        self.gateway_info = {}
        with self._lock:
            self.cams.clear()

    def poll_once(self, max_idade=0.0):
        """Pede e aplica o estado do gateway. Com max_idade, quem esperou pela vez reaproveita a consulta que
        entretanto se fez (comecada ha menos de max_idade s) em vez de repetir."""
        if not self.enabled or not self.host:
            self.gateway_ok = False
            return
        host = self.host
        quer = time.monotonic()
        with self._poll_lock:
            if max_idade and self._poll_t >= quer - max_idade:
                return                          # a consulta que se fez enquanto esperava (ou logo antes) serve
            self._poll_t = time.monotonic()
            with self._ordem_lock:
                self._pedido += 1
                pedido = self._pedido
            s, r = _http(host, "GET", "/api/status")
        with self._aplicar_lock:
            if pedido < self._aplicado:
                return                          # chegou depois de uma resposta mais nova: nao conta
            self._aplicado = pedido
            self._aplicar(host, s, r)

    def _aplicar(self, host, s, r):
        if not self.enabled or host != self.host:     # desligado ou mudado enquanto se esperava: nao conta
            self.gateway_ok = False
            return
        if s != 200 or not isinstance(r, dict) or not r.get("ok"):
            if self.gateway_ok:
                self.log("GoPro gateway %s not answering" % self.host)
                # O gateway inteiro caiu: para as GoPro e o mesmo que perderem a ligacao (antes nao ficava no shoot
                # log nem dava o aviso, e ao voltar uma GoPro que parou entretanto aparecia como "parou sozinha").
                with self._lock:
                    eram = [(c, c.running()) for c in self.cams.values() if c.d.get("connected")]
                self.gateway_ok = False
                for cam, gravava in eram:
                    self.log("GoPro %s lost" % cam.name)
                    self._ligacao_mudou(cam, gravava)
            self.gateway_ok = False
            return
        ok_antes = self.gateway_ok
        if not self.gateway_ok:
            self.log("GoPro gateway %s is up (%s)" % (self.host, r.get("fw")))
        self.gateway_ok = True
        self.gateway_info = {k: r.get(k) for k in ("fw", "uptime_s", "ip", "wifi_rssi", "token", "ap", "led_type")}
        modo = "f6" if r.get("mode") in ("f6", "f6solo") else "gopro"
        self.solo = r.get("mode") == "f6solo"
        if modo != self.modo:
            if self.modo is not None or modo == "f6":
                self.log("S3 gateway %s is in %s mode" % (self.host, "F6 (FRC-8)" if modo == "f6" else "GoPro"))
            self.modo = modo
        self.gateway_info["mode"] = modo
        if modo == "f6":
            # O S3 faz de FRC-8 do Zoom F6: as GoPro ficam guardadas nele, mas aqui nao ha nenhuma (sem "removed").
            self.gateway_info["f6"] = r.get("f6") or {}
            with self._lock:
                self.cams.clear()
            return
        vistas = set()
        for c in r.get("cams") or []:
            lid = c.get("id")
            if not lid:
                continue
            cid = self.gid(lid)
            vistas.add(cid)
            with self._lock:
                cam = self.cams.get(cid)
                if cam is None:
                    cam = self.cams[cid] = GoPro(self, cid, c, lid)
                    self.log("GoPro %s found in the gateway" % (c.get("name") or cid))
                antes = cam.running()
                ligada_antes = bool(cam.d.get("connected")) and ok_antes    # com o gateway de antes desta resposta
                cam.d = dict(c)
            if cam.connected() != ligada_antes:
                self.log("GoPro %s %s" % (cam.name, "connected" if cam.connected() else "lost"))
                self._ligacao_mudou(cam, antes)
                antes = cam.running() if cam.connected() else antes
            agora = cam.running()
            if agora != antes and cam.connected():
                # comandado pelo bridge nos ultimos segundos: nao e "sozinha"
                if not (cam._expect is not None and cam._expect == agora and time.time() - cam._expect_at < 10):
                    cam._expect, cam._expect_at = agora, time.time()
                    self.log("GoPro %s %s by itself (not commanded by the bridge)" % (cam.name, "started" if agora else "stopped"))
                    if self.hub.on_unexpected:
                        try:
                            self.hub.on_unexpected(cam, agora)
                        except Exception as e:
                            self.log("GoPro callback: %r" % e)
        with self._lock:
            for cid in list(self.cams):
                if cid not in vistas:
                    self.log("GoPro %s removed" % self.cams[cid].name)
                    del self.cams[cid]
        if not self.enabled:                 # desligado a meio do processamento: nada fica para tras
            self._limpa()

    def _ligacao_mudou(self, cam, gravava):
        """Uma GoPro perdeu ou recuperou a ligacao. Caiu a gravar: fica "incerta" e regista-se no shoot log; ao voltar
        diz-se se continua a gravar (mesmo take) ou se parou enquanto nao se via."""
        def shot(evento, nota):
            if self.hub.shot:
                try:
                    self.hub.shot(cam, evento, nota)
                except Exception as e:
                    self.log("GoPro callback: %r" % e)
        if not cam.connected():
            if gravava and not getattr(cam, "rec_incerto", None):
                cam.rec_incerto = time.time()
                shot("fault", "link lost while recording")
                if self.hub.on_lost:
                    try:
                        self.hub.on_lost(cam)
                    except Exception as e:
                        self.log("GoPro callback: %r" % e)
        elif getattr(cam, "rec_incerto", None):
            cam.rec_incerto = None
            a_gravar = bool(cam.d.get("recording"))
            shot("fault" if a_gravar else "stop", "link back: still recording" if a_gravar else "stopped while the link was down")
            cam._expect, cam._expect_at = a_gravar, time.time()     # nao e "sozinha"

    def any_running(self):
        with self._lock:
            return any(c.running() for c in self.cams.values())

    def info(self):
        with self._lock:
            n = len(self.cams)
        return {"idx": self.idx, "name": self.label(), "host": self.host, "ok": self.gateway_ok,
                "lead_ms": self.hub.lead_ms, "enabled": self.enabled, "cams": n, **self.gateway_info}

    # ---- configuracao ----
    def set_enabled(self, on):
        """Liga ou desliga o gateway no bridge (o gateway em si nao muda)."""
        on = bool(on)
        if not on and self.any_running():
            return False, "a GoPro is recording: stop it first"
        self.enabled = on
        self.hub.save()
        if not on:
            self._limpa()
        self.log("GoPro gateway switched %s in the bridge" % ("ON" if on else "OFF"))
        if on:
            threading.Thread(target=self.poll_once, daemon=True).start()
        return True, None

    def set_mode(self, modo, psk=None):
        """O selector do S3: "gopro" (as GoPro por Bluetooth), "f6" (o FRC-8 do Zoom F6 pela USB, com o Atom) ou "f6solo"
        (F6 autonomo: o S3 abre o seu Wi-Fi com a palavra-passe `psk` e serve a pagina do comando; sem o Atom). O S3 reinicia."""
        if modo not in ("gopro", "f6", "f6solo"):
            return False, "mode: gopro, f6 or f6solo"
        if modo == "f6solo" and not (isinstance(psk, str) and 8 <= len(psk) <= 63):
            return False, "the Wi-Fi password of the F6 remote has 8 to 63 characters"
        if not self.enabled or not self.host:
            return False, "the gateway is off in the bridge or has no address"
        if self.any_running():
            return False, "a GoPro is recording: stop it first"
        args = {"m": modo}
        if modo == "f6solo":
            args["psk"] = psk              # so vai ao S3 (na query, na rede das camaras); nunca para o registo
        s, r = _http(self.host, "POST", "/api/mode", args, timeout=5)
        if s == 200 and isinstance(r, dict) and r.get("ok"):
            nome = {"f6": "F6", "f6solo": "F6 standalone"}.get(modo, "GoPro")
            if r.get("note") == "already":               # ja estava nesse modo (e, no autonomo, com essa palavra-passe): nao reiniciou
                self.log("S3 gateway %s: already in %s mode" % (self.host, nome))
            elif modo == "f6solo" and "password" in str(r.get("note") or ""):
                self.log("S3 gateway %s: %s mode, new Wi-Fi password, restarting" % (self.host, nome))
            else:
                self.log("S3 gateway %s: %s mode, restarting" % (self.host, nome))
            return True, None
        if s == 404:
            return False, "the S3 firmware has no mode selector: update it first (Firmware)"
        erro = ((r or {}).get("error") if isinstance(r, dict) else None) or "the gateway did not answer"
        if modo == "f6solo" and s == 400 and "f6solo" not in str(erro) and "psk" not in str(erro):
            erro = "the S3 firmware does not have the standalone F6 mode yet: update it first (Firmware)"
        return False, erro

    def set_led(self, tipo):
        """O tipo de LED da placa S3: "rgb" (StampS3), "simple" (XIAO ESP32S3) ou "off". Aplica-se logo, sem reiniciar."""
        if tipo not in ("rgb", "simple", "off"):
            return False, "led type: rgb, simple or off"
        if not self.enabled or not self.host:
            return False, "the gateway is off in the bridge or has no address"
        s, r = _http(self.host, "POST", "/api/led", {"t": tipo}, timeout=5)
        if s == 200 and isinstance(r, dict) and r.get("ok"):
            self.log("S3 gateway %s: LED type %s" % (self.host, tipo))
            return True, None
        if s in (404, 405):
            return False, "the S3 firmware has no LED type setting: update it first (Firmware)"
        return False, ((r or {}).get("error") if isinstance(r, dict) else None) or "the gateway did not answer"

    def f6_host(self):
        """O endereco deste S3 se esta no modo F6 e a responder, senao None."""
        # No autonomo (f6solo) o S3 e o comando do F6 de um iPhone: o Atom nao o usa como ligacao ao F6 (se o
        # alcancasse, ia ligar-se-lhe e competir com quem esta a comandar o F6 por la).
        return self.host if self.enabled and self.gateway_ok and self.modo == "f6" and not self.solo else None

    def set_host(self, host):
        host = str(host or "").strip()
        if not host or len(host) > 60 or any(c in host for c in " /\\?#"):
            return False, "host: an IP address or name, e.g. 192.168.4.96"
        for g in self.hub.gws.values():
            if g is not self and g.host and g.host.split(":")[0] == host.split(":")[0]:
                return False, "that address already belongs to %s" % g.label()
        if self.any_running() and host != self.host:
            return False, "a GoPro is recording: change the address after the take"
        self.host = host
        self.hub.save()
        self.log("GoPro gateway is now %s" % host)
        return True, None

    def reset(self, what="reboot"):
        """Repoe o gateway: reboot (reinicia-o), bonds (esquece os emparelhamentos Bluetooth) ou factory
        (esquece tudo: GoPro e emparelhamentos). Se o HTTP dele nao responder, o reboot vai por UDP, so em unicast ao
        endereco guardado (o pacote leva o token: nunca em broadcast). O UDP nao tem confirmacao: se o S3 mudou de IP
        (DHCP) o pacote perde-se sem erro e so o reset por USB o repoe."""
        if what not in ("reboot", "bonds", "factory"):
            return False, "what: reboot | bonds | factory"
        if self.any_running():
            return False, "a GoPro is recording: stop it first"
        if not self.enabled:
            return False, "the gateway is switched off in the bridge (Devices > GoPro > Gateway)"
        if not self.host:
            return False, "no gateway address yet"
        s, r = _http(self.host, "POST", "/api/reset", {"what": what}, timeout=5)
        if s == 200 and isinstance(r, dict) and r.get("ok"):
            self.log("GoPro gateway reset: %s" % what)
            return True, None
        if what != "reboot":
            return False, ((r or {}).get("error") if isinstance(r, dict) else None) or "the gateway did not answer (try the reboot: it also works without HTTP)"
        # So ao endereco do gateway (unicast): o pacote leva o token, e num broadcast (x.y.z.255) qualquer aparelho da rede
        # o lia. Sem endereco IP conhecido nao ha para onde o mandar em segredo.
        enviados = 0
        alvo = self.host.split(":")[0]
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                tok = fw_ota.token_cab().get("X-GW-Token", "")
                sk.sendto(("GPGW-RESET " + tok).strip().encode(), (alvo, RESET_PORT))
                enviados += 1
            finally:
                sk.close()
        except OSError:
            pass
        self.log("GoPro gateway: HTTP did not answer, UDP reset sent to %s only (no acknowledgement: if the S3 does not restart it may have a new IP, use the USB reset)" % alvo)
        return (True, "HTTP did not answer: sent the UDP reset to %s (no acknowledgement; if the S3 does not restart, use the USB reset)" % alvo) if enviados else (False, "could not send the UDP reset")

    # ---- procura e gestao (passam pelo gateway) ----
    def scan(self, secs=8):
        """Procura GoPro a anunciar-se. Bloqueia ate acabar. (ok, lista | erro)."""
        if not self.enabled:
            return False, "the gateway is switched off in the bridge (Devices > GoPro > Gateway)"
        if not self.host:
            return False, "no gateway address yet"
        s, r = _http(self.host, "POST", "/api/scan", {"secs": int(secs)})
        if s != 200:
            return False, "the gateway did not start the scan (HTTP %s)" % s
        fim = time.time() + int(secs) + 6
        while time.time() < fim:
            time.sleep(1.0)
            s, r = _http(self.host, "GET", "/api/scan")
            if s == 200 and isinstance(r, dict) and r.get("done"):
                return True, r.get("found") or []
        return False, "the scan did not finish"

    def add(self, mac, tipo, name):
        if not self.enabled:
            return False, "the gateway is switched off in the bridge (Devices > GoPro > Gateway)"
        if not self.host:
            return False, "no gateway address yet"
        s, r = _http(self.host, "POST", "/api/add", {"mac": mac, "type": int(tipo), "name": name or ""})
        if s == 200 and r and r.get("ok"):
            self.poll_once()
            return True, self.gid(r.get("id"))
        return False, (r or {}).get("error") if isinstance(r, dict) else "the gateway did not answer"

    def remove(self, cam):
        if not self.enabled:
            return False, "the gateway is switched off in the bridge (Devices > GoPro > Gateway)"
        if cam.running():
            return False, "%s is recording: stop it first" % cam.name
        s, r = _http(self.host, "POST", "/api/remove", {"id": cam.lid})
        if s == 200 and r and r.get("ok"):
            self.poll_once()
            return True, None
        return False, (r or {}).get("error") if isinstance(r, dict) else "the gateway did not answer"


class GoproFleet:
    """As GoPro de todos os gateways S3, com a interface das outras frotas (ids, get, in_targets, status...).

    O primeiro gateway (idx 1) e o de sempre e os seus metodos e atributos (host, enabled, gateway_ok...) continuam a
    valer sem argumentos; os outros acrescentam-se com add_gateway(). Os metodos de gestao aceitam `gw` (o numero)."""

    MAX_GATEWAYS = 4

    def __init__(self, log, cfg_path):
        self.log, self.cfg_path = log, cfg_path
        self.on_unexpected = None            # on_unexpected(cam, a_gravar)
        self.shot = None
        self.on_lost = None                  # on_lost(cam): caiu a gravar
        self.lead_ms = 0                     # avanco do QR da hora: compensa o tempo que a camara demora a assentar a hora
        self.gws = {}
        self.memoria = {}                    # host -> numero que o gateway teve: reinserido, volta com os mesmos ids
        self.found = {}                      # ip -> ultima vez que um S3 desconhecido se anunciou
        self._vivo = False
        try:
            cfg = gravar.ler_json(cfg_path) or {}
            self.lead_ms = int(cfg.get("lead_ms") or 0)
            if isinstance(cfg.get("gateways"), list):          # formato actual (pode estar vazio: tirou-se tudo)
                for e in cfg["gateways"]:
                    try:
                        n = int(e.get("idx"))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if 1 <= n <= self.MAX_GATEWAYS and n not in self.gws:
                        self.gws[n] = Gateway(self, n, str(e.get("host") or ""), e.get("enabled", True) is not False,
                                              str(e.get("name") or ""))
                self.memoria = {str(k): int(v) for k, v in (cfg.get("memoria") or {}).items()
                                if str(v).isdigit() and 1 <= int(v) <= self.MAX_GATEWAYS}
            else:                                                # formato antigo: o primeiro no topo, os outros em "extra"
                self.gws[1] = Gateway(self, 1, str(cfg.get("host") or ""), cfg.get("enabled", True) is not False)
                for e in cfg.get("extra") or []:
                    try:
                        n = int(e.get("idx"))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if 2 <= n <= self.MAX_GATEWAYS and n not in self.gws:
                        self.gws[n] = Gateway(self, n, str(e.get("host") or ""), e.get("enabled", True) is not False,
                                              str(e.get("name") or ""))
        except (OSError, ValueError):
            pass
        if not self.gws and not os.path.exists(cfg_path):        # instalacao nova: o primeiro gateway, por preencher
            self.gws[1] = Gateway(self, 1)

    # ---- o primeiro gateway (o de menor numero), como antes; sem nenhum, valores neutros ----
    def _primeiro(self):
        return self.gws[min(self.gws)] if self.gws else None

    host = property(lambda self: self._primeiro().host if self._primeiro() else "")
    enabled = property(lambda self: self._primeiro().enabled if self._primeiro() else False)
    gateway_ok = property(lambda self: self._primeiro().gateway_ok if self._primeiro() else False)
    gateway_info = property(lambda self: self._primeiro().gateway_info if self._primeiro() else {})

    def save(self):
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"lead_ms": self.lead_ms,
                           "gateways": [{"idx": n, "host": g.host, "enabled": g.enabled, "name": g.name}
                                        for n, g in sorted(self.gws.items())],
                           "memoria": self.memoria}, f)
            gravar.trocar(tmp, self.cfg_path)
        except OSError as e:
            self.log("GoPro: could not save %s: %s" % (self.cfg_path, e))

    def gw(self, n=None):
        """O gateway numero n; sem numero, o primeiro que existir."""
        if not n:
            return self._primeiro()
        try:
            return self.gws.get(int(n))
        except (TypeError, ValueError):
            return None

    def set_lead(self, ms):
        """Quanto o QR da hora vai adiantado (ms). Positivo = a camara fica menos atrasada. Vale para todos."""
        try:
            ms = int(round(float(ms)))
        except (TypeError, ValueError):
            return False, "lead: a number of milliseconds, e.g. 1400"
        if not -10000 <= ms <= 10000:
            return False, "lead: between -10000 and 10000 ms"
        self.lead_ms = ms
        self.save()
        self.log("GoPro time QR lead is now %d ms" % ms)
        return True, None

    def set_enabled(self, on, gw=None):
        g = self.gw(gw)
        return g.set_enabled(on) if g else (False, "no such gateway")

    def set_led(self, tipo, gw=None):
        g = self.gw(gw)
        return g.set_led(tipo) if g else (False, "no such gateway")

    def set_mode(self, modo, gw=None, psk=None):
        g = self.gw(gw)
        return g.set_mode(modo, psk) if g else (False, "no such gateway")

    def f6_gateway(self):
        """(host, nome) do primeiro S3 no modo F6, ou None."""
        for _, g in sorted(self.gws.items()):
            if g.f6_host():
                return g.host, g.label()
        return None

    def set_host(self, host, gw=None):
        g = self.gw(gw)
        return g.set_host(host) if g else (False, "no such gateway")

    def reset(self, what="reboot", gw=None):
        g = self.gw(gw)
        return g.reset(what) if g else (False, "no such gateway")

    def scan(self, secs=8, gw=None):
        g = self.gw(gw)
        return g.scan(secs) if g else (False, "no such gateway")

    def add(self, mac, tipo, name, gw=None):
        g = self.gw(gw)
        return g.add(mac, tipo, name) if g else (False, "no such gateway")

    def remove(self, cid):
        cam = self.get(cid)
        if not cam:
            return False, "unknown GoPro"
        return cam.fleet.remove(cam)

    # ---- varios gateways ----
    def add_gateway(self, host, name=""):
        host = str(host or "").strip()
        if not host or len(host) > 60 or any(c in host for c in " /\\?#"):
            return False, "host: an IP address or name, e.g. 192.168.4.97"
        if any(g.host and g.host.split(":")[0] == host.split(":")[0] for g in self.gws.values()):
            return False, "that gateway is already here"
        if parece_c3(host):
            return False, "that is a C3 gateway (BMPCC), not an S3 (GoPro)"
        # reinserido, volta ao numero que tinha (se estiver livre): os ids das GoPro (e os nomes de referencia
        # que o operador lhes deu) nao mudam.
        n = self.memoria.get(host)
        if n not in range(1, self.MAX_GATEWAYS + 1) or n in self.gws:
            n = next((k for k in range(1, self.MAX_GATEWAYS + 1) if k not in self.gws), None)
        if n is None:
            return False, "at most %d S3 gateways" % self.MAX_GATEWAYS
        nome = re.sub(r"[^\w .\-]", "", str(name or "")).strip()[:20]
        g = self.gws[n] = Gateway(self, n, host, True, nome)
        self.found.pop(host.split(":")[0], None)
        self.memoria[host] = n
        self.save()
        if self._vivo:
            g.start()
        self.log("GoPro gateway %d added (%s)" % (n, host))
        return True, n

    def remove_gateway(self, n):
        """Tira um gateway (qualquer, tambem o primeiro) do bridge. Recusado a gravar. Ao tirar-se, as suas GoPro
        saem das listas; o gateway em si (e os emparelhamentos que guarda) ficam como estao, por isso voltar a
        junta-lo traz as mesmas GoPro."""
        try:
            n = int(n)
        except (TypeError, ValueError):
            return False, "no such gateway"
        g = self.gws.get(n)
        if not g:
            return False, "no such gateway"
        if g.any_running():
            return False, "a GoPro is recording: stop it first"
        g.stop()
        if g.host:
            self.memoria[g.host] = n
        del self.gws[n]
        self.save()
        self.log("GoPro gateway %d removed" % n)
        return True, None

    def gateways(self):
        return [g.info() for _, g in sorted(self.gws.items())]

    def disponiveis(self):
        """S3 que se anunciaram agora e ainda nao estao na lista."""
        agora = time.time()
        usados = {g.host.split(":")[0] for g in self.gws.values() if g.host}
        return sorted(ip for ip, t in list(self.found.items()) if agora - t < 30 and ip not in usados)

    # ---- consulta ----
    def start(self):
        if self._vivo:
            return
        self._vivo = True
        for g in list(self.gws.values()):
            g.start()
        threading.Thread(target=vigiado(self._beacon_loop, "GoPro beacon", self.log), daemon=True).start()

    def poll_once(self):
        for g in list(self.gws.values()):
            g.poll_once()

    def _beacon_loop(self):
        """Um gateway diz onde esta ("GPGW ip=..."). Com um so, se o endereco mudou (DHCP) corrige-se; com varios
        nao ha como saber qual mudou: o desconhecido fica na lista dos disponiveis para o operador o juntar."""
        sock = None
        while self._vivo:
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("", BEACON_PORT))
                    sock.settimeout(2.0)
                data, _ = sock.recvfrom(200)
            except socket.timeout:
                continue
            except OSError:
                sock = None
                time.sleep(5)
                continue
            txt = data.decode("utf-8", "replace")
            if not txt.startswith("GPGW ip="):
                continue
            partes = txt[8:].split()
            ip = partes[0] if partes else ""
            if not ip or any(g.host.split(":")[0] == ip for g in self.gws.values() if g.host):
                continue
            g1 = self._primeiro()
            if len(self.gws) == 1 and g1.enabled and (not g1.gateway_ok or not g1.host):
                self.log("GoPro gateway announced itself at %s (was %s)" % (ip, g1.host or "none"))
                g1.host = ip
                self.memoria[ip] = g1.idx
                self.save()
            else:
                self.found[ip] = time.time()

    # ---- o que o bridge usa ----
    @property
    def cams(self):
        out = {}
        for g in list(self.gws.values()):
            with g._lock:
                out.update(g.cams)
        return out

    def ids(self):
        return sorted(self.cams)

    def get(self, cid):
        return self.cams.get(cid)

    def in_targets(self, targets):
        return [c for cid, c in sorted(self.cams.items()) if cid in (targets or [])]

    def any_running(self):
        return any(g.any_running() for g in list(self.gws.values()))

    def status(self):
        return {cid: c.status() for cid, c in sorted(self.cams.items())}

    def gateway(self):
        """O primeiro gateway (o campo de sempre); todos em gateways(). Sem nenhum, um vazio."""
        g = self._primeiro()
        return g.info() if g else {"idx": 0, "name": "", "host": "", "ok": False, "enabled": False, "cams": 0,
                                   "lead_ms": self.lead_ms}

"""O iPhone com a app Blackmagic Camera (3.4 ou mais nova) como alvo de REC do bridge.

A app existe tambem para Android (3.4 ou mais nova) e funciona com o bridge (testado). Atencao ao bitrate: um formato pesado pode
ser mais do que o telemovel escreve, e a gravacao falha a meio de uma take; testar varios minutos no formato real antes de filmar.
O que segue foi medido com um iPhone.

A app tem um servidor web (HTTPS, porta 4444, certificado proprio) com a REST
API das camaras Blackmagic (ver os YAML que a app serve em
/control/documentation.html). Testado a 2026-10-08 com um iPhone 16 Pro Max,
app 3.5:
  - REC: POST /transports/0/record; STOP: POST /transports/0/stop (204);
  - estado: GET /transports/0/record -> {"recording", "clipName"};
  - o clipName pedido no REC e IGNORADO: o nome do ficheiro e da app;
  - claquete: PUT /slates/nextClip e /slates/lastClip (cena, take 1-99,
    goodTake so no lastClip);
  - avisos: WebSocket /control/api/v1/event/websocket (subscribe + eventos
    propertyValueChanged).

Aqui: uma thread com o WebSocket e outra que pergunta o que ele nao avisa.
Pedir pouco ao iPhone (2026-10-12): com um pedido HTTPS novo por pergunta (um
handshake TLS cada) e rajadas de 13 pedidos de 5 em 5 s, a imagem da app
travava e saltava enquanto parada. Agora:
  - duas ligacoes HTTPS mantidas abertas (perguntas e comandos), sem handshake
    por pedido; um comando nunca espera por uma pergunta;
  - o WebSocket subscreve tudo o que a app anuncia em /event/list (REC, espaco,
    formato, claquete, ISO, obturador, WB, iris, zoom, bateria...): isso deixa
    de se perguntar;
  - o resto pergunta-se UM pedido de cada vez, devagar; o timecode e uma
    amostra de 3 em 3 s, que serve tambem de sinal de vida;
  - sem WebSocket pergunta-se o REC de 1 em 1 s, como antes.

Varios iPhones: Fleet (um BMCam por iPhone, ids iphone1, iphone2...; nome, IP,
porta e cena de cada um em iphones.json ao lado do bridge).
"""
import base64
import http.client
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
from vigiado import vigiado
import gravar
from curto import erro

API = "/control/api/v1"
WS_PATH = API + "/event/websocket"
CONFIRM = 3.0               # s para a app confirmar REC/STOP
POLL_TICK = 1.0             # o ciclo das perguntas: no maximo UM pedido por volta (nada de rajadas)
POLL_TC = 3.0               # timecode, 1 amostra (e o sinal de vida)
POLL_LENTO = 30.0           # cada coisa que o WebSocket nao avisa
VIVO = 8.0                  # s sem resposta nenhuma para dar o iPhone por perdido
WS_SILENCIO = 10.0          # WebSocket calado e nada respondeu ha tanto: pergunta-se o REC
# Propriedade -> chave do estado. O WebSocket subscreve as que a app anunciar em /event/list.
PROPS = {"/transports/0/record": "record", "/media/workingset": "workingset",
         "/system/videoFormat": "videoFormat", "/system/codecFormat": "codecFormat",
         "/slates/nextClip": "nextClip", "/transports/0/timecode/source": "tcsource",
         "/video/iso": "iso", "/video/shutter": "shutter", "/video/whiteBalance": "wb",
         "/lens/iris": "iris", "/lens/zoom": "zoom", "/camera/power": "power"}
# Sem /event/list: as que se sabe que a app 3.5 aceita (testado a 2026-10-08).
WS_PROPS = ["/transports/0/record", "/media/workingset", "/system/videoFormat", "/system/codecFormat",
            "/slates/nextClip", "/slates/lastClip", "/transports/0/timecode/source"]
# O que se pergunta quando o WebSocket nao o avisa (um de cada vez, de POLL_LENTO em POLL_LENTO).
LENTOS = [("product", "/system/product")] + [(k, p) for p, k in PROPS.items() if k != "record"]
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE          # a app usa um certificado proprio


def _tc_secs(txt):
    m = re.match(r"(\d\d):(\d\d):(\d\d)[:;.](\d\d)", txt or "")
    if not m:
        return None, None
    h, mi, s, f = map(int, m.groups())
    return h * 3600 + mi * 60 + s, f


def shutter_normal(v):
    """O angulo do obturador em graus, com no maximo 1 casa (272.2727... -> 272.2; 180.0 -> 180), ou None se nao der para
    confiar. A app manda o valor tal como o calcula (com todas as casas) e o ecra enchia-se de decimais. Aceita numero ou
    texto (com virgula ou ponto); fora de 0 a 360 graus nao se mostra."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        t = v.strip().replace(",", ".")
        if not re.fullmatch(r"\d{1,4}(\.\d{1,20})?", t):
            return None
        v = float(t)
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")) or not 0 < v <= 360:
            return None
        n = int(float(v) * 10 + 1e-9) / 10.0       # trunca (272.27 -> 272.2): e o que a app mostra, nao 272.3
        return int(n) if n == int(n) else n
    return None


class BMCam:
    """Interface de alvo de REC do bridge (como o f6): start, ready, running,
    set_running, status; mais set_scene, on_mark, configure."""

    def __init__(self, cid, cfg, salvar, log, ltc_secs=None, fps=lambda: 25):
        """cfg: {name, host, port, enabled, scene} deste iPhone (partilhado com a
        Fleet, que o guarda com salvar())."""
        self.id, self.cfg, self._salvar = cid, cfg, salvar
        self._log, self.ltc_secs, self.fps = log, ltc_secs, fps
        self.on_unexpected = None          # on_unexpected(a_gravar): comecou/parou sem o bridge
        self.shot = None                   # shot(evento, nota): linha no shoot log
        self.on_fault = None               # on_fault(texto): aviso a vista (o REC aceite que a app nao fez)
        self.on_clip = None                # on_clip(nome): nome do ficheiro que a app acabou de gravar (depois do STOP)
        self._lock = threading.Lock()
        self._st = {}
        self._shutter_avisado = None       # o mesmo para o angulo do obturador
        self._ok_at = 0.0                  # ultimo pedido que correu bem
        self._ws_ok = False
        self._rem_val, self._rem_t = None, 0.0     # ultimo "tempo que falta" da app e quando mudou
        self._expect = None
        self._cmd_n = 0                 # sobe a cada REC/STOP: o _confirmar de um comando ultrapassado desiste sem queixa
        self.take_no = None                # a take do shoot log em que o iPhone esta a gravar
        self.error = None
        self._gen = 0                      # muda quando a configuracao muda (as threads recomecam)
        self._vivo = True                  # False quando o iPhone e retirado da lista
        self._tc_ultimo, self._tc_desde = None, time.time()
        self._ws_props = set()             # o que o WebSocket esta a avisar (isso nao se pergunta)
        self._ws_extras_enviados = False   # nesta sessao pediram-se os extras (ISO, WB, lente...)
        self._ws_sem_extras = False        # a app recusou-os: ficam a ser perguntados devagar
        # Ligacoes HTTPS mantidas abertas: "poll" (perguntas de fundo) e "cmd" (REC, claquete...).
        self._http = {"poll": None, "cmd": None}
        self._http_lock = {"poll": threading.Lock(), "cmd": threading.Lock()}

    @property
    def name(self):
        return self.cfg.get("name") or self.id

    def log(self, msg):
        self._log(msg.replace("Mobile", self.name, 1) if msg.startswith("Mobile") else "%s: %s" % (self.name, msg))

    # --- configuracao ------------------------------------------------------------
    def configure(self, host=None, port=None, enabled=None, name=None):
        # Primeiro valida-se tudo, depois aplica-se: um campo invalido nao deixa os anteriores mudados a meio.
        if host is not None:
            host = str(host).strip()
            if not re.match(r"^[A-Za-z0-9.\-:]{1,100}$", host):
                return False, "invalid host"
        if port is not None:
            try:
                port = int(port)
            except (TypeError, ValueError):
                return False, "invalid port"
            if not 1 <= port <= 65535:
                return False, "invalid port"
        if name is not None:
            name = " ".join(str(name).split())[:24]
            if not name:
                return False, "empty name"
        # Mudar o endereco ou desligar a ligacao a meio de uma take apaga o estado: o bridge julgaria que parou.
        liga_muda = (host is not None and host != self.cfg.get("host")) or \
                    (port is not None and port != self.cfg.get("port")) or enabled is False
        if liga_muda and self.running():
            return False, "%s is recording: change it after the take" % (self.cfg.get("name") or "the mobile")
        if host is not None:
            self.cfg["host"] = host
        if port is not None:
            self.cfg["port"] = port
        if enabled is not None:
            self.cfg["enabled"] = bool(enabled)
        if name is not None:
            self.cfg["name"] = name
        self._guardar()
        self._gen += 1
        self._st, self._ok_at, self._ws_ok = {}, 0.0, False
        for canal in self._http:
            with self._http_lock[canal]:
                self._fechar_http(canal)
        self.log("Mobile: %s:%s %s" % (self.cfg["host"], self.cfg["port"],
                                       "enabled" if self.cfg["enabled"] else "disabled"))
        return True, None

    def _guardar(self):
        self._salvar()

    # --- HTTP ----------------------------------------------------------------------
    def _fechar_http(self, canal):
        c, self._http[canal] = self._http[canal], None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _req(self, method, path, body=None, timeout=3, canal="poll"):
        """(status, json) ou (None, None). Na ligacao HTTPS do canal, aberta uma vez e reaproveitada:
        sem um handshake TLS por pedido. Se a app fechou a ligacao parada, abre-se outra e repete-se
        uma vez (os pedidos daqui sao todos idempotentes: REC e STOP sao rotas separadas)."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if canal == "cmd":
            self._cmd_uso = time.time()
        with self._http_lock[canal]:
            for tentativa in (1, 2):
                c = self._http[canal]
                reaproveitada = c is not None
                if c is None:
                    c = self._http[canal] = http.client.HTTPSConnection(
                        self.cfg["host"], int(self.cfg["port"]), timeout=timeout, context=_CTX)
                try:
                    c.timeout = timeout
                    if c.sock is not None:
                        c.sock.settimeout(timeout)
                    c.request(method, API + path, body=data, headers=headers)
                    r = c.getresponse()
                    corpo = r.read()
                except (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                        ConnectionResetError, BrokenPipeError) as e:
                    self._fechar_http(canal)
                    if reaproveitada and tentativa == 1:
                        continue
                    self.error = erro(e)
                    return None, None
                except Exception as e:
                    self._fechar_http(canal)
                    self.error = erro(e)
                    return None, None
                self._ok_at = time.time()        # respondeu, mesmo que com erro
                try:
                    return r.status, (json.loads(corpo) if corpo else None)
                except ValueError:
                    return r.status, None
        return None, None

    # --- interface de alvo -----------------------------------------------------------
    def start(self):
        threading.Thread(target=self._ws_loop, daemon=True, name="iphone-ws").start()
        threading.Thread(target=vigiado(self._poll_loop, "Mobile " + self.id, self.log), daemon=True, name="iphone-poll").start()

    def connected(self):
        return self.cfg.get("enabled") and time.time() - self._ok_at < VIVO

    def ready(self):
        if not self.cfg.get("enabled"):
            return False, "%s disabled in the bridge" % self.name
        if not self.connected():
            return False, ("%s not reachable at %s:%s (Blackmagic Camera open, web server on, "
                           "same network?)" % (self.name, self.cfg["host"], self.cfg["port"]))
        return True, None

    def running(self):
        return bool((self._st.get("record") or {}).get("recording"))

    def alvo(self):
        """Para onde o iPhone vai (ver gopro.GoPro.alvo): enquanto espera a confirmacao do ultimo comando, o que se pediu."""
        if self._expect is not None and time.monotonic() - getattr(self, "_cmd_t", 0.0) < CONFIRM:
            return self._expect
        return self.running()

    def set_running(self, on):
        if on:
            ok, why = self.ready()
            if not ok:
                return False, why
        elif not self.connected():
            return False, "%s not reachable" % self.name
        self._expect = bool(on)
        self._cmd_n += 1
        n = self._cmd_n
        t0 = time.monotonic()
        self._cmd_t = t0
        s, _ = self._req("POST", "/transports/0/record" if on else "/transports/0/stop", {} if on else None,
                         canal="cmd")
        t_cmd = time.monotonic() - t0
        if s != 204:
            return False, "%s refused %s (HTTP %s, %.2f s)" % (self.name, "REC" if on else "STOP", s, t_cmd)
        threading.Thread(target=self._confirmar, args=(bool(on), t0, t_cmd, n), daemon=True).start()
        return True, None

    def _confirmar(self, on, t0=None, t_cmd=0.0, n=None):
        t0 = t0 or time.monotonic()
        fim = time.monotonic() + CONFIRM
        while time.monotonic() < fim:
            if n is not None and n != self._cmd_n:
                return                  # um comando mais recente (REC->STOP rapido) ultrapassou este: nada a confirmar
            if not self._ws_ok:
                self._ler_record(canal="cmd")
            if self.running() == on:
                # Os tempos dizem onde se perde: o pedido (rede/HTTPS) ou a app a mudar de estado.
                self.log("%s confirmed %s in %.2f s (command answered in %.2f s)"
                         % (self.name, "REC" if on else "STOP", time.monotonic() - t0, t_cmd))
                self._registar_tc("REC" if on else "STOP")
                if not on:
                    self._fechar_take()
                    self._clip_fechado()
                return
            time.sleep(0.2)
        if n is not None and n != self._cmd_n:
            return
        self.log("Mobile did NOT confirm %s within %.0f s" % ("REC" if on else "STOP", CONFIRM))
        # A app aceitou o pedido mas nao mudou de estado: antes ficava so no registo de eventos, e o shoot log com um
        # start que nao houve. Agora fica no shoot log e da aviso a vista.
        txt = "%s did NOT %s (the app accepted the command but did not change state in %.0f s)" % (
            self.name, "start recording" if on else "stop recording", CONFIRM)
        for fn, args in ((self.shot, ("fault", txt)), (self.on_fault, (txt,))):
            if fn:
                try:
                    fn(*args)
                except Exception as e:                  # pylint: disable=broad-except
                    self.log("Mobile: %r" % e)

    def check_tc(self, amostras=10):
        """Mede ja o desvio do TC do iPhone para o LTC (a pedido, a seguir a um
        sync, sem gravar). (ok, dict | erro)."""
        if not self.connected():
            return False, "%s not reachable" % self.name
        self._ler_tc(amostras)
        st = self._st
        tc, off, err = st.get("tc"), st.get("tc_offset"), st.get("tc_err")
        fonte = (st.get("tcsource") or {}).get("timecodeInputSource") or "?"
        if tc is None:
            return False, "the mobile did not answer the timecode request"
        if off is None:
            return False, "no LTC in the bridge to compare with (GET LTC first)"
        txt = "%s: TC %s (%s) is %+.2f s vs LTC (+-%.2f, %d samples)" % (self.name, tc, fonte, off, err or 0, amostras)
        self.log(txt)
        return True, {"offset": off, "err": err, "tc": tc, "source": fonte, "text": txt}

    def _registar_tc(self, quando):
        """No shoot log: o TC do iPhone e o desvio para o LTC neste instante. E o
        que permite acertar o clip no pos quando o iPhone esta em TimeOfDay."""
        if not self.shot:
            return
        self._ler_tc(3)
        st = self._st
        off, err, tc = st.get("tc_offset"), st.get("tc_err"), st.get("tc")
        fonte = (st.get("tcsource") or {}).get("timecodeInputSource") or "?"
        if tc is None:
            return
        nota = "%s: mobile TC %s (%s)" % (quando, tc, fonte)
        if off is not None:
            nota += " is %+.2f s vs LTC (+-%.2f)" % (off, err or 0)
        else:
            nota += ", no LTC in the bridge to compare"
        try:
            self.shot("tc", nota)
        except Exception as e:
            self.log("Mobile: shoot log: %r" % e)

    # --- claquete ---------------------------------------------------------------------
    def set_scene(self, scene):
        scene = str(scene or "").strip()
        if not scene or not re.search(r"[A-Za-z1-9]", scene):
            return False, "the scene needs a letter or a non-zero digit"
        s, r = self._req("PUT", "/slates/nextClip", {"clip": {"scene": scene}}, canal="cmd")
        if s not in (200, 204):
            return False, "%s refused the scene (HTTP %s%s)" % (self.name, s, ": %s" % r if r else "")
        self.cfg["scene"] = scene
        self._guardar()
        s, r = self._req("GET", "/slates/nextClip", timeout=2, canal="cmd")
        if s == 200 and r is not None:
            self._set("nextClip", r)
        self.log("Mobile: scene set to %s" % scene)
        return True, None

    def set_next_slate(self, cena, take, reel=None):
        """Claquete do clip seguinte: a cena do bridge (ou, sem ela, a deste iPhone), o reel e o take. Levanta erro se falhar."""
        clip = {}
        cena = cena or self.cfg.get("scene")
        if cena:
            clip["scene"] = cena
        if reel:
            clip["reel"] = int(reel)                    # a app aceita 1-999 (a claquete ja validou)
        if take:
            clip["take"] = (int(take) - 1) % 99 + 1     # a app aceita 1-99
        if not clip:
            return
        s, r = self._req("PUT", "/slates/nextClip", {"clip": clip}, canal="cmd")
        if s not in (200, 204):
            raise ConnectionError("HTTP %s%s" % (s, ": %s" % r if r else ""))

    def _fechar_take(self):
        """Depois do STOP: cena e take do shoot log no clip que acabou."""
        clip = {}
        if self.cfg.get("scene"):
            clip["scene"] = self.cfg["scene"]
        if self.take_no:
            clip["take"] = (int(self.take_no) - 1) % 99 + 1     # a app aceita 1-99
        if not clip:
            return
        s, r = self._req("PUT", "/slates/lastClip", {"clip": clip}, canal="cmd")
        if s in (200, 204):
            self.log("Mobile: last clip slate %s" % ", ".join("%s %s" % kv for kv in clip.items()))
        else:
            self.log("Mobile: could not write the slate of the last clip (HTTP %s %s)" % (s, r or ""))

    def on_mark(self, kind):
        """G / N do bridge: goodTake no ultimo clip do iPhone."""
        if kind not in ("good", "no-good") or not self.connected():
            return

        def fazer():
            s, _ = self._req("PUT", "/slates/lastClip", {"clip": {"goodTake": kind == "good"}}, canal="cmd")
            self.log("Mobile: last clip %s %s" % ("GOOD" if kind == "good" else "NO-GOOD",
                                                   "" if s in (200, 204) else "FAILED (HTTP %s)" % s))
        threading.Thread(target=fazer, daemon=True).start()

    # --- estado -------------------------------------------------------------------------
    def status(self):
        st = dict(self._st)
        ok, why = self.ready()
        ws = st.get("workingset") or {}
        disco = next((d for d in (ws.get("workingset") or []) if d), {}) or {}
        pw = st.get("power") or {}
        bat = (pw.get("batteries") or [{}])[0] if pw.get("batteries") else {}
        rec = st.get("record") or {}
        vf, cf = st.get("videoFormat") or {}, st.get("codecFormat") or {}
        d = {
            "id": self.id, "name": self.name, "host": self.cfg["host"], "port": self.cfg["port"], "enabled": self.cfg["enabled"],
            "connected": bool(self.connected()), "ready": ok, "why": why, "websocket": self._ws_ok,
            "running": bool(rec.get("recording")), "rec_since": st.get("rec_since"),
            "clip": rec.get("clipName"), "take_no": self.take_no, "scene": self.cfg.get("scene"),
            "product": (st.get("product") or {}).get("productName"),
            "app_version": (st.get("product") or {}).get("softwareVersion"),
            "battery": bat.get("chargeRemainingPercent"), "power_source": pw.get("source"),
            "remaining_s": self._restante(), "free_bytes": disco.get("remainingSpace"),
            "clips": disco.get("clipCount"),
            "format": vf.get("name"), "codec": cf.get("codec"),
            "iso": (st.get("iso") or {}).get("iso"),
            "shutter": shutter_normal((st.get("shutter") or {}).get("shutterAngle")),
            "shutter_auto": (st.get("shutter") or {}).get("continuousShutterAutoExposure"),
            "wb": (st.get("wb") or {}).get("whiteBalance"),
            "iris": (st.get("iris") or {}).get("apertureStop"),
            "focal": (st.get("zoom") or {}).get("focalLength"),
            "tc": st.get("tc"), "tc_source": (st.get("tcsource") or {}).get("timecodeInputSource"),
            "tc_offset": st.get("tc_offset"), "tc_err": st.get("tc_err"),
            "tc_frozen_s": (round(time.time() - self._tc_desde)
                            if (st.get("tcsource") or {}).get("timecodeInputSource") == "External"
                            and st.get("tc") is not None and time.time() - self._tc_desde > 8 else None),
            "next_slate": ((st.get("nextClip") or {}).get("clip") or {}),
            "error": None if self.connected() else self.error,
        }
        if not d["connected"]:
            # Desligado: o que se leu da app ja nao vale (antes ficava a bateria e o tempo de cartao da ultima leitura).
            for k in ("battery", "power_source", "remaining_s", "free_bytes", "clips", "format", "codec", "iso",
                      "shutter", "shutter_auto", "wb", "iris", "focal", "tc", "tc_source", "tc_offset", "tc_err",
                      "tc_frozen_s"):
                d[k] = None
            d["next_slate"] = {}
        return d

    def _set(self, chave, valor):
        with self._lock:
            if chave == "record":
                antes = (self._st.get("record") or {}).get("recording")
                agora = bool((valor or {}).get("recording"))
                if agora and not antes:
                    self._st["rec_since"] = time.time()
                elif not agora:
                    self._st["rec_since"] = None
                self._st[chave] = valor
                if antes is not None and antes != agora:
                    self._mudou(agora)
                return
            self._st[chave] = valor
            if chave == "shutter":
                bruto = (valor or {}).get("shutterAngle") if isinstance(valor, dict) else valor
                if shutter_normal(bruto) is None and bruto is not None and bruto != self._shutter_avisado:
                    self._shutter_avisado = bruto
                    self.log("Mobile: shutter angle from the app not usable, shown as -: %r" % (bruto,))
            if chave == "workingset":
                rem = self._restante_app()
                if rem != self._rem_val:                 # so conta quando a app muda o valor
                    self._rem_val, self._rem_t = rem, time.time()

    def _restante_app(self):
        ws = self._st.get("workingset") or {}
        disco = next((d for d in (ws.get("workingset") or []) if d), {}) or {}
        return disco.get("remainingRecordTime")

    def _restante(self):
        """O tempo que a app diz que falta; a gravar, desce por conta propria (a app so o actualiza no STOP)."""
        rem = self._restante_app()
        desde = self._st.get("rec_since")
        if isinstance(rem, (int, float)) and desde and rem == self._rem_val:
            return max(0, int(rem - (time.time() - max(self._rem_t, desde))))
        return rem

    def _mudou(self, agora):
        if self._expect is not None and agora == self._expect:
            return                      # comandado pelo bridge: o _confirmar trata
        self._expect = agora
        self.log("Mobile %s by itself (not commanded by the bridge)" % ("started" if agora else "stopped"))
        if self.on_unexpected:
            try:
                self.on_unexpected(agora)
            except Exception as e:
                self.log("Mobile callback: %r" % e)
        if not agora:                   # a linha do stop ja existe (on_unexpected): so entao se pode anotar o clip
            threading.Thread(target=self._clip_fechado, daemon=True).start()

    def _clip_fechado(self):
        """Depois do STOP: o nome do ficheiro que a app acabou de gravar (GET /transports/0/record) vai para o shoot log."""
        if not self.on_clip:
            return
        try:
            s, r = self._req("GET", "/transports/0/record", timeout=2, canal="cmd")
            nome = r.get("clipName") if s == 200 and isinstance(r, dict) else None
            if nome:
                self.on_clip(str(nome))
            else:
                self.log("Mobile: no clip name after STOP (HTTP %s)" % s)
        except Exception as e:                  # pylint: disable=broad-except
            self.log("Mobile: clip name: %r" % e)

    def _ler_record(self, canal="poll"):
        t0 = time.monotonic()
        s, r = self._req("GET", "/transports/0/record", timeout=2, canal=canal)
        # Pedido antes de um REC/STOP e respondido depois (as duas ligacoes andam em paralelo): e o estado de antes
        # da ordem, e aplicado por cima da confirmacao dava um "comecou/parou sozinho" que nao houve.
        if t0 < getattr(self, "_cmd_t", 0.0):
            return
        if s == 200 and isinstance(r, dict):
            self._set("record", r)

    # --- as threads ------------------------------------------------------------------------
    def _poll_loop(self):
        prox, gen_visto, tc_em = {}, None, 0.0
        while self._vivo:
            if not self.cfg.get("enabled"):
                time.sleep(1)
                continue
            if gen_visto != self._gen:              # configuracao nova: pergunta-se tudo outra vez
                prox, gen_visto = {}, self._gen
            agora = time.time()
            if not self._ws_ok:
                self._ler_record()                  # sem WebSocket: o REC de segundo a segundo, como antes
            if agora - tc_em >= POLL_TC:
                tc_em = agora
                self._ler_tc(1)                     # uma amostra: o TC e o sinal de vida
            else:
                for chave, path in LENTOS:
                    if path in self._ws_props or agora < prox.get(chave, 0):
                        continue                    # o WebSocket avisa, ou ainda nao e altura
                    prox[chave] = agora + POLL_LENTO
                    s, r = self._req("GET", path, timeout=2)
                    if s == 200 and r is not None:
                        self._set(chave, r)
                    break                           # um so pedido por volta: nada de rajadas
            # A ligacao dos comandos mantem-se aberta: parada, a app fecha-a e o REC seguinte tinha de abrir outra
            # (novo aperto de mao HTTPS, lento no Atom). Um pedido leve de 20 em 20 s, so com ela parada.
            if agora - getattr(self, "_cmd_uso", 0) > 20 and self.connected() and not self._http_lock["cmd"].locked():
                self._req("GET", "/transports/0/record", timeout=2, canal="cmd")
            time.sleep(POLL_TICK)

    def _ler_tc(self, amostras=5):
        """TC do iPhone e o desvio para o LTC do bridge.

        Cada pedido demora de 30 a 400 ms, e o TC lido e o do instante em que o
        iPhone o leu, algures nesse intervalo. Pede-se varias vezes e vale a
        amostra de MENOR tempo de resposta (Cristian): o erro e, no maximo,
        metade desse tempo. O iPhone mostra o TC em frames (40 ms a 25 fps)."""
        melhor = None                     # (rtt, desvio, display)
        ultimo = None
        for _ in range(amostras):
            t0 = time.time()
            s, r = self._req("GET", "/transports/0/timecode", timeout=2)
            t1 = time.time()
            if s != 200 or not isinstance(r, dict):
                continue
            ultimo = r.get("display")
            secs, f = _tc_secs(ultimo)
            if secs is None or not self.ltc_secs:
                continue
            try:
                ref = self.ltc_secs((t0 + t1) / 2)
            except Exception:
                ref = None
            if ref is None:
                continue
            d = (secs + f / float(self.fps() or 25)) - ref
            d = (d + 43200) % 86400 - 43200
            if melhor is None or (t1 - t0) < melhor[0]:
                melhor = (t1 - t0, d, ultimo)
            time.sleep(0.05)
        if ultimo is None:
            return
        self._set("tc", ultimo)
        # O timecode parado e um perigo quando a fonte e externa: o clip sai com
        # um TC que nao anda. Guarda-se desde quando o valor nao muda.
        if ultimo != self._tc_ultimo:
            self._tc_ultimo, self._tc_desde = ultimo, time.time()
        if melhor is None:
            self._set("tc_offset", None)
            self._set("tc_err", None)
            return
        self._set("tc_offset", round(melhor[1], 2))
        self._set("tc_err", round(melhor[0] / 2 + 0.02, 2))     # +-meio RTT, +-meio frame

    def _ws_loop(self):
        while self._vivo:
            gen = self._gen
            if not self.cfg.get("enabled"):
                time.sleep(1)
                continue
            inicio, self._ws_extras_enviados = time.time(), False
            try:
                self._ws_sessao(gen)
            except Exception as e:
                if self._ws_ok:
                    self.log("Mobile: websocket closed (%s); polling instead" % e)
            # Se a app fechou o WebSocket logo a seguir aos extras, e por causa deles: nao se insiste.
            if self._ws_extras_enviados and time.time() - inicio < 10 and not self._ws_sem_extras:
                self._ws_sem_extras = True
                self.log("Mobile: the app refused the extra subscriptions; asking them slowly instead")
            self._ws_ok = False
            self._ws_props = set()
            time.sleep(3)

    def _ws_sessao(self, gen):
        host, port = self.cfg["host"], int(self.cfg["port"])
        # O que a app anuncia (uma vez por sessao); sem a lista, tenta-se o que se conhece.
        st, lista = self._req("GET", "/event/list", timeout=3)
        anuncia = set(lista.get("events") or []) if st == 200 and isinstance(lista, dict) else None
        base = [p for p in WS_PROPS if anuncia is None or p in anuncia] or WS_PROPS
        extras = [] if self._ws_sem_extras else [
            p for p in PROPS if p not in WS_PROPS and (anuncia is None or p in anuncia)]
        s = socket.create_connection((host, port), timeout=5)
        s = _CTX.wrap_socket(s, server_hostname=host)
        try:
            chave = base64.b64encode(os.urandom(16)).decode()
            s.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (WS_PATH, host, port, chave)).encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                x = s.recv(4096)
                if not x:
                    raise ConnectionError("closed during handshake")
                resp += x
            if b" 101 " not in resp.split(b"\r\n", 1)[0]:
                raise ConnectionError(resp.split(b"\r\n", 1)[0].decode("latin-1"))
            # O conjunto conhecido primeiro; os extras num pedido a parte, para uma recusa nao estragar o resto.
            _ws_enviar(s, json.dumps({"type": "request", "id": 1,
                                      "data": {"action": "subscribe", "properties": base}}))
            if extras:
                _ws_enviar(s, json.dumps({"type": "request", "id": 2,
                                          "data": {"action": "subscribe", "properties": extras}}))
                self._ws_extras_enviados = True
            self._ws_props = {p for p in base if p in PROPS}
            self._ws_ok = True
            self._ok_at = time.time()
            self.log("Mobile: websocket connected")
            s.settimeout(2)
            while gen == self._gen and self.cfg.get("enabled") and self._vivo:
                try:
                    m = _ws_ler(s)
                except socket.timeout:
                    # Calado e normal com o iPhone parado. So se pergunta quando NADA respondeu ha
                    # muito (o timecode de 3 em 3 s mantem o _ok_at fresco): nada de GET de 2 em 2 s.
                    if time.time() - self._ok_at > WS_SILENCIO:
                        self._ler_record()
                        if time.time() - self._ok_at > WS_SILENCIO:
                            raise ConnectionError("no answer")
                    continue
                if m is None:
                    raise ConnectionError("closed by the mobile")
                self._ok_at = time.time()
                try:
                    msg = json.loads(m)
                except ValueError:
                    continue
                d = msg.get("data") or {}
                if msg.get("type") == "event" and d.get("action") == "propertyValueChanged":
                    chave = PROPS.get(d.get("property"))
                    if chave:
                        self._set(chave, d.get("value"))
                elif msg.get("type") == "response" and isinstance(d.get("values"), dict):
                    for prop, valor in d["values"].items():   # o subscribe devolve os valores actuais
                        if PROPS.get(prop):
                            self._set(PROPS[prop], valor)
                            self._ws_props.add(prop)          # confirmado: deixa de se perguntar
        finally:
            s.close()


class Fleet:
    """Os iPhones do bridge. Cada um e um alvo de REC com id proprio (iphone1,
    iphone2...). A lista (nome, IP, porta, ligado, cena) fica em cfg_path."""

    def __init__(self, log, cfg_path, ltc_secs=None, fps=lambda: 25):
        self.log, self.cfg_path, self.ltc_secs, self.fps = log, cfg_path, ltc_secs, fps
        self.on_unexpected = None          # on_unexpected(cam, a_gravar)
        self.shot = None                   # shot(cam, evento, nota): linha no shoot log
        self.on_fault = None               # on_fault(cam, texto): aviso a vista
        self.on_clip = None                # on_clip(cam, nome): nome do ficheiro gravado, para a linha do stop
        self._lock = threading.Lock()
        self._arrancou = False
        lista = None
        try:
            lista = gravar.ler_json(cfg_path)
        except (OSError, ValueError):
            pass
        if not isinstance(lista, dict) or not isinstance(lista.get("iphones"), dict):
            # primeira vez: o iPhone testado a 2026-10-08
            lista = {"iphones": {"iphone1": {"name": "Mobile A", "host": "192.168.4.11", "port": 4444,
                                             "enabled": True, "scene": ""}}}
        self._cfg = lista
        self.cams = {}
        for cid, c in sorted(lista["iphones"].items()):
            self._criar(cid, c)

    def _criar(self, cid, c):
        c.setdefault("port", 4444)
        c.setdefault("enabled", True)
        c.setdefault("scene", "")
        cam = BMCam(cid, c, self.save, self.log, self.ltc_secs, self.fps)
        cam.on_unexpected = lambda a, cam=cam: self.on_unexpected and self.on_unexpected(cam, a)
        cam.shot = lambda ev, nota, cam=cam: self.shot and self.shot(cam, ev, nota)
        cam.on_fault = lambda txt, cam=cam: self.on_fault and self.on_fault(cam, txt)
        cam.on_clip = lambda nome, cam=cam: self.on_clip and self.on_clip(cam, nome)
        self.cams[cid] = cam
        return cam

    def save(self):
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._cfg, f, indent=1)
            gravar.trocar(tmp, self.cfg_path)
        except OSError as e:
            self.log("Mobiles: could not save %s: %s" % (self.cfg_path, e))

    def start(self):
        self._arrancou = True
        for cam in list(self.cams.values()):
            cam.start()

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

    def add(self, name, host, port=4444):
        with self._lock:
            host_n = str(host or "").strip()
            for outro in self.cams.values():
                if host_n and outro.cfg.get("host") == host_n and int(outro.cfg.get("port") or 4444) == int(port or 4444):
                    return False, "%s already has that address" % (outro.cfg.get("name") or outro.id)
            for cid_r, c_r in (self._cfg.get("removed") or {}).items():
                if host_n and isinstance(c_r, dict) and c_r.get("host") == host_n and int(c_r.get("port") or 4444) == int(port or 4444):
                    return False, "it is in the saved list (%s): restore it from there" % (c_r.get("name") or cid_r)
            n = 1
            while "iphone%d" % n in self.cams or "iphone%d" % n in (self._cfg.get("removed") or {}):
                n += 1
            cid = "iphone%d" % n
            letra = chr(ord("A") + (n - 1) % 26)
            c = {"name": "Mobile %s" % letra, "host": "", "port": 4444, "enabled": True, "scene": ""}
            cam = self._criar(cid, c)
            ok, err = cam.configure(host=host, port=port, name=name or c["name"])
            if not ok:
                del self.cams[cid]
                return False, err
            self._cfg["iphones"][cid] = c
            self.save()
        if self._arrancou:
            cam.start()
        self.log("Mobiles: %s added (%s:%s)" % (cam.name, c["host"], c["port"]))
        return True, cid

    def remove(self, cid):
        with self._lock:
            cam = self.cams.get(cid)
            if not cam:
                return False, "no mobile %s" % cid
            if cam.running():
                return False, "%s is recording: stop it first" % cam.name
            cam._vivo = False
            del self.cams[cid]
            # Guarda-se a configuracao: retirar nao apaga, volta-se a activar da lista.
            self._cfg.setdefault("removed", {})[cid] = self._cfg["iphones"].pop(cid, None) or dict(cam.cfg)
            self.save()
        self.log("Mobiles: %s removed (kept in the saved list)" % cam.name)
        return True, None

    def removed_list(self):
        """Os iPhones retirados e guardados: id -> configuracao."""
        with self._lock:
            return {k: dict(v) for k, v in (self._cfg.get("removed") or {}).items() if isinstance(v, dict)}

    def forget(self, cid):
        """Apaga de vez um iPhone da lista dos guardados (retirados)."""
        with self._lock:
            c = (self._cfg.get("removed") or {}).pop(cid, None)
            if not isinstance(c, dict):
                return False, "no saved mobile %s" % cid
            self.save()
        self.log("Mobiles: %s forgotten" % (c.get("name") or cid))
        return True, None

    def restore(self, cid):
        """Volta a activar um iPhone guardado, com o mesmo id."""
        with self._lock:
            c = (self._cfg.get("removed") or {}).get(cid)
            if not isinstance(c, dict):
                return False, "no saved mobile %s" % cid
            novo = cid                        # o id e estavel: volta com o que tinha (so muda se estiver em uso)
            n = 1
            while novo in self.cams:
                while "iphone%d" % n in self.cams or "iphone%d" % n in self._cfg["removed"]:
                    n += 1
                novo = "iphone%d" % n
            self._cfg["removed"].pop(cid)
            self._cfg["iphones"][novo] = c
            cam = self._criar(novo, c)
            self.save()
        if self._arrancou:
            cam.start()
        self.log("Mobiles: %s restored" % cam.name)
        return True, novo


def _ws_enviar(s, texto):
    b = texto.encode()
    mascara = os.urandom(4)
    if len(b) < 126:
        cab = bytes([0x81, 0x80 | len(b)])
    elif len(b) < 65536:
        cab = bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(b))
    else:
        cab = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", len(b))
    s.sendall(cab + mascara + bytes(c ^ mascara[i % 4] for i, c in enumerate(b)))


def _ws_ler(s):
    """Uma mensagem de texto (junta fragmentos); responde a pings; None = fechado."""
    partes = []
    while True:
        h = _n(s, 2)
        fin, op, ln = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", _n(s, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", _n(s, 8))[0]
        mascara = _n(s, 4) if h[1] & 0x80 else None
        dados = _n(s, ln)
        if mascara:
            dados = bytes(c ^ mascara[i % 4] for i, c in enumerate(dados))
        if op == 8:
            return None
        if op == 9:                                   # ping -> pong
            mascara2 = os.urandom(4)
            s.sendall(bytes([0x8A, 0x80 | len(dados)]) + mascara2
                      + bytes(c ^ mascara2[i % 4] for i, c in enumerate(dados)))
            continue
        if op == 10:
            continue
        partes.append(dados)
        if fin:
            return b"".join(partes).decode("utf-8", "replace")


def _n(s, n):
    b = b""
    while len(b) < n:
        x = s.recv(n - len(b))
        if not x:
            raise ConnectionError("closed")
        b += x
    return b

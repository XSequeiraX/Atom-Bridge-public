"""BMPCC extra, cada uma no seu gateway ESP32-C3 (Wi-Fi): alvos de REC como as GoPro e os iPhones.

A BMPCC principal ("bmpcc") continua no c3_bm.py / Bluetooth do Atom, com a descodificacao completa
no bridge.py. Estas sao as outras: cada uma tem o seu C3 (modo Bridge) e aqui le-se o essencial do
protocolo Blackmagic -- REC, bateria, timecode -- e manda-se o REC, tudo por /api/bm/poll e /api/bm/write.

bmpccs.json (ao lado do bridge.py):
  {"cams": {"bm2": {"name": "BMPCC 2", "host": "192.168.4.30"}}}

Os C3 anunciam-se por beacon UDP ("C3GW ip=..."); o c3_bm.py e quem escuta e reencaminha para aqui
(Fleet.beacon), para os dois nao disputarem a porta.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
import gravar
import tc_vigia
import fw_ota
from curto import erro

POLL = 0.25               # s entre pedidos a cada C3
QUEDA = 4.0               # s sem resposta para dar o C3 por perdido
HTTP_TIMEOUT = 1.5
CONFIRMA = 3.0            # s a esperar que a camara confirme o REC/STOP
ENCONTRADO = 30.0         # s que um C3 anunciado fica na lista "found" sem novo beacon
MAX_CAMS = 8


def _http(host, metodo, caminho):
    # O C3 com gw_token pede o X-GW-Token em todos os POST (REC, bm/write...); os GET (poll, status) nao o levam.
    req = urllib.request.Request("http://%s%s" % (host, caminho), method=metodo,
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
    except (OSError, ValueError):
        return 0, None


def parece_s3(host):
    """True se o aparelho nesse endereco responde como um gateway S3 das GoPro (e nao como um C3)."""
    s, r = _http(host, "GET", "/api/status")
    return s == 200 and isinstance(r, dict) and isinstance(r.get("cams"), list) and "bm" not in r


def comando_rec(on):
    """O pacote da Outgoing Camera Control que inicia/pára a gravação (o mesmo do bridge.py)."""
    d = bytearray([255, 5, 0, 0, 10, 1, 1, 0, 0, 0, 0, 0])
    d[8] = 2 if on else 0
    return bytes(d)


class BmCam:
    """Uma BMPCC no seu C3, com a interface de um alvo de REC."""

    def __init__(self, fleet, cid, dados):
        self.fleet, self.id = fleet, cid
        self.d = dict(dados)
        self.take_no = None
        self.pending = None
        self.recording = None          # None ate chegar o primeiro pacote de transporte
        self.battery = None
        self.tc = None
        self.desvio = tc_vigia.Desvio()  # timecode contra o LTC do Atom (fleet.ltc_secs)
        self.phase = None
        self.mode = None
        self.mem = None                # [livre, maior bloco] do C3
        self.rssi = None               # sinal da BMPCC no C3 (dBm, do ultimo anuncio visto)
        self.wifi_rssi = None          # sinal do Wi-Fi do C3 ate ao AP (dBm)
        self.ok_em = 0.0
        self.error = None
        self._since, self._tcs = -1, -1
        self._expect, self._expect_at = None, 0.0
        self._cmd_n = 0                 # sobe a cada REC/STOP: um comando ultrapassado por outro nao se queixa
        self._rec_t0 = None
        self._stop = threading.Event()
        self._thread = None

    # ---- identidade e estado ----
    @property
    def name(self):
        return self.d.get("name") or self.id

    @property
    def host(self):
        return (self.d.get("host") or "").strip()

    def reachable(self):
        return time.time() - self.ok_em < QUEDA

    def connected(self):
        return self.reachable() and self.mode == "bridge" and self.phase == "ready"

    def ready(self):
        if not self.host:
            return False, "%s has no C3 address" % self.name
        if not self.reachable():
            return False, "%s: the C3 is not answering (%s)" % (self.name, self.host)
        if self.mode != "bridge":
            return False, "%s: the C3 is in standalone mode (switch it to Bridge)" % self.name
        if self.phase != "ready":
            return False, "%s: the BMPCC is not ready on the C3 (%s)" % (self.name, self.phase or "?")
        return True, ""

    def running(self):
        # Caiu a gravar: conta como a gravar (a camara deve continuar a rodar) ate voltar e dizer o estado, ou 30 min.
        incerto = getattr(self, "rec_incerto", None)
        return bool(self.recording) or bool(incerto and time.time() - incerto < 1800)

    def _perdeu(self):
        """A ligacao a camara caiu (ou ficou sem estado). Se estava a gravar, fica "incerto" e regista-se no shoot log."""
        if self.recording and not getattr(self, "rec_incerto", None):
            self.rec_incerto = time.time()
            if self.fleet.shot:
                self.fleet.shot(self, "fault", "link lost while recording")
            if self.fleet.on_lost:
                self.fleet.on_lost(self)
        self.recording = None
        self.desvio.limpar()

    def on_mark(self, especie):
        return None

    def status(self):
        ok, why = self.ready()
        lig = self.connected()
        return {
            "id": self.id, "name": self.name, "host": self.host, "c3_ok": self.reachable(),
            "mode": self.mode, "phase": self.phase,
            "connected": self.connected(), "ready": ok, "why": why,
            "running": self.running(), "pending": self.pending,
            "rec_since": self._rec_t0 if self.running() else None,
            "battery": self.battery if lig else None, "timecode": self.tc if lig else None,   # desligada: nada velho
            "rssi": self.rssi if lig else None, "mem": self.mem if self.reachable() else None,
            "wifi_rssi": self.wifi_rssi if self.reachable() else None,
            "take_no": self.take_no, "error": None if ok else why,
        }

    # ---- REC ----
    def alvo(self):
        """Para onde a camara vai (ver gopro.GoPro.alvo): com um REC/STOP em curso, o que se pediu."""
        return {"starting": True, "stopping": False}.get(self.pending, self.running())

    def set_running(self, on):
        """Grava / para: escreve o comando no C3 e espera que o pacote de transporte da camara o confirme."""
        on = bool(on)
        ok, why = self.ready()
        if not ok:
            return False, why
        self._expect, self._expect_at = on, time.time()
        self._cmd_n += 1
        meu = self._cmd_n
        self.pending = "starting" if on else "stopping"
        try:
            for n in range(3):          # o Wi-Fi a soluçar: repete (o comando diz o estado, repetir nao faz mal)
                if meu != self._cmd_n:
                    return False, "superseded by a newer command"
                s, r = _http(self.host, "POST", "/api/bm/write?hex=" + comando_rec(on).hex())
                if s != 0:
                    break
                time.sleep(0.25 * (n + 1))
            if s != 200 or not (r or {}).get("ok"):
                return False, (r or {}).get("error") or ("the C3 did not answer" if s == 0 else "HTTP %s" % s)
            t0 = time.time()
            while time.time() - t0 < CONFIRMA:
                if self.recording == on:
                    self.fleet.log("%s confirmed %s (%.1f s)" % (self.name, "REC" if on else "STOP", time.time() - t0))
                    return True, None
                if meu != self._cmd_n:
                    return False, "superseded by a newer command"
                time.sleep(0.05)
            return False, "the BMPCC did not confirm in time"
        finally:
            if meu == self._cmd_n:
                self.pending = None

    def escrever(self, data):
        """Um comando qualquer para a BMPCC (Outgoing Camera Control), pelo C3. Levanta ConnectionError se nao foi."""
        for n in range(3):
            s, r = _http(self.host, "POST", "/api/bm/write?hex=" + bytes(data).hex())
            if s != 0:
                break
            time.sleep(0.25 * (n + 1))
        if s != 200 or not (r or {}).get("ok"):
            raise ConnectionError((r or {}).get("error") or ("the C3 did not answer" if s == 0 else "HTTP %s" % s))

    # ---- leitura ----
    def _pacotes(self, raw):
        """Os pacotes de uma notificacao (podem vir varios seguidos): (categoria, parametro, bytes)."""
        i = 0
        while i < len(raw) - 4:
            n = raw[i + 1]
            if n == 0 or i + n + 4 > len(raw):
                break
            yield raw[i + 4], raw[i + 5], raw[i:i + n + 4]
            i += n + 4

    def _evento(self, raw, recs=None):
        """Os pacotes de uma notificacao. Com `recs` (lista), o estado de gravacao junta-se la em vez de se aplicar ja."""
        for cat, par, p in self._pacotes(raw):
            if cat == 10 and par == 1 and len(p) > 8:
                if recs is None:
                    self._rec(p[8] == 2)
                else:
                    recs.append(p[8] == 2)
            elif cat == 12 and par == 1 and len(p) > 9:
                self.battery = round(p[8] / 255 * 100)

    def _rec(self, on):
        if getattr(self, "rec_incerto", None):     # voltou depois de cair a gravar: diz-se o que aconteceu
            self.rec_incerto = None
            if self.fleet.shot:
                self.fleet.shot(self, "fault" if on else "stop",
                                "link back: still recording" if on else "stopped while the link was down")
            self.recording = on
            self._rec_t0 = self._rec_t0 if on else None
            return
        antes = self.recording
        self.recording = on
        if antes is None or antes == on:
            return
        self._rec_t0 = time.time() if on else None
        feito_por_nos = self._expect == on and time.time() - self._expect_at < 5
        if not feito_por_nos and self.fleet.on_unexpected:
            threading.Thread(target=self.fleet.on_unexpected, args=(self, on), daemon=True).start()

    def _tc(self, raw):
        if len(raw) >= 12:
            bcd = lambda v: ((v >> 4) & 0xF) * 10 + (v & 0xF)
            self.tc = "%02d:%02d:%02d:%02d" % (bcd(raw[11]), bcd(raw[10]), bcd(raw[9]), bcd(raw[8]))
            if self.fleet.ltc_secs:
                agora = time.time()
                self.desvio.tc_mudou(self.tc, agora, self.fleet.ltc_secs(agora))

    def _poll_uma_vez(self):
        s, r = _http(self.host, "GET", "/api/bm/poll?since=%d&tcs=%d" % (self._since, self._tcs))
        agora = time.time()
        if s == 200 and isinstance(r, dict):
            self.ok_em, self.mode, self.error = agora, "bridge", None
            self.mem, self.rssi, self.wifi_rssi = r.get("mem"), r.get("rssi"), r.get("wifi_rssi")
            fase = r.get("phase")
            if fase != "ready":
                # Sem camara pronta: nada do que se sabe vale; a proxima vez le o estado todo.
                self._perdeu()
                self.phase, self._since, self._tcs = fase, -1, -1
                return
            self.phase = fase
            # Leitura de raiz (ligou agora, ou voltou): o C3 devolve os ultimos pacotes que guardou, alguns antigos. Um
            # REC/STOP velho la no meio dava um "link back" errado ou um "parou sozinha" que nao houve: dessa leva so
            # conta o ULTIMO estado de gravacao, que e o actual.
            recs = [] if self._since < 0 else None
            for hx in r.get("ev") or []:
                try:
                    self._evento(bytes.fromhex(hx), recs)
                except ValueError:
                    pass
            if recs:
                self._rec(recs[-1])
            if r.get("tc"):
                try:
                    self._tc(bytes.fromhex(r["tc"]))
                except ValueError:
                    pass
                self._tcs = r.get("tc_seq", self._tcs)
            self._since = r.get("seq", self._since)
        elif s == 409:
            self.ok_em, self.mode, self.phase = agora, "standalone", None
            self._perdeu()
            self._since, self._tcs = -1, -1
        else:
            self.error = "the C3 is not answering"
            if agora - self.ok_em > QUEDA:
                self._perdeu()
                self.phase, self._since, self._tcs = None, -1, -1

    def _loop(self):
        while not self._stop.is_set():
            if self.host:
                try:
                    self._poll_uma_vez()
                except Exception as e:      # nunca deixa o fio morrer
                    self.error = erro(e)
            self._stop.wait(POLL)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="bmfleet-" + self.id)
        self._thread.start()

    def stop(self):
        self._stop.set()


class Fleet:
    def __init__(self, log, cfg_path):
        self.log, self.cfg_path = log, cfg_path
        self.on_unexpected = None            # on_unexpected(cam, a_gravar)
        self.on_lost = None                  # on_lost(cam): caiu a gravar
        self.shot = None
        self.ltc_secs = None                 # ltc_secs(t): o LTC do Atom num instante (o bridge da-o), para o desvio do TC
        self.cams = {}
        self.found = {}                      # ip -> ultima vez que anunciou
        self.memoria = {}                    # host -> {"id", "name"} de uma BMPCC tirada: reinserida, volta igual
        self._lock = threading.Lock()
        try:
            cfg = gravar.ler_json(cfg_path) or {}
            for cid, d in (cfg.get("cams") or {}).items():
                if isinstance(d, dict) and re.fullmatch(r"bm\d{1,2}", cid):
                    self.cams[cid] = BmCam(self, cid, d)
            for h, m in (cfg.get("memoria") or {}).items():
                if isinstance(m, dict) and re.fullmatch(r"bm\d{1,2}", str(m.get("id"))):
                    self.memoria[str(h)] = {"id": m["id"], "name": str(m.get("name") or "")}
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            tmp = self.cfg_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"cams": {cid: c.d for cid, c in self.cams.items()}, "memoria": self.memoria}, f, indent=1)
            gravar.trocar(tmp, self.cfg_path)
        except OSError as e:
            self.log("BMPCC fleet: cannot save config: %s" % e)

    def start(self):
        with self._lock:
            for c in self.cams.values():
                c.start()

    # ---- o que o bridge usa (a mesma interface das outras frotas) ----
    def ids(self):
        with self._lock:
            return sorted(self.cams)

    def get(self, cid):
        return self.cams.get(cid)

    def in_targets(self, targets):
        with self._lock:
            return [c for cid, c in sorted(self.cams.items()) if cid in (targets or [])]

    def any_running(self):
        with self._lock:
            return any(c.running() for c in self.cams.values())

    def status(self):
        with self._lock:
            return {cid: c.status() for cid, c in sorted(self.cams.items())}

    # ---- C3 conhecidos / anunciados ----
    def hosts(self):
        """Os enderecos que pertencem a esta frota: o c3_bm.py nao os usa como o seu C3."""
        out = set()
        for c in list(self.cams.values()):
            if c.host:
                out.add(c.host)
                out.add(c.host.rsplit(":", 1)[0])      # o beacon traz so o IP, sem porta
        return out

    def beacon(self, ip):
        self.found[ip] = time.time()

    def disponiveis(self, excluir=()):
        """C3 que anunciaram agora e ainda nao estao em nenhuma BMPCC (nem a principal)."""
        agora = time.time()
        usados = self.hosts() | set(e for e in excluir if e)
        return sorted(ip for ip, t in list(self.found.items()) if agora - t < ENCONTRADO and ip not in usados)

    # ---- gestao ----
    def add(self, name, host):
        host = str(host or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-:]{1,100}", host):
            return False, "invalid address"
        if parece_s3(host):                  # fora do lock: pode demorar
            return False, "that is an S3 gateway (GoPro), not a C3"
        with self._lock:
            if len(self.cams) >= MAX_CAMS:
                return False, "at most %d extra BMPCCs" % MAX_CAMS
            if host in self.hosts() or host.rsplit(":", 1)[0] in self.hosts():
                return False, "that C3 is already a BMPCC here"
            # reinserida, volta com o id e o nome que tinha (se o id estiver livre): o alias, o shoot log e os alvos
            # de REC continuam a referir a mesma camara.
            lembrado = self.memoria.get(host) or {}
            cid = lembrado.get("id")
            if not cid or cid in self.cams:
                n = 2
                while "bm%d" % n in self.cams:
                    n += 1
                cid = "bm%d" % n
            nome = re.sub(r"[^\w .\-]", "", str(name or "")).strip()[:30] or lembrado.get("name") or "BMPCC %s" % cid[2:]
            cam = BmCam(self, cid, {"name": nome, "host": host})
            self.cams[cid] = cam
            self.memoria[host] = {"id": cid, "name": nome}
            self.save()
        cam.start()
        self.log("BMPCC fleet: %s added (%s)" % (nome, host))
        return True, {"id": cid, "name": nome}

    def remove(self, cid):
        with self._lock:
            c = self.cams.get(cid)
            if not c:
                return False, "unknown BMPCC"
            if c.running():
                return False, "recording: stop it first"
            c.stop()
            if c.host:
                self.memoria[c.host] = {"id": cid, "name": c.name}
            del self.cams[cid]
            self.save()
        self.log("BMPCC fleet: %s removed" % c.name)
        return True, None

    def rename(self, cid, name):
        c = self.cams.get(cid)
        nome = re.sub(r"[^\w .\-]", "", str(name or "")).strip()[:30]
        if not c or not nome:
            return False, "unknown BMPCC or empty name"
        c.d["name"] = nome
        self.save()
        return True, None

    def set_host(self, cid, host):
        c = self.cams.get(cid)
        host = str(host or "").strip()
        if not c or not re.fullmatch(r"[A-Za-z0-9.\-:]{1,100}", host):
            return False, "unknown BMPCC or invalid address"
        if c.running():
            return False, "recording: stop it first"
        c.d["host"] = host
        c.recording, c.phase, c.mode, c.ok_em, c._since, c._tcs = None, None, None, 0.0, -1, -1
        self.save()
        return True, None

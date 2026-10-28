"""Gateways simulados para treino e testes (2026-10-14): um C3 com uma BMPCC e um S3 com GoPros, a falar o mesmo HTTP
que os verdadeiros. Sem equipamento ligado, a consola e a pagina funcionam como no plateau.

O modo de treino normal e na consola: GLOBAL SETTINGS > TRAINING > Demo mode (o bridge arranca-os ele proprio e junta
as camaras DEMO; desligar tira-as). O que segue e para correr a parte, a mao:

  python3 /opt/camera-bridge/simulador.py                 C3 em 127.0.0.1:8172 e S3 em 127.0.0.1:8170 (3 GoPro)
  python3 simulador.py --c3 2 --gopros 2                  dois C3 (8172, 8173) e um S3 com 2 GoPro
  python3 simulador.py --lan                              tambem na rede (0.0.0.0), para o iPad ou outro PC

Depois, na consola: DEVICES > GoPro > Add an S3 gateway: 127.0.0.1:8170, e para cada C3 uma BMPCC extra com o
endereco 127.0.0.1:8172 (ou GATEWAYS > C3 > Address para o C3 principal). Nao ha beacon: o simulador nunca se mete num
rig verdadeiro sozinho.

Em ambos: GET /sim/drop?secs=10 deixa o gateway sem responder (queda), para treinar o "link lost"; e
GET /sim/state mostra o estado interno.
"""
import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


class Base(BaseHTTPRequestHandler):
    sim = None

    def log_message(self, *a):
        pass

    def _json(self, d, code=200):
        b = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _pedido(self, metodo):
        u = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        corpo = self.rfile.read(n) if n else b""
        if u.path == "/sim/drop":
            self.sim.caido_ate = time.time() + float(q.get("secs", 10))
            return self._json({"ok": True, "down_s": float(q.get("secs", 10))})
        if u.path == "/sim/state":
            return self._json(self.sim.interno())
        if time.time() < self.sim.caido_ate:
            self.close_connection = True          # "queda": nao responde (o cliente da timeout)
            time.sleep(min(5, self.sim.caido_ate - time.time()))
            return None
        r = self.sim.rota(metodo, u.path, q, corpo)
        if r is None:
            return self._json({"ok": False, "error": "no such route"}, 404)
        return self._json(*r) if isinstance(r, tuple) else self._json(r)

    def do_GET(self):
        self._pedido("GET")

    def do_POST(self):
        self._pedido("POST")


# ---- C3 com uma BMPCC ----------------------------------------------------------------------------------------------
class C3Sim:
    """O C3 em modo Bridge com a BMPCC pronta: pacotes de transporte e bateria como os da camara, timecode = hora local."""

    def __init__(self, nome="BMPCC sim"):
        self.nome, self.caido_ate = nome, 0.0
        self.lock = threading.Lock()
        self.seq, self.ev = 0, []
        self.tc_seq, self.tc = 0, None
        self.recording = False
        self.bateria = random.randint(60, 95)
        self.metadados = {}
        self._empurrar(self._pacote_bateria())
        self._empurrar(self._pacote_rec())
        threading.Thread(target=self._ciclo, daemon=True).start()

    def _empurrar(self, b):
        with self.lock:
            self.seq += 1
            self.ev.append((self.seq, b))
            del self.ev[:-32]

    def _pacote_rec(self):
        return bytes([255, 5, 0, 0, 10, 1, 1, 0, 2 if self.recording else 0, 0, 0, 0])

    def _pacote_bateria(self):
        return bytes([255, 6, 0, 0, 12, 1, 1, 0, round(self.bateria / 100 * 255), 0, 0, 0])

    def _ciclo(self):
        ultimo = None
        while True:
            t = time.time()
            lt = time.localtime(t)
            fr = int((t % 1) * 25)
            tc = bytes([0, 0, 0, 0, 0, 0, 0, 0, _bcd(fr), _bcd(lt.tm_sec), _bcd(lt.tm_min), _bcd(lt.tm_hour)])
            with self.lock:
                self.tc, self.tc_seq = tc, self.tc_seq + 1
            if self.recording and lt.tm_sec % 30 == 0 and lt.tm_sec != ultimo:
                self.bateria = max(1, self.bateria - 1)
                self._empurrar(self._pacote_bateria())
            ultimo = lt.tm_sec
            time.sleep(0.04)

    def _escrever(self, data):
        if len(data) > 8 and data[4] == 10 and data[5] == 1:          # REC / STOP
            quer = data[8] == 2

            def mudar():
                time.sleep(0.3)                       # a camara demora um pouco a confirmar
                self.recording = quer
                self._empurrar(self._pacote_rec())
            threading.Thread(target=mudar, daemon=True).start()
        elif len(data) > 8 and data[4] == 12:                         # claquete (reel, cena, take)
            self.metadados[{0: "reel", 2: "scene", 3: "take"}.get(data[5], str(data[5]))] = data[8:4 + data[1]].hex()
        return True

    def interno(self):
        return {"kind": "c3", "recording": self.recording, "battery": self.bateria, "slate": self.metadados}

    def rota(self, metodo, path, q, corpo):
        estado = {"ok": True, "fw": "fiz_bm_c3 sim", "mode": "bridge", "wifi": "sta_up", "bm": "ready", "fiz": "off",
                  "recording": self.recording, "mem_free": 80000, "mem_max_block": 30000,
                  "bm_rssi": -60, "wifi_rssi": -52, "atom": True, "uptime_s": int(time.monotonic())}
        if path == "/api/status":
            return estado
        if path == "/api/fw":
            return {"ok": True, "fw": "fiz_bm_c3 sim", "files": {}, "trial": False, "rollback": False}
        if path == "/api/bm/poll":
            since, tcs = int(q.get("since", -1)), int(q.get("tcs", -1))
            with self.lock:
                d = {"ok": True, "mode": "bridge", "phase": "ready", "seq": self.seq, "recording": self.recording,
                     "ev": [b.hex() for s, b in self.ev if s > since], "lost": False,
                     "mem": [80000, 30000], "rssi": -60, "rssi_age_s": 1, "wifi_rssi": -52}
                if self.tc is not None and self.tc_seq != tcs:
                    d["tc"], d["tc_seq"] = self.tc.hex(), self.tc_seq
            return d
        if path == "/api/bm/write" and metodo == "POST":
            try:
                data = bytes.fromhex(q.get("hex") or corpo.decode().strip())
            except ValueError:
                return {"ok": False, "error": "invalid hex"}, 400
            return {"ok": self._escrever(data)}
        if path == "/api/mode":
            return {"ok": True, "mode": "bridge"}
        return None


# ---- S3 com GoPros -------------------------------------------------------------------------------------------------
class S3Sim:
    def __init__(self, n=3, nome="GoPro sim "):
        self.caido_ate = 0.0
        self.lock = threading.Lock()
        self.cams = [{"id": "gp%d" % (i + 1), "name": "%s%d" % (nome, i + 1), "mac": "aa:bb:cc:dd:ee:%02x" % (i + 1),
                      "connected": True, "battery": random.randint(50, 100), "recording": False, "rec_s": None,
                      "remaining_s": 7200 + 600 * i, "rssi": -55 - 5 * i, "error": None,
                      "cmd": {"seq": 0, "state": "done", "ok": True, "msg": "", "ms": 0}, "_t0": None}
                     for i in range(n)]

    def _cam(self, cid):
        return next((c for c in self.cams if c["id"] == cid), None)

    def interno(self):
        return {"kind": "s3", "cams": [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.cams]}

    def _estado(self):
        with self.lock:
            cams = []
            for c in self.cams:
                d = {k: v for k, v in c.items() if not k.startswith("_")}
                if c["recording"] and c["_t0"]:
                    d["rec_s"] = int(time.time() - c["_t0"])
                cams.append(d)
        return {"ok": True, "fw": "gopro_gw sim", "uptime_s": int(time.monotonic()), "ip": "127.0.0.1",
                "wifi": "up", "wifi_rssi": -50, "led": "ok", "cams": cams}

    def rota(self, metodo, path, q, corpo):
        if path == "/api/status":
            return self._estado()
        if path == "/api/rec" and metodo == "POST":
            c = self._cam(q.get("id"))
            if not c:
                return {"ok": False, "error": "no camera"}, 404
            with self.lock:
                if c["cmd"]["state"] != "done":
                    return {"ok": False, "error": "another command is still running"}, 409
                c["cmd"] = {"seq": c["cmd"]["seq"] + 1, "state": "running", "ok": False, "msg": "", "ms": 0}
                seq, quer = c["cmd"]["seq"], q.get("on") in ("1", "true")

            def fazer():
                time.sleep(0.8 if quer else 1.5)      # como as GoPro: o STOP demora mais a confirmar
                with self.lock:
                    c["recording"] = quer
                    c["_t0"] = time.time() if quer else None
                    c["cmd"] = {"seq": seq, "state": "done", "ok": True, "msg": "confirmed", "ms": 800 if quer else 1500}
            threading.Thread(target=fazer, daemon=True).start()
            return {"ok": True, "queued": True, "seq": seq}
        if path == "/api/scan":
            return {"ok": True, "done": True, "found": []} if metodo == "GET" else {"ok": True, "secs": 1}
        if path == "/api/reset" and metodo == "POST":
            return {"ok": True, "what": q.get("what", "reboot")}
        if path == "/api/log":
            return {"ok": True, "lines": ["simulated gateway"]}
        return None


def servir(sim, porta, host="127.0.0.1"):
    """Arranca o servidor numa thread. Devolve o servidor (server.server_address da a porta real se porta=0)."""
    h = type("H", (Base,), {"sim": sim})
    srv = ThreadingHTTPServer((host, porta), h)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description="Simulated C3 (BMPCC) and S3 (GoPro) gateways for training and tests.")
    ap.add_argument("--c3", type=int, default=1, help="how many C3 with a BMPCC (ports from 8172)")
    ap.add_argument("--gopros", type=int, default=3, help="GoPros on the simulated S3 (0: no S3), port 8170")
    ap.add_argument("--lan", action="store_true", help="listen on the network too (0.0.0.0), not only on the Atom")
    a = ap.parse_args()
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    for i in range(a.c3):
        servir(C3Sim("BMPCC sim %d" % (i + 1)), 8172 + i, host)
        print("C3 with a BMPCC: %s:%d" % (host, 8172 + i))
    if a.gopros:
        servir(S3Sim(a.gopros), 8170, host)
        print("S3 with %d GoPro: %s:8170" % (a.gopros, host))
    print("Ctrl-C stops. Drop one for 10 s: curl http://127.0.0.1:<port>/sim/drop?secs=10")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Simulador da consola: corre o bridge-ctl.py num pseudo-terminal contra um bridge falso e devolve o ecra (pyte).

Uso directo:  python3 ci/consola_sim.py 100x40 [cenario] [teclas...]
  cenarios: termos (avisos de licenca por aceitar) | normal | falhas (gateways e camaras em baixo) | rec (a gravar) | fim (acabou de parar: pagina de dados da take)
  teclas: texto literal, ou \\e=ESC, \\r=Enter, \\d=seta baixo, \\u=seta cima; !wait:N espera N s; !clear fecha a take
Do ci/verificar.py: correr(cols, linhas, cenario, teclas) -> [linhas do ecra]. Nada fica fora de uma pasta temporaria.
"""
import fcntl
import http.server
import json
import os
import pty
import shutil
import select
import signal
import struct
import sys
import tempfile
import termios
import threading
import time

import pyte

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CEN, POSTS, LIMPA, T0 = ["normal"], [""], [""], [0.0]
TERMOS = [None]          # None: ja aceites; False: por aceitar (cenario "termos"); True: aceitou-se agora


def scen(c):
    st = {"camera_connected": True, "battery_percent": 80, "remaining_time": 3000, "bmpcc_active": True, "is_recording": False,
          "timecode": "10:00:00:00",
          "c3_gateway": {"state": "online", "host": "192.168.4.20", "mode": "bridge", "wifi_rssi": -52, "rssi": -60, "enabled": True},
          "gopro_gateways": [{"idx": 1, "ok": True, "host": "192.168.4.21", "ip": "192.168.4.21", "wifi_rssi": -48, "uptime_s": 7200,
                              "enabled": True, "mode": "gopro", "token": True},
                             {"idx": 2, "ok": True, "host": "192.168.4.22", "ip": "192.168.4.22", "wifi_rssi": -60, "uptime_s": 600,
                              "enabled": True, "mode": "f6", "token": True, "f6": {"connected": True}}],
          "uplink_ssid": "Studio", "clock_sources": {"ntp": {"ok": True}}, "clock_effective": "ntp",
          "power": {"ac": True, "battery": {"percent": 90}}, "soc_temp_c": 52, "mem_percent": 40, "load1": 0.4,
          "ltc": {"now_secs": 36000.0, "offset": 0.0, "fps": 25, "date": time.strftime("%Y-%m-%d"), "tc": "10:00:00", "age": 120, "via": "ble"},
          "rec_targets": ["bmpcc", "iphone1", "gp1"],
          "f6": {"frc": {"connected": True, "armed": [1, 2, 3], "recording": False}, "last_jam": 1}}
    dv = {"devices": [
        {"id": "iphone1", "type": "iphone", "name": "iPhone Cabine", "connected": True, "battery": 70, "remaining_s": 5000, "enabled": True,
         "host": "192.168.4.50", "port": 4444, "iso": 400},
        {"id": "gp1", "type": "gopro", "name": "GoPro 1", "connected": True, "battery": 55, "remaining_s": 3000, "gw": 1, "rssi": -60,
         "gateway_ok": True},
        {"id": "gp2", "type": "gopro", "name": "GoPro 2", "connected": True, "battery": 45, "remaining_s": 2000, "gw": 2, "rssi": -65,
         "gateway_ok": True},
        {"id": "canon1", "type": "canon", "name": "R6", "ip": "192.168.4.60", "connected": True, "battery_percent": 60, "movie_mode": True, "remaining_time": 2000,
         "av": "4.0", "tv": "50", "iso": 800},
        {"id": "canon2", "type": "canon", "name": "R5", "ip": "10.0.0.9", "connected": True, "battery_percent": 60, "movie_mode": False},
        {"id": "sony1", "type": "sony", "name": "A7 IV", "model": "ILCE-7M4", "via": "ip", "host": "192.168.4.80", "connected": True,
         "enabled": True, "battery": 64, "remaining_s": 4200, "iso": "800", "fnumber": "2.8", "shutter": "1/50",
         "caps": {"usb": True, "lan": False, "wifi": True, "ssh": False, "label": "A7 IV"}}],
        "order": [], "alias": {"iphone1": "A-cam"}}
    net = {"ap_up": True, "ap_ssid": "CAM-AP", "ap_chan": 6, "ap_cams": ["iPhone Cabine", "GoPro 1"], "uplink_chan": 11}
    if c == "falhas":
        st["fault"] = True; st["fault_reason"] = "GoPro 1 link lost while recording"
        st["gopro_gateways"][0]["ok"] = False
        st["c3_gateway"]["state"] = "offline"
        dv["devices"][0]["connected"] = False
        dv["devices"][1]["connected"] = False
        net["ap_up"] = False
        st["uplink_ssid"] = ""
        st["clock_effective"] = "unset"; st["clock_sources"] = {}
    if c == "fim":
        gravando = (time.time() - T0[0]) < 4
        st["is_recording"] = gravando
        dv["devices"][0]["running"] = gravando
        if not gravando and not os.path.exists(LIMPA[0]):
            st["take_pending"] = {"take": 7, "since": 4242.0, "lock": True}
    if c == "rec":
        st["take_rec"] = {"take": 7, "since": time.time() - 192, "cameras": 2}
        st["is_recording"] = True
        dv["devices"][0]["running"] = True
    return st, dv, net

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        st, dv, net = scen(CEN[0])
        p = self.path.split("?")[0]
        if p == "/api/status": return self._send(200, st)
        if p == "/api/devices": return self._send(200, dv)
        if p == "/api/net/status": return self._send(200, net)
        if p == "/api/system/terms":
            if self.command == "POST":
                TERMOS[0] = True
                return self._send(200, {"ok": True, "accepted": True})
            return self._send(200, {"ok": True, "accepted": TERMOS[0] is not False, "version": 1,
                                    "text": ["ATOM BRIDGE: LICENSES AND NOTICES", "", "NO WARRANTY"]})
        if p == "/api/system/sony_sdk":
            return self._send(200, {"ok": True, "available": True, "sdk": "0x2020000", "models": ["ILCE-7M4", "ILME-FX3"],
                                    "caps": {"ILCE-7M4": {"usb": True, "lan": False, "wifi": True, "ssh": False, "label": "A7 IV"}},
                                    "found": [{"model": "ILCE-7M4", "connection": "Network", "ip": "192.168.4.80"}]})
        if p == "/api/slate/take/skip":
            open(LIMPA[0], "w").close(); open(POSTS[0], "a").write(json.dumps({"SKIP": True}) + "\n")
            return self._send(200, {"ok": True})
        if p == "/api/slate/take":
            if self.command == "POST":
                open(LIMPA[0], "w").close()
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                open(POSTS[0], "a").write(json.dumps(body) + "\n")
                return self._send(200, {"ok": True})
            return self._send(200, {"ok": True, "name": "Day 1", "scene": "3A", "reel": "2", "shot": "MS door", "last_take": 7, "next_take": 8})
        return self._send(200, {})
    do_POST = do_GET


def correr(cols, rows, cenario="normal", teclas=(), consola=None):
    """O ecra (lista de linhas) depois de arrancar a consola no cenario e carregar nas teclas."""
    tmp = tempfile.mkdtemp(prefix="consola-sim-")
    TERMOS[0] = False if cenario == "termos" else None
    CEN[0], POSTS[0], LIMPA[0], T0[0] = cenario, os.path.join(tmp, "posts.json"), os.path.join(tmp, "take_cleared"), time.time()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update({"BRIDGE_URL": "http://127.0.0.1:%d" % srv.server_address[1], "TERM": "xterm-256color",
                           "LANG": "C.UTF-8", "HOME": tmp, "BRIDGE_CTL_MENUS": os.path.join(tmp, "menus")})
        os.execvp(sys.executable, [sys.executable, consola or os.path.join(RAIZ, "bridge-ctl.py")])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    os.kill(pid, signal.SIGWINCH)
    scr = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(scr)

    def pump(t):
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    d = os.read(fd, 65536)
                except OSError:
                    return
                if not d:
                    return
                stream.feed(d)

    def tecla(s):
        if s.startswith("!wait:"):
            pump(float(s[6:]))
            return
        if s == "!clear":
            open(LIMPA[0], "w").close()
            return
        s = s.replace("\\e", "\x1b").replace("\\r", "\r").replace("\\d", "\x1bOB").replace("\\u", "\x1bOA")
        os.write(fd, s.encode())

    try:
        pump(3.0)
        for k in teclas:
            tecla(k)
            pump(1.2)
        return [line.rstrip() for line in scr.display]
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    c, l = (int(x) for x in sys.argv[1].lower().split("x"))
    for n, linha in enumerate(correr(c, l, sys.argv[2] if len(sys.argv) > 2 else "normal", sys.argv[3:])):
        print("%2d|%s" % (n, linha))

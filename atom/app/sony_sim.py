"""Simulador do sony_ctl: o mesmo protocolo (comandos no stdin, eventos JSON no stdout), sem SDK nem camara.

Para os testes (ci/) e para experimentar a consola e a pagina sem equipamento:
  sony.Fleet(..., comando=[sys.executable, "sony_sim.py"])
Uma camara com o IP a acabar em .99 nao responde (erro de ligacao do SDK), para se ver esse caminho.
"""
import json
import sys
import threading
import time

import sony

_lock = threading.Lock()
cams = {}


def emitir(d):
    with _lock:
        sys.stdout.write(json.dumps(d) + "\n")
        sys.stdout.flush()


def estado(cid):
    c = cams.get(cid)
    if not c or not c["ligada"]:
        return
    emitir({"ev": "state", "id": cid, "rec": c["rec"], "battery_pct": c["bat"], "battery_unit": 2,
            "slot1_s": c["resta"], "slot2_s": 0xFFFFFFFF, "iso": 800, "fnumber": 280, "shutter": 0x00010032,
            "tc_preset": c["tc"]})


def ciclo():
    while True:
        time.sleep(2)
        for cid, c in list(cams.items()):
            if c["ligada"]:
                if c["rec"] == 1:
                    c["resta"] = max(0, c["resta"] - 2)
                    c["bat"] = max(1, c["bat"] - (1 if int(time.time()) % 60 < 2 else 0))
                estado(cid)


def main():
    emitir({"ev": "ready", "sdk": "SIM"})
    threading.Thread(target=ciclo, daemon=True).start()
    for linha in sys.stdin:
        a = linha.split()
        if not a:
            continue
        cmd, cid = a[0], (a[1] if len(a) > 1 else "")
        if cmd == "quit":
            break
        if cmd == "add":
            if len(a) < 5 or a[2] not in sony.MODELOS:
                emitir({"ev": "result", "id": cid, "cmd": "add", "ok": False, "code": "0x8000",
                        "error": "model not in this SDK: %s" % (a[2] if len(a) > 2 else "")})
                continue
            cams[cid] = {"modelo": a[2], "via": a[3], "end": a[4], "ligada": False, "rec": 0, "bat": 87, "resta": 5400,
                         "tc": 0}
            emitir({"ev": "result", "id": cid, "cmd": "add", "ok": True})
        elif cmd == "connect":
            c = cams.get(cid)
            if not c:
                emitir({"ev": "result", "id": cid, "cmd": "connect", "ok": False, "code": "0x8000"})
                continue
            emitir({"ev": "result", "id": cid, "cmd": "connect", "ok": True})

            def ligar(cid=cid, c=c):
                time.sleep(0.5)
                if c["end"].endswith(".99"):
                    emitir({"ev": "error", "id": cid, "code": "0x8202"})
                    return
                c["ligada"] = True
                emitir({"ev": "connected", "id": cid})
                estado(cid)
            threading.Thread(target=ligar, daemon=True).start()
        elif cmd in ("disconnect", "del"):
            c = cams.get(cid)
            if c:
                c["ligada"] = False
            if cmd == "del":
                cams.pop(cid, None)
            emitir({"ev": "result", "id": cid, "cmd": cmd, "ok": True})
        elif cmd == "rec" and len(a) >= 3:
            c = cams.get(cid)
            ok = bool(c and c["ligada"])
            emitir({"ev": "result", "id": cid, "cmd": "rec_on" if a[2] == "1" else "rec_off", "ok": ok,
                    **({} if ok else {"code": "0x8000"})})
            if ok:
                def mudar(cid=cid, c=c, on=a[2] == "1"):
                    time.sleep(0.4)
                    c["rec"] = 1 if on else 0
                    estado(cid)
                threading.Thread(target=mudar, daemon=True).start()
        elif cmd == "tc" and len(a) >= 3:
            c = cams.get(cid)
            emitir({"ev": "result", "id": cid, "cmd": "tc", "ok": bool(c and c["ligada"] and c["rec"] == 0)})
        elif cmd == "read":
            estado(cid)
        elif cmd == "enum":
            emitir({"ev": "found", "cameras": [{"model": "ILCE-7M4", "connection": "Network", "ip": "192.168.4.80",
                                                "ssh": False, "serial": "d8c0a6000001"}]})
        elif cmd == "models":
            emitir({"ev": "models", "models": sorted(sony.MODELOS)})
        else:
            emitir({"ev": "error", "error": "unknown command: %s" % cmd})


if __name__ == "__main__":
    main()

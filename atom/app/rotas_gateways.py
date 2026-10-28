"""Os gateways ESP32 (C3, S3): estado, firmware, configuracao por USB (/api/gateways...).

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import bmfleet as bmfleet_mod
import json
import os
import threading
import time
from flask import jsonify, request
import nucleo
from nucleo import (AP_IFACE, GW_FW_CFG, _alvos_tirar, _body, _gp_gw, _gp_gw_ok, _gw_fw_atualizar, _gw_fw_auto,
    _gw_fw_lista, _gw_fw_lock, _gw_fw_ultimo, _rec_alvo, _rec_parado, anything_recording, app, log_event,
    save_settings, state)


@app.route('/api/gateways/c3')
def api_c3():
    """O gateway C3 da BMPCC principal e os C3 anunciados que nao pertencem a nenhuma BMPCC (found)."""
    return jsonify(dict(nucleo.c3_bm.estado(), found=nucleo.bmfleet.disponiveis([nucleo.c3_bm.host()])))


@app.route('/api/gateways/c3/enabled', methods=['POST'])
def api_c3_enabled():
    # Desligado, o bridge deixa de procurar o C3 e de tentar religar a BMPCC por ele.
    on = bool(_body().get("on"))
    if not on and anything_recording():
        return jsonify({"ok": False, "error": "recording: switch the C3 off after the take"}), 409
    nucleo.c3_bm.set_ligado(on)
    if not on:
        state["_auto_reconnect"] = False
    return jsonify({"ok": True, "enabled": on})


@app.route('/api/gateways/c3/host', methods=['POST'])
def api_c3_host():
    h = str(_body().get("host") or "").strip()
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: change the C3 after the take"}), 409
    if h and (h in nucleo.bmfleet.hosts() or h.rsplit(":", 1)[0] in nucleo.bmfleet.hosts()):
        return jsonify({"ok": False, "error": "that C3 belongs to an extra BMPCC"}), 409
    if h and bmfleet_mod.parece_s3(h):
        return jsonify({"ok": False, "error": "that is an S3 gateway (GoPro), not a C3"}), 409
    ok, err = nucleo.c3_bm.set_host(h)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/gateways/c3/firmware', methods=['GET', 'POST'])
def api_c3_fw():
    """Firmware do gateway C3 por Wi-Fi: GET compara o do C3 com o do repositorio; POST envia o que mudou
    (?action=rollback volta ao anterior). Recusado a gravar: o C3 reinicia."""
    import fw_ota
    host = nucleo.c3_bm.host()
    if request.method == 'GET':
        return jsonify(fw_ota.c3_estado(host))
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: update the C3 after the take"}), 409
    if request.args.get("action") == "rollback":
        ok, d = fw_ota.c3_reverter(host)
    else:
        ok, d = fw_ota.c3_atualizar(host, log_event)
    log_event("C3 firmware %s: %s" % (request.args.get("action") or "update", "ok" if ok else d.get("error")))
    return jsonify({**d, "ok": ok}), (200 if ok else 409)


_gw_usb_job = {"running": False, "log": [], "result": None, "since": 0.0}

_gw_usb_lock = threading.Lock()   # o "ha um a correr?" e o "passa a correr" numa so volta: dois POST juntos nao arrancam os dois

GW_USB_JOB_MAX = 900.0        # um trabalho "a correr" ha mais do que isto esta preso (os passos tem tempos limite): liberta-se


def _gw_usb_log(msg):
    _gw_usb_job["log"].append(time.strftime("%H:%M:%S") + " " + msg)
    del _gw_usb_job["log"][:-300]


def _gw_usb_correr(porta, tipo, wifi, usar_ap, modo, apagar=False):
    import gw_usb
    try:
        ok, d = gw_usb.configurar(porta, tipo, wifi, usar_ap, modo, _gw_usb_log, apagar)
    except Exception as e:                      # nunca deixa o trabalho preso em "a correr"
        ok, d = False, {"error": "unexpected error: %s: %s" % (type(e).__name__, str(e)[:160])}
    if ok and d.get("kind") != "m5stick":
        nucleo.c3_bm.desligar()
        if d.get("kind") == "c3":
            try:
                _gw_usb_procurar_c3(d)
            except Exception as e:                  # o diagnostico nunca estraga uma gravacao que correu bem
                _gw_usb_log("could not check the C3 on the network: %s" % str(e)[:120])
    log_event("Gateway by USB: %s" % (f"{d.get('kind', '?').upper()} ok ({d.get('mode') or 'files'})" if ok else d.get("error")))
    _gw_usb_job["result"] = {**d, "ok": ok}
    _gw_usb_job["running"] = False
    _gw_usb_job["since"] = 0.0


def _gw_usb_procurar_c3(d, tempo=60.0):
    """Depois de gravar um C3: o bridge procura-o (liga-o se estava desligado) e espera o beacon dele. Se nao aparecer,
    pergunta-lhe pela USB como esta o Wi-Fi. O resultado fica no registo do trabalho e em d["found"]."""
    import gw_usb
    if not nucleo.c3_bm.ligado():
        nucleo.c3_bm.set_ligado(True)
        _gw_usb_log("The C3 gateway was OFF in the bridge: switched ON")
    fixo = (nucleo.c3_bm._cfg().get("host") or "").strip()
    if fixo:
        _gw_usb_log("Note: the bridge looks for the C3 at the fixed address %s (GATEWAYS > C3 > Address; empty = automatic)"
                    % fixo)
    def esperar(segundos):
        _gw_usb_log("Waiting for the C3 to join the Wi-Fi and show up (up to %d s) ..." % segundos)
        t0 = time.time()
        while time.time() - t0 < segundos:
            novos = sorted(ip for ip, t in list(nucleo.c3_bm.vistos.items()) if t > t0)
            if novos:
                d["found"] = novos
                _gw_usb_log("The Atom sees the C3 at %s" % ", ".join(novos))
                if fixo and fixo not in novos:
                    _gw_usb_log("  but the bridge is set to %s: clear the fixed address if this is the main C3" % fixo)
                return True
            time.sleep(1)
        return False


    if esperar(tempo):
        return
    _gw_usb_log("The C3 did not show up on the network")
    d["found"] = []
    diag = gw_usb.c3_diagnostico(d.get("port"), _gw_usb_log)
    if diag and diag.get("sta"):
        _gw_usb_log("  it is on the Wi-Fi but the Atom does not hear it: is it EXT WI-FI (%s)?"
                    % (gw_usb.ap_wifi(AP_IFACE)[0] or AP_IFACE))
        return
    # Como no S3: um reinicio pela USB nem sempre chega (o Bluetooth do C3 pode ficar sem arrancar); desligar a
    # alimentacao resolve. O operador desliga e volta a ligar; o Atom espera o C3 outra vez.
    _gw_usb_log(">>> UNPLUG the C3 from the USB and plug it in again <<<")
    if not gw_usb.esperar_religar(d.get("port"), _gw_usb_log, nome="C3"):
        _gw_usb_log("The C3 was not unplugged: unplug it and plug it in again; the Atom finds it by itself")
        return
    if not esperar(tempo):
        _gw_usb_log("Still not on the network: run Setup / update by USB again and send the screen of this log")


@app.route('/api/gateways/usb', methods=['GET', 'POST'])
def api_gateways_usb():
    """Configuracao inicial e actualizacao de qualquer gateway (C3 ou S3) ligado por USB ao Atom: GET diz as portas, as
    ferramentas e as imagens; POST {"port", "use_ap": bool, "wifi": {"ssid", "psk"}} identifica a placa, grava-lhe o firmware e o
    Wi-Fi (o das cameras com use_ap). As passwords nunca vao para o registo nem para a resposta.
    "kind": "m5stick" grava o comando de bolso do F6 (M5StickS3): "mode" setup (imagem completa + o Wi-Fi do S3 do F6 em "wifi"),
    update (so a aplicacao) ou wifi (so o nome e a palavra-passe do AP)."""
    import gw_usb
    if request.method == 'GET':
        if request.args.get("job"):               # o trabalho em curso (ou o ultimo): passos e resultado
            return jsonify({"ok": True, "running": _gw_usb_job["running"], "log": list(_gw_usb_job["log"]),
                            "result": _gw_usb_job["result"]})
        return jsonify({"ok": True, **gw_usb.estado()})
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: set up the gateway after the take"}), 409
    b = _body()
    wifi = b.get("wifi") if isinstance(b.get("wifi"), dict) else None
    if wifi is not None:
        wifi = {"ssid": str(wifi.get("ssid") or "").strip(), "psk": str(wifi.get("psk") or "")}
    tipo = b.get("kind") if b.get("kind") in ("c3", "s3", "m5stick") else "auto"
    porta = str(b["port"]) if b.get("port") else None
    modo = b.get("mode") if b.get("mode") in ("update", "setup", "wifi") else "auto"
    with _gw_usb_lock:
        if _gw_usb_job["running"] and time.time() - _gw_usb_job["since"] < GW_USB_JOB_MAX:
            return jsonify({"ok": False, "error": "a gateway USB job is already running (it ends by itself in a few minutes)"}), 409
        _gw_usb_job.update({"running": True, "log": [], "result": None, "since": time.time()})
    if b.get("async"):                            # a consola segue os passos em directo
        threading.Thread(target=_gw_usb_correr, args=(porta, tipo, wifi, bool(b.get("use_ap")), modo, bool(b.get("erase"))),
                         daemon=True).start()
        return jsonify({"ok": True, "started": True})
    _gw_usb_correr(porta, tipo, wifi, bool(b.get("use_ap")), modo, bool(b.get("erase")))
    r = _gw_usb_job["result"]
    return jsonify(r), (200 if r.get("ok") else 409)


@app.route('/api/gateways/s3/firmware', methods=['GET', 'POST'])
def api_gopros_fw():
    """Firmware do gateway S3 (GoPro) por Wi-Fi: POST grava no S3 o .bin que veio do GitHub (atom/app/fw/s3);
    ?action=rollback volta ao anterior. Nao aceita um .bin enviado de fora. Recusado a gravar."""
    import fw_ota
    n, erro = _gp_gw_ok({"gw": request.args.get("gw")})
    if erro:
        return erro
    g = nucleo.gofleet.gw(n)
    host = g.host
    if request.method == 'GET':
        return jsonify(fw_ota.s3_estado(host))
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: update the S3 after the take"}), 409
    if request.args.get("action") == "rollback":
        ok, d = fw_ota.s3_reverter(host)
        return jsonify({**d, "ok": ok}), (200 if ok else 409)
    if request.get_data():                      # o firmware do S3 so vem do GitHub, pelo Atom: nada enviado de fora
        return jsonify({"ok": False, "error": "the S3 firmware comes only from GitHub (update the bridge)"}), 400
    ok, d = fw_ota.s3_atualizar(host, log_event)
    log_event("S3 firmware update: %s" % ("ok" if ok else d.get("error")))
    return jsonify({**d, "ok": ok}), (200 if ok else 409)


@app.route('/api/gateways/firmware', methods=['GET', 'POST'])
def api_gateways_fw():
    """GET: o estado do firmware de todos os gateways. POST {"ids": [...]} (omitido = todos os atrasados): actualiza-os.
    POST {"auto": true|false} liga/desliga a actualizacao automatica."""
    if request.method == 'GET':
        return jsonify({"auto": _gw_fw_auto(), "gateways": _gw_fw_lista(), "last": _gw_fw_ultimo})
    b = _body()
    if "auto" in b:
        try:
            tmp = GW_FW_CFG + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"auto": bool(b["auto"])}, f)
            os.replace(tmp, GW_FW_CFG)
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True, "auto": bool(b["auto"])})
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: update the gateways after the take"}), 409
    if not _gw_fw_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "a gateway update is already running"}), 409
    try:
        ids = b.get("ids")
        # Atrasados; e um S3 a que o Atom nunca gravou nada ("unknown" com .bin) so se o pedido o nomear.
        itens = [x for x in _gw_fw_lista()
                 if (x["state"] == "outdated" and (ids is None or x["id"] in ids))
                 or (x["state"] == "unknown" and x["kind"] == "s3" and x.get("target") and ids is not None and x["id"] in ids)]
        res = _gw_fw_atualizar(itens)
        return jsonify({"ok": all(r["ok"] for r in res), "results": res, "updated": [r["id"] for r in res if r["ok"]]})
    finally:
        _gw_fw_lock.release()


@app.route('/api/gateways/s3/reset', methods=['POST'])
def api_gopros_reset():
    """Repoe o gateway ESP32-S3: reboot | bonds | factory."""
    d = _body()
    n, erro = _gp_gw_ok(d)
    if erro:
        return erro
    erro = _gw_parado(n, "resetting the gateway", tudo=True)
    if erro:
        return erro
    ok, r = nucleo.gofleet.reset(str(d.get("what") or "reboot"), n)
    return jsonify({"ok": ok, "error": None if ok else r, "note": r if ok else None}), (200 if ok else 409)


@app.route('/api/gateways/s3/mode', methods=['POST'])
def api_gopros_mode():
    """O selector do S3: {"mode": "gopro"|"f6"|"f6solo", "gw": n, "psk": "..."}. O S3 guarda e reinicia. `psk`: a palavra-passe
    do Wi-Fi do comando autonomo (f6solo); nao se regista."""
    d = _body()
    n, erro = _gp_gw_ok(d)
    if erro:
        return erro
    if nucleo.f6.running() and d.get("mode") in ("gopro", "f6solo"):
        return jsonify({"ok": False, "error": "the F6 is recording: stop it first"}), 409
    erro = _gw_parado(n, "changing the gateway mode", tudo=True)
    if erro:
        return erro
    ok, err = nucleo.gofleet.set_mode(str(d.get("mode") or ""), n, d.get("psk"))
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/gateways/s3/led', methods=['POST'])
def api_gopros_led():
    """O tipo de LED da placa S3: {"type": "rgb"|"simple"|"off", "gw": n}. Aplica-se logo."""
    d = _body()
    n, erro = _gp_gw_ok(d)
    if erro:
        return erro
    ok, err = nucleo.gofleet.set_led(str(d.get("type") or ""), n)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/gateways/s3/enabled', methods=['POST'])
def api_gopros_enabled():
    """Liga/desliga o gateway das GoPro so no bridge (o gateway fisico desligado nao e avaria).
    {"on": true|false}: so um booleano conta (um texto como "no" nao liga nada por engano)."""
    b = request.get_json(silent=True)
    on = b.get("on") if isinstance(b, dict) else None
    if not isinstance(on, bool):
        return jsonify({"ok": False, "error": "on: true or false"}), 400
    n, erro = _gp_gw_ok(b)
    if erro:
        return erro
    if not on:
        erro = _gw_parado(n, "switching the gateway off")
        if erro:
            return erro
    ok, err = nucleo.gofleet.set_enabled(on, n)
    g = nucleo.gofleet.gw(n)
    return jsonify({"ok": ok, "error": err, "enabled": bool(g and g.enabled)}), (200 if ok else 409)


@app.route('/api/gateways/s3')
def api_gateways_s3():
    """Os gateways S3 das GoPro (o primeiro em gateway, todos em gateways) e as GoPro que se anunciam (found)."""
    return jsonify({"ok": True, "gateway": nucleo.gofleet.gateway(), "gateways": nucleo.gofleet.gateways(),
                    "found": nucleo.gofleet.disponiveis()})


def _gw_parado(n, o_que, tudo=False):
    """None se o gateway n (None = o primeiro) pode ser mexido; senao a resposta 409: uma GoPro deste gateway grava ou esta
    para gravar (alvo / comando por confirmar: um START em curso conta, so running() nao o via), ou um REC/STOP corre.
    `tudo`: recusa tambem se qualquer outra coisa grava (reset / modo: o S3 reinicia e leva o F6 consigo). O lock do REC so
    segura a verificacao: a operacao (rede) faz-se depois, fora dele."""
    g = nucleo.gofleet.gw(n)
    with _rec_parado() as livre:
        if not livre:
            return jsonify({"ok": False, "error": "a REC/STOP is in progress: try again in a moment"}), 409
        if (tudo and anything_recording()) or any(
                cam.fleet is g and (_rec_alvo(cam) or getattr(cam, "pending", None)) for cam in list(nucleo.gofleet.cams.values())):
            return jsonify({"ok": False, "error": f"Recording — not {o_que} now"}), 409
    return None


@app.route('/api/gateways/s3/add', methods=['POST'])
def api_gopros_gateway_add():
    """Mais um gateway S3: {host, name}. O primeiro nunca se acrescenta (ja existe)."""
    d = _body()
    ok, r = nucleo.gofleet.add_gateway(d.get("host"), d.get("name"))
    if ok:
        nucleo.gofleet.gws[r].poll_once()
    return (jsonify({"ok": True, "gw": r}), 200) if ok else (jsonify({"ok": False, "error": r}), 400)


@app.route('/api/gateways/s3/remove', methods=['POST'])
def api_gopros_gateway_remove():
    n = _gp_gw(_body())
    if not n:
        return jsonify({"ok": False, "error": "gw: invalid"}), 400
    if nucleo.gofleet.gw(n) is None:
        return jsonify({"ok": False, "error": "no such gateway"}), 404
    erro = _gw_parado(n, "removing the gateway")
    if erro:
        return erro
    ids =[c for c, cam in nucleo.gofleet.cams.items() if cam.fleet.idx == n]
    ok, err = nucleo.gofleet.remove_gateway(n)
    if ok:
        _alvos_tirar(ids)
        save_settings()
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/gateways/s3/host', methods=['POST'])
def api_gopros_host():
    """Endereco do gateway ESP32-S3 das GoPro."""
    d = _body()
    n, erro = _gp_gw_ok(d)
    if erro:
        return erro
    if str(d.get("host") or "").strip() != (nucleo.gofleet.gw(n).host or ""):
        erro = _gw_parado(n, "changing the gateway address")
        if erro:
            return erro
    ok, err = nucleo.gofleet.set_host(d.get("host"), n)
    if ok:
        nucleo.gofleet.gw(n).poll_once()
    return jsonify({"ok": ok, "error": err}), (200 if ok else 400)


@app.route('/api/gateways/s3/scan', methods=['POST'])
def api_gopros_scan():
    """Procura GoPro a anunciar-se (o gateway varre; demora uns segundos)."""
    d = _body()
    n, erro = _gp_gw_ok(d)
    if erro:
        return erro
    ok, r = nucleo.gofleet.scan(d.get("secs") or 8, n)
    return (jsonify({"ok": True, "found": r}), 200) if ok else (jsonify({"ok": False, "error": r}), 409)

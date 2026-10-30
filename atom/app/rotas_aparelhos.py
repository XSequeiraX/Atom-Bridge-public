"""Os aparelhos (/api/devices...) e o REC (/api/rec).

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import bmfleet as bmfleet_mod
import sony
import f6_card
import os
import re
import threading
import time
from flask import Response, jsonify, request, send_from_directory, stream_with_context
from curto import erro as erro_curto
import nucleo
from nucleo import (SHOOT_LOG_PATH, _CANON_ORIGEM, _a_gravar_ids, _alvos_lock, _alvos_tirar, _bmpcc_activar,
    _bmpcc_gravar, _bmpcc_guardar, _bmpcc_livre, _body, _camera_cancel, _dev_canon_removidos, _dev_gravar,
    _dev_iphones_tirados_cfg, _dev_ler, _dev_lock, _dev_resolve, _do_rec_group, _gp_gw_ok, _rec_alvo,
    _rec_group_lock, _rec_parado, _reconnect_lock, _remove_bmpcc, anything_recording, app, connect_camera_dbus,
    disconnect_camera, log_event, save_settings, state, take_lock, write_camera)


def get_iso_command(iso_value):
    """Build ISO command: Category 1, Parameter 14, Type 3 (int32)"""
    import struct
    data = bytearray(12)
    data[0] = 255       # destination
    data[1] = 8         # length
    data[2] = 0         # command id
    data[3] = 0         # reserved
    data[4] = 1         # category: Video
    data[5] = 14        # parameter: ISO
    data[6] = 3         # type: int32
    data[7] = 0         # padding
    struct.pack_into('<i', data, 8, iso_value)
    return data


@app.route('/api/devices/<id>/path', methods=['POST'])
def api_dev_path(id):
    """[bmpcc] Por onde vai a BMPCC principal: {"via": "c3"} (o gateway C3, o normal) ou {"via": "bluez"} (o Bluetooth do
    proprio Atom, plano B, com a camara emparelhada ao Atom: Bluetooth > Pair a new device). Recusado a gravar."""
    _t, _h, erro = _dev_exige(id, ("bmpcc",), "path")
    if erro:
        return erro
    via = _body().get("via")
    if via not in ("c3", "bluez"):
        return jsonify({"ok": False, "error": "via: c3 | bluez"}), 400
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: change the BMPCC path after the take"}), 409
    if (via == "c3") == nucleo.c3_bm.ativo():
        return jsonify({"ok": True, "via": via, "note": "already"})
    try:
        disconnect_camera()                     # pelo caminho de antes
    except Exception as e:                      # pylint: disable=broad-except
        log_event(f"BMPCC path: disconnect: {e}")
    nucleo.c3_bm.set_via(via)
    state["camera_connected"] = False
    # Pelo Bluetooth do Atom o bridge liga-se sozinho (como o C3 faz quando a BMPCC fica pronta nele).
    state["_auto_reconnect"] = via == "bluez" and nucleo.BMPCC_ACTIVE
    return jsonify({"ok": True, "via": via, "address": nucleo.CAMERA_ADDRESS})


def _bmpcc_connect():
    # Check-and-set camera_connecting under a lock so two rapid requests
    # (double tap, two devices) can't both slip past the check and start
    # concurrent connect attempts.
    if not nucleo.BMPCC_ACTIVE:
        return jsonify({"ok": False, "error": "BMPCC removed: restore it from Devices"}), 409
    with _reconnect_lock:
        if state["camera_connecting"] or state["camera_connected"]:
            return jsonify({"ok": False, "error": "Already connecting"}), 409
        state["camera_connecting"] = True
    _camera_cancel.clear()
    state["_auto_reconnect"] = True
    def do_connect():
        try:
            connect_camera_dbus()
        except Exception as e:
            log_event(f"Camera error: {e}")
        finally:
            state["camera_connecting"] = False
    threading.Thread(target=do_connect, daemon=True).start()
    return jsonify({"ok": True})


def _bmpcc_disconnect():
    if state.get("is_recording"):
        return jsonify({"ok": False, "error": "the BMPCC is recording: stop it first"}), 409
    state["_auto_reconnect"] = False
    # Aborta tambem uma tentativa em curso: o Disconnect() do BlueZ cancela uma
    # ligacao pendente, o que faz a chamada bloqueada retornar de imediato em
    # vez de esperar pelo timeout dele.
    _camera_cancel.set()
    disconnect_camera()
    return jsonify({"ok": True})


@app.route('/api/rec/targets', methods=['POST'])
def api_rec_target():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "the body must be a JSON object"}), 400
    valid = {"bmpcc", "f6"} | set(nucleo.canon_ccapi.CAMERAS.keys()) | set(nucleo.iphs.ids()) | set(nucleo.gofleet.ids()) | set(nucleo.bmfleet.ids()) | set(nucleo.sonys.ids())
    desconhecidos = []
    with _alvos_lock:
        if data.get("id") is not None or data.get("toggle") is not None:
            # Uma mudanca so (arma/desarma um device) aplicada a lista que o bridge tem AGORA. Mandar a lista inteira
            # feita a partir de um status antigo (a pagina e a consola leem de segundo a segundo) desfazia o
            # toque anterior quando dois vinham seguidos.
            did = str(data.get("toggle") if data.get("toggle") is not None else data.get("id"))
            if did not in valid:
                return jsonify({"ok": False, "error": "unknown device %s" % did,
                                "targets": list(state.get("rec_targets") or [])}), 400
            atual = [t for t in (state.get("rec_targets") or []) if t != did]
            on = (did not in (state.get("rec_targets") or [])) if data.get("toggle") is not None else bool(data.get("on"))
            targets = atual + ([did] if on else [])
        else:
            targets = data.get("targets")
            if targets is None:                      # tolerate the old single-value form
                if "target" not in data:
                    return jsonify({"ok": False, "error": "targets (a list of ids), id/on or toggle needed",
                                    "targets": list(state.get("rec_targets") or [])}), 400
                t = data.get("target")
                if t is not None and not isinstance(t, str):
                    return jsonify({"ok": False, "error": "target: a device id",
                                    "targets": list(state.get("rec_targets") or [])}), 400
                targets = list(valid) if t == "all" else ([t] if t else [])
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                return jsonify({"ok": False, "error": "targets: a list of device ids",
                                "targets": list(state.get("rec_targets") or [])}), 400
            desconhecidos = [t for t in dict.fromkeys(targets) if t not in valid]
            # A forma antiga {target}, ou uma lista em que nenhum id existe (nada a aplicar): erro, sem mexer nos alvos.
            if desconhecidos and (data.get("targets") is None or not any(t in valid for t in targets)):
                return jsonify({"ok": False, "error": "unknown device %s" % ", ".join(desconhecidos),
                                "targets": list(state.get("rec_targets") or [])}), 400
            # Na lista, um id que ja nao existe (uma Canon tirada; o "ALL" da pagina manda a lista fixa) larga-se e o resto
            # aplica-se (ignored): antes um id velho dava 400 a lista toda e o botao nao fazia nada.
        targets = [t for t in dict.fromkeys(targets) if t in valid]
        state["rec_targets"] = targets
    save_settings()
    log_event(f"Rec targets: {'+'.join(targets) if targets else 'none'}")
    r = {"ok": True, "targets": targets}
    if desconhecidos:
        r["ignored"] = desconhecidos
    return jsonify(r)


# ---- o registo unico: UM id por aparelho, em toda a API (o tipo e um campo, nunca parte do caminho) ----
_DEV_TIPOS_API = ("canon", "mobile", "gopro", "bmpcc", "bmpcc_extra", "sony", "f6")
# O tipo dos telemoveis com a app Blackmagic Camera (iPhone ou Android) chamava-se "iphone": continua a valer como
# sinonimo a entrada (?type=iphone, {"type": "iphone"}), para nao partir scripts e clientes antigos. Os ids ("iphone1")
# e o ficheiro iphones.json ficam como estavam.
_TIPO_SINONIMO = {"iphone": "mobile"}


def _tipo_norm(t):
    return _TIPO_SINONIMO.get(t, t) if isinstance(t, str) else t


def _dev_exige(cid, tipos, acao, removido_ok=False, ativo_erro=None):
    """(tipo, handle, None) se o device existe, e do tipo certo, e esta em servico; senao (None, None, resposta de erro):
    404 {"error": "no device <id>"} se o id nao existe; 409 "<acao> is not supported by <tipo> devices" se o tipo nao tem a
    accao (`tipos` = os que a tem; None = todos); 409 se esta retirado (a menos que `removido_ok`: restore, forget); 409
    `ativo_erro` se esta em servico e a accao so serve a um retirado."""
    tipo, h, est = _dev_resolve(cid)
    if tipo is None:
        return None, None, (jsonify({"ok": False, "error": f"no device {cid}"}), 404)
    if tipos and tipo not in tipos:
        return None, None, (jsonify({"ok": False, "error": f"{acao} is not supported by {tipo} devices"}), 409)
    if est == "removido" and not removido_ok:
        txt = "BMPCC removed: restore it from Devices" if cid == "bmpcc" else f"{cid} is removed: restore it first"
        return None, None, (jsonify({"ok": False, "error": txt}), 409)
    if est == "ativo" and ativo_erro:
        return None, None, (jsonify({"ok": False, "error": ativo_erro}), 409)
    return tipo, h, None


def _dev_item(tipo, cid, h, d=None):
    """O estado de um device em servico, como o endpoint por-tipo antigo o dava, mais "id" e "type"."""
    if tipo == "canon":
        extra = (d if d is not None else _dev_ler()).get("canons") or {}
        it = dict(h.state, name=h.name, ip=h.ip, port=h.port, builtin=cid in _CANON_ORIGEM and cid not in extra,
                  settings_polling=nucleo.canon_ccapi.settings_polling)
    elif tipo == "bmpcc":
        it = {"name": "BMPCC", "address": nucleo.CAMERA_ADDRESS, "active": nucleo.BMPCC_ACTIVE, "adapter": nucleo.CAMERA_ADAPTER,
              "connected": bool(state.get("camera_connected")), "connecting": bool(state.get("camera_connecting")),
              "recording": bool(state.get("is_recording")), "via_c3": bool(nucleo.c3_bm.ativo())}
    elif tipo == "f6":
        it = dict(nucleo.f6.status())
    else:
        it = dict(h.status())
    it["id"], it["type"] = cid, tipo
    return it


def _dev_itens(tipo=None):
    """Os devices em servico (um item cada); `tipo` filtra."""
    d = _dev_ler()
    out = []
    if tipo in (None, "canon"):
        out += [_dev_item("canon", cid, cam, d) for cid, cam in list(nucleo.canon_ccapi.CAMERAS.items())]
    if tipo in (None, "bmpcc") and nucleo.BMPCC_ACTIVE:
        out.append(_dev_item("bmpcc", "bmpcc", None))
    for t, frota in (("mobile", nucleo.iphs), ("gopro", nucleo.gofleet), ("bmpcc_extra", nucleo.bmfleet), ("sony", nucleo.sonys)):
        if tipo in (None, t):
            out += [_dev_item(t, cid, cam) for cid, cam in sorted(frota.cams.items())]
    if tipo in (None, "f6"):
        out.append(_dev_item("f6", "f6", nucleo.f6))
    return out


def _dev_removidos(tipo=None):
    """Os devices retirados que se podem restaurar ou esquecer: {id, type, name, ip, ...}."""
    d = _dev_ler()
    out = []
    if tipo in (None, "canon"):
        for cid, c in _dev_canon_removidos(d).items():
            nome, ip = (c.get("name"), c.get("ip")) if isinstance(c, dict) else (c.name, c.ip)
            out.append({"id": cid, "type": "canon", "name": nome, "ip": ip})
    if tipo in (None, "mobile"):
        out += [{"id": cid, "type": "mobile", "name": c.get("name"), "ip": c.get("host")}
                for cid, c in _dev_iphones_tirados_cfg().items()]
    if tipo in (None, "bmpcc"):
        out += [{"id": k, "type": "bmpcc", "name": v.get("alias") or "BMPCC", "ip": v.get("address"),
                 "address": v.get("address")} for k, v in (d.get("saved_bmpcc") or {}).items()]
    if tipo in (None, "sony"):
        out += [{"id": cid, "type": "sony", "name": c.get("name"), "ip": c.get("host"), "model": c.get("model"),
                 "via": c.get("via"), "serial": c.get("serial")} for cid, c in sorted(nucleo.sonys.removed_list().items())]
    return out


@app.route('/api/devices')
def api_devices():
    """Todos os aparelhos do rig: devices (em servico, um item cada com id e type), removed (retirados, para restaurar ou
    esquecer), order, alias e o bloco bmpcc. ?type= filtra por tipo (canon, mobile (ou iphone), gopro, bmpcc, bmpcc_extra, sony, f6)."""
    if not request.headers.get("X-Bridge-Service"):
        state["_last_ui_poll"] = time.time()
    tipo = _tipo_norm(request.args.get("type"))
    if tipo is not None and tipo not in _DEV_TIPOS_API:
        return jsonify({"ok": False, "error": "type: " + " | ".join(_DEV_TIPOS_API)}), 400
    d = _dev_ler()
    return jsonify({"ok": True, "devices": _dev_itens(tipo), "removed": _dev_removidos(tipo),
                    "order": d.get("order") or [], "alias": d.get("alias") or {},
                    "bmpcc": {"address": nucleo.CAMERA_ADDRESS, "active": nucleo.BMPCC_ACTIVE, "adapter": nucleo.CAMERA_ADAPTER}})


@app.route('/api/devices/<id>')
def api_device(id):
    """Um aparelho (em servico ou retirado) pelo seu id: {"device": {...}}; 404 se o id nao existe."""
    tipo, h, est = _dev_resolve(id)
    if tipo is None:
        return jsonify({"ok": False, "error": f"no device {id}"}), 404
    if est == "ativo":
        it = _dev_item(tipo, id, h)
    else:
        it = next((r for r in _dev_removidos(tipo) if r["id"] == id), None) or {"id": id, "type": tipo, "name": "BMPCC"}
    it["removed"] = est == "removido"
    return jsonify({"ok": True, "device": it})


@app.route('/api/devices/<id>/alias', methods=['POST'])
def api_dev_alias(id):
    """[canon, mobile, gopro, bmpcc, bmpcc_extra, sony, f6] Alias da camara, um nome curto de referencia (vai para a coluna alias do shoot log):
    {"alias": texto}. Vazio apaga."""
    import re
    alias = str(_body().get("alias") or "").strip()
    _t, _h, erro = _dev_exige(id, None, "alias")
    if erro:
        return erro
    if alias and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,9}", alias):
        return jsonify({"ok": False, "error": "reference: 1 to 10 letters, digits, space, - or _"}), 400
    with _dev_lock:
        d = _dev_ler()
        if alias:
            d.setdefault("alias", {})[id] = alias
        else:
            (d.get("alias") or {}).pop(id, None)
        try:
            _dev_gravar(d)
        except OSError as e:
            return jsonify({"ok": False, "error": f"could not save devices.json: {e}"}), 500
    log_event(f"Devices: reference of {id} is now {alias or '(none)'}")
    return jsonify({"ok": True, "alias": alias})


def _batch_order(b):
    """A ordem dos aparelhos no ecra principal: {"ids": [...]} (os que faltarem ficam no fim)."""
    ordem = b.get("ids")
    if not isinstance(ordem, list) or not all(isinstance(x, str) and 0 < len(x) <= 24 for x in ordem):
        return jsonify({"ok": False, "error": "ids: a list of device ids"}), 400
    vistos = []
    for x in ordem:
        if x not in vistos:
            vistos.append(x)
    with _dev_lock:
        d = _dev_ler()
        d["order"] = vistos
        try:
            _dev_gravar(d)
        except OSError as e:
            return jsonify({"ok": False, "error": f"could not save devices.json: {e}"}), 500
    return jsonify({"ok": True})


def _id_reservado(cid):
    """Ids reservados: os fixos (bmpcc, f6), a palavra batch (POST /api/devices/batch), os nomes dos tipos, os que as outras frotas geram
    sozinhas (iphone2, gp3, bm2, sony1) e os das BMPCC guardadas (b + 6 hex), e as palavras reservadas (all, none, so digitos): um device novo nunca os leva."""
    return (cid in ("bmpcc", "f6", "batch", "all", "none", "canon", "gopro", "bmpcc_extra") or cid.isdigit() or cid.startswith(("iphone", "sony")) or re.fullmatch(r"(bm|gp)\d+", cid) is not None
            or re.fullmatch(r"b[0-9a-f]{6}", cid) is not None)


def _dev_id_ocupado(cid):
    """O id nao pode ser dado a um device novo: reservado, em servico, retirado/guardado ou de uma Canon de origem
    (mesmo esquecida). Um id nunca se reutiliza enquanto o device existir."""
    return (_id_reservado(cid) or cid in _CANON_ORIGEM or _dev_resolve(cid)[0] is not None
            or cid in (_dev_ler().get("canons") or {}))


def _add_canon(b):
    """Mais um corpo Canon (CCAPI). O IP tem de ser fixo (reserva de DHCP ou no corpo)."""
    import ipaddress
    import re

    nome = str(b.get("name") or "").strip()
    ip = str(b.get("ip") or "").strip()
    try:
        porta = int(b.get("port") or 8080)
    except (TypeError, ValueError, OverflowError):         # OverflowError: 1e999 do JSON
        porta = 0
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,11}", nome):
        return jsonify({"ok": False, "error": "name: 1 to 12 letters, digits, space, - or _"}), 400
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return jsonify({"ok": False, "error": "that is not an IPv4 address"}), 400
    if not 1 <= porta <= 65535:
        return jsonify({"ok": False, "error": "port out of range"}), 400
    with _dev_lock:
        if any(c.ip == ip for c in list(nucleo.canon_ccapi.CAMERAS.values())):
            return jsonify({"ok": False, "error": "another Canon already has that IP"}), 409
        if any(c.name.lower() == nome.lower() for c in list(nucleo.canon_ccapi.CAMERAS.values())):
            return jsonify({"ok": False, "error": "another Canon already has that name"}), 409
        guardadas = _dev_ler().get("saved") or {}
        if any(c.get("ip") == ip or str(c.get("name")).lower() == nome.lower() for c in guardadas.values()):
            return jsonify({"ok": False, "error": "it is in the saved list: restore it from there"}), 409
        base = re.sub(r"[^a-z0-9]", "", nome.lower()) or "canon"
        if _id_reservado(base):               # "mobile", "bm2"...: nunca ficam livres a juntar um numero
            base = "canon" + base
        cid, n = base, 2
        while _dev_id_ocupado(cid):
            cid, n = f"{base}{n}", n + 1
        d = _dev_ler()
        d.setdefault("canons", {})[cid] = {"name": nome, "ip": ip, "port": porta, "https": bool(b.get("https")) or porta == 443}
        try:
            _dev_gravar(d)
        except OSError as e:
            return jsonify({"ok": False, "error": f"could not save devices.json: {e}"}), 500
        nucleo.canon_ccapi.CAMERAS[cid] = nucleo.canon_ccapi.CanonCamera(nome, ip, port=porta, use_https=bool(b.get("https")) or porta == 443)
    log_event(f"Devices: Canon {nome} added ({ip}:{porta}) as {cid}")
    return jsonify({"ok": True, "id": cid})


def _remove_canon(cid):
    with _rec_parado() as livre, _dev_lock:
        if not livre:
            return jsonify({"ok": False, "error": "a REC/STOP is in progress: try again in a moment"}), 409
        cam = nucleo.canon_ccapi.CAMERAS.get(cid)
        if cam is None:
            return jsonify({"ok": False, "error": f"no Canon {cid}"}), 404
        if cam.a_gravar():
            return jsonify({"ok": False, "error": f"{cam.name} is recording: stop it first"}), 409
        d = _dev_ler()
        if cid in (d.get("canons") or {}):
            d.setdefault("saved", {})[cid] = d["canons"].pop(cid)      # guarda-se: volta pela lista
        elif cid in _CANON_ORIGEM:
            d.setdefault("removed", [])
            if cid not in d["removed"]:
                d["removed"].append(cid)
        try:
            _dev_gravar(d)
        except OSError as e:
            return jsonify({"ok": False, "error": f"could not save devices.json: {e}"}), 500
        try:
            cam._auto_reconnect = False
            cam.state["auto_reconnect"] = False
            cam.disconnect()
        except Exception:
            pass
        nucleo.canon_ccapi.CAMERAS.pop(cid, None)
        try:
            _alvos_tirar(cid)
        except Exception:
            pass
    log_event(f"Devices: Canon {cam.name} removed")
    return jsonify({"ok": True})


def _restore_canon(cid):
    """Volta a activar uma Canon retirada: de origem (R10, R6) ou acrescentada."""
    with _dev_lock:
        d = _dev_ler()
        if cid in nucleo.canon_ccapi.CAMERAS:
            return jsonify({"ok": False, "error": f"{cid} is already active"}), 409
        if cid in (d.get("removed") or []) and cid in _CANON_ORIGEM and cid not in (d.get("forgotten") or []):
            d["removed"].remove(cid)
            cam = _CANON_ORIGEM[cid]
        elif cid in (d.get("saved") or {}):
            c = d["saved"].pop(cid)
            if any(x.ip == c.get("ip") for x in list(nucleo.canon_ccapi.CAMERAS.values())):
                return jsonify({"ok": False, "error": "another Canon has taken that IP"}), 409
            d.setdefault("canons", {})[cid] = c
            cam = nucleo.canon_ccapi.CanonCamera(c["name"], c["ip"], port=int(c.get("port") or 8080),
                                          use_https=bool(c.get("https")))
        else:
            return jsonify({"ok": False, "error": f"{cid} is not in the saved list"}), 404
        try:
            _dev_gravar(d)
        except OSError as e:
            return jsonify({"ok": False, "error": f"could not save devices.json: {e}"}), 500
        nucleo.canon_ccapi.CAMERAS[cid] = cam
    log_event(f"Devices: Canon {cam.name} restored")
    return jsonify({"ok": True, "id": cid})


def _restore_iphone(cid, frota=None):
    """Volta a activar um iPhone (ou uma Sony) retirado (mantem o id; so tem um novo se o antigo ja esta em uso)."""
    ok, r = (frota or nucleo.iphs).restore(cid)
    if ok and r != cid:
        with _dev_lock:
            d = _dev_ler()
            a = (d.get("alias") or {}).pop(cid, None)
            if a:
                d["alias"][r] = a
                _dev_gravar(d)
    return (jsonify({"ok": True, "id": r}), 200) if ok else (jsonify({"ok": False, "error": r}), 404)


def _add_bmpcc(b):
    """Poe uma BMPCC em servico (so uma de cada vez): a que la estava fica guardada."""
    import re
    a = str(b.get("address") or "").strip().upper()
    alias = str(b.get("alias") or "").strip()
    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", a):
        return jsonify({"ok": False, "error": "address: AA:BB:CC:DD:EE:FF"}), 400
    if alias and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,9}", alias):
        return jsonify({"ok": False, "error": "reference: 1 to 10 letters, digits, space, - or _"}), 400
    porque = _bmpcc_livre()
    if porque:
        return jsonify({"ok": False, "error": porque}), 409
    with _dev_lock:
        d = _dev_ler()
        mesmo = nucleo.BMPCC_ACTIVE and a == nucleo.CAMERA_ADDRESS
        if not mesmo:
            _bmpcc_guardar(d)
        _bmpcc_activar(d, a, alias if (alias or not mesmo) else (d.get("alias") or {}).get("bmpcc", ""))
        erro = _bmpcc_gravar(d)
        if erro:
            return erro
    log_event(f"Devices: BMPCC {a} in service")
    return jsonify({"ok": True, "id": "bmpcc", "address": a})


def _restore_bmpcc(cid):
    """Volta a pôr em servico uma BMPCC guardada (a que estava em servico fica guardada)."""
    porque = _bmpcc_livre()
    if porque:
        return jsonify({"ok": False, "error": porque}), 409
    with _dev_lock:
        d = _dev_ler()
        p = (d.get("saved_bmpcc") or {}).get(cid)
        if not p:
            return jsonify({"ok": False, "error": f"{cid} is not in the saved list"}), 404
        _bmpcc_guardar(d)
        _bmpcc_activar(d, p["address"], p.get("alias") or "")
        erro = _bmpcc_gravar(d)
        if erro:
            return erro
    log_event(f"Devices: BMPCC {p['address']} restored")
    return jsonify({"ok": True, "id": "bmpcc", "address": p["address"]})


def _bmpcc_chave(cid):
    """O id na lista dos guardados: a BMPCC principal retirada ("bmpcc") e a que esta guardada com o seu endereco."""
    if cid != "bmpcc":
        return cid
    return next((k for k, v in (_dev_ler().get("saved_bmpcc") or {}).items() if v.get("address") == nucleo.CAMERA_ADDRESS), cid)


@app.route('/api/devices', methods=['POST'])
def api_devices_add():
    """Acrescenta um aparelho: {"type": "canon", name, ip, port, https} | {"type": "mobile", name, host, port} |
    {"type": "gopro", mac, model, name, gw} | {"type": "bmpcc", address, alias} | {"type": "bmpcc_extra", name, host} |
    {"type": "sony", name, model, via: "ip" | "usb", host | serial, ssh, user, password}
    (409 se os avisos de licenca ainda nao foram aceites: GET/POST /api/system/terms).
    Devolve o id novo (o da BMPCC e sempre "bmpcc")."""
    b = _body()
    tipo = _tipo_norm(b.get("type"))
    if tipo == "f6":
        return jsonify({"ok": False, "error": "add is not supported by f6 devices"}), 409
    adders = {"canon": _add_canon, "mobile": _add_iphone, "gopro": _add_gopro, "bmpcc": _add_bmpcc,
              "bmpcc_extra": _add_bmpcc_extra, "sony": _add_sony}
    if not isinstance(tipo, str) or tipo not in adders:
        return jsonify({"ok": False, "error": "type: " + " | ".join(adders)}), 400
    return adders[tipo](b)


@app.route('/api/devices/<id>/remove', methods=['POST'])
def api_dev_remove(id):
    """[canon, mobile, gopro, bmpcc, bmpcc_extra, sony] Tira o aparelho de servico (recusado a gravar). Canon, mobile, BMPCC
    e Sony ficam na lista dos retirados (restore); a GoPro e a BMPCC extra apagam-se."""
    tipo, _h, erro = _dev_exige(id, ("canon", "mobile", "gopro", "bmpcc", "bmpcc_extra", "sony"), "remove")
    if erro:
        return erro
    if tipo == "canon":
        return _remove_canon(id)
    if tipo == "bmpcc":
        return _remove_bmpcc()
    return _remover_da_frota({"mobile": nucleo.iphs, "gopro": nucleo.gofleet, "bmpcc_extra": nucleo.bmfleet,
                              "sony": nucleo.sonys}[tipo], id)


@app.route('/api/devices/<id>/restore', methods=['POST'])
def api_dev_restore(id):
    """[canon, mobile, bmpcc, sony] Volta a pôr em servico um aparelho retirado (a BMPCC que estava em servico fica guardada)."""
    tipo, _h, erro = _dev_exige(id, ("canon", "mobile", "bmpcc", "sony"), "restore", removido_ok=True,
                                ativo_erro=f"{id} is already active")
    if erro:
        return erro
    if tipo == "canon":
        return _restore_canon(id)
    if tipo in ("mobile", "sony"):
        return _restore_iphone(id, nucleo.sonys if tipo == "sony" else None)
    return _restore_bmpcc(_bmpcc_chave(id))


@app.route('/api/devices/batch', methods=['POST'])
def api_devices_batch():
    """Uma accao sobre varios aparelhos: {"action": ..., "type": ...}. order {ids: [...]} (ordem no ecra principal);
    reset {type: canon} (tira as Canon ligadas de um estado preso); settings_polling {type: canon, on: bool} (liga/desliga a
    leitura de Av/Tv/ISO...); lead {type: gopro, ms: N} (avanco do QR da hora)."""
    b = _body()
    acao = b.get("action")
    suportam = {"order": None, "reset": "canon", "settings_polling": "canon", "lead": "gopro"}
    if not isinstance(acao, str) or acao not in suportam:
        return jsonify({"ok": False, "error": "action: " + " | ".join(suportam)}), 400
    if acao == "order":
        return _batch_order(b)
    tipo = b.get("type")
    if tipo not in _DEV_TIPOS_API:
        return jsonify({"ok": False, "error": "type: " + " | ".join(_DEV_TIPOS_API)}), 400
    if tipo != suportam[acao]:
        return jsonify({"ok": False, "error": f"{acao} is not supported by {tipo} devices"}), 409
    return {"reset": _batch_reset_canon, "settings_polling": _batch_canon_settings_polling,
            "lead": _batch_gopro_lead}[acao]()


@app.route('/api/rec/strict', methods=['POST'])
def api_strict_rec():
    on = bool(_body().get("on", False))
    state["strict_rec"] = on
    save_settings()
    log_event("Strict recording mode " + ("ON — an uncommanded BMPCC stop raises an alarm"
                                          if on else "off"))
    return jsonify({"ok": True, "on": on})


def _batch_reset_canon():
    """Force every connected Canon out of a stuck recording/movie-mode state."""
    if anything_recording():
        return jsonify({"ok": False, "error": "Recording — not resetting the cameras now"}), 409
    done = []
    for cam in list(nucleo.canon_ccapi.CAMERAS.values()):
        if cam.state["connected"]:
            threading.Thread(target=cam.force_idle, daemon=True).start()
            done.append(cam.name)
    if not done:
        return jsonify({"ok": False, "error": "No Canon connected"}), 409
    log_event("Canon reset (force idle): %s" % ", ".join(done))
    return jsonify({"ok": True, "cameras": done})


def do_rec_group(targets, grupo=False):
    """Serializa accoes de grupo.

    Sem isto, dois REC com poucos segundos de intervalo (pagina, consola, USB) corriam ao mesmo tempo, liam a mesma
    fotografia do estado e decidiam os dois a mesma coisa. O lock e o que garante que o segundo ve o que o primeiro
    comandou. O REC so mexe no que estiver em modo video (uma Canon em modo foto nao grava: fica de fora).
    """
    with _rec_group_lock:
        recusa = _rec_recusado_pela_take(targets, grupo)
        if recusa:
            return False, {"ok": False, "error": recusa, "take_data": True}
        return _do_rec_group(targets, grupo)


def _rec_recusado_pela_take(targets, grupo):
    """O motivo, se este REC ia COMECAR uma gravacao com a pagina de dados da take por fechar (parar nunca se recusa). Vale para
    todos os clientes (consola, pagina web, controladores USB): a pagina existe em todos e qualquer um a pode saltar."""
    if not state.get("_take_pendente") or not take_lock():
        return None
    gravam = _a_gravar_ids()
    arranca = not gravam if grupo else not (set(targets or []) & gravam)
    if not arranca:
        return None
    return "Take data pending: fill it in or skip it first (console or web page)"


@app.route('/api/devices/<id>/jam', methods=['POST'])
def api_dev_jam(id):
    """[f6] 5 s de LTC a andar pela saida de auscultadores, para o F6 acertar o TC."""
    _t, _h, erro = _dev_exige(id, ("f6",), "jam")
    if erro:
        return erro
    if anything_recording():
        return jsonify({"ok": False, "error": "Recording — not sending timecode now"}), 409
    ok, err = nucleo.f6.jam()
    if not ok:
        log_event(f"F6 jam refused — {err}")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/devices/<id>/user', methods=['POST'])
def api_dev_user(id):
    """[f6] Tecla USER do F6 pela funcao: mark, clear_clip, circled (f6_ctl.USER_KEYS)."""
    _t, _h, erro = _dev_exige(id, ("f6",), "user")
    if erro:
        return erro
    func = str(_body().get("func") or "")
    ok, err = nucleo.f6.user(func)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


def _f6_scene():
    """Nome da cena no F6 (campo name) ou cena +1 (campo inc)."""
    dados = _body()
    ok, err = nucleo.f6.scene_inc() if dados.get("inc") else nucleo.f6.set_scene(dados.get("name"))
    if not ok:
        log_event(f"F6 scene refused — {err}")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/devices/<id>/note', methods=['POST'])
def api_dev_note(id):
    """[f6] Nota da PROXIMA take no F6 (metadado NOTE), pelo teclado do FRC-8."""
    _t, _h, erro = _dev_exige(id, ("f6",), "note")
    if erro:
        return erro
    ok, err = nucleo.f6.set_note(_body().get("text"))
    if not ok:
        log_event(f"F6 note refused — {err}")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/devices/<id>/track_name', methods=['POST'])
def api_dev_track_name(id):
    """[f6] Nome de uma pista no F6 (metadado TRACK NAME da proxima take)."""
    _t, _h, erro = _dev_exige(id, ("f6",), "track_name")
    if erro:
        return erro
    d = _body()
    ok, err = nucleo.f6.set_track_name(d.get("track"), d.get("name"))
    if not ok:
        log_event(f"F6 track name refused — {err}")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


def _f6card(fn):
    """Corre uma operacao do cartao do F6; os erros dela vao como 409 legivel."""
    try:
        return jsonify({"ok": True, **(fn() or {})})
    except f6_card.CardError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:
        log_event(f"F6 card error: {e!r}")
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route('/api/devices/<id>/card/status')
def api_dev_card_status(id):
    """[f6] Estado do cartao SD do F6 (montado, leitura em curso, leitor de som)."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/status")
    if erro:
        return erro
    return jsonify(f6_card.status())


@app.route('/api/devices/<id>/card/scan', methods=['POST'])
def api_dev_card_scan(id):
    """[f6] Le o cartao (so leitura) e cruza com o shoot log; deixa um CSV."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/scan")
    if erro:
        return erro
    def fazer():
        rep = f6_card.scan(SHOOT_LOG_PATH, os.path.dirname(SHOOT_LOG_PATH))
        log_event(f"F6 card read: {len(rep['takes'])} takes")
        return {"report": rep}
    return _f6card(fazer)


@app.route('/api/devices/<id>/card/report.csv')
def api_dev_card_csv(id):
    """[f6] O CSV da ultima leitura do cartao (descarrega)."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/report.csv")
    if erro:
        return erro
    p = f6_card.last_csv()
    if not p or not os.path.exists(p):
        return jsonify({"ok": False, "error": "read the card first"}), 404
    return send_from_directory(os.path.dirname(p), os.path.basename(p), as_attachment=True)


@app.route('/api/devices/<id>/card/ls')
def api_dev_card_ls(id):
    """[f6] Lista uma pasta do cartao: ?path=."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/ls")
    if erro:
        return erro
    return _f6card(lambda: f6_card.ls(request.args.get("path", "")))


@app.route('/api/devices/<id>/card/op', methods=['POST'])
def api_dev_card_op(id):
    """[f6] mkdir / rename / rename_take / move / trash / empty_trash no cartao."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/op")
    if erro:
        return erro
    d = _body()
    def fazer():
        msg = f6_card.op(str(d.get("op") or ""), str(d.get("path") or ""),
                         str(d.get("name") or ""), str(d.get("dest") or ""),
                         str(d.get("confirm") or ""))
        log_event(msg)
        return {"message": msg}
    return _f6card(fazer)


@app.route('/api/devices/<id>/card/play', methods=['POST'])
def api_dev_card_play(id):
    """[f6] Toca um WAV do cartao pela saida de som do Atom (o LTC larga-a entretanto)."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/play")
    if erro:
        return erro
    d = _body()
    def fazer():
        r = f6_card.player.play(str(d.get("path") or ""), d.get("start") or 0)
        log_event(f"F6 card: playing {r['file']}")
        return r
    return _f6card(fazer)


@app.route('/api/devices/<id>/card/stop', methods=['POST'])
def api_dev_card_stop(id):
    """[f6] Para o som do cartao."""
    _t, _h, erro = _dev_exige(id, ("f6",), "card/stop")
    if erro:
        return erro
    return _f6card(f6_card.player.stop)


@app.route('/api/devices/<id>/clock', methods=['POST'])
def api_dev_clock(id):
    """[gopro] Hora da camara (por BLE, 1 s) contra a do bridge: serve para ver se o QR a acertou."""
    _t, cam, erro = _dev_exige(id, ("gopro",), "clock")
    if erro:
        return erro
    ok, r = cam.check_clock()
    return (jsonify({"ok": True, **r}), 200) if ok else (jsonify({"ok": False, "error": r}), 409)


def _batch_gopro_lead():
    """O avanco do QR da hora (ms): compensa o tempo que a GoPro demora a assentar a hora que le."""
    ok, err = nucleo.gofleet.set_lead(_body().get("ms"))
    return jsonify({"ok": ok, "error": err, "lead_ms": nucleo.gofleet.lead_ms}), (200 if ok else 400)


def _dev_forget(kind, cid):
    """Apaga de vez um aparelho da lista dos guardados. `kind` e o tipo do device."""
    if kind in ("mobile", "sony"):
        ok, err = (nucleo.sonys if kind == "sony" else nucleo.iphs).forget(cid)
        if ok:
            with _dev_lock:
                d = _dev_ler()
                if (d.get("alias") or {}).pop(cid, None) is not None:
                    try:
                        _dev_gravar(d)
                    except OSError:
                        pass
    else:
        with _dev_lock:
            d = _dev_ler()
            ok, err = True, None
            if kind == "canon" and cid in (d.get("saved") or {}):
                d["saved"].pop(cid)
            elif kind == "canon" and cid in (d.get("removed") or []) and cid in _CANON_ORIGEM:
                if cid not in d.setdefault("forgotten", []):
                    d["forgotten"].append(cid)
            elif kind == "bmpcc" and cid in (d.get("saved_bmpcc") or {}):
                d["saved_bmpcc"].pop(cid)
            else:
                ok, err = False, f"{cid} is not in the saved list"
            if ok:
                (d.get("alias") or {}).pop(cid, None)
                try:
                    _dev_gravar(d)
                except OSError as e:
                    ok, err = False, f"could not save devices.json: {e}"
    if ok:
        log_event(f"Devices: {kind} {cid} forgotten")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 404)


@app.route('/api/devices/<id>/forget', methods=['POST'])
def api_dev_forget(id):
    """[canon, mobile, bmpcc, sony] Apaga de vez um aparelho retirado da lista dos guardados."""
    tipo, _h, erro = _dev_exige(id, ("canon", "mobile", "bmpcc", "sony"), "forget", removido_ok=True,
                                ativo_erro=f"{id} is in service: remove it first")
    if erro:
        return erro
    return _dev_forget(tipo, _bmpcc_chave(id) if tipo == "bmpcc" else id)


def _remover_da_frota(frota, cid):
    """Tira um iPhone / GoPro / BMPCC extra: 404 se nao existe; 409 se grava ou esta para gravar (alvo, nao so running: um
    START ainda por confirmar conta) ou se um REC/STOP esta a decorrer (o lock do REC de grupo)."""
    cam = frota.get(cid)
    if cam is None:
        return jsonify({"ok": False, "error": f"no device {cid}"}), 404
    # O lock do REC so segura a verificacao (rapida): o remove de uma GoPro faz rede (POST 3 s + poll + GET 3 s) e, com o
    # lock durante isso, um STOP das outras cameras ficava preso ate ~9 s.
    with _rec_parado() as livre:
        if not livre:
            return jsonify({"ok": False, "error": "a REC/STOP is in progress: try again in a moment"}), 409
        if cam.running() or _rec_alvo(cam):
            return jsonify({"ok": False, "error": f"{cam.name} is recording: stop it first"}), 409
    ok, err = frota.remove(cid)
    if ok:
        _alvos_tirar(cid)
        save_settings()
        # O alias vai com o aparelho: o gateway volta a dar gp1..gp3, e uma GoPro nova herdava a referencia da antiga.
        # (Um iPhone ou uma Sony retirados ficam na lista dos guardados e guarda o alias: o restore devolve-o.)
        if frota not in (nucleo.iphs, nucleo.sonys):
            with _dev_lock:
                d = _dev_ler()
                if (d.get("alias") or {}).pop(cid, None) is not None:
                    try:
                        _dev_gravar(d)
                    except OSError as e:
                        log_event(f"Devices: could not clear the reference of {cid}: {e}")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


def _add_gopro(d):
    n, erro = _gp_gw_ok(d)
    if erro:
        return erro
    ok, r = nucleo.gofleet.add(d.get("mac"), d.get("model") or 1, d.get("name"), n)
    return (jsonify({"ok": True, "id": r}), 200) if ok else (jsonify({"ok": False, "error": r}), 400)


def _add_bmpcc_extra(d):
    h = str(d.get("host") or "").strip()
    if h and h.rsplit(":", 1)[0] == (nucleo.c3_bm.host() or "").rsplit(":", 1)[0]:
        return jsonify({"ok": False, "error": "that C3 is the main BMPCC's gateway"}), 400
    ok, r = nucleo.bmfleet.add(d.get("name"), h)
    return (jsonify({"ok": True, **r}), 200) if ok else (jsonify({"ok": False, "error": r}), 400)


def _config_bmpcc_extra(cid):
    """Nome e/ou endereco do C3 desta BMPCC extra: {name, host}, o que vier."""
    d = _body()
    if "host" in d:
        h = str(d.get("host") or "").strip()
        if h and h.rsplit(":", 1)[0] == (nucleo.c3_bm.host() or "").rsplit(":", 1)[0]:
            return jsonify({"ok": False, "error": "that C3 is the main BMPCC's gateway"}), 400
        if h and bmfleet_mod.parece_s3(h):
            return jsonify({"ok": False, "error": "that is an S3 gateway (GoPro), not a C3"}), 400
    if "name" in d:
        ok, err = nucleo.bmfleet.rename(cid, d.get("name"))
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
    if "host" in d:
        ok, err = nucleo.bmfleet.set_host(cid, d.get("host"))
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True})


def _add_iphone(d):
    """Mais um iPhone: nome, IP e porta (o web server da app Blackmagic Camera)."""
    ok, r = nucleo.iphs.add(d.get("name"), d.get("host"), d.get("port") or 4444)
    return (jsonify({"ok": True, "id": r}), 200) if ok else (jsonify({"ok": False, "error": r}), 400)


def _config_iphone(cam):
    """Nome / IP / porta / ligado deste iPhone (fica em iphones.json)."""
    d = _body()
    ok, err = cam.configure(d.get("host"), d.get("port"), d.get("enabled"), d.get("name"))
    return jsonify({"ok": ok, "error": err}), (200 if ok else 400)


def _add_sony(d):
    """Uma Sony pelo Camera Remote SDK: pela rede (a camara entra no Wi-Fi das cameras do Atom, ou cabo) ou por USB."""
    ok, r = nucleo.sonys.add(d.get("name"), d.get("model"), d.get("via") or "ip", d.get("host"), d.get("serial"),
                             bool(d.get("ssh")), d.get("user"), d.get("password"))
    if not ok and r == sony.TERMOS_FALTA:
        return jsonify({"ok": False, "error": r}), 409           # os avisos de licenca ainda nao foram aceites
    return (jsonify({"ok": True, "id": r}), 200) if ok else (jsonify({"ok": False, "error": r}), 400)


def _config_sony(cid):
    """Nome / modelo / ligacao / ligado desta Sony: {name, model, via, host, serial, ssh, user, password, enabled}."""
    d = _body()
    k = {a: d.get(a) for a in ("name", "model", "via", "host", "serial", "user", "password") if a in d}
    for a in ("ssh", "enabled"):
        if a in d:
            k[a] = bool(d.get(a))
    ok, err = nucleo.sonys.configure(cid, **k)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 400)


@app.route('/api/system/sony_sdk')
def api_sony_sdk():
    """O ajudante da Sony (Camera Remote SDK): {available, why, sdk, models, caps, found}. ?scan=1 procura camaras (USB e
    rede local; a lista chega em found uns segundos depois)."""
    if request.args.get("scan"):
        nucleo.sonys.procurar()
    return jsonify({"ok": True, **nucleo.sonys.resumo()})


@app.route('/api/devices/<id>/config', methods=['POST'])
def api_dev_config(id):
    """[mobile, bmpcc_extra, sony] Nome / endereco / porta / ligado: mobile {name, host, port, enabled}; BMPCC extra
    {name, host}; Sony {name, model, via, host, serial, ssh, user, password, enabled}."""
    tipo, cam, erro = _dev_exige(id, ("mobile", "bmpcc_extra", "sony"), "config")
    if erro:
        return erro
    if tipo == "sony":
        return _config_sony(id)
    return _config_iphone(cam) if tipo == "mobile" else _config_bmpcc_extra(id)


@app.route('/api/devices/<id>/tc', methods=['POST'])
def api_dev_tc(id):
    """[mobile, sony] Mobile: mede ja o desvio do timecode para o LTC (10 amostras). Sony: escreve o Time Code Preset
    (camara parada): {"tc": "HH:MM:SS:FF"}, ou sem tc o LTC de agora."""
    tipo, cam, erro = _dev_exige(id, ("mobile", "sony"), "tc")
    if erro:
        return erro
    if tipo == "sony":
        tc = _body().get("tc")
        if not tc:
            secs = nucleo.ltc_secs_now()
            if secs is None:
                return jsonify({"ok": False, "error": "no LTC jam: give the timecode"}), 409
            tc = nucleo.format_tc(secs, (state.get("_ltc_jam") or {}).get("fps") or 25)
        ok, err = cam.set_timecode(tc)
        if ok:
            log_event(f"{cam.name}: timecode preset {tc}")
        return (jsonify({"ok": True, "tc": tc}), 200) if ok else (jsonify({"ok": False, "error": err}), 409)
    ok, r = cam.check_tc()
    return (jsonify({"ok": True, **r}), 200) if ok else (jsonify({"ok": False, "error": r}), 409)


@app.route('/api/devices/<id>/scene', methods=['POST'])
def api_dev_scene(id):
    """[mobile, f6] Cena da claquete: mobile {scene} (proximos clips); F6 {name} ou {inc: true} (cena +1)."""
    tipo, cam, erro = _dev_exige(id, ("mobile", "f6"), "scene")
    if erro:
        return erro
    if tipo == "f6":
        return _f6_scene()
    ok, err = cam.set_scene(_body().get("scene"))
    if not ok:
        log_event(f"{cam.name} scene refused — {err}")
    return jsonify({"ok": ok, "error": err}), (200 if ok else 409)


@app.route('/api/devices/<id>/rec', methods=['POST'])
def api_dev_rec(id):
    """[canon, mobile, gopro, bmpcc, bmpcc_extra, sony, f6] REC/STOP so deste device, sem mexer nos alvos nem nos outros: o botao
    REC de cada linha, da consola e do USB. Passa pelo mesmo REC de grupo (fila, intencao e confirmacao) com um so
    alvo."""
    tipo, cam, erro = _dev_exige(id, None, "rec")
    if erro:
        return erro
    if tipo == "canon" and not cam.state["connected"]:
        return jsonify({"ok": False, "error": "Camera not connected"}), 409
    ok, payload = do_rec_group([id])
    return (jsonify(payload), 200) if ok else (jsonify(payload), 409)


@app.route('/api/rec', methods=['POST'])
def api_rec_selected():
    """Grava/para so as camaras seleccionadas que estiverem em modo video (as Canon so gravam em modo video)."""
    ok, payload = do_rec_group(state.get("rec_targets") or [], grupo=True)
    return (jsonify(payload), 200) if ok else (jsonify(payload), 409)


@app.route('/api/devices/<id>/iso', methods=['POST'])
def api_dev_iso(id):
    """[bmpcc] ISO da BMPCC principal: {"iso": 800}."""
    _t, _h, erro = _dev_exige(id, ("bmpcc",), "iso")
    if erro:
        return erro
    if not state["camera_connected"] or not nucleo.camera_char_path:
        return jsonify({"error": "Camera not connected"}), 409
    data = _body()
    iso_value = data.get("iso", 0)
    if not iso_value:
        return jsonify({"error": "No ISO value"}), 400
    try:
        iso_value = int(iso_value)
    except (TypeError, ValueError, OverflowError):         # OverflowError: 1e999 do JSON
        return jsonify({"ok": False, "error": "iso: a number"}), 400
    try:
        command = get_iso_command(iso_value)
        write_camera(command)
        log_event(f"ISO set to {iso_value}")
    except Exception as e:
        log_event(f"ISO write error: {e}")
        return jsonify({"ok": False, "error": f"the BMPCC did not take the ISO: {e}"}), 502
    return jsonify({"ok": True})


def _batch_canon_settings_polling():
    """Turn the Av/Tv/ISO/shuttermode/moviequality reads on or off.

    Off means the bridge stops asking the bodies for them entirely — the point
    is camera battery, since every CCAPI request is radio time. The dropdowns
    go inert in the UI to make it obvious the values are no longer live.
    """
    data = _body()
    nucleo.canon_ccapi.settings_polling = bool(data.get("on", True))
    save_settings()
    log_event("Canon settings polling " + ("ON" if nucleo.canon_ccapi.settings_polling else "OFF (saving camera battery)"))
    return jsonify({"ok": True, "on": nucleo.canon_ccapi.settings_polling})


@app.route('/api/devices/<id>/connect', methods=['POST'])
def api_dev_connect(id):
    """[canon, bmpcc] Liga a camara: a Canon pelo CCAPI; a BMPCC pelo Bluetooth (so uma tentativa de cada vez)."""
    tipo, cam, erro = _dev_exige(id, ("canon", "bmpcc"), "connect")
    if erro:
        return erro
    if tipo == "bmpcc":
        return _bmpcc_connect()
    threading.Thread(target=cam.connect, daemon=True).start()
    return jsonify({"ok": True})


@app.route('/api/devices/<id>/disconnect', methods=['POST'])
def api_dev_disconnect(id):
    """[canon, bmpcc] Desliga a camara (recusado a gravar)."""
    tipo, cam, erro = _dev_exige(id, ("canon", "bmpcc"), "disconnect")
    if erro:
        return erro
    if tipo == "bmpcc":
        return _bmpcc_disconnect()
    if cam.state.get("recording"):
        return jsonify({"ok": False, "error": f"{cam.name} is recording: stop it first"}), 409
    cam.disconnect()
    return jsonify({"ok": True})


@app.route('/api/devices/<id>/af', methods=['POST'])
def api_dev_af(id):
    """[canon] So foca. Devolve se a Canon ACEITOU o comando -- nao se focou mesmo
    (ver trigger_af() no canon_ccapi.py: o "Focus OK/NG" real da CCAPI so
    vem no stream de Live View incidental info, nao implementado aqui).
    """
    _t, cam, erro = _dev_exige(id, ("canon",), "af")
    if erro:
        return erro
    if not cam.state["connected"]:
        return jsonify({"ok": False, "error": "Camera not connected"}), 409
    ok = cam.trigger_af()
    # focus: "ok"/"ng" so quando a camera o diz (live view ligado); None nao
    # quer dizer que falhou, quer dizer que ela nao disse.
    if not ok:
        return jsonify({"ok": False, "error": "AF not accepted by the camera", "focus": None}), 502
    return jsonify({"ok": True, "focus": getattr(cam, "af_focus", None)})


@app.route('/api/devices/<id>/liveview.mjpg', methods=['GET'])
def api_dev_liveview(id):
    """[canon] O live view da Canon em MJPEG (multipart/x-mixed-replace)."""
    _t, cam, erro = _dev_exige(id, ("canon",), "liveview.mjpg")
    if erro:
        return erro
    if not cam.state["connected"]:
        return jsonify({"error": "Camera not connected"}), 409
    return Response(
        stream_with_context(cam.stream_liveview_frames()),
        mimetype=f'multipart/x-mixed-replace; boundary={nucleo.canon_ccapi.LIVEVIEW_BOUNDARY}',
    )


@app.route('/api/devices/<id>/afpoint', methods=['POST'])
def api_dev_afpoint(id):
    """[canon] Move a moldura de AF para um ponto do live view (u, v em 0..1)."""
    _t, cam, erro = _dev_exige(id, ("canon",), "afpoint")
    if erro:
        return erro
    if not cam.state["connected"]:
        return jsonify({"ok": False, "error": "Camera not connected"}), 409
    data = _body()
    if data.get("u") is None or data.get("v") is None:
        return jsonify({"ok": False, "error": "u and v required"}), 400
    try:
        out = cam.set_af_point(data["u"], data["v"])
    except Exception as e:
        return jsonify({"ok": False, "error": erro_curto(e)}), 503
    return jsonify({"ok": True, **out})


@app.route('/api/devices/<id>/afauto', methods=['POST'])
def api_dev_afauto(id):
    """[canon] Volta ao AF automatico (o metodo de antes do primeiro clique)."""
    _t, cam, erro = _dev_exige(id, ("canon",), "afauto")
    if erro:
        return erro
    if not cam.state["connected"]:
        return jsonify({"ok": False, "error": "Camera not connected"}), 409
    try:
        return jsonify({"ok": True, "method": cam.af_auto()})
    except Exception as e:
        return jsonify({"ok": False, "error": erro_curto(e)}), 503


@app.route('/api/devices/<id>/afframe', methods=['GET'])
def api_dev_afframe(id):
    """[canon] Moldura(s) de AF seleccionada(s) e recorte do live view (escala da imagem fixa)."""
    _t, cam, erro = _dev_exige(id, ("canon",), "afframe")
    if erro:
        return erro
    if not cam.state["connected"]:
        return jsonify({"ok": False, "error": "Camera not connected"}), 409
    try:
        lv = cam.lv_info()
    except Exception as e:
        return jsonify({"ok": False, "error": erro_curto(e)}), 503
    img = lv.get("image") or {}
    frames = [{k: f.get(k) for k in ("x", "y", "width", "height", "status")}
              for f in lv.get("afframe", []) if f.get("select") == 1]
    return jsonify({"ok": True,
                    "image": {k: img.get(k) for k in
                              ("positionx", "positiony", "positionwidth", "positionheight")},
                    "frames": frames})

# So caminhos dentro de /contents podem entrar no proxy abaixo -- rel_path
# vem de um parametro de query do browser, e sem isto seria um proxy aberto
# para qualquer endpoint CCAPI do corpo (incluindo os de controlo).
# Prefixo E caracteres: so letras, digitos e _ . - / (os nomes do cartao: /contents/sd/100CANON/IMG_0001.JPG). Um '..',
# um '%2e', uma barra invertida, um '?' ou um '#' deixavam sair de /contents (o requests normaliza o '..' e chegava-se a
# /shooting/control/recbutton).
CONTENT_PATH_RE = re.compile(r"^/ccapi/ver1[01]0/contents/[A-Za-z0-9_./-]*$")


def _content_path_ok(rel):
    """O caminho entra no proxy de /contents? Verifica-se tambem depois de desfazer o %-encoding (ate 3 vezes)."""
    from urllib.parse import unquote
    if not isinstance(rel, str):
        return False
    visto = rel
    for _ in range(4):
        if (not CONTENT_PATH_RE.match(visto) or ".." in visto or "//" in visto or "/./" in visto or "%" in visto
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in visto)):
            return False
        depois = unquote(visto)
        if depois == visto:
            return True
        visto = depois
    return False


@app.route('/api/devices/<id>/setting/<name>', methods=['POST'])
def api_dev_setting(id, name):
    """[canon] Muda um ajuste simples da Canon (Av, Tv, ISO, modo de obturador, qualidade de video...): {"value": ...}."""
    _t, cam, erro = _dev_exige(id, ("canon",), "setting")
    if erro:
        return erro
    if name not in nucleo.canon_ccapi.SIMPLE_SETTINGS:
        return jsonify({"error": "unknown setting"}), 404
    if not cam.state["connected"]:
        return jsonify({"error": "Camera not connected"}), 409
    # Av/Tv/ISO mexem-se a meio de um take (e uma decisao do operador); o modo de obturador e a qualidade de video
    # mudam o formato da camara e podem parar a gravacao.
    if name in ("shuttermode", "moviequality") and cam.a_gravar():
        return jsonify({"error": f"{cam.name} is recording: change {name} after the take"}), 409
    value = _body().get("value")
    if not value or not isinstance(value, (str, int, float)):
        return jsonify({"ok": False, "error": "value required"}), 400
    if not cam.set_simple_setting(name, value):
        return jsonify({"ok": False, "error": f"the camera did not take {name}"}), 502
    return jsonify({"ok": True})


@app.route('/api/devices/<id>/sync_time', methods=['POST'])
def api_dev_sync_time(id):
    """[canon] Acerta o relogio da Canon pelo do bridge (accao do operador, recusada a gravar)."""
    _t, cam, erro = _dev_exige(id, ("canon",), "sync_time")
    if erro:
        return erro
    if not cam.state["connected"]:
        return jsonify({"error": "Camera not connected"}), 409
    if cam.a_gravar():
        return jsonify({"error": f"{cam.name} is recording: set its clock after the take"}), 409
    # Explicit operator action — bypasses the trusted-clock guard.
    cam.sync_datetime(force=True)
    return jsonify({"ok": True})

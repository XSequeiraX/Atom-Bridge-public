"""A claquete e a pagina de dados da take (/api/slate...).

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import claquete
import time
from flask import jsonify, request
from nucleo import (_body, _claquete_enviado, _marcar, _nota_no_log, _take_lock, anything_recording, app, format_tc,
    log_event, log_shot, ltc_secs_at, save_settings, state, take_lock, take_prevista)


def claquete_estado():
    sl = state.get("slate") or {}
    return {"scene": sl.get("scene"), "reel": sl.get("reel"), "next_take": take_prevista(),
            "sent": dict(_claquete_enviado)}


@app.route('/api/slate/beep', methods=['POST'])
def api_slate():
    """Regista uma claquete sonora emitida pelo telemovel.

    O bip nasce no altifalante do telemovel, nao aqui: e o telemovel que esta
    junto as camaras, e a placa do Pi so tem saida de linha. Para alinhar
    camaras umas com as outras a latencia nao conta -- todas ouvem o mesmo
    clique no mesmo instante. O timecode que registamos serve para amarrar esse
    clique ao tempo absoluto, e ai ha uns milissegundos de incerteza entre o
    relogio do telemovel, o deste Pi e a latencia de audio do iOS.
    """
    data = _body()
    try:
        ms_ago = max(0.0, min(5000.0, float(data.get("ms_ago") or 0)))
    except (TypeError, ValueError, OverflowError):
        ms_ago = 0.0
    when = time.time() - ms_ago / 1000.0

    secs = ltc_secs_at(when)
    if secs is None:
        # Sem jam nao ha timecode, mas a claquete continua a servir para
        # alinhar camaras entre si -- por isso regista-se na mesma, dizendo
        # de onde veio a hora.
        tc = time.strftime("%H:%M:%S", time.localtime(when))
        source = "System clock (no LTC stored)"
    else:
        fps = (state.get("_ltc_jam") or {}).get("fps") or 25
        tc = format_tc(secs, fps)
        source = "LTC"
    log_event(f"SLATE @ {tc} [{source}]")
    log_shot("SLATE", "slate", f"{tc} ({source})")
    return jsonify({"ok": True, "tc": tc, "source": source})


# === Claquete do clip seguinte: a API (o ciclo esta junto dos takes) ===
@app.route('/api/slate', methods=['GET', 'POST'])
def api_clip_slate():
    """GET: cena, reel e o take do clip seguinte. POST {scene, reel}: muda-os (vazio tira); as camaras recebem-nos
    entre takes."""
    if request.method == 'GET':
        return jsonify({"ok": True, **claquete_estado()})
    b = _body()
    try:
        cena, reel = claquete.validar(b.get("scene"), b.get("reel"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    state["slate"] = {"scene": cena, "reel": reel}
    save_settings()
    log_event("Slate: scene %s, reel %s (the cameras get it between takes)" % (cena or "-", reel or "-"))
    return jsonify({"ok": True, **claquete_estado()})


TAKE_NOME_MAX, TAKE_TEXTO_MAX = 40, 80


def _uma_linha(txt, tecto):
    return " ".join(str(txt).split())[:tecto]


def take_info():
    """Os valores de agora da pagina de dados da take: o que vem preenchido da take anterior, e o numero da ultima e da seguinte."""
    ti = state.get("take_info") or {}
    sl = state.get("slate") or {}
    return {"name": ti.get("name") or "", "scene": sl.get("scene") or "", "reel": sl.get("reel") or "",
            "shot": ti.get("shot") or "", "last_take": int(state.get("_take_no") or 0), "next_take": take_prevista(),
            "pending": bool(state.get("_take_pendente")), "lock": take_lock()}


@app.route('/api/slate/take', methods=['GET', 'POST'])
def api_slate_take():
    """A pagina de dados da take (consola, no fim de uma gravacao). GET: o nome da sessao, a cena, o reel, a descricao do plano, a ultima
    take e a seguinte. POST {name, scene, reel, shot, good, note}, todos opcionais, so o que vier: `name` e o da sessao ou
    sequencia (um nome novo volta a numerar as takes a partir de 1; o mesmo nome continua a contar; vazio limpa o nome sem reiniciar),
    `scene` e `reel` seguem para as camaras pela claquete, `shot` fica numa nota da take seguinte, `good` ("good" | "no-good")
    marca a take que acabou e `note` fica nessa take. Recusado a gravar. Guardar (mesmo com o corpo vazio) fecha a pagina em
    todos os clientes; /api/slate/take/skip fecha-a sem guardar. Com a pagina por fechar o bridge recusa um REC novo
    (/api/slate/take/lock desliga isso)."""
    if request.method == 'GET':
        return jsonify({"ok": True, **take_info()})
    b = _body()
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: fill it in after the take stops"}), 409
    nome = _uma_linha(b["name"], TAKE_NOME_MAX) if "name" in b and b["name"] is not None else None
    shot = _uma_linha(b["shot"], TAKE_TEXTO_MAX) if "shot" in b and b["shot"] is not None else None
    nota = _uma_linha(b["note"], TAKE_TEXTO_MAX) if b.get("note") else ""
    bom = b.get("good") or ""
    if bom not in ("", "good", "no-good"):
        return jsonify({"ok": False, "error": "good: good | no-good | empty"}), 400
    ultima = int(state.get("_take_no") or 0)
    if (bom or nota) and not ultima:
        return jsonify({"ok": False, "error": "no take yet: nothing to mark or note"}), 409
    sl = state.get("slate") or {}
    try:
        cena, reel = claquete.validar(b["scene"] if "scene" in b else sl.get("scene"),
                                      b["reel"] if "reel" in b else sl.get("reel"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # Primeiro o que e da take que ACABOU (a marca e a nota ficam nela, com o nome da sessao dela); so depois o que e da seguinte.
    if bom:
        _marcar(bom)
    if nota:
        _nota_no_log(nota, str(ultima))
    ti = dict(state.get("take_info") or {})
    if nome is not None and nome != (ti.get("name") or ""):
        with _take_lock:
            if nome:                                   # um nome novo: a numeracao recomeca (vazio nao reinicia nada)
                state["_take_no"] = 0
        log_event("Take session name: %s%s" % (nome or "(empty)", " (take numbering restarts at 1)" if nome else ""))
        ti["name"] = nome
    if shot is not None:
        ti["shot"] = shot
    state["take_info"] = ti
    if state.get("slate") != {"scene": cena, "reel": reel}:
        state["slate"] = {"scene": cena, "reel": reel}
        log_event("Slate: scene %s, reel %s (the cameras get it between takes)" % (cena or "-", reel or "-"))
    save_settings()
    # O que e da take SEGUINTE (cena, reel, descricao do plano) fica numa nota dessa take, para o shoot log ter o plano.
    plano = ", ".join(x for x in (("scene %s" % cena) if cena else "", ("reel %s" % reel) if reel else "",
                                  ("shot %s" % ti.get("shot")) if ti.get("shot") else "") if x)
    seguinte = take_prevista()
    if plano and seguinte and (shot is not None or "scene" in b or "reel" in b):
        _nota_no_log("next take: " + plano, str(seguinte), fonte="TAKE")
    if state.get("_take_pendente"):
        state["_take_pendente"] = None               # preenchida: fecha em todos os clientes e o REC volta a poder comecar
        log_event("Take data saved: the recording lock is released")
    return jsonify({"ok": True, **take_info()})


@app.route('/api/slate/take/skip', methods=['POST'])
def api_slate_take_skip():
    """Salta a pagina de dados da take em todos os clientes (consola, pagina web) sem guardar nada: a take fica sem dados e o
    REC volta a poder comecar. Funciona sempre, tambem a gravar."""
    if state.get("_take_pendente"):
        state["_take_pendente"] = None
        log_event("Take data skipped: the recording lock is released")
    return jsonify({"ok": True, **take_info()})


@app.route('/api/slate/take/lock', methods=['GET', 'POST'])
def api_slate_take_lock():
    """GET: o bloqueio esta ligado? POST {"enabled": true|false}: liga ou desliga o bloqueio (ligado, um REC novo e recusado ate a
    pagina de dados da take ser guardada ou saltada; desligado, a pagina abre na mesma mas nao impede nada)."""
    if request.method == 'POST':
        b = _body()
        if not isinstance(b.get("enabled"), bool):
            return jsonify({"ok": False, "error": "enabled: true | false"}), 400
        ti = dict(state.get("take_info") or {})
        ti["lock"] = b["enabled"]
        state["take_info"] = ti
        save_settings()
        log_event("Take data lock %s" % ("ON" if b["enabled"] else "OFF"))
    return jsonify({"ok": True, "enabled": take_lock()})

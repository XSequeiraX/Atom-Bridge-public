"""O relogio: fontes, LTC, NTP, telemovel, acertar as camaras (/api/time...).

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import ltc_audio
import os
import tc1_ble
import time
from flask import jsonify, request
import nucleo
from nucleo import (CLOCK_SOURCE_LABELS, NTP_MARKER, TC_AGREE_SECONDS, _bcd, _body, _boot_id, _run,
    anything_recording, app, clock_sources, effective_clock_source, log_event, ltc_epoch_now, ltc_secs_at,
    save_settings, state, write_camera)


def ltc_jam(force=False, via="cable"):
    """Jam sync: le o LTC uma vez e guarda o desvio para o relogio do Atom.

    A partir daqui o Atom anda sozinho e o gerador pode ser desligado e levado
    para outro lado -- que e como se trabalha com timecode a serio. O que
    guardamos nao e uma hora, e a DIFERENCA entre o gerador e o relogio local;
    o relogio local, disciplinado por NTP, nao foge.
    """
    # O sinal demora a assentar depois de se ligar o cabo, e uma leitura
    # marginal falha na continuidade. Tentar aqui em vez de obrigar o operador
    # a carregar no botao tres vezes -- foi o que aconteceu ao vivo.
    # Cinco e nao tres: com o barramento USB do Pi 3B+ partilhado entre o dongle
    # wi-fi, o Bluetooth e o audio, medi cerca de 40% de capturas limpas. A
    # cinco tentativas a probabilidade de falhar todas cai para ~8%.
    r = None
    # Pelos anuncios Bluetooth dos TC-1 (tc1_ble.py): uma so leitura de ~15 s,
    # ja com mediana -- repetir nao ajuda, o que falha e a app estar fechada.
    if via == "ble":
        try:
            r = tc1_ble.read()
        except Exception as e:
            state["_ltc_error"] = str(e)
            log_event(f"GET LTC (Bluetooth) failed — {e}")
            return None, str(e)
    for tentativa in ((1, 2, 3, 4, 5) if r is None else ()):
        try:
            r = ltc_audio.read()
            break
        except Exception as e:
            state["_ltc_error"] = str(e)
            log_event(f"GET LTC: attempt {tentativa} failed — {e}")
            if tentativa < 5:
                time.sleep(1)
    if not r:
        return None, state["_ltc_error"] or "no valid LTC signal"

    lt = time.localtime(r["at"])
    sys_secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (r["at"] % 1)
    offset = (r["secs"] - sys_secs + 43200) % 86400 - 43200

    # Validamos no momento do jam, nao continuamente: um gerador em free-run le
    # uma hora plausivel que nada tem a ver com o relogio de parede, e foi assim
    # que a BMPCC ficou uma vez acertada para o dia anterior. Depois de aceite,
    # confia-se.
    ok, _ = nucleo.canon_ccapi.clock_is_trustworthy()
    if ok and not force and abs(offset) > TC_AGREE_SECONDS:
        state["_ltc_error"] = (f"LTC differs from NTP by {int(abs(offset))}s — looks free-running; "
                               f"confirm to force")
        log_event(f"GET LTC refused: {state['_ltc_error']}")
        return None, state["_ltc_error"]

    # A DATA tambem, e nao so a hora. O TC-1 manda a data nos user bits, mas a
    # app da Deity so a actualiza quando o operador a confirma -- caso
    # contrario ela repete o dia em que se mexeu na app pela ultima vez. Uma
    # data velha traz a hora do dia CERTA, por isso passava a verificacao acima
    # sem levantar nada, e o jam ficava ancorado ao dia errado. Escrever o dia
    # errado nos metadados e o erro que mais custa a desfazer em pos.
    if ok and not force and r.get("date"):
        hoje = time.localtime()
        if tuple(r["date"]) != (hoje.tm_year, hoje.tm_mon, hoje.tm_mday):
            lida = "%04d-%02d-%02d" % tuple(r["date"])
            state["_ltc_error"] = (f"LTC date is {lida}, the system clock says "
                                   f"{time.strftime('%Y-%m-%d', hoje)} — confirm the date in "
                                   f"the Deity app, or confirm to force")
            log_event(f"GET LTC refused: {state['_ltc_error']}")
            return None, state["_ltc_error"]

    state["_ltc_error"] = None
    state["_ltc_jam"] = {
        "offset": offset, "at": r["at"], "tc": r["tc"], "fps": r["fps"],
        "continuity": r["continuity"], "forced": bool(force and abs(offset) > TC_AGREE_SECONDS),
        # Para detectar saltos do relogio depois do jam: se o NTP corrigir a
        # hora, a diferenca guardada deixa de valer e o jam tem de ser refeito.
        "clock_ref": time.time() - time.monotonic(),
        "mono": time.monotonic(),
        "boot_id": _boot_id(),
        # User bits (SMPTE 309M). Os geradores usam-nos para a data, que o LTC
        # nao transporta de outra forma -- guardamos em bruto ate sabermos o
        # esquema deste Deity, que so se confirma com sinal a serio.
        "user_bits": r.get("user_bits"),
        "user_bits_stable": r.get("user_bits_stable"),
        "date": r.get("date"),
        "secs": r.get("secs"),
        "via": r.get("via", "cable"),
    }
    save_settings()
    _ub = r.get("user_bits")
    log_event(f"GET LTC{' (Bluetooth)' if r.get('via') == 'ble' else ''}: "
              f"{r['tc']} @{r['fps']}fps, offset {offset:+.2f}s"
              + (f", user bits {_ub}" if _ub and _ub != "00000000" else ""))
    return state["_ltc_jam"], None


# As quatro fontes de relogio, cada uma por si. Ate 2026-09-12 havia duas
# "familias" (timecode e hora de parede) e a escolha DENTRO de cada uma era
# automatica: o BM ganhava ao LTC quando estava ao vivo, e o telemovel ganhava
# ao NTP quando era mais recente. O operador pediu o contrario -- escolha
# explicita entre as quatro, sem nada a trocar por si:
#
#   bm       -- timecode ao vivo da BMPCC (nao se vai buscar: esta ou nao esta)
#   ltc      -- o jam LTC guardado do TC-1
#   ntp      -- relogio de parede do Atom, com proveniencia NTP
#   iphone   -- relogio de parede do Atom, com proveniencia telemovel
#
# Os dois ultimos leem o MESMO relogio fisico -- o Atom tem um so relogio de
# sistema; o que os distingue e de onde veio a ultima hora que lhe foi
# escrita. Por isso escolher "ntp" sem nunca se ter feito GET NTP e uma fonte
# que nao se tem, e nao um sinonimo de "usa a hora que tiveres ai".
CLOCK_SOURCES = ("bm", "ltc", "ntp", "iphone")


def get_clock_commands(when=None):
    """Packets to set the BMPCC's real-time clock — Configuration 7.0/7.2.

        7.0 Real Time Clock  int32  [0] time BCD HHMMSSFF (UTC)
                                    [1] date BCD YYYYMMDD
        7.2 Timezone         int32  minutes offset from UTC

    The camera wants UTC with the zone carried separately; sending local time
    would bake the offset into every clip's metadata.
    """
    import struct as _s
    lt = when or time.localtime()
    utc = time.gmtime(time.mktime(lt))
    tz_minutes = int(-time.altzone if lt.tm_isdst > 0 else -time.timezone) // 60

    rtc = bytearray(16)
    rtc[0] = 255            # destination
    rtc[1] = 12             # length: 4 header fields + 8 payload
    rtc[4] = 7              # category: Configuration
    rtc[5] = 0              # parameter: Real Time Clock
    rtc[6] = 3              # type: int32
    rtc[7] = 0              # operation: assign
    _s.pack_into('<i', rtc, 8, _bcd(time.strftime("%H%M%S", utc) + "00"))
    _s.pack_into('<i', rtc, 12, _bcd(time.strftime("%Y%m%d", utc)))

    tz = bytearray(12)
    tz[0] = 255
    tz[1] = 8               # 4 + 4 payload
    tz[4] = 7
    tz[5] = 2               # parameter: Timezone
    tz[6] = 3
    tz[7] = 0
    _s.pack_into('<i', tz, 8, tz_minutes)
    return rtc, tz, utc, tz_minutes


def sync_bmpcc_clock(force=False):
    """Push a trusted clock into the BMPCC, same priority rules as the Canons."""
    if not state.get("camera_connected") or not nucleo.camera_char_path:
        return False, "camera not connected"
    when = None
    source = None
    if not force:
        src = nucleo.canon_ccapi.time_source_hook and nucleo.canon_ccapi.time_source_hook()
        if src is not None:
            when, source = src, effective_clock_source()
        else:
            ok, why = nucleo.canon_ccapi.clock_gate()
            if not ok:
                log_event(f"BMPCC: clock NOT set — the selected source ({why}) "
                          f"has nothing to give right now")
                return False, f"no source ({why})"
            source = why
    else:
        source = "manual"
    rtc, tz, utc, tzm = get_clock_commands(when)
    try:
        write_camera(rtc)
        time.sleep(0.2)
        write_camera(tz)
    except Exception as e:
        log_event(f"BMPCC: failed to set the clock: {e}")
        return False, str(e)
    log_event(f"BMPCC: clock set to {time.strftime('%Y-%m-%d %H:%M:%S', utc)} UTC "
              f"(tz {tzm:+d}min) [source: {source}]")
    return True, source


# How long a phone-set clock stays trusted. Long enough for a shoot, short
# enough that a stale one does not quietly become authoritative next week.
PHONE_TRUST_HOURS = 12


@app.route('/api/time/phone/offer', methods=['POST'])
def api_time_offer():
    """The web UI hands us the phone's clock. Stored, never applied on its own.

    On a location with no internet the operator's phone is the only correct
    clock present — it comes off the cellular network. This just records the
    offer; adopting it is a button, because a browser can send anything.
    """
    data = _body()
    try:
        epoch = float(data.get("epoch_ms", 0)) / 1000.0
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "invalid time"}), 400
    # Sanity only: anything before 2025 or far in the future is not a clock.
    if not (1735689600 < epoch < 4102444800):
        return jsonify({"error": "implausible time"}), 400
    state["_phone_time"] = epoch
    state["_phone_time_at"] = time.time()
    state["_phone_delta"] = int(epoch - time.time())
    return jsonify({"ok": True, "delta": state["_phone_delta"]})


def ntp_age_texto():
    """Ha quanto tempo o timesyncd sincronizou mesmo, em texto.

    ntp_last_sync() vive no canon_ccapi.py -- e tambem quem decide se a
    etiqueta do badge diz "NTP" ou "telemovel" quando os dois estao validos
    ao mesmo tempo, por isso e ali que faz sentido ter uma unica fonte.
    """
    t = nucleo.canon_ccapi.ntp_last_sync()
    if not t:
        return "never this boot"
    d = max(0, int(time.time() - t))
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    return f"{d // 3600}h{(d % 3600) // 60:02d}m ago"


@app.route('/api/time/phone/adopt', methods=['POST'])
def api_time_adopt():
    """Set the Atom's clock from the phone's — operator action, never automatic."""
    if anything_recording():
        return jsonify({"error": "Recording — not touching the clock now"}), 409
    off = state.get("_phone_time")
    at = state.get("_phone_time_at", 0)
    if not off or time.time() - at > 120:
        return jsonify({"error": "no recent time from the phone"}), 400
    target = off + (time.time() - at)          # account for the delay since
    # O guarda existe para nao estragar um relogio bom sem querer, nao para
    # trancar o operador: em locacao sem internet este botao E a unica hora
    # certa do edificio. Por isso avisa com a idade do sincronismo e deixa
    # insistir -- foi a falta deste force que deixou o Pi sem saida.
    force = bool(_body().get("force"))
    ok, src = nucleo.canon_ccapi.clock_is_trustworthy()
    if ok and src == "NTP" and not force:
        delta = target - time.time()
        return jsonify({"error": "NTP says it is synced (last real contact %s) \u2014 "
                                 "the phone is %+.1fs from it. Force to use the phone anyway."
                                 % (ntp_age_texto(), delta)}), 409
    r = _run(["date", "-s", f"@{target:.3f}"], timeout=10)
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or "failed").strip()}), 500
    nucleo.canon_ccapi.manual_trust_since = time.time()
    nucleo.canon_ccapi.manual_trust_until = nucleo.canon_ccapi.manual_trust_since + PHONE_TRUST_HOURS * 3600
    # Proprio da PHONE, distinto de manual_trust_since/until -- esses sao
    # partilhados com o SET SYSTEM CLOCK FROM LTC (o mesmo balde "override
    # manual valido" para efeitos de poder escrever numa camara). O botao
    # GET PHONE TIME precisa de saber especificamente SE E QUANDO foi este
    # botao, nao aquele, que trouxe a hora -- senao acender-se-ia tambem
    # quando quem a acertou foi o LTC.
    state["_phone_adopted_at"] = nucleo.canon_ccapi.manual_trust_since
    log_event(f"System clock set from the phone: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return jsonify({"ok": True, "now": time.strftime("%Y-%m-%d %H:%M:%S")})


@app.route('/api/time/cameras/sync', methods=['POST'])
def api_clocks_sync():
    """Set every connected camera's clock, on the operator's say-so.

    Deliberately not automatic on connect: a timecode generator in free-run
    looks like a clock and is not one, and writing the wrong date into a body
    mid-shoot is worse than leaving whatever it had.
    """
    if anything_recording():
        return jsonify({"error": "Recording \u2014 not setting the camera clocks now"}), 409
    done, failed = [], []
    ok, why = sync_bmpcc_clock()
    if state.get("camera_connected"):
        (done if ok else failed).append(f"BMPCC ({why})")
    for cam in list(nucleo.canon_ccapi.CAMERAS.values()):
        if cam.state["connected"]:
            before = cam.state.get("clock_source")
            cam.sync_datetime()
            after = cam.state.get("clock_source")
            (done if after else failed).append(f"{cam.name} ({after or before or '?'})")
    if not done and not failed:
        return jsonify({"ok": False, "error": "No camera connected"}), 409
    return jsonify({"ok": True, "acertadas": done, "falhadas": failed})


@app.route('/api/time/source', methods=['POST'])
def api_clock_source():
    """Escolhe qual das quatro fontes manda. Nao faz fetch de nada.

    Recusa uma fonte que nao se tem, em vez de a aceitar e ficar em silencio
    sem relogio: o operador pediu explicitamente um alerta que o mande fazer
    o fetch primeiro. Sem esta recusa, escolher CLK->NTP num sitio onde nunca
    houve rede deixava as camaras sem serem acertadas e nada o dizia.
    """
    data = _body()
    pref = data.get("source")
    if not isinstance(pref, str):                       # uma lista/objecto nao e hashable: nao pode ir ao dict abaixo
        pref = None
    # Nomes antigos: "ltc" era o timecode, "clock" era a familia da hora de
    # parede que escolhia sozinha entre NTP e telemovel.
    pref = {"timecode": "ltc", "clock": "ntp"}.get(pref, pref)
    if pref not in CLOCK_SOURCES:
        return jsonify({"error": "source must be one of "
                                 + ", ".join(CLOCK_SOURCES)}), 400
    fontes = clock_sources()
    if not fontes[pref]["ok"]:
        etiqueta = CLOCK_SOURCE_LABELS[pref]
        comofazer = {"bm": "connect the BMPCC and wait for its timecode",
                     "ltc": "press GET LTC with the generator connected",
                     "ntp": "press GET NTP (needs an uplink)",
                     "iphone": "press GET MOBILE TIME"}[pref]
        temos = [CLOCK_SOURCE_LABELS[k] for k in CLOCK_SOURCES if fontes[k]["ok"]]
        return jsonify({"error": f"No clock from {etiqueta} — {comofazer}, or pick "
                                 f"another source."
                                 + (f" Available now: {', '.join(temos)}." if temos
                                    else " Nothing is available right now.")}), 409
    state["clock_source_pref"] = pref
    save_settings()
    log_event(f"Clock source set to {CLOCK_SOURCE_LABELS[pref]}")
    return jsonify({"ok": True, "source": pref, "effective": effective_clock_source()})


@app.route('/api/time/ntp/sync', methods=['POST'])
def api_ntp_sync():
    """Forca uma sincronizacao NTP imediata e deixa o NTP ligado.

    Desde 2026-10-07 o NTP manda: o timesyncd fica sempre a correr. Sem ele o
    relogio do Atom fugia ~33 ppm ao TC-1 e o LTC desalinhava em ~20 min.
    (Entre 2026-09-12 e essa data o relogio so mexia por botao e este botao
    desligava o timesyncd no fim -- regra revogada pelo operador.)
    """
    if anything_recording():
        return jsonify({"error": "Recording — not touching the clock now"}), 409

    r = _run(["ip", "-4", "route", "get", "1.1.1.1"], timeout=6)
    if r.returncode != 0 or not r.stdout.strip():
        return jsonify({"error": "no route to the internet — NTP cannot run and the "
                                 "current clock is left untouched. On location use "
                                 "the mobile time or SET SYSTEM CLOCK FROM LTC instead."}), 503

    # Para medir o salto e preciso uma referencia que o NTP nao mexa: qualquer
    # diferenca entre o tempo decorrido no relogio de parede e no monotonico E
    # o salto, por definicao. Medir so com time.time() contava tambem a duracao
    # do arranque do timesyncd e reportava-a como se fosse correccao do relogio.
    before_wall, before_mono = time.time(), time.monotonic()
    was_ok, _ = nucleo.canon_ccapi.clock_is_trustworthy()
    rr = _run(["timedatectl", "set-ntp", "true"], timeout=25)
    if rr.returncode == 0:
        rr = _run(["systemctl", "restart", "systemd-timesyncd"], timeout=25)
    if rr.returncode != 0:
        return jsonify({"error": (rr.stderr or "timesyncd failed to start").strip()}), 500

    try:
        marker = "/run/systemd/timesync/synchronized"
        synced_ok = False
        deadline = time.time() + 25
        while time.time() < deadline:
            if os.path.exists(marker):
                synced_ok = True
                jump = round((time.time() - before_wall)
                             - (time.monotonic() - before_mono), 1)
                # Um override manual anterior (telemovel/LTC) manda na etiqueta
                # ate expirar sozinho ou ate isto acontecer: um FORCE NTP FETCH
                # que o operador pediu e que teve sucesso a serio. So esta accao
                # explicita devolve a etiqueta ao NTP -- nunca o tempo a passar.
                nucleo.canon_ccapi.manual_trust_until = 0.0
                nucleo.canon_ccapi.manual_trust_since = 0.0
                log_event(f"Forced NTP: synced (jump {jump:+.1f}s), clock now "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
                return jsonify({"ok": True, "now": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "jump": jump, "was_synced": was_ok,
                                "server": _ntp_server(),
                                "effective": effective_clock_source()})
            time.sleep(1)
        log_event("Forced NTP: did not sync in time")
        return jsonify({"error": "did not sync within 25s — the clock kept its value but is now "
                                 "marked untrusted"}), 504
    finally:
        # NTP manda desde 2026-10-07: o timesyncd fica a correr. Ja nao se
        # desliga aqui -- era isto que voltava a soltar o relogio.
        pass
        # systemd >= 257 apaga o marcador ao parar o timesyncd; sem ele o
        # clock_is_trustworthy() acha que nunca houve sync. Repoe-no so se a
        # sincronizacao correu bem (vive em /run, desaparece no reboot).
        if synced_ok and not os.path.exists(marker):
            try:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                open(marker, "a").close()
            except OSError:
                pass


def _ntp_server():
    try:
        out = _run(["timedatectl", "show-timesync", "--property=ServerName"],
                   timeout=6).stdout.strip()
        return out.split("=", 1)[1] if "=" in out else None
    except Exception:
        return None


@app.route('/api/time/ltc/jam', methods=['POST'])
def api_ltc_jam():
    """Agarra o timecode do gerador. Bloqueia alguns segundos a gravar audio."""
    body = _body()
    force = bool(body.get("force"))
    j, err = ltc_jam(force=force, via="ble" if body.get("via") == "ble" else "cable")
    if not j:
        return jsonify({"ok": False, "error": err}), 503
    # O pylint nao consegue inferir que o `return` acima garante que j existe,
    # porque a garantia vem do par (dict|None, str|None) devolvido pelo ltc_jam.
    # pylint: disable=unsubscriptable-object
    return jsonify({"ok": True, "tc": j["tc"], "fps": j["fps"],
                    "offset": round(j["offset"], 2), "continuity": j["continuity"],
                    "user_bits": j.get("user_bits"),
                    "user_bits_stable": j.get("user_bits_stable"),
                    "effective": effective_clock_source()})


@app.route('/api/time/ltc/set_system_clock', methods=['POST'])
def api_ltc_set_pi_clock():
    """Acerta o relogio do Atom pelo gerador de timecode.

    Faz sentido em locacao sem internet, quando o relogio de sistema nao e de
    confianca (no Pi de antes, sem RTC, arrancou 1h14m errado).
    O gerador manda data e hora completas (a data vem nos user bits), por isso
    e uma fonte melhor que um palpite.

    O cuidado nao obvio: mudar o relogio dispara a deteccao de saltos e
    invalidaria o proprio jam. Como sabemos exactamente o que mudamos, o jam e
    reancorado em vez de ser deitado fora.
    """
    if anything_recording():
        return jsonify({"error": "Recording — not touching the clock now"}), 409
    j = state.get("_ltc_jam")
    e = ltc_epoch_now()
    if e is None:
        if not j:
            return jsonify({"error": "no LTC stored — press GET LTC first"}), 400
        return jsonify({"error": "the LTC reading carried no date in the user bits; "
                                 "the generator has to send it"}), 400

    force = bool(_body().get("force"))
    ok, why = nucleo.canon_ccapi.clock_is_trustworthy()
    if ok and why == "NTP" and not force:
        drift = abs(e - time.time())
        return jsonify({"error": f"NTP is synced and the clock already matches "
                                 f"({drift:.1f}s from LTC) — not touching it"}), 409

    antes = time.strftime("%Y-%m-%d %H:%M:%S")
    r = _run(["date", "-s", f"@{e:.3f}"], timeout=10)
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or "failed").strip()}), 500

    # Reancorar: depois disto o relogio do Atom E a hora do LTC, logo o desvio
    # passa a zero e a referencia de salto tem de ser refeita, senao a nossa
    # propria correccao invalidava a leitura guardada.
    #
    # O guarda parece redundante -- se `e` nao e None entao `j` existe -- mas
    # essa garantia vive dentro do ltc_epoch_now() e nao aqui. Torna-la
    # explicita custa uma linha e sobrevive a quem mexer naquela funcao.
    if not j:
        return jsonify({"error": "no LTC stored"}), 400
    now = time.time()
    lt = time.localtime(now)
    j["offset"] = 0.0
    j["at"] = now
    j["secs"] = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (now % 1)
    j["date"] = (lt.tm_year, lt.tm_mon, lt.tm_mday)
    j["clock_ref"] = now - time.monotonic()
    j["mono"] = time.monotonic()
    nucleo.canon_ccapi.manual_trust_since = now
    nucleo.canon_ccapi.manual_trust_until = now + PHONE_TRUST_HOURS * 3600
    save_settings()
    log_event(f"System clock set from LTC: {antes} -> "
              f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return jsonify({"ok": True, "before": antes,
                    "now": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "effective": effective_clock_source()})


@app.route('/api/time/ltc/clear', methods=['POST'])
def api_ltc_clear():
    state["_ltc_jam"] = None
    save_settings()
    log_event("Stored LTC cleared")
    return jsonify({"ok": True, "effective": effective_clock_source()})


@app.route('/api/time/ntp/clear', methods=['POST'])
def api_ntp_clear():
    """Esquece o NTP como fonte. NAO desacerta o relogio do Atom.

    O Atom tem um so relogio de sistema: apagar o marcador do timesyncd faz o
    bridge deixar de dizer que tem hora vinda do NTP, mas a hora que la esta
    fica exactamente a mesma. Serve para nao ficar a arrastar uma
    proveniencia de manha quando ja se esta noutro sitio -- e e o unico
    caminho de volta, porque o marcador nunca se apaga sozinho durante o boot.
    """
    if anything_recording():
        return jsonify({"error": "Recording — not touching the clock now"}), 409
    try:
        os.remove(NTP_MARKER)
    except FileNotFoundError:
        return jsonify({"ok": True, "already": True,
                        "effective": effective_clock_source()})
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    log_event("NTP forgotten as a clock source (the system clock itself is unchanged)")
    return jsonify({"ok": True, "effective": effective_clock_source()})


@app.route('/api/time/phone/clear', methods=['POST'])
def api_phone_clear():
    """Esquece o telemovel como fonte. Tambem nao desacerta o relogio do Atom.

    Limpa a janela de confianca do override manual, que e o que faz o
    clock_is_trustworthy() dizer "telemovel". A hora fica onde estava.
    """
    if anything_recording():
        return jsonify({"error": "Recording — not touching the clock now"}), 409
    state["_phone_adopted_at"] = None
    nucleo.canon_ccapi.manual_trust_since = 0.0
    nucleo.canon_ccapi.manual_trust_until = 0.0
    log_event("Mobile time forgotten as a clock source (the system clock itself is unchanged)")
    return jsonify({"ok": True, "effective": effective_clock_source()})


@app.route('/api/time/reference')
def api_time_ref():
    """A hora do bridge para a pagina do QR: `src=ltc` (a do LTC, se houver) ou `system`."""
    agora = time.time()
    lt = time.localtime(agora)
    tod, src = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (agora % 1), "system"
    if request.args.get("src", "ltc") == "ltc":
        s = ltc_secs_at(agora)
        if s is not None:
            tod, src = s, "ltc"
    return jsonify({"ok": True, "src": src, "tod": tod, "date": [lt.tm_year, lt.tm_mon, lt.tm_mday],
                    "tz_min": int(-time.timezone // 60), "dst": bool(lt.tm_isdst > 0),
                    "lead_ms": nucleo.gofleet.lead_ms})


@app.route('/api/time/ltc/send', methods=['POST'])
def api_ltc_send():
    """LTC a andar pela saida de auscultadores para um aparelho que faz jam pela entrada de audio
    (DJI Mic 3, como o F6). {"to": "DJI Mic 3", "secs": 15}"""
    import math
    if anything_recording():
        return jsonify({"ok": False, "error": "Recording — not sending timecode now"}), 409
    b = request.get_json(silent=True)
    b = b if isinstance(b, dict) else {}
    quem = str(b.get("to") or "DJI Mic 3")[:20]
    try:
        secs = float(b.get("secs") or 15)
    except (TypeError, ValueError, OverflowError):
        secs = 15.0
    secs = max(3.0, min(60.0, secs)) if math.isfinite(secs) else 15.0
    ok, err = nucleo.f6.jam(secs, quem=quem)
    if not ok:
        log_event(f"{quem} jam refused \u2014 {err}")
    return jsonify({"ok": ok, "error": err, "secs": secs}), (200 if ok else 409)

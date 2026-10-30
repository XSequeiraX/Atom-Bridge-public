"""O estado (/api/status), o sistema, a rede, os servicos, o modo de treino, os perfis e o preflight.

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import claquete
import json
import licencas
import os
import perfis
import re
import threading
import time
from flask import jsonify, request
import nucleo
from nucleo import (_take_ativos, AP_IFACE, BATTERY_WARN_PCT, CARD_WARN_SECS, DEMO_C3_PORTA, DEMO_S3_PORTA, GW_FW_PENDENTE,
    QUEDAS_JANELA, SHOOT_LOG_PATH, STALE_WARNING_HOURS, TC_FRESH_SECONDS, UPLINK_IFACE, _CONF, _alvos_lock, _body,
    _demo_hosts, _demo_tirar_do_rig, _dev_ler, _read_power, _run, adapter_mac, anything_recording, app, apply_led,
    clear_fault, clock_sources, connect_camera_dbus, desvios_tc, effective_clock_source, jam_clock_stepped,
    jam_invalid_reason, log_event, ltc_epoch_now, ltc_secs_now, ltc_time_of_day, note_fault, ntp_synced_at,
    save_settings, state, take_lock, timecode_time_of_day, uplink_ssid, web_remota, wlan_radio)


AP_CON = _CONF.get("AP_CON") or "PS_Control"


def take_pendente_info():
    """{take, since, lock} enquanto a pagina de dados da take espera por quem a preencha (ou salte), senao None. Vai no /api/status:
    a consola e a pagina web abrem-na todos, e qualquer um a fecha."""
    tp = state.get("_take_pendente")
    return {"take": tp["take"], "since": tp["since"], "lock": take_lock()} if tp else None



# === FAULT FLAG: o que a pagina e a consola leem (o resto esta no nucleo) ===
# "Something went wrong that costs a take": a body that was rolling dropped
# off, or a REC command gave up after its retries. Latched briefly rather than
# instantaneous, because the whole point is that you may be looking away at
# the moment it happens.
FAULT_WINDOW = 60


def fault_active():
    """Uma falha fica presa ate ser dispensada; um aviso expira sozinho.

    A distincao e deliberada. Um aviso descreve uma condicao que ainda e
    verdade e que volta a disparar sozinha -- bateria fraca, cartao a encher --
    por isso expirar esta certo, e prende-lo so deixava lixo velho no ecra. Uma
    falha descreve um acontecimento que ja passou, como uma take que morreu:
    desaparecer sozinha ao fim de um minuto significa nao a veres se estavas a
    olhar para a camara em vez de para o telemovel.
    """
    if not state.get("_fault_time"):
        return False
    if state.get("_fault_kind") == "fault":
        return True
    return (time.time() - state["_fault_time"]) < FAULT_WINDOW


def ltc_date_mismatch():
    """A data do gerador difere da do Atom? Devolve (data_ltc, data_pi) ou None.

    Discordarem e sinal forte de que o relogio do Atom esta errado -- aconteceu
    hoje, com o Pi a arrancar sem rede e a estimar a data. Vale a pena dize-lo
    em vez de deixar passar calado.
    """
    e = ltc_epoch_now()
    if e is None:
        return None
    a, b = time.localtime(e), time.localtime()
    if (a.tm_year, a.tm_mon, a.tm_mday) == (b.tm_year, b.tm_mon, b.tm_mday):
        return None
    return (time.strftime("%Y-%m-%d", a), time.strftime("%Y-%m-%d", b))


def bmtc_ltc_delta():
    """Segundos que o timecode da BMPCC leva de avanco sobre o LTC guardado.

    So existe com as duas fontes vivas ao mesmo tempo, e e por isso que a
    interface mostra as duas: ligar a camara promove o timecode dela a
    referencia, e sem esta conta a troca era silenciosa mesmo quando as duas
    discordam. A BMPCC so publica o timecode ao segundo inteiro, por isso a
    resolucao aqui e de um segundo -- chega para ver um desvio, nao chega para
    medir frames.
    """
    ltc = ltc_secs_now()
    if ltc is None or not state.get("camera_connected"):
        return None
    if time.time() - state.get("_last_tc_change", 0) > TC_FRESH_SECONDS:
        return None
    parts = (state.get("timecode") or "").split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    hh, mm, ss = (int(p) for p in parts)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    bm = hh * 3600 + mm * 60 + ss
    # A meia-noite pelo meio nao pode virar um desvio de 24 horas.
    return round((bm - ltc + 43200) % 86400 - 43200)

# As reservas de DHCP sao a fonte de verdade para os MAC das camaras: e la que
# o IP fixo de cada corpo esta definido. Ler daqui em vez de duplicar a lista
# significa que trocar de camara so obriga a mexer num sitio.
DHCP_RESERVATIONS = "/etc/NetworkManager/dnsmasq-shared.d/canon-cameras.conf"


def known_camera_macs():
    """MAC -> nome do corpo, a partir das reservas de DHCP."""
    ip_to_name = {cam.ip: cam.name for cam in list(nucleo.canon_ccapi.CAMERAS.values())
                  if getattr(cam, "ip", None)}
    macs = {}
    try:
        with open(DHCP_RESERVATIONS) as f:
            for line in f:
                line = line.strip()
                if not line.startswith("dhcp-host="):
                    continue
                parts = line[len("dhcp-host="):].split(",")
                if len(parts) >= 2:
                    mac = parts[0].strip().lower()
                    ip = parts[-1].strip()
                    macs[mac] = ip_to_name.get(ip, ip)
    except Exception:
        pass
    return macs


def ap_aparelhos(macs):
    """Quem sao os aparelhos ligados ao AP das camaras que nao sao camaras: [{mac, ip, name}]. O IP vem da tabela de
    vizinhos e dos leases do DHCP; o nome dos gateways e iPhones que o bridge conhece por esse IP, ou o nome que o
    aparelho deu ao DHCP; sem nada disso, "unknown"."""
    import glob
    ip_de, nome_dhcp = {}, {}
    for path in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
        try:
            with open(path) as f:
                for linha in f:
                    p = linha.split()
                    if len(p) >= 4:
                        ip_de[p[1].lower()] = p[2]
                        if p[3] != "*":
                            nome_dhcp[p[1].lower()] = p[3]
        except OSError:
            pass
    try:
        for linha in _run(["ip", "neigh", "show", "dev", AP_IFACE], timeout=5).stdout.splitlines():
            p = linha.split()
            if "lladdr" in p:
                ip_de[p[p.index("lladdr") + 1].lower()] = p[0]
    except Exception:
        pass
    por_ip = {}
    try:
        h = (nucleo.c3_bm.host() or "").split(":")[0]
        if h:
            por_ip[h] = "C3 gateway"
        for ip in list(nucleo.c3_bm.vistos):
            por_ip.setdefault(ip, "C3 gateway (not in use)")
    except Exception:
        pass
    try:
        for cam in list(nucleo.bmfleet.cams.values()):
            if cam.host:
                por_ip[cam.host.split(":")[0]] = "C3 of %s" % cam.name
    except Exception:
        pass
    try:
        for g in nucleo.gofleet.gateways():
            if g.get("host"):
                por_ip[g["host"].split(":")[0]] = "%s gateway" % (g.get("name") or "S3")
        for ip in list(nucleo.gofleet.found):
            por_ip.setdefault(ip, "S3 gateway (not added)")
    except Exception:
        pass
    try:
        for c in list(nucleo.iphs.status().values()) + list(nucleo.sonys.status().values()):
            if c.get("host"):
                por_ip[c["host"]] = c.get("name") or "Mobile"
    except Exception:
        pass
    out = []
    for mac in macs:
        ip = ip_de.get(mac)
        out.append({"mac": mac, "ip": ip, "name": por_ip.get(ip) or nome_dhcp.get(mac) or "unknown"})
    return sorted(out, key=lambda d: (d["name"] == "unknown", d["name"]))


def ap_memorizados(ligados):
    """Os aparelhos que o AP das camaras conhece mas que nao estao ligados agora: as reservas de DHCP (as camaras) e
    os que ja pediram endereco (leases). [{mac, ip, name, kind}]"""
    import glob
    ligados = set(ligados)
    out = {}
    for mac, nome in known_camera_macs().items():
        if mac not in ligados:
            out[mac] = {"mac": mac, "ip": None, "name": nome, "kind": "reserved"}
    for path in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
        try:
            with open(path) as f:
                for linha in f:
                    p = linha.split()
                    if len(p) >= 4 and p[1].lower() not in ligados:
                        m = p[1].lower()
                        nome = out.get(m, {}).get("name") or (p[3] if p[3] != "*" else "unknown")
                        out[m] = {"mac": m, "ip": p[2], "name": nome, "kind": out.get(m, {}).get("kind") or "lease"}
        except OSError:
            pass
    return sorted(out.values(), key=lambda d: (d["kind"] != "reserved", d["name"]))


def wifi_interfaces():
    """As interfaces Wi-Fi do Atom (iw dev): [{iface, type: AP|managed, ssid, channel}]. Cada uma tem a sua entrada
    em Devices > WIRELESS na consola (um AP novo aparece la sozinho)."""
    out, cur = [], None
    try:
        for linha in _run(["iw", "dev"], timeout=6).stdout.splitlines():
            t = linha.strip()
            if t.startswith("Interface "):
                cur = {"iface": t.split()[1], "type": None, "ssid": None, "channel": None}
                out.append(cur)
            elif cur is not None and t.startswith("type "):
                cur["type"] = t.split()[1]
            elif cur is not None and t.startswith("ssid "):
                cur["ssid"] = t[5:].strip()
            elif cur is not None and t.startswith("channel "):
                cur["channel"] = t.split()[1]
    except Exception:
        pass
    return out


def clock_family_state(family):
    """O estado de UMA familia, independentemente da que esta seleccionada.

    Calcular as duas em separado deixa a interface dizer se a fonte que NAO
    esta em uso tambem esta boa -- sem isto, ler o LTC com o NTP seleccionado
    nao dava sinal nenhum de que a leitura tinha resultado.
    """
    if family == "timecode":
        if timecode_time_of_day() is not None:
            return "bmtc"                # ao vivo pela BMPCC
        if ltc_time_of_day() is not None:
            return "ltc"                 # leitura guardada
        if jam_clock_stepped():
            return "tc-stepped"          # existe mas ja nao e de fiar
        return "tc-freerun" if state.get("_tc_drift") else "tc-none"
    # O relogio do Atom tanto pode vir do NTP como do telemovel; sao graus de
    # confianca diferentes e o operador tem de os distinguir.
    ok, why = nucleo.canon_ccapi.clock_is_trustworthy()
    if not ok:
        return "none"
    return "phone" if why == "telemovel" else "ntp"


def _read_cpu_temp():
    """Temperatura da CPU em C, ou None. Num x86 (o Atom) a
    zona 0 costuma ser ACPI sem leitura util, por isso procura-se a melhor."""
    import glob
    cands = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        try:
            tipo = open(z + "/type").read().strip().lower()
            t = int(open(z + "/temp").read().strip()) / 1000.0
        except Exception:
            continue
        if 0 < t < 130:
            pref = 0 if any(k in tipo for k in ("pkg", "cpu", "core", "soc")) else 1
            cands.append((pref, -t, t))
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            nome = open(h + "/name").read().strip().lower()
        except Exception:
            nome = ""
        for f in glob.glob(h + "/temp*_input"):
            try:
                t = int(open(f).read().strip()) / 1000.0
            except Exception:
                continue
            if 0 < t < 130:
                pref = 0 if any(k in nome for k in ("coretemp", "k10temp", "cpu")) else 2
                cands.append((pref, -t, t))
    if not cands:
        return None
    return int(round(sorted(cands)[0][2]))


def get_system_health():
    """Atom resource pressure — swap activity in particular is the known
    culprit behind laggy-bridge complaints on this Zero 2 W (415MB RAM),
    unrelated to any bug in the bridge itself."""
    health = {"mem_percent": None, "mem_available_mb": None, "swap_used_mb": None, "load1": None, "load_pct": None}
    try:
        meminfo = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
        mem_total = meminfo.get('MemTotal', 0)
        mem_avail = meminfo.get('MemAvailable', 0)
        swap_total = meminfo.get('SwapTotal', 0)
        swap_free = meminfo.get('SwapFree', 0)
        if mem_total:
            health["mem_percent"] = round((mem_total - mem_avail) / mem_total * 100)
        health["mem_available_mb"] = round(mem_avail / 1024)
        health["swap_used_mb"] = round((swap_total - swap_free) / 1024)
    except Exception:
        pass
    try:
        health["load1"] = round(os.getloadavg()[0], 2)
        # O load average e o numero medio de processos a correr ou a espera: so se le contra o numero de nucleos. Em
        # percentagem da capacidade (100 = os nucleos todos ocupados; acima, ha fila) diz-se logo se preocupa.
        health["load_pct"] = round(health["load1"] / (os.cpu_count() or 1) * 100)
    except Exception:
        pass
    health["soc_temp_c"] = _read_cpu_temp()
    health["power"] = _read_power()
    return health


def quedas_na_hora(qual):
    agora = time.time()
    return sum(1 for t in state.get("_quedas", {}).get(qual, [])
               if agora - t < QUEDAS_JANELA)

_cache_dados = {}

_cache_em_curso = set()

_cache_lock = threading.Lock()


def _em_cache(chave, ttl, fn):
    """O valor de fn() com no maximo `ttl` s de idade, sem prender o pedido: passado o prazo devolve o anterior e
    refresca numa thread (uma de cada vez). So a primeira leitura espera. Para o /api/status, que a pagina, a consola e
    o USB pedem varias vezes por segundo, nao correr iw, ler /sys e pastas a cada pedido no CPU fraco do Atom."""
    agora = time.time()
    with _cache_lock:
        ent = _cache_dados.get(chave)
        refrescar = ent is not None and agora - ent[0] > ttl and chave not in _cache_em_curso
        if refrescar:
            _cache_em_curso.add(chave)
    if ent is None:
        v = fn()
        with _cache_lock:
            _cache_dados[chave] = (time.time(), v)
        return v
    if refrescar:
        def correr():
            try:
                v = fn()
                with _cache_lock:
                    _cache_dados[chave] = (time.time(), v)
            except Exception:                   # pylint: disable=broad-except
                pass
            finally:
                with _cache_lock:
                    _cache_em_curso.discard(chave)
        threading.Thread(target=correr, daemon=True).start()
    return ent[1]


@app.route('/api/status', methods=['GET'])
def api_status():
    if not request.headers.get("X-Bridge-Service"):         # os servicos (USB) nao contam como alguem a ver
        state["_last_ui_poll"] = time.time()
    # Filter out internal keys starting with _
    d = {k: v for k, v in list(state.items()) if not k.startswith('_')}
    d.update(_em_cache("health", 2, get_system_health))
    # Surface the fault latch. Until now it only reached the 4-digit panel's
    # blink rate and the log, so an operator watching the phone UI had no way
    # to know a take had gone wrong.
    d["fault"] = fault_active()
    d["take_pending"] = take_pendente_info()         # a pagina de dados da take por fechar (ver /api/slate/take)
    ativos = list(_take_ativos.values())             # o take a gravar: desde quando e quantas camaras (faixa de REC da consola)
    d["take_rec"] = {"take": state.get("_take_no"), "since": min(ativos), "cameras": len(ativos)} if ativos else None
    # Idade da propria pagina, para a UI dar por si mesma desactualizada. Um
    # separador ja aberto (sobretudo o PWA no ecra principal do iPhone) nao
    # volta a ir buscar o ui.html sozinho so porque o ficheiro mudou no
    # disco -- fica a correr o JS que tinha em memoria desde que abriu, por
    # vezes horas antes. Visto ao vivo a 2026-09-11: um botao corrigido no
    # servidor continuava com o comportamento antigo porque o telefone nunca
    # tinha recarregado a pagina.
    try:
        d["ui_mtime"] = os.path.getmtime(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ui.html"))
    except OSError:
        d["ui_mtime"] = None
    d["clock_effective"] = effective_clock_source()
    d["f6"] = nucleo.f6.status()
    d["iphones"] = nucleo.iphs.status()
    d["gopros"] = nucleo.gofleet.status()
    d["bmpcc_extras"] = nucleo.bmfleet.status()
    d["sonys"] = nucleo.sonys.status()
    d["gopro_gateway"] = nucleo.gofleet.gateway()
    d["gopro_gateways"] = nucleo.gofleet.gateways()
    d["c3_gateway"] = nucleo.c3_bm.estado()
    # As duas familias em separado, para a interface poder marcar tambem a que
    # nao esta seleccionada.
    d["clock_family"] = {"timecode": clock_family_state("timecode"),
                         "clock": clock_family_state("clock")}
    j = state.get("_ltc_jam")
    _ltc_now = ltc_secs_now()
    d["ltc"] = None if not j else {
        "tc": j["tc"], "fps": j["fps"], "continuity": j["continuity"],
        "offset": round(j["offset"], 2), "forced": j.get("forced", False),
        "age": int(time.time() - j["at"]),
        "stale": (time.time() - j["at"]) > STALE_WARNING_HOURS * 3600,
        "stepped": jam_clock_stepped(),
        "invalid_reason": jam_invalid_reason(),
        "approx": j.get("approx"),
        "user_bits": j.get("user_bits"),
        "user_bits_stable": j.get("user_bits_stable"),
        "via": j.get("via", "cable"),
        "date": ("%04d-%02d-%02d" % tuple(j["date"])) if j.get("date") else None,
        "date_mismatch": ltc_date_mismatch(),
        # None e nao 0: um zero e um numero valido, e a interface desenhava com
        # ele um contador parado em 00:00:00 a debitar frames -- foi o que se
        # viu depois de um reinicio, com o jam ja invalidado.
        "now_secs": None if _ltc_now is None else round(_ltc_now, 3),
    }
    d["ltc_error"] = state.get("_ltc_error")
    # As quatro fontes, cada uma por si: a interface acende o interior de
    # todas as que tem valor e a cercadura so na escolhida. d["ntp"] e
    # d["phone"] ficam como aliases porque o /view e testes antigos ja os
    # leem -- e a mesma informacao, so com os nomes de antes.
    d["clock_sources"] = clock_sources()
    d["ntp"] = d["clock_sources"]["ntp"]
    d["phone"] = d["clock_sources"]["iphone"]
    # Segundos do dia do proprio Atom, com fraccao: alimenta o contador da UI
    # quando a fonte escolhida e o NTP.
    _n = time.time()
    _lt = time.localtime(_n)
    d["sys_secs"] = round(_lt.tm_hour * 3600 + _lt.tm_min * 60
                          + _lt.tm_sec + (_n % 1), 3)
    d["sys_date"] = time.strftime("%Y-%m-%d", _lt)
    d["bm_date"] = state.get("_bm_date")
    # De onde veio a hora do Atom: o NTP entra pelo uplink, e saber por que rede
    # torna a afirmacao verificavel em vez de um "NTP" no ar.
    _sync = ntp_synced_at()
    _ssid = _em_cache("uplink_ssid", 10, uplink_ssid)
    d["ntp_via"] = _ssid if _sync else None
    d["ntp_age"] = int(_n - _sync) if _sync else None
    d["tc_drift"] = state.get("_tc_drift", 0)
    d["tc_ltc_delta"] = bmtc_ltc_delta()
    d["tc_offsets"] = desvios_tc()
    d["demo"] = demo_ligado()
    d["code_version"] = versao_instalada()
    d["bmpcc_via"] = "c3" if nucleo.c3_bm.ativo() else "bluez"
    d["anything_recording"] = anything_recording()      # o instalador espera por isto antes de reiniciar o bridge
    d["bmpcc_address"] = nucleo.CAMERA_ADDRESS if nucleo.BMPCC_ACTIVE else None
    d["drops_1h"] = {"bmpcc": quedas_na_hora("bmpcc")}
    _mode, _freq = _em_cache("wlan_radio", 10, wlan_radio)
    d["uplink_freq"] = _freq
    d["uplink_ssid"] = _ssid
    d["wlan_mode"] = _mode
    # None quando o radio nao diz nada: "nao sei" nao e a mesma coisa que
    # "esta bem", e um aviso que grita sem certeza deixa de ser lido.
    d["uplink_24ghz"] = None if _freq is None else _freq < 3000
    d["phone_delta"] = state.get("_phone_delta")
    d["phone_fresh"] = (time.time() - state.get("_phone_time_at", 0)) < 120
    # So the badge can show a retry loop and offer to stop it, the way the
    # Canon ones do. A loop you cannot see is a loop you cannot call off.
    d["camera_auto_reconnect"] = bool(state.get("_auto_reconnect", False)) and not state["camera_connected"]
    _dd = _dev_ler()
    d["dev_order"] = _dd.get("order") or []
    d["dev_alias"] = _dd.get("alias") or {}
    d["bmpcc_active"] = nucleo.BMPCC_ACTIVE
    d["fault_reason"] = state.get("_fault_reason", "")
    d["fault_kind"] = state.get("_fault_kind", "fault")
    d["fault_sticky"] = bool(state.get("_fault_time")
                             and state.get("_fault_kind") == "fault")
    d["fault_since"] = (int(time.time() - state["_fault_time"])
                        if state.get("_fault_time") else None)
    return jsonify(d)


@app.route('/api/fault/test', methods=['POST'])
def api_fault_test():
    """Raise a fake fault so the alarm can be verified before a shoot.

    Same reasoning as pressing the test button on a smoke alarm: an indicator
    you have never seen fire is one you cannot trust. Clears itself with the
    normal FAULT_WINDOW.
    """
    kind = _body().get("kind", "fault")
    note_fault("ALARM TEST" if kind == "fault" else "WARNING TEST",
               kind="warn" if kind == "warn" else "fault")
    return jsonify({"ok": True, "kind": kind})


@app.route('/api/fault/clear', methods=['POST'])
def api_fault_clear():
    clear_fault()
    # O operador viu a falha: uma camara que caiu a gravar e nao voltou deixa de contar como a gravar.
    state["_bm_rec_incerto"] = None
    for cam in list(nucleo.bmfleet.cams.values()) + list(nucleo.gofleet.cams.values()):
        cam.rec_incerto = None
    return jsonify({"ok": True})


@app.route('/api/system/led', methods=['POST'])
def api_led():
    on = bool(_body().get("on", True))
    if not apply_led(on):
        return jsonify({"error": "Dongle LED not found"}), 404
    state["led_on"] = on
    save_settings()
    log_event(f"Dongle LED {'on' if on else 'off'}")
    return jsonify({"ok": True, "on": on})


@app.route('/api/system/reboot', methods=['POST'])
def api_system_reboot():
    """Reinicia o Atom (recusado a gravar)."""
    return _system_action("reboot")


@app.route('/api/system/shutdown', methods=['POST'])
def api_system_shutdown():
    """Desliga o Atom (recusado a gravar)."""
    return _system_action("shutdown")


def _system_action(action):
    """Reboot or power off the Atom.

    Both refuse mid-take like everything else here, and both run detached so
    the HTTP response actually reaches the phone before the box goes down —
    otherwise you press the button and cannot tell whether it took.
    """
    cmds = {"reboot": ["systemctl", "reboot"],
            "shutdown": ["systemctl", "poweroff"]}
    if action not in cmds:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    if anything_recording():
        return jsonify({"ok": False, "error": "Recording — not disconnecting anything now"}), 409

    def later():
        time.sleep(1.5)
        # Entretanto pode ter arrancado um REC (de outra pagina, da consola...): confere-se mesmo antes de agir.
        if anything_recording():
            log_event(f"{'REBOOT' if action == 'reboot' else 'SHUTDOWN'} cancelled: a recording started in the meantime")
            return
        _run(cmds[action], timeout=20)

    threading.Thread(target=later, daemon=True).start()
    log_event(f"{'REBOOT' if action == 'reboot' else 'SHUTDOWN'} requested by the operator")
    return jsonify({"ok": True, "action": action})


@app.route('/api/preflight', methods=['GET'])
def api_preflight():
    """One answer to "posso filmar?", built only from state we already hold.

    Deliberately opinionated about what counts as ready: every check here is
    something that has actually cost a take, or would.
    """
    checks = []

    def add(ok, label, detail=""):
        checks.append({"ok": bool(ok), "label": label, "detail": str(detail)})

    def nota(label, detail=""):
        """Linha informativa: conta para a leitura, nao para o veredicto.

        Existe porque as condicoes que derrubaram os links a 8 de Setembro --
        uplink em 2,4 GHz, AP no ar, um so adaptador -- nao sao erros: sao
        escolhas com consequencias, e o operador e que decide se as aceita.
        Marca-las como falha fazia o botao dizer NOT READY numa montagem
        perfeitamente legitima, e um alarme que grita sem razao deixa de ser
        lido.
        """
        checks.append({"ok": None, "label": label, "detail": str(detail)})

    bat = state.get("battery_percent", -1)
    add(state["camera_connected"] or not nucleo.BMPCC_ACTIVE,
        "BMPCC connected" if nucleo.BMPCC_ACTIVE else "BMPCC (removed from this session)",
        f"{bat}%" if bat >= 0 else "no reading")
    if state["camera_connected"] and 0 <= bat <= BATTERY_WARN_PCT:
        add(False, "BMPCC battery", f"{bat}% — below {BATTERY_WARN_PCT}%")

    for cam in list(nucleo.canon_ccapi.CAMERAS.values()):
        cb = cam.state["battery_percent"]
        add(cam.state["connected"], f"{cam.name} connected",
            f"{cb}%" if cb >= 0 else "no reading")
        if cam.state["connected"] and 0 <= cb <= BATTERY_WARN_PCT:
            add(False, f"{cam.name} battery", f"{cb}% — below {BATTERY_WARN_PCT}%")
        if cam.state["connected"]:
            # As Canon so gravam em modo video: em modo foto o REC e ignorado.
            add(bool(cam.state.get("movie_mode")), f"{cam.name} video mode",
                "in video mode" if cam.state.get("movie_mode") else "in PHOTO mode: switch the camera to video, it will not record")

    # iPhones (app Blackmagic Camera): ligados, bateria, cartao, timecode a andar.
    for _ip in nucleo.iphs.status().values():
        if not _ip.get("enabled", True):
            continue
        _nm = _ip.get("name") or "Mobile"
        _lig = bool(_ip.get("connected"))
        _bat = _ip.get("battery")
        add(_lig, f"{_nm} connected",
            (f"{_bat}%" if _bat is not None else "no reading") if _lig else "not reachable")
        if _lig and _bat is not None and 0 <= _bat <= BATTERY_WARN_PCT:
            add(False, f"{_nm} battery", f"{_bat}% \u2014 below {BATTERY_WARN_PCT}%")
        _rem = _ip.get("remaining_s")
        if _lig and _rem is not None:
            add(_rem > CARD_WARN_SECS, f"{_nm} card", f"{int(_rem) // 60} min left")
        if _lig and _ip.get("tc_frozen_s") is not None:
            add(False, f"{_nm} timecode", f"external timecode stuck for {_ip['tc_frozen_s']} s")

    # GoPro (pelo gateway ESP32-S3): ligadas, bateria, cartao.
    _gps = nucleo.gofleet.status()
    for _gw in nucleo.gofleet.gateways():
        if _gw["enabled"] and _gw["host"] and not _gw["ok"]:
            add(False, "GoPro gateway" if _gw["idx"] == 1 else f"GoPro gateway {_gw['idx']}", f"{_gw['host']} not answering")
    for _gp in _gps.values():
        _nm = _gp.get("name") or "GoPro"
        _lig = bool(_gp.get("connected"))
        _bat = _gp.get("battery")
        add(_lig, f"{_nm} connected",
            (f"{_bat}%" if _bat is not None else "no reading") if _lig else (_gp.get("why") or "not reachable"))
        if _lig and _bat is not None and 0 <= _bat <= BATTERY_WARN_PCT:
            add(False, f"{_nm} battery", f"{_bat}% \u2014 below {BATTERY_WARN_PCT}%")
        _rem = _gp.get("remaining_s")
        if _lig and _rem is not None:
            add(_rem > CARD_WARN_SECS, f"{_nm} card", f"{int(_rem) // 60} min left")

    # BMPCC extra (cada uma no seu C3): ligada e bateria.
    for _bm in nucleo.bmfleet.status().values():
        _nm = _bm.get("name") or "BMPCC"
        _lig = bool(_bm.get("connected"))
        _bat = _bm.get("battery")
        add(_lig, f"{_nm} connected",
            (f"{_bat}%" if _bat is not None else "no reading") if _lig else (_bm.get("why") or "not reachable"))
        if _lig and _bat is not None and 0 <= _bat <= BATTERY_WARN_PCT:
            add(False, f"{_nm} battery", f"{_bat}% \u2014 below {BATTERY_WARN_PCT}%")

    # Sony (Camera Remote SDK): o ajudante, ligadas, bateria, cartao.
    if nucleo.sonys.cams and not nucleo.sonys.disponivel():
        add(False, "Sony SDK", nucleo.sonys.motivo)
    for _so in nucleo.sonys.status().values():
        if not _so.get("enabled", True):
            continue
        _nm = _so.get("name") or "Sony"
        _lig = bool(_so.get("connected"))
        _bat = _so.get("battery")
        add(_lig, f"{_nm} connected",
            (f"{_bat}%" if _bat is not None else "no reading") if _lig else (_so.get("error") or "not reachable"))
        if _lig and _bat is not None and 0 <= _bat <= BATTERY_WARN_PCT:
            add(False, f"{_nm} battery", f"{_bat}% \u2014 below {BATTERY_WARN_PCT}%")
        _rem = _so.get("remaining_s")
        if _lig and _rem is not None:
            add(_rem > CARD_WARN_SECS, f"{_nm} card", f"{int(_rem) // 60} min left")

    # F6 alvo de REC sem nenhuma pista armada: o REC e recusado.
    if "f6" in (state.get("rec_targets") or []) and hasattr(nucleo.f6, "armed_any") and nucleo.f6.armed_any() is False:
        add(False, "F6 tracks", "no track armed: REC is blocked until a track is armed")

    eff = effective_clock_source()
    add(eff in ("bmtc", "ltc", "ntp", "phone"), "Time source",
        {"bmtc": "live timecode from the BMPCC", "ltc": "stored LTC reading",
         "tc-stepped": "LTC INVALID — clock stepped after it",
         "ntp": "NTP", "phone": "Mobile",
         "tc-none": "NO TIMECODE", "tc-freerun": "TIMECODE FREE-RUNNING",
         "unset": "NO SOURCE PICKED YET",
         "none": "NONE"}.get(eff, eff))

    if state["camera_connected"]:
        add(bool(state.get("slot1_present") or state.get("slot2_present")),
            "BMPCC card", state.get("remaining_time") or "")

    try:
        writable = os.access(os.path.dirname(SHOOT_LOG_PATH), os.W_OK)
    except Exception:
        writable = False
    add(writable, "Shoot log writable", SHOOT_LOG_PATH)

    # === CONDICOES DE RADIO, informativas ===
    modo, mhz = wlan_radio()
    if mhz is None:
        nota("Uplink", "no wifi on %s" % UPLINK_IFACE)
    else:
        nota("Uplink", f"{uplink_ssid() or modo or '?'} \u00b7 {mhz} MHz"
                       + (" \u00b7 SHARES THE BAND WITH BLUETOOTH" if mhz < 3000 else ""))
    try:
        info = _run(["iw", "dev", AP_IFACE, "info"], timeout=6).stdout
        no_ar = "type AP" in info
        canal = next((l.strip().split()[1] for l in info.splitlines()
                      if l.strip().startswith("channel")), "?")
    except Exception:
        no_ar, canal = False, "?"
    nota("Camera AP", f"{AP_CON} up on channel {canal}" if no_ar else f"{AP_CON} offline")
    if os.path.isdir("/sys/bus/usb/drivers/ath9k_htc"):                  # o dongle do AP (AR9271) precisa do firmware 1.4.0
        fw_ok = any(os.path.isfile(d + "/ath9k_htc/htc_9271-1.4.0.fw") for d in ("/lib/firmware", "/usr/lib/firmware"))
        add(fw_ok, "AP dongle firmware", "1.4.0" if fw_ok else "missing: apt install firmware-ath9k-htc")
    try:
        adaptadores = sorted(os.listdir("/sys/class/bluetooth"))
    except OSError:
        adaptadores = []
    nota("Link drops (1h)", f"BMPCC {quedas_na_hora('bmpcc')}")
    nota("Bluetooth adapters", f"{len(adaptadores)} \u00b7 BMPCC on {nucleo.CAMERA_ADAPTER}")
    j = state.get("_ltc_jam")
    if not j:
        nota("LTC jam", "none stored")
    else:
        idade = int(time.time() - j["at"])
        porque = jam_invalid_reason()
        nota("LTC jam", f"{j['tc']} \u00b7 {idade // 3600}h{idade % 3600 // 60:02d} old"
                        + (f" \u00b7 {porque}" if porque else ""))

    try:
        st = os.statvfs("/")
        free_mb = st.f_bavail * st.f_frsize // (1024 * 1024)
        add(free_mb > 200, "Disk space", f"{free_mb} MB free")
    except Exception:
        add(False, "Disk space", "unknown")

    # Uma falha presa e um registo do que ja aconteceu, nao uma condicao actual:
    # bloquear o preflight com ela obrigava a dispensa-la so para o verde voltar,
    # que e como se ensina a ignorar avisos. Um aviso e diferente -- descreve
    # algo que ainda e verdade agora -- e esse continua a chumbar.
    _sticky = state.get("_fault_time") and state.get("_fault_kind") == "fault"
    if _sticky:
        add(True, "Fault to dismiss",
            f"{state.get('_fault_reason', '')} (does not block shooting)")
    else:
        add(not fault_active(), "No active faults", state.get("_fault_reason", ""))
    # A alimentacao do Atom: o carregador e a bateria do portatil (antes lia a subtensao do Raspberry Pi, que o Atom
    # nao tem, e dizia sempre "ok").
    try:
        _pw = _read_power()
    except Exception:                               # pylint: disable=broad-except
        _pw = {}
    _bat = _pw.get("battery") or {}
    if _pw.get("ac") is False:
        add(not ((_bat.get("percent") or 100) <= 20), "Atom power",
            "ON BATTERY %s%%" % (_bat.get("percent") if _bat.get("percent") is not None else "?"))
    else:
        add(True, "Atom power", "charger connected" if _pw.get("ac") else "unknown")

    h = get_system_health()
    for cam in list(nucleo.canon_ccapi.CAMERAS.values()):
        if cam.state["connected"]:
            t = cam.state.get("temperature")
            add(t in (None, "normal"), f"{cam.name} temperature", t or "no reading")

    add((h.get("swap_used_mb") or 0) < 100, "Memory",
        f"{h.get('mem_percent')}% used ({h.get('mem_available_mb')}MB free), swap {h.get('swap_used_mb')}MB")
    t = h.get("soc_temp_c")
    add(t is None or t < 70, "Temperature", f"{t}\u00B0C" if t is not None else "?")

    # As informativas ficam de FORA do veredicto. Sem este filtro, um `ok:
    # None` -- que e falso em Python -- fazia o botao dizer NOT READY sempre,
    # que e o oposto do que uma linha informativa deve provocar.
    return jsonify({"ok": all(c["ok"] for c in checks if c["ok"] is not None),
                    "checks": checks})


@app.route('/api/net/status', methods=['GET'])
def api_net_status():
    """Wi-Fi picture for the recovery panel.

    A separate endpoint on purpose: it shells out to iw/nmcli, which is far too
    expensive to fold into /api/status at its 1s poll. Fetched only while the
    panel is open.
    """
    out = {"uplink_ssid": None, "uplink_signal": None, "uplink_chan": None,
           "ap_chan": None, "ap_clients": None, "ap_cams": [], "ap_others": None,
           "ap_up": False, "ap_ssid": None, "ap_devices": [], "ap_known": [], "wifi_ifaces": [], "ap_iface": AP_IFACE,
           "uplink_ip": None,
           "services": {}}
    try:
        # The uplink address, which is the one worth showing: it changes with
        # every DHCP lease at every new venue, and it is what you type into a
        # browser. The camera AP's own address is fixed at 192.168.4.1 and
        # therefore tells you nothing.
        r = _run(["ip", "-4", "-o", "addr", "show", UPLINK_IFACE], timeout=6).stdout
        for part in r.split():
            if "/" in part and part.count(".") == 3:
                out["uplink_ip"] = part.split("/")[0]
                break
    except Exception:
        pass
    try:
        r = _run(["iw", "dev", UPLINK_IFACE, "link"], timeout=6).stdout
        for line in r.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                out["uplink_ssid"] = line.split(":", 1)[1].strip()
            elif line.startswith("signal:"):
                out["uplink_signal"] = line.split(":", 1)[1].strip().split()[0]
        info = _run(["iw", "dev", UPLINK_IFACE, "info"], timeout=6).stdout
        for line in info.splitlines():
            if "channel" in line:
                out["uplink_chan"] = line.strip().split()[1]
    except Exception:
        pass
    try:
        info = _run(["iw", "dev", AP_IFACE, "info"], timeout=6).stdout
        for line in info.splitlines():
            if "channel" in line:
                out["ap_chan"] = line.strip().split()[1]
            if "type AP" in line:
                out["ap_up"] = True
            if line.strip().startswith("ssid "):
                out["ap_ssid"] = line.strip()[5:].strip()
        dump = _run(["iw", "dev", AP_IFACE, "station", "dump"], timeout=6).stdout
        macs = [ln.split()[1].lower() for ln in dump.splitlines()
                if ln.startswith("Station") and len(ln.split()) > 1]
        known = known_camera_macs()
        cams = sorted({known[m] for m in macs if m in known})
        out["ap_clients"] = len(macs)
        out["ap_cams"] = cams
        out["ap_others"] = len(macs) - sum(1 for m in macs if m in known)
        out["ap_devices"] = ap_aparelhos([m for m in macs if m not in known])
        out["ap_known"] = ap_memorizados(macs)
        agora = ap_aparelhos(macs)                  # todos os ligados agora (as camaras pelo nome da reserva)
        for o in agora:
            if o["mac"] in known:
                o["name"], o["camera"] = known[o["mac"]], True
        out["ap_now"] = agora
    except Exception:
        pass
    out["wifi_ifaces"] = wifi_interfaces()
    return jsonify(out)


# Whitelisted recovery actions. Everything here touches the radio or a service
# the shoot depends on, so all of it is refused while a camera is rolling.
NET_ACTIONS = {
    "ap_cameras":   (["nmcli", "-w", "25", "connection", "up", AP_CON, "ifname", AP_IFACE],
                     "Restarting the camera AP"),
    "restart_nm":     (["systemctl", "restart", "NetworkManager"], "Restarting NetworkManager"),
}


def _wifi_profiles():
    """Perfis wi-fi guardados que servem de uplink, com o que se ve deles agora.

    So os de modo cliente: as duas entradas PS_ sao pontos de acesso e ligar-se
    a elas aqui nao faz sentido nenhum. E devolve-se a frequencia porque a banda
    e a informacao que decide -- uma rede de 2,4 GHz divide o ar com o
    Bluetooth, e o operador tem de poder ver isso ANTES de escolher.
    """
    try:
        bruto = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=8).stdout
    except Exception:
        return []
    # Tira-se o SUFIXO em vez de cortar no primeiro ":". Um SSID pode conter
    # dois pontos, e o split pela esquerda transformava "Cabo:Ethernet" em
    # "Cabo" -- um perfil que depois nunca casava com a lista de visiveis. O
    # nmcli escapa os separadores dentro dos valores, por isso desfaz-se isso
    # a seguir.
    _SUF = ":802-11-wireless"
    nomes = [l[:-len(_SUF)].replace("\\:", ":") for l in bruto.splitlines()
             if l.endswith(_SUF)]
    visiveis = {}
    try:
        lista = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,FREQ", "device", "wifi",
                      "list", "ifname", UPLINK_IFACE], timeout=12).stdout
        for linha in lista.splitlines():
            partes = linha.rsplit(":", 2)
            if len(partes) != 3 or not partes[0]:
                continue
            ssid, sinal, freq = partes
            ssid = ssid.replace("\\:", ":")
            try:
                sinal, freq = int(sinal), int(freq.split()[0])
            except (ValueError, IndexError):
                continue
            # A mesma rede aparece uma vez por ponto de acesso; fica a mais forte.
            if ssid not in visiveis or sinal > visiveis[ssid]["signal"]:
                visiveis[ssid] = {"signal": sinal, "freq": freq}
    except Exception:
        pass
    activa = uplink_ssid(ttl=0)
    saida = []
    for nome in nomes:
        try:
            modo = _run(["nmcli", "-t", "-f", "802-11-wireless.mode",
                         "connection", "show", nome], timeout=6).stdout.strip()
        except Exception:
            modo = ""
        if modo.endswith(":ap"):
            continue
        v = visiveis.get(nome)
        saida.append({
            "name": nome,
            "visible": v is not None,
            "signal": v["signal"] if v else None,
            "freq": v["freq"] if v else None,
            "band24": (v["freq"] < 3000) if v else None,
            "active": nome == activa,
        })
    # Visiveis primeiro, e dentro dessas as mais fortes: e a ordem por que se
    # escolhe, e obriga a menos leitura de quem esta a filmar.
    saida.sort(key=lambda r: (not r["visible"], -(r["signal"] or 0), r["name"].lower()))
    return saida


@app.route('/api/net/wifi/list', methods=['GET'])
def api_net_wifi_list():
    """Redes guardadas que dao para escolher, com sinal e banda.

    A varredura NAO e automatica: o `nmcli` responde de cache e e assim que
    fica, porque varrer mexe no radio e isso ja derrubou ligacoes BLE hoje.
    Passa-se `?rescan=1` so quando o operador pede, com um botao proprio.
    """
    if request.args.get("rescan") == "1":
        if anything_recording():                # varrer mexe no radio: a meio de uma take derruba ligacoes BLE
            # A lista de sempre (sem varrer) com o erro: a pagina mostra-o e nao apaga a lista que ja tinha.
            return jsonify({"networks": _wifi_profiles(), "active": uplink_ssid(),
                            "ok": False, "error": "Recording — not touching the network now"}), 409
        try:
            _run(["nmcli", "device", "wifi", "rescan", "ifname", UPLINK_IFACE],
                 timeout=20)
            time.sleep(3)      # dar tempo aos resultados de chegarem
        except Exception:
            pass
    return jsonify({"networks": _wifi_profiles(), "active": uplink_ssid(ttl=0)})


@app.route('/api/net/wifi/connect', methods=['POST'])
def api_net_wifi_connect():
    """Liga o uplink a uma rede GUARDADA, escolhida pelo operador.

    O nome e validado contra a lista de perfis antes de chegar ao nmcli -- e
    texto vindo do cliente, e a lista de perfis e a unica autoridade sobre o que
    e legitimo. Redes novas nao entram aqui: precisam de palavra-passe, e para
    isso existe o AP PS_Setup com o portal.
    """
    nome = str(_body().get("name") or "").strip()
    if not nome:
        return jsonify({"error": "no network given"}), 400
    if nome not in {p["name"] for p in _wifi_profiles()}:
        return jsonify({"error": "not a saved network"}), 400
    if anything_recording():
        return jsonify({"error": "Recording — not touching the network now"}), 409

    def work():
        log_event(f"Switching uplink to {nome}...")
        try:
            r = _run(["nmcli", "-w", "25", "connection", "up", nome,
                      "ifname", UPLINK_IFACE], timeout=40)
            log_event(f"Uplink {nome}: "
                      + ("ok" if r.returncode == 0 else f"failed (rc={r.returncode})"))
        except Exception as e:
            log_event(f"Uplink {nome}: error {e}")

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})


@app.route('/api/net/tailscale', methods=['GET'])
def api_tailscale():
    """Estado do Tailscale, que e o caminho de acesso remoto ao Atom.

    Interessa saber isto antes de sair de ao pe do equipamento: se o tunel
    estiver em baixo, a unica forma de lhe chegar passa a ser o AP das camaras.
    Custa um shell-out, por isso e pedido a mao e nao no polling.
    """
    r = _run(["tailscale", "status", "--json"], timeout=10)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": (r.stderr or "tailscale failed").strip()[:200]}), 503
    try:
        d = json.loads(r.stdout)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"bad output: {e}"}), 500
    eu = d.get("Self") or {}
    pares = (d.get("Peer") or {}).values()
    online = sum(1 for p in pares if p.get("Online"))
    saude = [h for h in (d.get("Health") or []) if h]
    return jsonify({
        "ok": True,
        "state": d.get("BackendState"),
        "hostname": eu.get("HostName"),
        "ips": eu.get("TailscaleIPs") or [],
        "online": bool(eu.get("Online")),
        "peers_total": len(list(pares)),
        "peers_online": online,
        "health": saude,
    })


@app.route('/api/net/action', methods=['POST'])
def api_net_action():
    action = _body().get("action")
    if action == "restart_bridge":
        if anything_recording():
            return jsonify({"error": "Recording — the bridge will not restart now"}), 409
        def later():
            time.sleep(1)
            if anything_recording():                      # um REC pode ter arrancado desde o pedido
                log_event("Bridge restart cancelled: a recording started in the meantime")
                return
            _run(["systemctl", "restart", "camera-bridge"], timeout=20)
        threading.Thread(target=later, daemon=True).start()
        log_event("Restarting the bridge at the operator's request")
        return jsonify({"ok": True})
    if action not in NET_ACTIONS:
        return jsonify({"error": "unknown action"}), 400
    if anything_recording():
        return jsonify({"error": "Recording — not touching the network now"}), 409
    cmd, msg = NET_ACTIONS[action]

    def work():
        log_event(msg + "...")
        try:
            r = _run(cmd)
            log_event(f"{msg}: {'ok' if r.returncode == 0 else 'failed (rc=%d)' % r.returncode}")
        except Exception as e:
            log_event(f"{msg}: error {e}")

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})


@app.route('/api/bluetooth/reset', methods=['POST'])
def api_reset_bluetooth():
    """Restart bluetoothd, then bring the BLE devices back.

    The cure for BlueZ reaching a state nothing else clears — concretely, a
    GATT notify session left acquired by a client that died, after which every
    start_notify() is refused with org.bluez.Error.NotPermitted "Notify
    acquired" and no amount of disconnecting helps. Refuses mid-take, since it
    drops both BLE links.

    Verifica que os adaptadores VOLTARAM, em vez de assumir que voltaram.
    Visto ao vivo a 2026-09-11: `systemctl restart bluetooth` respondeu bem e
    o log dizia "bluetoothd restarted", mas nenhum adaptador se registou no
    D-Bus outra vez -- os aparelhos BLE passaram a falhar com "adapter not
    found"/"not paired" em loop, e nem um segundo reset nem um restart do
    bridge resolveram, so um reboot a serio do Atom. Sem esta verificacao o
    operador fica a carregar em botoes que nunca tiveram hipotese nenhuma.
    """
    if anything_recording():
        return jsonify({"error": "Recording — Bluetooth will not restart now"}), 409

    def espera_adaptadores(alvos, prazo):
        fim = time.time() + prazo
        em_falta = list(alvos)
        while em_falta and time.time() < fim:
            em_falta = [h for h in alvos if adapter_mac(h) is None]
            if em_falta:
                time.sleep(1)
        return em_falta

    def work():
        import subprocess
        alvos = sorted({nucleo.CAMERA_ADAPTER})
        state["_auto_reconnect"] = False
        em_falta = alvos
        for tentativa in (1, 2):
            log_event(f"Restarting bluetoothd (attempt {tentativa}/2)...")
            try:
                subprocess.run(["systemctl", "restart", "bluetooth"],
                               capture_output=True, timeout=45)
            except Exception as e:
                log_event(f"Failed to restart bluetoothd: {e}")
                return
            em_falta = espera_adaptadores(alvos, 10)
            if not em_falta:
                break
            # Nudge: o kernel ja tem o adaptador (senao nem valia a pena
            # tentar aqui — resolve_adapter so nos deu este nome porque o viu
            # em sysfs), mas o bluetoothd nao o apanhou. Um reset da interface
            # gera um evento novo de "index added" no socket mgmt, que e o
            # que o bluetoothd usa para descobrir adaptadores.
            for h in em_falta:
                _run(["hciconfig", h, "reset"], timeout=10)
            em_falta = espera_adaptadores(alvos, 5)
            if not em_falta:
                break
        if em_falta:
            log_event(f"Bluetooth restart did NOT bring back: {', '.join(em_falta)} — "
                      f"reconnecting is pointless like this. Reboot the system.")
            return
        log_event("bluetoothd restarted — reconnecting BMPCC")
        state["_auto_reconnect"] = True
        threading.Thread(target=connect_camera_dbus, daemon=True).start()

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True})



VERSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.json")

_versao_cache = {"mtime": None, "v": None}


def versao_instalada():
    """{commit, date, subject, installed} do codigo que corre (o instalador escreve-o no fim, so se o bridge novo
    arrancou), ou None num Atom ainda sem ele."""
    try:
        m = os.stat(VERSION_PATH).st_mtime
    except OSError:
        return None
    if m != _versao_cache["mtime"]:
        try:
            with open(VERSION_PATH) as f:
                v = json.load(f)
            _versao_cache.update(mtime=m, v=v if isinstance(v, dict) else None)
        except (OSError, ValueError):
            return None
    return _versao_cache["v"]


# === Actualizar do GitHub ===
def _repo_da_instalacao():
    """A pasta do clone de onde o instalador correu (repo.path, escrito por ele); sem o ficheiro (instalacao antiga), a de sempre."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo.path")) as f:
            p = f.read().strip()
        if p and os.path.isdir(os.path.join(p, ".git")):
            return p
    except OSError:
        pass
    # Sem o repo.path (instalacao antiga): procura-se um clone do bridge nas homes (o bridge corre como root: nao ha ~).
    import glob
    for p in sorted(glob.glob("/home/*/Atom-Bridge") + glob.glob("/home/*/Bridge-Control")):
        if os.path.isdir(os.path.join(p, ".git")):
            return p
    return "/home/sequeira/Atom-Bridge"


UPDATE_REPO = _repo_da_instalacao()
# O repositorio publico (sem autenticacao); o mesmo do atualizar_atom.sh. BRIDGE_REPO_URL muda-o.
UPDATE_URL = os.environ.get("BRIDGE_REPO_URL", "https://github.com/XSequeiraX/Atom-Bridge-public")

UPDATE_LOG = "/var/log/bridge-update.log"

UPDATE_UNIT = "bridge-update"


def _update_dono():
    import pwd
    return pwd.getpwuid(os.stat(UPDATE_REPO).st_uid).pw_name


def _update_git(*args, timeout=60):
    r = _run(["runuser", "-u", _update_dono(), "--", "git", "-C", UPDATE_REPO] + list(args), timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _update_a_correr():
    return _run(["systemctl", "is-active", "--quiet", UPDATE_UNIT], timeout=10).returncode == 0


@app.route('/api/system/terms', methods=['GET'])
def api_system_terms_get():
    """Os avisos de licenca (Sony, Canon, Blackmagic...) e se o operador ja os aceitou: {accepted, version, at, text}. A
    consola mostra-os antes de arrancar e a pagina na primeira visita: uma vez, nao a cada camara."""
    return jsonify(dict(licencas.estado(), ok=True, text=licencas.TEXTO))


@app.route('/api/system/terms', methods=['POST'])
def api_system_terms_post():
    """Aceita os avisos de licenca: {"accept": true}. Fica guardado (termos.json) ate o texto mudar."""
    if _body().get("accept") is not True:
        return jsonify({"ok": False, "error": "send {\"accept\": true} to accept the license notices"}), 400
    d = licencas.aceitar()
    log_event("License notices accepted by the operator")
    return jsonify({"ok": True, "accepted": True, "version": d["version"], "at": d["at"]})


@app.route('/api/system/update', methods=['GET'])
def api_system_update_check():
    if request.args.get("log"):
        try:
            with open(UPDATE_LOG, encoding="utf-8", errors="replace", newline="") as f:   # newline="": o \r nao vira \n
                # As barras de progresso do apt e do curl redesenham a mesma linha com \r e o registo traz cores
                # (ESC[...): limpa-se aqui para o ecra receber texto simples. A consola limpa tambem.
                bruto = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]", "", f.read()[-60000:])
                linhas = [(l.split("\r")[-1] if l.split("\r")[-1].strip() else l.split("\r")[0]).rstrip()
                          for l in bruto.split("\n")][-120:]
        except OSError:
            linhas = []
        return jsonify({"ok": True, "running": _update_a_correr(), "log": linhas})
    if not os.path.isdir(os.path.join(UPDATE_REPO, ".git")):
        return jsonify({"ok": False, "error": f"no git clone at {UPDATE_REPO}"}), 500
    try:
        # Um clone que veio de outro repositorio (o antigo, privado) passa a seguir o publico, como o atualizar_atom.sh faz.
        _update_git("remote", "set-url", "origin", UPDATE_URL)
        rc, out = _update_git("fetch", "--quiet", "origin", "main", timeout=90)
        if rc:
            return jsonify({"ok": False, "error": "git fetch failed: " + out[-300:]}), 502
        _, atual = _update_git("log", "-1", "--format=%h %s")
        _, clone = _update_git("rev-parse", "--short", "HEAD")
        _, novos = _update_git("log", "--format=%h %s", "HEAD..origin/main")
        if _update_git("merge-base", "--is-ancestor", "HEAD", "origin/main")[0]:
            # A historia do repositorio e outra: nao ha lista de commits que se leia; o update passa a segui-lo e
            # guarda o que houver de local num ramo de reserva (atualizar_atom.sh).
            novos = "(the repository changed: the update follows the public one; nothing local is lost)"
        _, sujo = _update_git("status", "--porcelain", "--untracked-files=no")
    except Exception as e:                                  # pylint: disable=broad-except
        return jsonify({"ok": False, "error": str(e)}), 500
    inst = versao_instalada() or {}
    # O clone pode estar a frente do que corre: o git pull fez-se e o instalador falhou (ou nunca correu). Antes dizia
    # "Already up to date" na mesma, e parecia que o Atom nao actualizava.
    pendente = bool(inst.get("commit")) and clone.strip() != inst.get("commit")
    return jsonify({"ok": True, "repo": UPDATE_REPO, "current": atual, "installed": inst or None,
                    "install_pending": pendente,
                    "new": [l for l in novos.splitlines() if l.strip()],
                    "local_changes": bool(sujo.strip()), "running": _update_a_correr()})


@app.route('/api/system/update', methods=['POST'])
def api_system_update():
    import shlex
    if anything_recording():
        return jsonify({"error": "Recording \u2014 not updating now"}), 409
    if _update_a_correr():
        return jsonify({"error": "an update is already running"}), 409
    try:
        open(UPDATE_LOG, "w").close()       # o registo recomeca vazio: so se ve esta actualizacao
    except OSError:
        pass
    r = shlex.quote(UPDATE_REPO)
    cmd = ("set -e; echo; echo \"== $(date '+%F %T') update from GitHub\"; "
           # um git que morreu a meio (corte de energia) deixa o index.lock: so se apaga sem nenhum git a correr
           "if [ -f " + r + "/.git/index.lock ] && ! pgrep -x git >/dev/null; then rm -f " + r + "/.git/index.lock; fi; "
           # atualizar_atom.sh --so-puxar: avanca em fast-forward e, se a historia do repositorio mudou, o clone passa a
           # segui-la (guardando antes as alteracoes locais); o git pull --ff-only falhava nesse caso
           "runuser -u " + shlex.quote(_update_dono()) + " -- bash " + r + "/atualizar_atom.sh --so-puxar; "
           "bash " + r + "/atom-install/install_atom.sh; echo \"== $(date '+%F %T') done\"")
    p = _run(["systemd-run", "--unit", UPDATE_UNIT, "--collect", "--quiet",
              "-p", "StandardOutput=append:" + UPDATE_LOG, "-p", "StandardError=append:" + UPDATE_LOG,
              "bash", "-c", cmd], timeout=20)
    if p.returncode:
        return jsonify({"error": "could not start the update: " + (p.stderr or p.stdout).strip()[-300:]}), 500
    b = _body()
    if b.get("then_gateways"):                  # e depois grava estes gateways (o bridge ja reiniciado)
        ids = b["then_gateways"] if isinstance(b["then_gateways"], list) else None
        try:
            tmp = GW_FW_PENDENTE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ids": ids, "em": time.time()}, f)
            os.replace(tmp, GW_FW_PENDENTE)
        except OSError as e:
            log_event(f"Gateway firmware after the update: not saved ({e})")
    log_event("Update from GitHub started by the operator (the bridge restarts during it)")
    return jsonify({"ok": True})


# === Perfis do rig (perfis.py) ===
def _perfil_atual():
    sl = state.get("slate") or {}
    return {"rec_targets": list(state.get("rec_targets") or []), "strict_rec": bool(state.get("strict_rec")),
            "slate": {"scene": sl.get("scene"), "reel": sl.get("reel")},
            "c3_enabled": nucleo.c3_bm.ligado(),
            "s3_enabled": {str(g["idx"]): bool(g["enabled"]) for g in nucleo.gofleet.gateways()}}


def _perfil_aplicar(p):
    """Aplica um perfil. Devolve as notas (o que nao se aplicou e porque)."""
    notas = []
    valid = {"bmpcc", "f6"} | set(nucleo.canon_ccapi.CAMERAS.keys()) | set(nucleo.iphs.ids()) | set(nucleo.gofleet.ids()) | set(nucleo.bmfleet.ids()) | set(nucleo.sonys.ids())
    alvos = [t for t in (p.get("rec_targets") or []) if t in valid]
    faltam = [t for t in (p.get("rec_targets") or []) if t not in valid]
    if faltam:
        notas.append("not in this rig now: " + ", ".join(faltam))
    with _alvos_lock:
        state["rec_targets"] = alvos
    state["strict_rec"] = bool(p.get("strict_rec"))
    sl = p.get("slate") or {}
    try:
        cena, reel = claquete.validar(sl.get("scene"), sl.get("reel"))
        state["slate"] = {"scene": cena, "reel": reel}
    except ValueError as e:
        notas.append(str(e))
    save_settings()
    if "c3_enabled" in p and bool(p["c3_enabled"]) != nucleo.c3_bm.ligado():
        nucleo.c3_bm.set_ligado(bool(p["c3_enabled"]))
    for idx, on in (p.get("s3_enabled") or {}).items():
        g = nucleo.gofleet.gw(idx)
        if not g:
            notas.append("S3 %s is not in this rig" % idx)
        elif g.enabled != bool(on):
            ok, err = g.set_enabled(bool(on))
            if not ok:
                notas.append("S3 %s: %s" % (idx, err))
    return notas


@app.route('/api/profiles', methods=['GET', 'POST'])
def api_profiles():
    """GET: os perfis e o estado actual. POST {"save": nome} guarda o actual; {"apply": nome} aplica (recusado a
    gravar); {"delete": nome} apaga."""
    if request.method == 'GET':
        return jsonify({"ok": True, "profiles": perfis.listar(), "current": _perfil_atual()})
    b = _body()
    try:
        if b.get("save"):
            nome = perfis.guardar(b["save"], _perfil_atual())
            log_event(f"Rig profile saved: {nome}")
            return jsonify({"ok": True, "saved": nome})
        if b.get("delete"):
            perfis.apagar(str(b["delete"]))
            log_event(f"Rig profile deleted: {b['delete']}")
            return jsonify({"ok": True})
        if b.get("apply"):
            p = perfis.obter(str(b["apply"]))
            if p is None:
                return jsonify({"ok": False, "error": "no such profile"}), 404
            if anything_recording():
                return jsonify({"ok": False, "error": "recording: change the profile after the take"}), 409
            notas = _perfil_aplicar(p)
            log_event("Rig profile applied: %s%s" % (b["apply"], " (%s)" % "; ".join(notas) if notas else ""))
            return jsonify({"ok": True, "notes": notas, "current": _perfil_atual()})
    except KeyError:
        return jsonify({"ok": False, "error": "no such profile"}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": False, "error": "save | apply | delete"}), 400

_demo = {"srv": [], "gw": None, "bm": None}

_demo_lock = threading.Lock()


def demo_ligado():
    return bool(_demo["srv"])


def demo_ligar():
    import simulador
    with _demo_lock:
        if demo_ligado():
            return True, None
        if anything_recording():
            return False, "recording: start the demo after the take"
        s3, c3 = _demo_hosts()
        try:
            _demo["srv"] = [simulador.servir(simulador.S3Sim(2, "DEMO-"), DEMO_S3_PORTA),
                            simulador.servir(simulador.C3Sim("DEMO-BM"), DEMO_C3_PORTA)]
        except OSError as e:
            demo_desligar_servidores()
            return False, "could not start the simulated gateways: %s" % e
        _demo_tirar_do_rig()                          # restos de uma vez anterior
        ok, n = nucleo.gofleet.add_gateway(s3, "DEMO")
        if ok:
            _demo["gw"] = n
            nucleo.gofleet.gw(n).start()
        ok2, r = nucleo.bmfleet.add("DEMO-BM", c3)
        if ok2:
            _demo["bm"] = r["id"]
        log_event("Demo mode ON: simulated GoPros DEMO-1, DEMO-2 (gateway DEMO) and BMPCC DEMO-BM joined the rig")
        return True, None if (ok and ok2) else "demo started with problems: %s %s" % (
            "" if ok else n, "" if ok2 else r)


def demo_desligar_servidores():
    for srv in _demo["srv"]:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:                             # pylint: disable=broad-except
            pass
    _demo["srv"] = []


def demo_desligar():
    with _demo_lock:
        if not demo_ligado():
            return True, None
        s3, c3 = _demo_hosts()
        demo_a_gravar = any(c.running() for c in list(nucleo.gofleet.cams.values()) if c.fleet.host == s3) or \
            any(c.running() for c in list(nucleo.bmfleet.cams.values()) if c.host == c3)
        if demo_a_gravar:
            return False, "a DEMO camera is recording: stop the take first"
        _demo_tirar_do_rig()
        demo_desligar_servidores()
        _demo.update(gw=None, bm=None)
        log_event("Demo mode OFF: the DEMO cameras left the rig")
        return True, None


# Servicos que se podem desligar para poupar o Atom: a pagina para os outros aparelhos (o servidor fica, a consola
# e o USB falam com ele) e o servico a parte do USB (controladores).
SERVICOS = {"usb": "bridge-usb"}


def _servicos_estado():
    d = {"web": web_remota()}
    for k, unidade in SERVICOS.items():
        d[k] = _run(["systemctl", "is-active", "--quiet", unidade], timeout=10).returncode == 0
    return d


@app.route('/api/services', methods=['GET', 'POST'])
def api_services():
    """GET: {web, usb} ligados ou nao. POST {web|usb: true|false}: liga ou desliga (fica assim nos arranques
    seguintes). Desligar a pagina a partir de outro aparelho corta-o a ele: so pela consola se volta a ligar."""
    if request.method == 'POST':
        b = _body()
        if anything_recording() and any(k in b and not b[k] for k in ("web", "usb")):
            # Desligar a pagina ou os controladores USB a meio de um take tira ao operador o STOP.
            return jsonify({"ok": False, "error": "Recording — not turning services off now",
                            "services": _servicos_estado()}), 409
        for k in [k for k in ("web", "usb") if k in b]:
            on = bool(b[k])
            if k == "web":
                state["web_remote"] = on
                save_settings()
            else:
                r = _run(["systemctl", "enable" if on else "disable", "--now", SERVICOS[k]], timeout=30)
                if r.returncode:
                    return jsonify({"ok": False, "error": "%s: %s" % (SERVICOS[k], (r.stderr or r.stdout).strip()[-200:]),
                                    "services": _servicos_estado()}), 500
            log_event("%s turned %s" % ({"web": "Web page (other devices)",
                                         "usb": "USB controllers service"}[k], "ON" if on else "OFF"))
    return jsonify({"ok": True, "services": _servicos_estado()})


@app.route('/api/demo', methods=['GET', 'POST'])
def api_demo():
    """GET: {"on"}. POST {"on": true|false}: liga/desliga o modo de treino."""
    if request.method == 'POST':
        on = bool(_body().get("on"))
        ok, err = demo_ligar() if on else demo_desligar()
        if not ok:
            return jsonify({"ok": False, "error": err, "on": demo_ligado()}), 409
        return jsonify({"ok": True, "on": demo_ligado(), "note": err})
    return jsonify({"ok": True, "on": demo_ligado()})

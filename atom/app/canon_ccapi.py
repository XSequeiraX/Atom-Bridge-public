#!/usr/bin/env python3
"""
Canon CCAPI client — a submodule of camera_bridge.py, independent from its
BMPCC BLE control code.

Talks to Canon EOS bodies (R10, R6 Mark II, ...) over Canon's native
Camera Control API once Wi-Fi / CCAPI is enabled and activated on the
camera (requires the one-time CCAPI Activation Tool over USB, then
Menu > Network settings > Connect to app > Advanced connection >
Camera Control API).

Endpoints below were verified live against a Canon EOS R10
(firmware 1.8.0) reachable at http://<ip>:8080/ccapi — plain HTTP,
NOT HTTPS, on port 8080. The camera's own "connect device to camera"
screen shows the exact base URL to use; if a body reports a different
scheme/port, override it in CAMERAS below.

Recording has no dedicated GET status endpoint (event/polling never
carries a recording boolean, as seen on the bodies we tested), so
"recording" state is tracked locally based on the actions we send —
mirrors what the physical record button would do.
"""

import os
import time
import threading
import requests
import urllib3

from curto import curto

# Camera bodies that only support HTTPS (e.g. the R6 II) use CCAPI's
# self-signed cert, so sessions below run with verify=False by design —
# silence the resulting per-request InsecureRequestWarning noise in the
# journal rather than suppressing it ad hoc at every call site.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REQUEST_TIMEOUT = 6
# `shooting/control/af` action:start NAO solta o motor de foco sozinho --
# fica activo ate um action:stop explicito (visto ao vivo 2026-09-16: sem
# isto o AF ficava preso "a focar" para sempre). Tempo para o deixar tentar
# focar antes de o soltar.
AF_SETTLE_SECONDS = 1.0


def ntp_last_sync():
    """Epoch do ultimo toque em /var/lib/systemd/timesync/clock, ou 0.

    CUIDADO: nao e "a ultima sincronizacao a serio" apesar do nome do
    ficheiro. Confirmado ao vivo a 2026-09-11: um `date -s` sem nada de NTP
    por tras tambem lhe bate o mtime, sem nenhuma linha no journal do
    timesyncd -- e um `date -s` e exactamente o que CLK->IPHONE e
    set_pi_clock_from_ltc fazem. Serve para a idade mostrada no botao FORCE
    NTP FETCH (onde um pequeno erro nao importa); NAO server para decidir se
    o NTP e mais novo que um override manual -- ver clock_is_trustworthy(),
    que tentou isso e comparava o override com o proprio efeito colateral.
    """
    try:
        return os.path.getmtime("/var/lib/systemd/timesync/clock")
    except OSError:
        return 0


def clock_is_trustworthy():
    """Is this Atom's clock worth writing into a camera body?

    sync_datetime() makes the Atom the time source for every Canon it connects
    to, and on a shoot there is usually no internet. Without this check the Atom
    that booted with no RTC and never reached NTP would confidently stamp a
    wrong date onto every clip's metadata — and onto shoot_log.csv, which
    exists precisely to line multicam footage up in post. Writing nothing and
    leaving the body's own clock alone is strictly better than writing a time
    we know we cannot vouch for.

    Returns (ok, source).
    """
    # systemd-timesyncd only creates este marcador uma vez sincronizado, e
    # NUNCA MAIS o apaga ate ao proximo boot -- diz "ja sincronizei alguma
    # vez", nao diz "esta certo agora". Por isso nao chega para escolher a
    # ETIQUETA quando um override manual (telemovel/LTC) tambem esta valido:
    # via CLK->IPHONE numa locacao sem rede, o ficheiro continuava ali de uma
    # sincronizacao de horas antes, e o badge da UI mostrava "NTP" a verde
    # mesmo sendo o telemovel a mandar no relogio agora. NOTE: se este box
    # mudar para chrony/ntpsec, este ficheiro deixa de existir.
    ntp_ok = os.path.exists("/run/systemd/timesync/synchronized")
    phone_ok = time.time() < manual_trust_until
    if not ntp_ok and not phone_ok:
        return False, "unsynced"
    if phone_ok and not ntp_ok:
        return True, "telemovel"
    if ntp_ok and not phone_ok:
        return True, "NTP"
    # Os dois validos ao mesmo tempo: o override manual GANHA SEMPRE, pela
    # duracao inteira da sua janela de confianca -- nunca por o relogio ter
    # avancado uns segundos ou umas horas. NUNCA trocar sozinho para "NTP":
    # o operador carregou num botao precisamente porque nao confiava no NTP
    # naquele momento (normalmente por nao ter rede), e um trinco de arranque
    # que nunca se apaga nao pode reverter essa decisao por trás das costas
    # dele. So um FORCE NTP FETCH explicito, ou o fim das 12h de confianca,
    # devolvem a etiqueta ao NTP. Ja tentei um meio-termo (comparar
    # timestamps, depois uma janela de 90s) e os dois trocavam sozinhos --
    # visto ao vivo a 2026-09-11, com o operador sem ter tocado em nada.
    return True, "telemovel"

EVENT_POLLING_TIMEOUT = 20  # ver110/event/polling blocks server-side until something changes
EVENT_POLLING_READ_TIMEOUT = EVENT_POLLING_TIMEOUT + REQUEST_TIMEOUT  # margin over the server's own block
EVENT_POLL_ERROR_BACKOFF = 3  # avoid spinning tight if the endpoint errors out instantly
# Floor on one full poll cycle (long-poll + refresh_status). event/polling
# returns the instant anything changes, and while recording the camera's
# own remainingtime ticks every second — without this floor that turns into
# refresh_status()'s ~9 HTTP round-trips every second, per body, over TLS on
# the R6 II. Costs at most this much staleness on a real event.
EVENT_POLL_MIN_CYCLE = 1.5
# ...and a much slower floor for when nothing is going on. Measured: with
# nothing recording and nobody watching the UI, the bridge was still doing ~5
# requests/second to the bodies forever, because event/polling returns
# immediately rather than blocking, so every cycle hit the fast floor. That is
# camera radio time, and therefore battery, spent on readings no one reads.
EVENT_POLL_IDLE_CYCLE = 10.0
# While we believe a body is recording, re-verify it every so often. `recording`
# is otherwise inferred purely from the commands we send, so a camera that stops
# ON ITS OWN — full card, overheating, flat battery — leaves the bridge showing
# a take that is not happening, and REC ALL will not stop what is not running.
#
# Baixado de 20 para 4 em 2026-09-08. A 20 ciclos isto dava ~30s de indicador a
# mentir, e o caso que motivou a mudanca nao e a camara falhar sozinha: e o
# operador parar a gravacao no proprio corpo da maquina. Trinta segundos a olhar
# para um REC que ja nao existe e tempo a mais numa rodagem. O custo sao dois
# GETs e uma pausa de 1,5s dentro do ciclo, de seis em seis segundos, e so
# enquanto se julga estar a gravar -- quando nao esta, o EVENT_POLL_IDLE_CYCLE
# continua a poupar a camara na mesma.
RECORDING_VERIFY_EVERY = 4    # poll cycles; ~6s ao piso rapido, ~7,5s com a sonda

# Optional hook: callable() -> bool, "someone is actually looking". The bridge
# wires this to "a UI client polled us recently"; recording always counts as
# active regardless.
activity_hint = None

# Optional hook: callable() -> time.struct_time, a time source that outranks
# the Atom's own clock. The bridge wires this to the BMPCC's LTC timecode.
time_source_hook = None
# Diz de ONDE veio a hora que o time_source_hook devolveu, para o registo nao
# atribuir a fonte errada. Sem isto o log dizia sempre "BMPCC timecode".
time_source_label = None
# Optional hook: callable() -> (ok, etiqueta). Substitui o
# clock_is_trustworthy() como portao das escritas quando o bridge tem uma
# fonte de relogio escolhida a mao (desde 2026-09-12, quatro fontes
# explicitas). E ele que impede o fallback silencioso: com CLK->BM escolhido
# e a camara em baixo, o time_source_hook devolve None e sem este portao a
# hora de parede do Atom era escrita nas camaras como se nada fosse.
clock_gate_hook = None


def clock_gate():
    """(ok, etiqueta) para decidir se se escreve o relogio numa camara."""
    if clock_gate_hook is not None:
        try:
            return clock_gate_hook()
        except Exception:
            pass
    return clock_is_trustworthy()

# Set by the bridge when the operator adopts the time from a phone on the
# camera AP. At a venue with no internet that phone is the only correct clock
# in the building, and without this the guard would keep refusing to stamp the
# cameras even after the Atom had been set from it.
manual_trust_until = 0.0
# Quando o override manual comecou — para clock_is_trustworthy() saber se e
# mais novo ou mais velho que a ultima sincronizacao NTP a serio, e nao so se
# os dois "ainda estao validos" (o que nao diz qual dos dois manda agora).
manual_trust_since = 0.0
# refresh_status() used to re-read all five shooting settings every cycle, but
# Av/Tv/ISO/shuttermode/moviequality do not change on their own — only when the
# operator turns a dial or the shooting mode shifts. Reading them every time
# was 5 of the 9 HTTP round-trips per cycle, and on a camera body every one of
# those is Wi-Fi radio time and therefore battery. They now refresh every Nth
# cycle, plus immediately whenever movie mode flips (which changes what the
# camera will accept, so the ability lists really do move).
SETTINGS_REFRESH_EVERY = 6

# Operator switch: stop reading the changeable settings from the bodies at all.
# Av/Tv/ISO/shuttermode/moviequality are only worth polling if you intend to
# change them from the bridge; during a shoot where the dials are set on the
# camera itself, those reads are pure Wi-Fi radio time on a battery. Turning
# this off removes them entirely instead of merely rate-limiting them.
settings_polling = True
DROP_RETRY_SECONDS = 5
RECONNECT_BACKOFF_SECONDS = 10
ENTER_MOVIE_MODE_RETRY_SECONDS = 0.5
ENTER_MOVIE_MODE_MAX_ATTEMPTS = 6  # ~3s of retrying while the camera finishes the photo->movie transition
LIVEVIEW_BOUNDARY = "ccapiliveview"  # must match the Content-Type boundary set by the Flask route

# Simple {"value": ..., "ability": [...]} GET / {"value": ...} PUT settings:
# AV, TV, ISO and shutter mode (elec.
# 1st-curtain / mechanical / fully electronic-silent). "ability" lists
# exactly what the camera will currently accept (it shrinks to [] when the
# setting can't be changed in the current shooting mode/lens), so the UI is
# driven off it instead of a hardcoded list of values. Maps name -> CCAPI
# version, since shuttermode only exists under ver110.
SIMPLE_SETTINGS = {
    "av": "ver100", "tv": "ver100", "iso": "ver100", "shuttermode": "ver110",
    "moviequality": "ver100",  # ability is [] outside movie mode — same "n/a until valid" UI handling as the rest
}

# Errors that mean "camera is unreachable", as opposed to e.g. a 4xx for an
# endpoint the current shooting mode/model doesn't support.
_CONNECTION_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

# Optional hook the main bridge sets to observe confirmed recording
# start/stop transitions (e.g. for its own shoot log), without this module
# importing back into camera_bridge.py.
on_recording_change = None  # callable(camera_name: str, recording: bool)

# Optional hook for "something went wrong that costs a take" — a body that was
# rolling dropped off, or a REC command gave up after its retries. The bridge
# uses it to flag the fault on the 4-digit panel.
on_fault = None  # callable(reason: str)

# Optional hook for a body's thermal state changing to anything but normal.
on_temperature = None  # callable(camera_name: str, status: str)


def _fault(reason):
    if on_fault:
        try:
            on_fault(reason)
        except Exception:
            pass


class CanonCamera:
    """One Canon EOS body reachable over CCAPI."""

    def __init__(self, name, ip, port=8080, use_https=False):
        self.name = name
        self.ip = ip
        self.port = port
        self.base = f"{'https' if use_https else 'http'}://{ip}:{port}"
        self._session = requests.Session()
        self._session.verify = False
        self._lock = threading.Lock()
        self._auto_reconnect = True
        # Optimistic until proven otherwise; flips to False for good on the
        # first sign this body/firmware doesn't implement ver110's
        # event/polling, so we don't retry a known-404 every cycle.
        self._event_polling_supported = True
        # Bumped on every connect() so a stale _event_poll_loop thread from a
        # previous connection (still blocked in its long-poll GET when a
        # quick disconnect+reconnect happens) recognizes it's superseded and
        # exits, instead of running forever alongside the new one.
        self._poll_generation = 0
        self._refresh_count = 0
        self._verify_count = 0
        self._rec_cmd_seq = 0           # sobe a cada REC/STOP mandado: a verificacao periodica nao contradiz uma ordem
        # O CCAPI e de um fio so: duas chamadas em simultaneo dao 503 e uma das ordens perde-se. Por isso REC/STOP
        # passam um de cada vez (_cmd_lock) e cada pedido tem um numero (_rec_pedido): o que foi ultrapassado por
        # outro mais recente nao se executa, nem os reenvios do START depois de um STOP.
        self._cmd_lock = threading.Lock()
        self._ped_lock = threading.Lock()
        self._rec_pedido = 0
        self.rec_incerto = None      # caiu a gravar: conta como a gravar ate voltar e se confirmar (ou 30 min)
        self.state = {
            "connected": False,
            "connecting": False,
            # Mirrors _auto_reconnect so the UI can tell "off" from "retrying".
            # A body that dies mid-shoot leaves this True and the retry timer
            # running every 10s; the operator needs a way to call that off.
            "auto_reconnect": False,
            "recording": False,
            "movie_mode": False,
            "battery_percent": -1,
            "model": "",
            "remaining_time": None,      # seconds left to record (movie mode)
            "temperature": None,         # CCAPI 4.4.7 devicestatus/temperature
            "last_event": "Idle",
            "log": [],
        }
        for setting in SIMPLE_SETTINGS:
            self.state[setting] = None
            self.state[f"{setting}_options"] = []

    def a_gravar(self):
        """A gravar, ou caiu a gravar e ainda nao voltou (a camara continua a rodar: o REC seguinte tem de a parar)."""
        incerto = self.rec_incerto
        return bool(self.state["recording"]) or bool(incerto and time.time() - incerto < 1800)

    def _log(self, msg):
        msg = curto(msg)
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.state["log"].append(entry)
        if len(self.state["log"]) > 50:
            self.state["log"] = self.state["log"][-50:]
        self.state["last_event"] = msg
        print(f"[Canon:{self.name}] {entry}", flush=True)

    def _get(self, path, timeout=REQUEST_TIMEOUT):
        r = self._session.get(f"{self.base}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _get_raw(self, path, timeout=REQUEST_TIMEOUT):
        """Like _get() but doesn't parse the body — for callers that only
        care that the request succeeded, not what it returned."""
        r = self._session.get(f"{self.base}{path}", timeout=timeout)
        r.raise_for_status()
        return r

    def _post(self, path, body=None):
        r = self._session.post(f"{self.base}{path}", json=body or {}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _put(self, path, body=None):
        r = self._session.put(f"{self.base}{path}", json=body or {}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json() if r.content else {}

    def connect(self):
        with self._lock:
            if self.state["connected"] or self.state["connecting"]:
                return
            self.state["connecting"] = True
        self._auto_reconnect = True
        self.state["auto_reconnect"] = True
        self._log("Connecting...")
        try:
            info = self._get("/ccapi/ver100/deviceinformation")
            self.state["model"] = info.get("productname", self.name)
            if self.rec_incerto:
                # Caiu a gravar: antes de a dar por ligada, ver se continua a gravar (senao o bridge dizia "parou
                # enquanto nao se via" e o REC seguinte arrancava o grupo em vez de o parar).
                a_rodar = self._probe_recording()
                self.state["recording"] = a_rodar
                self.rec_incerto = None
                self._log("Back after the drop: %s" % ("STILL RECORDING" if a_rodar else "not recording"))
            self.state["connected"] = True
            self._log(f"Connected ({self.state['model']}, fw {info.get('firmwareversion', '?')})")
            # The clock is NOT set here any more. It is an operator action: a
            # free-running timecode generator reads like a wall clock and is
            # not one, and silently writing the wrong date into a body is worse
            # than leaving the one it had.
            self.refresh_status()
            self._start_event_loop()
        except Exception as e:
            self.state["connected"] = False
            self._log(f"Connect failed: {e}")
        finally:
            self.state["connecting"] = False

    def sync_datetime(self, force=False):
        """Push this Atom's clock to the camera.

        The camera has no NTP client of its own; this makes the Atom's clock
        the time source instead. Runs automatically on every connect(); also
        callable on demand from the UI mid-session.

        The automatic path refuses to write a clock we cannot vouch for (see
        clock_is_trustworthy). `force=True` is the operator pressing the sync
        button in the UI — that is an explicit assertion that the clock is
        right, so it always goes through.
        """
        if not self.state["connected"]:
            return
        # Priority: the shoot's own timecode first, then NTP, then nothing.
        now = None
        source = None
        if time_source_hook is not None:
            try:
                now = time_source_hook()
                if now is not None:
                    source = ((time_source_label and time_source_label())
                              or "timecode")
            except Exception:
                now = None
        if now is None:
            if not force:
                ok, src = clock_gate()
                if not ok:
                    self._log(f"Clock NOT synced — the selected clock source "
                              f"({src}) has nothing to give right now. Camera "
                              f"clock left alone.")
                    return
                source = src
            else:
                source = "manual"
            now = time.localtime()
        self.state["clock_source"] = source
        # %z must come from a real localtime struct: a struct_time we built by
        # hand (the timecode path does) carries no UTC offset, and strftime
        # then emits +0000 — right wall clock, wrong timezone, an hour of drift
        # in the metadata.
        dt_str = (time.strftime("%a, %d %b %Y %H:%M:%S ", now)
                  + time.strftime("%z", time.localtime()))
        try:
            self._put("/ccapi/ver100/functions/datetime", {"datetime": dt_str, "dst": now.tm_isdst > 0})
            self._log(f"Clock synced to {dt_str} [source: {source}]")
        except Exception as e:
            self._log(f"Clock sync failed: {e}")

    def _reset_shot_state(self):
        """Clear per-connection state that's stale/meaningless once
        disconnected — otherwise e.g. the Av/Tv/ISO dropdowns keep showing
        the last-known values (just greyed out) instead of "n/a" until a
        fresh connect() repopulates them."""
        self.state["recording"] = False
        self.state["movie_mode"] = False
        self.state["remaining_time"] = None
        for setting in SIMPLE_SETTINGS:
            self.state[setting] = None
            self.state[f"{setting}_options"] = []

    def disconnect(self):
        """Desliga a serio -- fecha a ligacao HTTP, nao so o estado local.

        Sem isto, o _event_poll_loop pode ficar preso ate
        EVENT_POLLING_READ_TIMEOUT (~26s) dentro do long-poll do
        event/polling, mesmo depois de "Disconnected" aparecer no log.
        A CCAPI so aceita UM cliente ligado de cada vez -- documentado:
        "Only one opposing device can be connected with a Canon camera.
        Operation is not assured when multiple opposing devices are
        connected simultaneously." -- por isso esse pedido zombie ocupa a
        unica vaga da camara e entala qualquer tentativa de religar ate
        expirar sozinho. Fechar a sessao aqui faz o pedido em curso abortar
        de imediato (ConnectionError, ja apanhado no loop via
        _CONNECTION_ERRORS), e uma sessao nova evita reutilizar um adaptor
        HTTP que acabou de ser fechado.
        """
        self._auto_reconnect = False
        self.state["auto_reconnect"] = False
        self.state["connected"] = False
        self._reset_shot_state()
        try:
            self._session.close()
        except Exception:
            pass
        self._session = requests.Session()
        self._session.verify = False  # certificado self-signed (R6 II via HTTPS)
        self._poll_generation += 1
        self._log("Disconnected")

    def _handle_drop(self):
        if not self.state["connected"]:
            return
        was_recording = self.state["recording"]
        self.state["connected"] = False
        self._reset_shot_state()
        if was_recording:
            self.rec_incerto = time.time()
            _fault(f"{self.name} dropped while recording")
        if self._auto_reconnect:
            self._log(f"Connection lost — will auto-reconnect in {DROP_RETRY_SECONDS}s...")
            threading.Timer(DROP_RETRY_SECONDS, self._try_reconnect).start()
        else:
            self._log("Connection lost")

    def _try_reconnect(self):
        if not self._auto_reconnect or self.state["connected"] or self.state["connecting"]:
            return
        self._log("Auto-reconnecting...")
        self.connect()
        if not self.state["connected"] and self._auto_reconnect:
            self._log(f"Reconnect failed — retrying in {RECONNECT_BACKOFF_SECONDS}s...")
            threading.Timer(RECONNECT_BACKOFF_SECONDS, self._try_reconnect).start()

    def refresh_status(self):
        if not self.state["connected"]:
            return
        # Cheap reachability probe — also doubles as a live model check.
        # A connection-level failure here means the camera actually dropped
        # off the network, so it drives auto-reconnect. Any other failure
        # below (e.g. 4xx for an endpoint this model/mode doesn't support)
        # is tolerated and simply leaves that piece of state stale.
        try:
            self._get("/ccapi/ver100/deviceinformation")
        except _CONNECTION_ERRORS:
            self._log("Lost contact with camera")
            self._handle_drop()
            return
        except Exception:
            pass

        # Prefer ver110's numeric batterylist; fall back to ver100's enum level
        try:
            bl = self._get("/ccapi/ver110/devicestatus/batterylist")
            level = bl.get("batterylist", [{}])[0].get("level")
            if str(level).isdigit():
                self.state["battery_percent"] = int(level)
        except Exception:
            try:
                batt = self._get("/ccapi/ver100/devicestatus/battery")
                mapping = {"full": 100, "high": 75, "half": 50, "low": 25, "empty": 5}
                self.state["battery_percent"] = mapping.get(batt.get("level"), -1)
            except Exception:
                pass
        mode_changed = False
        try:
            mm = self._get("/ccapi/ver100/shooting/control/moviemode")
            was = self.state["movie_mode"]
            self.state["movie_mode"] = mm.get("status") == "on"
            mode_changed = (was != self.state["movie_mode"])
        except Exception:
            pass
        # Overheat. CCAPI 4.4.7 reports the body's thermal restriction state,
        # which is the R6 II's known way of losing a long take — and it warns
        # BEFORE it starts degrading, which is the whole value.
        try:
            t = self._get("/ccapi/ver100/devicestatus/temperature").get("status")
            if t != self.state["temperature"]:
                old_t = self.state["temperature"]
                self.state["temperature"] = t
                if t and t != "normal":
                    self._log(f"Temperature: {t}")
                    if on_temperature:
                        try:
                            on_temperature(self.name, t)
                        except Exception:
                            pass
                elif old_t and old_t != "normal":
                    self._log("Temperature back to normal")
        except Exception:
            pass

        try:
            rec = self._get("/ccapi/ver100/shooting/information/recordable")
            self.state["remaining_time"] = rec.get("remainingtime")
        except Exception:
            pass
        self._refresh_count += 1
        due = (settings_polling
               and (mode_changed
               or self._refresh_count == 1
               or self._refresh_count % SETTINGS_REFRESH_EVERY == 0
               or any(self.state[k] is None for k in SIMPLE_SETTINGS)))
        if due:
            for setting, ver in SIMPLE_SETTINGS.items():
                try:
                    d = self._get(f"/ccapi/{ver}/shooting/settings/{setting}")
                    self.state[setting] = d.get("value")
                    self.state[f"{setting}_options"] = d.get("ability", [])
                except Exception:
                    pass

    def _start_event_loop(self):
        """Kick off the long-poll loop on its own thread, once per connect().

        No-op when this body already proved it doesn't implement ver110's
        event/polling — the module-level watchdog() covers those with its
        old fixed-interval refresh_status() instead.
        """
        if not self._event_polling_supported:
            return
        self._poll_generation += 1
        threading.Thread(target=self._event_poll_loop, args=(self._poll_generation,), daemon=True).start()

    def _event_poll_loop(self, generation):
        """Block on ver110/event/polling instead of refreshing on a fixed
        5s timer — CCAPI's event/polling holds the connection open
        server-side for up to EVENT_POLLING_TIMEOUT and returns as soon as
        something changes (or on its own timeout), so one blocking GET here
        replaces many idle refresh_status() cycles.

        Uses _get_raw() rather than _get() — the polling response's own
        payload isn't parsed for the changed fields (CCAPI's exact event
        shape hasn't been verified against a live camera, and getting that
        wrong would silently show stale state), so there's no reason to
        risk a body-parsing error on a call whose result is discarded
        anyway. Every time the long-poll returns for any reason (real event
        or its own timeout), refresh_status() runs once to pull
        authoritative values via the already-verified per-field GETs. Net
        effect: still correct, and GETs now fire at most every ~20s of
        idle time instead of a fixed 10-GETs-every-5s — parsing the payload
        directly to cut that further is a possible later optimization once
        the field names are confirmed live.

        `generation` pins this thread to the connect() call that started
        it — if a disconnect+reconnect happens while this thread is still
        blocked inside the GET below, the newer connect() bumps
        _poll_generation, so this (now-stale) thread exits on its next
        check instead of running forever alongside the new one.
        """
        first = True
        while (self.state["connected"] and self._event_polling_supported
               and generation == self._poll_generation):
            cycle_start = time.monotonic()
            try:
                self._get_raw("/ccapi/ver110/event/polling", timeout=EVENT_POLLING_READ_TIMEOUT)
            except _CONNECTION_ERRORS:
                self._log("Lost contact with camera")
                self._handle_drop()
                return
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    self._event_polling_supported = False
                    self._log("event/polling not supported by this body — falling back to fixed-interval refresh")
                    return
                time.sleep(EVENT_POLL_ERROR_BACKOFF)
                continue
            except Exception:
                time.sleep(EVENT_POLL_ERROR_BACKOFF)
                continue
            if first:
                self._log("event/polling active")
                first = False
            if not self.state["connected"] or generation != self._poll_generation:
                return
            self.refresh_status()
            # Rate-limit the cycle. Fast while it matters — recording, or a UI
            # client is watching — and much slower otherwise, so an idle rig
            # stops draining the camera for nothing.
            # Periodic reality check on a take we think is running.
            if self.state["recording"]:
                self._verify_count += 1
                if self._verify_count >= RECORDING_VERIFY_EVERY:
                    self._verify_count = 0
                    self._verificar_gravacao()
            else:
                self._verify_count = 0

            cycle = EVENT_POLL_MIN_CYCLE
            if not self.state["recording"] and activity_hint is not None:
                try:
                    if not activity_hint():
                        cycle = EVENT_POLL_IDLE_CYCLE
                except Exception:
                    pass
            leftover = cycle - (time.monotonic() - cycle_start)
            if leftover > 0:
                time.sleep(leftover)

    def set_simple_setting(self, name, value):
        """PUT one of av/tv/iso/shuttermode. Devolve se a camara aceitou."""
        ver = SIMPLE_SETTINGS.get(name)
        if not ver or not self.state["connected"]:
            return False
        try:
            self._put(f"/ccapi/{ver}/shooting/settings/{name}", {"value": value})
            self.state[name] = value
            self._refresh_count = 0   # re-read the abilities on the next cycle
            self._log(f"{name.upper()} set to {value}")
            return True
        except Exception as e:
            self._log(f"Set {name.upper()} failed: {e}")
            return False

    def start_recording(self):
        """So grava se a camara ja estiver FISICAMENTE em modo video.

        Nunca troca o modo por software -- pedido explicito do operador
        2026-09-16: "nao quero tudo dos dois mundos ao mesmo tempo". O
        modo e escolhido na maquina; o bridge le-o e obedece. Isto e a
        rede de seguranca do lado dos comandos (REC ALL, consola, iPad); a UI
        tem o seu proprio controlo de acesso (botao REC desactivado
        quando a camara nao esta em modo video).
        """
        pedido = self._novo_pedido()
        with self._cmd_lock:
            if pedido != self._rec_pedido:
                self._log("REC superseded by a newer command — skipped")
                return
            self._rec_cmd_seq += 1
            if not self.state["connected"]:
                return
            if not self.state["movie_mode"]:
                self._log("REC ignored — the camera is not in video mode: switch it to video on the body")
                return
            if self._start_recbutton(log_failure=False):
                return
        threading.Timer(ENTER_MOVIE_MODE_RETRY_SECONDS, self._retry_start_recbutton, args=(1, pedido)).start()

    def _novo_pedido(self):
        with self._ped_lock:
            self._rec_pedido += 1
            return self._rec_pedido

    def _probe_recording(self):
        """Best-effort "is this body actually rolling?".

        CCAPI has no endpoint that answers this (event/polling never
        carries a recording boolean), which is why `recording` is otherwise
        inferred from the commands we send. But in movie mode
        shooting/information/recordable.remainingtime counts down while
        recording, so two samples a second and a half apart settle it.

        Exists because a recbutton POST whose reply is lost leaves the camera
        rolling while we believe it idle — seen for real: an R10 recording with
        the bridge showing stopped, so REC ALL would not stop it, and the body
        also refused to leave movie mode ("During shooting or recording").
        """
        try:
            a = self._get("/ccapi/ver100/shooting/information/recordable").get("remainingtime")
            if a is None:
                return False
            time.sleep(1.5)
            b = self._get("/ccapi/ver100/shooting/information/recordable").get("remainingtime")
            return b is not None and b < a
        except Exception:
            return False

    def _verificar_gravacao(self):
        """A verificacao periodica de uma take que se julga a correr: a camara parou sem o bridge?"""
        seq = self._rec_cmd_seq
        # A prova demora ~1,5 s: um STOP do operador nesse intervalo fazia-a dizer "parou sozinha" (falha falsa no
        # shoot log). So conta se nenhuma ordem foi mandada entretanto e ainda se julga a gravar.
        if not self._probe_recording() and seq == self._rec_cmd_seq and self.state["recording"]:
            self.state["recording"] = False
            self._log("Camera stopped recording on its own")
            _fault(f"{self.name} stopped recording on its own")
            if on_recording_change:
                try:
                    on_recording_change(self.name, False)
                except Exception:
                    pass

    def _mark_recording(self, msg):
        self.state["recording"] = True
        self._log(msg)
        if on_recording_change:
            try:
                on_recording_change(self.name, True)
            except Exception:
                pass

    def _start_recbutton(self, log_failure=True):
        """Best-effort recbutton start. Returns success.

        Split out from start_recording() because the camera can still be
        mid photo->movie transition (mirror/sensor/viewfinder switch) right
        after moviemode turns on, and rejects recbutton with a 503 until
        that finishes.
        """
        self._rec_cmd_seq += 1
        try:
            self._post("/ccapi/ver100/shooting/control/recbutton", {"action": "start"})
            self.state["recording"] = True
            self._log("Recording START")
            if on_recording_change:
                try:
                    on_recording_change(self.name, True)
                except Exception:
                    pass
            return True
        except Exception as e:
            # The command may well have reached the camera with only the reply
            # lost. Check before reporting failure, otherwise we retry against
            # a body that is already rolling and end up leaving it recording
            # behind our back.
            if self._probe_recording():
                self._mark_recording("Recording START (confirmed by probe — reply was lost)")
                return True
            if log_failure:
                self._log(f"Record start failed: {e}")
            return False

    def _retry_start_recbutton(self, attempt, pedido):
        # Um STOP (ou um novo START) depois deste pedido cancela os reenvios: senao o reenvio ligava a camara
        # depois de o operador a ter mandado parar.
        with self._cmd_lock:
            if pedido != self._rec_pedido:
                return
            if not self.state["connected"] or self.state["recording"] or not self.state["movie_mode"]:
                return
            if self._start_recbutton(log_failure=False):
                return
            if attempt >= ENTER_MOVIE_MODE_MAX_ATTEMPTS:
                # Last check before declaring failure: if it is in fact rolling,
                # saying "failed" would strand it recording with nothing tracking it.
                if self._probe_recording():
                    self._mark_recording("Recording START (was rolling all along)")
                    return
                self._log("Could not start recording automatically — try REC again")
                _fault(f"{self.name} REC start gave up")
                return
        threading.Timer(ENTER_MOVIE_MODE_RETRY_SECONDS, self._retry_start_recbutton, args=(attempt + 1, pedido)).start()

    def stop_recording(self):
        pedido = self._novo_pedido()
        with self._cmd_lock:
            if pedido != self._rec_pedido:
                self._log("STOP superseded by a newer command — skipped")
                return
            self._stop_recording_locked()

    def _stop_recording_locked(self):
        if not self.state["connected"]:
            return
        self._rec_cmd_seq += 1
        try:
            self._post("/ccapi/ver100/shooting/control/recbutton", {"action": "stop"})
            self.state["recording"] = False
            self._log("Recording STOP")
            if on_recording_change:
                try:
                    on_recording_change(self.name, False)
                except Exception:
                    pass
        except Exception as e:
            # Como no START: o comando pode ter chegado e so a resposta se perdeu. Se ja nao grava, parou.
            if self.state["connected"] and not self._probe_recording():
                self.state["recording"] = False
                self._log("Recording STOP (confirmed by probe — reply was lost)")
                if on_recording_change:
                    try:
                        on_recording_change(self.name, False)
                    except Exception:
                        pass
                return
            self._log(f"Record stop failed: {e}")
            _fault(f"{self.name} REC stop failed")
            return
        # Ja NAO se sai do movie mode aqui. So se entrava por forca do
        # software (start_recording() ligava moviemode); sem essa forca de
        # entrada, forcar a saida tambem deixa de fazer sentido -- o modo e
        # sempre escolhido na maquina, fisicamente, nunca pelo bridge.

    def _exit_movie_mode(self, log_failure=True):
        """Best-effort leave movie mode (used by force idle). Returns success."""
        if not self.state["movie_mode"]:
            return True
        try:
            self._post("/ccapi/ver100/shooting/control/moviemode", {"action": "off"})
            self.state["movie_mode"] = False
            return True
        except Exception as e:
            # Camera is likely still busy finalizing the clip.
            if log_failure:
                self._log(f"Still finishing movie mode exit: {e}")
            return False

    def force_idle(self):
        """Drag a body out of a stuck recording / movie-mode state.

        For the desync where the camera is rolling but our state says it is
        not (a recbutton reply that got lost): recbutton stop is accepted even
        when we believe it is already stopped, and only once it really stops
        will moviemode off succeed — until then the body answers
        "During shooting or recording". Seen for real on the R10.
        """
        self._log("Force idle: stopping recording and leaving movie mode")
        # Com o lock dos comandos (nao se cruza com um START/STOP) e a anular os reenvios de START que ainda estejam a
        # espera; se o bridge julgava que gravava, o STOP passa pelo mesmo caminho dos outros (fecha a linha no shoot log).
        with self._cmd_lock:
            self._novo_pedido()
            self._rec_cmd_seq += 1
            era_a_gravar = bool(self.state["recording"])
            try:
                self._post("/ccapi/ver100/shooting/control/recbutton", {"action": "stop"})
            except Exception as e:
                self._log(f"Force idle: recbutton stop said {e}")
            self.state["recording"] = False
            if era_a_gravar and on_recording_change:
                try:
                    on_recording_change(self.name, False)
                except Exception:
                    pass
        for _ in range(4):
            time.sleep(1.5)
            with self._cmd_lock:
                if self._exit_movie_mode(log_failure=False):
                    break
        self.refresh_status()
        self._log(f"Force idle done — movie_mode={self.state['movie_mode']}")

    def trigger_af(self):
        """So foca, nao dispara -- CCAPI 4.8.8, `POST .../shooting/control/af`
        com action:start. O operador dispara a seguir independentemente do
        resultado -- o retorno aqui e so se a Canon ACEITOU o comando, NAO se
        focou de verdade. O "Focus OK/NG" real da propria CCAPI so vem
        embutido no stream binario de Live View incidental info, que
        exige live view activo e um parser proprio a parte -- nao existe
        aqui. Se a Canon devolver 503 "Device busy"/"Out of focus" etc. isso
        conta como falha por agora.

        action:start por si so NAO solta o motor de foco -- fica "a focar"
        indefinidamente ate um action:stop, por isso manda-se sempre um
        stop a seguir a um curto AF_SETTLE_SECONDS. Sem isto o AF ficava
        activo para sempre (visto ao vivo 2026-09-16).
        """
        self.af_focus = None
        if not self.state["connected"]:
            return False
        try:
            self._post("/ccapi/ver100/shooting/control/af", {"action": "start"})
            # O Focus OK/NG real so vem na informacao do live view (CCAPI 5.2).
            # O estado e um campo de bits: o nibble alto e o tipo de moldura
            # (0x10 normal, 0x20 rosto, 0x30 seguimento) e o baixo o foco
            # (1 OK, 2 NG). Medido na R6 II em foto: 0x10 -> 0x11 (OK) ou 0x12
            # (NG), decidido em 0,8-1,2 s. Com live view le-se ate 2 s e solta-se
            # assim que a camera decide; sem live view nao ha nada a ler e
            # espera-se o tempo de sempre. Em video (servo) ela nunca decide e
            # af_focus fica None.
            info = True
            t0 = time.monotonic()
            while time.monotonic() - t0 < (max(AF_SETTLE_SECONDS, 2.0) if info
                                           else AF_SETTLE_SECONDS):
                time.sleep(0.25)
                if not info:
                    continue
                try:
                    foc = [f.get("status") & 0x0F for f in self.lv_info().get("afframe", [])
                           if f.get("select") == 1 and isinstance(f.get("status"), int)]
                except Exception:
                    info = False
                    continue
                if 1 in foc:
                    self.af_focus = "ok"
                    break
                if 2 in foc:
                    self.af_focus = "ng"
                    break
            try:
                self._post("/ccapi/ver100/shooting/control/af", {"action": "stop"})
            except Exception as e:
                self._log(f"AF stop (end) failed: {e}")
            self._log("AF triggered" + (f" -- focus {self.af_focus}" if self.af_focus else ""))
            return True
        except Exception as e:
            self._log(f"AF failed: {e}")
            return False

    def stream_liveview_frames(self, fps=6):
        """Synthesize a multipart/x-mixed-replace MJPEG stream for the
        browser out of repeated single-frame grabs.

        This body (R10, fw 1.8.0) doesn't advertise the continuous
        /shooting/liveview/multipart feed — only the
        single-shot "flip" endpoint, confirmed by probing its
        live /ccapi endpoint list. Polling flip in a loop and wrapping each
        JPEG in a standard multipart boundary gets the same live-preview
        UX in a plain <img> tag, just at a capped, polled frame rate.
        """
        if not self.state["connected"]:
            raise RuntimeError("Camera not connected")
        try:
            self._post("/ccapi/ver100/shooting/liveview", {"liveviewsize": "small", "cameradisplay": "keep"})
        except Exception:
            # Already started, or the camera is momentarily busy — a failed
            # flip GET below is what actually surfaces a real problem.
            pass
        self._lv_mjpg = getattr(self, "_lv_mjpg", 0) + 1
        self._log("Live view started")
        interval = 1.0 / fps
        boundary = LIVEVIEW_BOUNDARY.encode()
        try:
            while True:
                if not self.state["connected"]:
                    break
                t0 = time.monotonic()
                try:
                    r = self._session.get(
                        f"{self.base}/ccapi/ver100/shooting/liveview/flip", timeout=REQUEST_TIMEOUT
                    )
                    r.raise_for_status()
                    frame = r.content
                except Exception:
                    # Frame not ready yet, or the camera is momentarily
                    # busy — CCAPI's own guidance is to just retry.
                    time.sleep(0.2)
                    continue
                yield (
                    b"--" + boundary + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame + b"\r\n"
                )
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    time.sleep(interval - elapsed)
        finally:
            # The generator only unwinds here once the browser side closes
            # the <img> connection (tab closed, LIVE toggled off, ...).
            self._lv_mjpg = max(0, getattr(self, "_lv_mjpg", 1) - 1)
            self._log("Live view stream closed")
            # O ultimo a ver saiu: desliga o live view na propria camera (ligado gasta-lhe bateria). Antes a
            # pagina fechava o fluxo e a camera ficava com o live view ligado.
            if self._lv_mjpg == 0 and self.state.get("connected"):
                try:
                    self.stop_liveview()
                except Exception as e:
                    self._log(f"Live view stop failed: {e}")

    def stop_liveview(self):
        """Desliga o live view na camera: deixa-lo ligado gasta bateria.

        Nao faz nada se houver um fluxo MJPEG aberto (outro cliente a ver).
        """
        if getattr(self, "_lv_mjpg", 0) > 0:
            self._log("Live view stop skipped: an MJPEG stream is still open")
            return False
        err = None
        # O arranque manda liveviewsize + cameradisplay; a camera pode exigir
        # os dois tambem para parar, por isso tenta-se com e sem.
        for body in ({"liveviewsize": "off", "cameradisplay": "keep"},
                     {"liveviewsize": "off"}):
            try:
                self._post("/ccapi/ver100/shooting/liveview", body)
                self._log("Live view stopped (camera)")
                return True
            except Exception as e:
                err = e
        self._log(f"Live view stop failed: {err}")
        return False

    # Metodos de AF em que a moldura tem posicao escolhida por quem opera.
    AF_MOVABLE = ("1point", "spot", "expand_cross", "expand_around",
                  "flexible_zone_1", "flexible_zone_2", "flexible_zone_3")

    def lv_info(self):
        """Informacao do live view: FF 00 01 <tamanho 4 bytes> <JSON> FF FF."""
        import json
        r = self._session.get(f"{self.base}/ccapi/ver100/shooting/liveview/flipdetail",
                              params={"kind": "info"}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        d = r.content
        if d[:3] != b"\xff\x00\x01":
            raise RuntimeError("unexpected live view info format")
        n = int.from_bytes(d[3:7], "big")
        return json.loads(d[7:7 + n].decode("utf-8", "replace")).get("liveviewdata", {})

    def set_af_point(self, u, v, method="1point"):
        """Move a moldura de AF para (u, v), fraccoes 0..1 do quadro do live view."""
        u = min(1.0, max(0.0, float(u)))
        v = min(1.0, max(0.0, float(v)))
        img = self.lv_info().get("image") or {}
        if not img.get("positionwidth") or not img.get("positionheight"):
            raise RuntimeError("live view info has no image crop")
        cur = self._get("/ccapi/ver100/shooting/settings/afmethod").get("value")
        if cur not in self.AF_MOVABLE:
            self._put("/ccapi/ver100/shooting/settings/afmethod", {"value": method})
            self._af_prev_method = cur
            self._log(f"AF method {cur} -> {method} (AF point by click)")
            cur = method
        x = round(img["positionx"] + u * img["positionwidth"])
        y = round(img["positiony"] + v * img["positionheight"])
        self._put("/ccapi/ver100/shooting/liveview/afframeposition",
                  {"positionx": x, "positiony": y})
        return {"x": x, "y": y, "method": cur}

    def af_auto(self):
        """Repoe o metodo de AF de antes do primeiro clique (whole_area por defeito)."""
        prev = getattr(self, "_af_prev_method", None) or "whole_area"
        self._put("/ccapi/ver100/shooting/settings/afmethod", {"value": prev})
        self._af_prev_method = None
        self._log(f"AF method -> {prev} (AF AUTO)")
        return prev


# === CONFIG ===
# Os corpos de origem. Os enderecos, e as Canon que se juntam ou tiram, mudam-se em DEVICES (consola ou pagina) e
# ficam em devices.json; o bridge aplica-os por cima destes ao arrancar (_dev_aplicar).
CANON_R10_IP = "192.168.4.100"
CANON_R6M2_IP = "192.168.4.75"

CAMERAS = {
    "r10": CanonCamera("R10", CANON_R10_IP),
    "r6m2": CanonCamera("R6", CANON_R6M2_IP, port=443, use_https=True),
}


def watchdog(interval=5):
    """Fixed-interval fallback poller.

    Only touches cameras whose body/firmware doesn't implement ver110's
    event/polling (CanonCamera._event_polling_supported went False after a
    live 404) — every other camera gets refreshed by its own
    _event_poll_loop() thread instead, started from connect().
    """
    while True:
        time.sleep(interval)
        for cam in CAMERAS.values():
            if cam.state["connected"] and not cam._event_polling_supported:
                cam.refresh_status()

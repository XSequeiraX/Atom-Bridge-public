"""O alvo de REC "f6" do bridge: o Zoom F6 comandado pelo FRC-8 (frc8.py).

O F6 em TIMECODE Int Free Run. REC/STOP vao pelo FRC-8, com confirmacao do
proprio F6; o timecode acerta-se com um jam (jam()): LTC a andar JAM_SECS pela
saida de auscultadores do Atom (f6_ltc.py), que so abre durante o jam.
(O modo Ext Auto Rec de antes foi retirado a 2026-10-08.)

Mais: teclas USER (Mark/Circled/Clear Clip), nome da cena, nota da proxima take,
nomes das pistas (como o teclado do FRC-8), e avisos para o shoot log.
"""
import os
import threading
import time


def frc8_user_notes():
    import frc8
    return frc8.NOTE_USER


def f6_ltc_preroll(gen):
    """O preroll do gerador (LTC parado no 1o valor antes de andar)."""
    try:
        import f6_ltc
        return f6_ltc.PREROLL
    except Exception:
        return 1.0

CONFIRM_TIMEOUT = 2.5      # o F6 confirmou o REC em 31 ms e o STOP em 1,1 s
JAM_SECS = float(os.environ.get("F6_JAM_SECS", "5"))
# Nome da cena no F6 (o inicio do nome dos ficheiros). O limite exacto do F6
# nao esta confirmado: 12 e uma margem segura; apaga-se SCENE_MAX+4 no inicio.
SCENE_MAX = int(os.environ.get("F6_SCENE_MAX", "12"))
NOTE_MAX = int(os.environ.get("F6_NOTE_MAX", "40"))
TRACK_MAX = int(os.environ.get("F6_TRACK_MAX", "12"))
# As teclas USER do FRC-8 tem a funcao que se escolhe no F6 (FRC-8 > User Key).
# Aqui diz-se qual delas tem cada funcao; 0 = nenhuma. No F6 a 2026-10-08:
# U1 Mark, U2 Clear Clip Indicator, U3 Circled.
USER_KEYS = {
    "mark": int(os.environ.get("F6_USER_MARK", "1")),
    "clear_clip": int(os.environ.get("F6_USER_CLEAR_CLIP", "2")),
    "circled": int(os.environ.get("F6_USER_CIRCLED", "3")),
}


class F6Control:
    def __init__(self, gen, link, log, on_unexpected=None):
        self.gen, self.link, self.log = gen, link, log
        self.on_unexpected = on_unexpected
        self._expect = None                # o ultimo estado que o bridge pediu
        self._expect_at = 0.0
        self._cmd_n = 0                    # sobe a cada REC/STOP: a confirmacao de um comando ultrapassado nao se queixa
        self._jam_until = None
        self._last_jam = None              # hora do ultimo jam pedido (time.time())
        self._sent = {}                    # {aparelho: hora}: LTC enviado a outros (DJI Mic 3...)
        self._jam_quem = None              # a quem vai o LTC do jam em curso (F6, DJI Mic 3...)
        self._jam_timer = None
        self._kbd_lock = threading.Lock()
        self._scene = None                 # o ultimo nome de cena escrito pelo bridge
        self._note = None                  # a nota da proxima take
        self._track_names = {}             # {pista: nome} escritos pelo bridge
        self.shot = None                   # shot(evento, nota): linha no shoot log (o bridge liga)
        self.on_fault = None               # on_fault(texto): aviso a vista (o bridge liga)
        link.on_rec_change = self._rec_changed
        link.on_clip = self._clip

    def start(self):
        self.link.start()
        self.gen.start()

    # --- interface do alvo -----------------------------------------------------
    def ready(self):
        if not self.link.connected:
            return False, ("F6 not connected: on the F6 press FRC-8 connect with the cable "
                           "unplugged, then plug the cable into the bridge (or put an S3 gateway in F6 mode: "
                           "cable from the F6 to the S3 first, then connect)")
        return True, None      # o TC e o INT do F6, acertado pelo jam

    def running(self):
        return bool(self.link.recording)

    def alvo(self):
        """Para onde o F6 vai: enquanto espera a confirmacao do ultimo REC/STOP, o que se pediu (ver gopro.GoPro.alvo).
        O F6 demora a confirmar; um STOP nesse intervalo nao se perde por o F6 ainda parecer parado."""
        if self._expect is not None and time.time() - self._expect_at < CONFIRM_TIMEOUT:
            return self._expect
        return self.running()

    def armed_any(self):
        """Ha pelo menos uma pista armada? None se ainda nao se sabe: F6 desligado, ou ligado ha
        menos de 3 s (o F6 ainda nao mandou o estado dos LEDs) -- sem isto, logo a seguir a ligar,
        as pistas pareciam todas desarmadas e o REC era recusado sem razao."""
        if not self.link.connected or time.time() - (self.link.since or 0) < 3:
            return None
        if not getattr(self.link, "arms_known", True):
            return None                    # o F6 nao disse: nao se bloqueia o REC por uma coisa que nao se sabe
        return any(self.link.arms[:self._tracks()])

    def set_running(self, on):
        if on:
            ok, why = self.ready()
            if not ok:
                return False, why
        elif not self.link.connected:
            return False, "F6 not connected"
        self._expect, self._expect_at = bool(on), time.time()
        self._cmd_n += 1
        n = self._cmd_n
        import frc8
        ok, err = self.link.press(frc8.NOTE_REC if on else frc8.NOTE_STOP)
        if not ok:
            # Pode ter chegado ao F6 e so a resposta do S3 ter-se perdido: se ele mudar, que conte como mudanca "sozinha"
            # (fica no shoot log) em vez de uma confirmacao de um comando que aqui se deu por falhado.
            self._expect = None
            return False, err
        # Nao bloquear o REC das camaras: a confirmacao vem do F6 e verifica-se a parte.
        threading.Thread(target=self._confirm, args=(bool(on), n), daemon=True).start()
        if on:
            threading.Thread(target=self._sem_pistas, daemon=True).start()
        return True, None

    def status(self):
        s = self.gen.status()
        ok, why = self.ready()
        s.update({"via": "frc", "ready": ok, "why": why, "running": self.running(),
                  "ltc_running": self.gen.running(), "jamming": self._jam_until is not None,
                  "jam_to": self._jam_quem if self._jam_until is not None else None,
                  "last_jam": self._last_jam, "jam_secs": JAM_SECS, "ltc_sent": dict(self._sent),
                  "armed_any": self.armed_any(), "user_keys": USER_KEYS, "scene": self._scene,
                  "note": self._note, "track_names": {str(k): v for k, v in self._track_names.items()},
                  "frc": self.link.status()})
        return s

    # --- teclas USER -------------------------------------------------------------
    def user(self, func):
        """Carrega na tecla USER que tem `func` (mark, clear_clip, circled). (ok, erro)."""
        n = USER_KEYS.get(func) or 0
        if n not in frc8_user_notes():
            return False, "no F6 user key set for %s" % func
        if not self.link.connected:
            return False, "F6 not connected"
        ok, err = self.link.press(frc8_user_notes()[n])
        if ok:
            self.log("F6: USER %d (%s)" % (n, func))
        return ok, err

    def on_mark(self, kind):
        """O G/M do bridge: good -> Circled, mark -> Mark. no-good nao mexe no F6
        (a tecla FALSE do F6 move o ficheiro; isso fica para quem esta no F6)."""
        func = {"good": "circled", "mark": "mark"}.get(kind)
        if not func or not self.link.connected:
            return
        threading.Thread(target=self.user, args=(func,), daemon=True).start()

    # --- nome da cena (o nome dos ficheiros) --------------------------------------
    def scene_inc(self):
        """SCENE INC: o numero da cena +1 (a proxima take ja vai com ele)."""
        ok, why = self._pode_editar()
        if not ok:
            return False, why
        import frc8
        ok, err = self.link.press(frc8.NOTE_SCENE_INC)
        if ok:
            self.log("F6: SCENE INC")
        return ok, err

    def set_scene(self, name):
        """Nome da cena (metadado SCENE da proxima take). Testado no F6 a
        2026-10-08; so conta com Scene Name Mode em User Name. (ok, erro)."""
        import frc8
        ok, err = self._escrever(frc8.NOTE_SCENE_NAME, name, SCENE_MAX, "scene name", self._menu_edit)
        if ok:
            self._scene = str(name).strip()
        return ok, err

    def set_note(self, text):
        """Nota da PROXIMA take (metadado NOTE): o F6 so a poe na take seguinte.
        POR TESTAR: o NOTE deve abrir Edit/History como o SCENE NAME. (ok, erro)."""
        import frc8
        ok, err = self._escrever(frc8.NOTE_NOTE, text, NOTE_MAX, "note", self._menu_edit)
        if ok:
            self._note = str(text).strip()
            self._shot("note", "F6 note for the NEXT take: %s" % self._note)
        return ok, err

    def set_track_name(self, track, name):
        """Nome de uma pista (metadado TRACK NAME da proxima take). POR TESTAR:
        o TRACK NAME deve abrir a lista das pistas e depois Edit/History. (ok, erro)."""
        import frc8
        try:
            track = int(track)
        except (TypeError, ValueError):
            return False, "track must be a number"
        if not 1 <= track <= frc8.TRACKS:
            return False, "track must be 1-%d" % frc8.TRACKS

        def menu():
            # Lista das pistas: ao topo, desce ate a pista, OK; depois Edit/History.
            self.log("F6 track name: track list -> top -> down %d -> OK" % (track - 1))
            self.link.menu_up(frc8.TRACKS + 4)
            self.link.menu_down(track - 1)
            ok, err = self.link.press(frc8.NOTE_OK)
            if not ok:
                return ok, err
            if self.link.wait_kbd(True, 1.0):
                return True, None
            return self._menu_edit()

        ok, err = self._escrever(frc8.NOTE_TRACK_NAME, name, TRACK_MAX, "track %d name" % track, menu)
        if ok:
            self._track_names[track] = str(name).strip()
        return ok, err

    def _menu_edit(self):
        """O menu Edit/History do F6 (o cursor pode estar no History): ao topo e OK."""
        import frc8
        self.log("F6 text: Edit/History menu -> up x2 -> OK (Edit)")
        self.link.menu_up(2)
        ok, err = self.link.press(frc8.NOTE_OK)
        if not ok:
            return ok, err
        if not self.link.wait_kbd(True, 2.0):
            return False, "the F6 did not open its text box: check the F6 screen"
        return True, None

    def _escrever(self, tecla, texto, maximo, nome, menu):
        """Como o teclado do FRC-8: a tecla abre o menu, `menu()` chega a caixa de
        texto, apaga-se o que la esta, escreve-se, ENTER. Cada passo no event log."""
        import frc8
        texto = str(texto or "").strip()
        if not texto:
            return False, "empty %s" % nome
        mau = sorted({c for c in texto if frc8.hid(c) is None})
        if mau:
            return False, "the F6 keyboard has no key for: %s" % " ".join(mau)
        if texto[0] in " @":
            return False, "the F6 does not allow a space or @ at the start"
        if len(texto) > maximo:
            return False, "%s longer than %d characters" % (nome, maximo)
        ok, why = self._pode_editar()
        if not ok:
            return False, why
        with self._kbd_lock:
            try:
                if not self.link.kbd_open:
                    ok, err = self.link.press(tecla)
                    if not ok:
                        return False, err
                    if not self.link.wait_kbd(True, 1.0):
                        ok, err = menu()
                        if not ok:
                            self.log("F6 %s: %s" % (nome, err))
                            return False, err
                for _ in range(maximo + 4):            # apagar o texto antigo
                    self.link.key(0, frc8.HID_BACKSPACE)
                for c in texto:
                    self.link.key(*frc8.hid(c))
                self.link.key(0, frc8.HID_ENTER)
            except OSError as e:
                return False, "F6 link lost: %s" % e
            if not self.link.wait_kbd(False, 2.0):
                self.log("F6 %s: typed %r + ENTER, the text box is still open" % (nome, texto))
                return False, "the F6 text box is still open after ENTER: check the F6 screen"
        self.log("F6: %s set to %s" % (nome, texto))
        return True, None

    # --- avisos no shoot log -------------------------------------------------------
    def _shot(self, evento, nota):
        if self.shot:
            try:
                self.shot(evento, nota)
            except Exception as e:
                self.log("F6 shoot log: %r" % e)

    def _clip(self, pista):
        self.log("F6 CLIP on track %d" % pista)
        self._shot("clip", "F6 CLIP track %d%s" % (pista, self._nome_pista(pista)))

    def _nome_pista(self, pista):
        n = self._track_names.get(pista)
        return " (%s)" % n if n else ""

    def _sem_pistas(self):
        # Depois do start (o REC das camaras e do F6 abre a take): o aviso fica
        # na take nova, e nao na anterior.
        time.sleep(0.5)
        if self.link.recording and self.armed_any() is False:
            self.log("F6 recording with NO TRACK ARMED")
            self._shot("warning", "F6 recording with NO TRACK ARMED (only the LR mix, if any)")

    def _tracks(self):
        import frc8
        return frc8.TRACKS

    def _pode_editar(self):
        if not self.link.connected:
            return False, "F6 not connected"
        if self.link.recording:
            return False, "the F6 is recording: change the scene between takes"
        return True, None

    # --- jam ---------------------------------------------------------------------
    def jam(self, secs=None, quem="F6"):
        """Poe o LTC a andar `secs` segundos (+ o preroll do gerador) e volta a
        parar. (ok, erro). O F6 acerta o relogio por ele e fica em INT.
        `quem`: o aparelho ligado a saida de auscultadores (F6, ou "DJI Mic 3": jam pela entrada
        de audio, como o F6). So o F6 conta como ultimo jam do F6."""
        secs = JAM_SECS if secs is None else float(secs)
        if quem == "F6" and self.running():
            return False, "the F6 is recording: jam it between takes"
        ok, why = self.gen.set_running(True)
        if not ok:
            if "jam" in (why or ""):
                why = ("no LTC stored -- first ESC > Time and clocks > LTC Get "
                       "(Bluetooth: Sidus app open; or cable)")
            return False, "cannot send timecode to the F6: %s" % why
        total = f6_ltc_preroll(self.gen) + secs
        if self._jam_timer:
            self._jam_timer.cancel()
        self._jam_until = time.time() + total
        self._jam_quem = quem
        if quem == "F6":
            self._last_jam = time.time()
        self._sent[quem] = time.time()
        t = threading.Timer(total, lambda: self._jam_end(t))
        t.daemon = True
        self._jam_timer = t
        t.start()
        self.log("%s jam: LTC running for %.0f s on the headphone output" % (quem, secs))
        return True, None

    def _jam_end(self, timer):
        # Um jam pedido de novo entretanto tem o seu proprio fim: este ja nao conta.
        if timer is not self._jam_timer:
            return
        self.gen.set_running(False)
        self._jam_until = self._jam_timer = None
        self.log("%s jam: done, audio output closed" % (self._jam_quem or "F6"))
        self._jam_quem = None

    # --- confirmacoes ----------------------------------------------------------
    def _confirm(self, on, n=None):
        if not self.link.wait_rec(on, CONFIRM_TIMEOUT):
            if n is not None and n != self._cmd_n:
                return                      # REC->STOP rapido: o comando mais novo tem a sua propria confirmacao
            txt = ("F6 did NOT confirm %s within %.1f s -- check the F6 (card, FRC-8 screen)"
                   % ("REC" if on else "STOP", CONFIRM_TIMEOUT))
            self.log(txt)
            # Antes so ficava no registo de eventos: o shoot log tinha um start que o F6 nao fez.
            self._shot("fault", txt)
            if self.on_fault:
                try:
                    self.on_fault(txt)
                except Exception as e:                  # pylint: disable=broad-except
                    self.log("F6: %r" % e)

    def _rec_changed(self, a_gravar):
        if self._expect is not None and a_gravar == self._expect:
            self.log("F6 confirmed %s" % ("REC" if a_gravar else "STOP"))
            return
        # Ninguem no bridge pediu: botao no proprio F6, cartao cheio, pilhas...
        self._expect = a_gravar
        if self.on_unexpected:
            self.on_unexpected(a_gravar)

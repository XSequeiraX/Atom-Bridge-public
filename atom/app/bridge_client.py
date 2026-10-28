"""Cliente do bridge para os servicos a parte (USB): ler os devices e mandar REC e alvos (2026-10-13).

Fala so pela API HTTP do bridge, a mesma da consola. As camaras vem do build_rows() da propria consola
(/usr/local/bin/bridge-ctl), que sabe ler BMPCC (/api/status), Canon, iPhones, GoPro e BMPCC extra (GET /api/devices); o Zoom F6 junta-se
no fim como device de AUDIO. Assim todos os servicos veem os mesmos devices, pela mesma ordem do ecra principal.
"""
import importlib.machinery
import importlib.util
import json
import threading
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
CONSOLA = "/usr/local/bin/bridge-ctl"


def api(method, path, body=None, timeout=4):
    """(status HTTP, JSON). Status 0 quando o bridge nao responde."""
    data = json.dumps(body or {}).encode() if method == "POST" else None
    cab = {"X-Bridge-Service": "1"}         # um servico do Atom, nao uma pessoa a ver (as Canons podem ir para repouso)
    if data:
        cab["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=cab)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception:                       # pylint: disable=broad-except
        return 0, {}


def build_rows_da_consola(caminho=None):
    """O build_rows() da consola instalada, ou None (lista minima)."""
    try:
        loader = importlib.machinery.SourceFileLoader("bridge_ctl", caminho or CONSOLA)
        spec = importlib.util.spec_from_loader("bridge_ctl", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m.build_rows
    except Exception as e:                  # pylint: disable=broad-except
        print("bridge_client: console module not loaded (%s); minimal device list" % e, file=sys.stderr, flush=True)
        return None


def f6_row(st):
    """O Zoom F6 como device de AUDIO (a consola mostra-o num painel proprio, fora do build_rows)."""
    f6 = st.get("f6")
    if not isinstance(f6, dict):
        return None
    frc = f6.get("frc") or {}
    if frc.get("armed") is None and frc.get("connected"):
        return {"id": "f6", "label": "F6", "conn": True, "rec": bool(frc.get("recording")),
                "batt": -1, "left": "", "mode": "AUDIO", "cfg": "TRACKS ?"}
    armadas = [a for a in (frc.get("armed") or []) if isinstance(a, int)]
    return {"id": "f6", "label": "F6", "conn": bool(frc.get("connected")), "rec": bool(frc.get("recording")),
            "batt": -1, "left": "", "mode": "AUDIO",
            "cfg": ("tracks " + " ".join(str(a) for a in armadas)) if armadas else "NO TRACK ARMED"}


class Rig:
    """O estado do rig visto de fora do bridge, e as accoes de video e audio."""

    FALHAS_OFFLINE = 3                      # leituras seguidas falhadas ate se dizer que o bridge esta offline
    IDADE_MAX_S = 8.0                       # o estado bom mais velho do que isto ja nao conta (3 x POLL + 3 s de timeout, com folga)
    RETRY_CONSOLA_S = 30.0                  # de quanto em quanto se tenta carregar outra vez o modulo da consola

    def __init__(self, build_rows=None):
        self._consola_lazy = build_rows is None     # sem build_rows dado: o da consola, a tentar de novo se falhar
        self.build_rows = build_rows if build_rows is not None else build_rows_da_consola()
        self._consola_tentada = time.time()
        self.st, self.rows, self.targets = {}, [], []
        self.devs = {}                      # o ultimo GET /api/devices bom: um falhado nao desloca os slots
        self.falhas = 0                     # leituras de /api/status falhadas seguidas
        self.devs_falhas = 0                # GET /api/devices falhados seguidos (as rows ficam velhas)
        self.rows_falhas = 0                # build_rows() falhado seguidas (conta a parte: o status pode estar bom)
        self.rows_velhas = False            # as rows sao as do ciclo anterior (build_rows falhou): o rec vem do status
        self._tentou_em = 0.0               # quando se tentou ler pela ultima vez (bem ou mal)
        self.ok_em = 0.0                    # quando o ultimo /api/status chegou bem
        self._ler_lock = threading.Lock()   # o ciclo e a API da consola leem ao mesmo tempo
        # Os alvos mudados aqui contam mais do que um /api/status pedido antes da mudanca: num Atom ocupado o status
        # demora, e o retrato antigo chegava depois do toque e repunha os alvos de antes (o controlador nao mostrava o que
        # se armou, e um segundo toque desfazia o primeiro no bridge).
        self._alvos_lock = threading.Lock()
        self._alvos_seq = 0

    @property
    def online(self):
        """O bridge respondeu ha pouco: um poll falhado isolado nao o torna offline (FALHAS_OFFLINE seguidos sim), e um
        estado com mais de IDADE_MAX_S ja nao conta (ninguem o esta a ler: servico em repouso ou ciclo preso)."""
        return self.ok_em > 0 and self.falhas < self.FALHAS_OFFLINE and time.time() - self.ok_em < self.IDADE_MAX_S

    def idade(self):
        """Segundos desde o ultimo estado bom (None se nunca houve)."""
        return time.time() - self.ok_em if self.ok_em else None

    def conhecido(self):
        """A lista de devices e fiavel: o bridge esta online e ha rows. Sem isto nao se mexe nos alvos."""
        return self.online and bool(self.rows)

    def ler(self):
        with self._ler_lock:
            return self._ler()

    def ler_se_velho(self, max_idade=2.0, min_gap=2.0):
        """Le agora se o estado tem mais de max_idade s (o primeiro envio depois de um repouso, um comando de um controlador que
        volta). Sem tentar de novo antes de min_gap s: um bridge parado nao prende quem chama de cada vez."""
        with self._ler_lock:
            i = self.idade()
            if i is not None and i < max_idade:
                return True
            if time.time() - self._tentou_em < min_gap:
                return False
            return self._ler()

    def _ler(self):
        self._tentou_em = time.time()
        seq = self._alvos_seq
        s, st = api("GET", "/api/status", timeout=3)
        if s != 200 or not isinstance(st, dict):
            # Um poll falhado nao apaga o que se sabia: so aos FALHAS_OFFLINE seguidos o bridge conta como offline
            # (e `online` diz-o). Antes um unico timeout punha rows=[] e um "todos os alvos" limpava os alvos todos.
            self.falhas += 1
            return False
        self.falhas = 0
        self.ok_em = time.time()
        self.st = st
        with self._alvos_lock:
            if seq == self._alvos_seq:          # ninguem mexeu nos alvos enquanto o status vinha
                self.targets = list(st.get("rec_targets") or [])
        if self.build_rows is None and self._consola_lazy and time.time() - self._consola_tentada > self.RETRY_CONSOLA_S:
            self._consola_tentada = time.time()
            self.build_rows = build_rows_da_consola()       # a consola pode ter sido instalada depois do arranque
        rows = None
        if self.build_rows:
            s2, dv = api("GET", "/api/devices", timeout=3)    # como a consola (Snapshot.poll_once)
            devs_novo = s2 == 200 and isinstance(dv, dict) and isinstance(dv.get("devices"), list)
            if devs_novo:
                self.devs = dv
                self.devs_falhas = 0
            else:
                self.devs_falhas += 1       # o /api/devices falhou: as rows vem do ultimo bom (velho)
            try:
                rows = self.build_rows(st, self.devs)       # sem o /api/devices deste ciclo, o ultimo bom
                self.rows_falhas, self.rows_velhas = 0, False
            except Exception:               # pylint: disable=broad-except
                rows = None
                self.rows_falhas += 1       # conta como leitura falhada das rows (nao do status)
                devs_novo = False
            if rows is None:
                rows = [dict(r) for r in self.rows if r.get("id") != "f6"] or None     # os de antes, nunca []
            if rows and not devs_novo:
                # Rows velhas (build_rows a falhar, ou o /api/devices sem responder: o iPhone, a GoPro e as outras linhas vem
                # dele): o rec/conn delas nao vale. O rec vem do status (nunca fica aceso com o status a dizer que nada
                # grava) e, falhando varias vezes, a ligacao tambem deixa de se afirmar. A BMPCC principal sai do status.
                self.rows_velhas = True
                gravar_st = bool(st.get("is_recording")) or bool(st.get("anything_recording"))
                falhas = max(self.rows_falhas, self.devs_falhas)
                for r in rows:
                    if r.get("id") == "bmpcc":
                        continue
                    if not gravar_st:
                        r["rec"] = False
                    if falhas >= self.FALHAS_OFFLINE:
                        r["conn"] = False
        if rows is None:
            self.rows_velhas = False
            rows = [{"id": "bmpcc", "label": "BMPCC", "conn": bool(st.get("camera_connected")),
                     "rec": bool(st.get("is_recording")), "batt": st.get("battery_percent", -1)}]
        f6 = f6_row(st)
        if f6:
            rows.append(f6)                 # o audio no fim, como no ecra principal
        self.rows = rows
        return True

    def a_gravar(self):
        if not self.online:                 # sem bridge nao se sabe: nao se acende o REC com dados velhos
            return False
        if self.rows_velhas:                # rows do ciclo anterior (build_rows a falhar): so o status diz
            return bool(self.st.get("is_recording")) or bool(self.st.get("anything_recording"))
        return any(r.get("rec") for r in self.rows) or bool(self.st.get("is_recording"))

    def slot(self, n):
        """O id do device no slot n (1..), pela ordem do ecra principal."""
        return self.rows[n - 1]["id"] if 1 <= n <= len(self.rows) else None

    def ids(self):
        return [r["id"] for r in self.rows]

    # -- accoes --
    def rec(self):
        """REC/STOP dos alvos (o R da consola)."""
        # O bridge responde em ate ~5 s (o grupo espera as camaras ate la): com 4 s o cliente dava o REC por recusado com
        # as camaras a gravar, e um segundo toque parava tudo.
        return api("POST", "/api/rec", timeout=12)

    def _mandar_alvos(self, corpo, local):
        """Manda a mudanca ao bridge, um pedido de cada vez (dois toques seguidos chegam pela ordem), e fica com a
        lista que o bridge aceitou (a da resposta; `local` so se a resposta a nao trouxer). Num erro nada muda aqui."""
        with self._alvos_lock:
            self._alvos_seq += 1
            s, d = api("POST", "/api/rec/targets", corpo)
            if s == 200:                    # so o que o bridge aceitou muda a lista local; um erro deixa-a como estava
                self.targets = list(d["targets"]) if isinstance(d, dict) and isinstance(d.get("targets"), list) \
                    else list(local)
            self._alvos_seq += 1                # um status pedido durante o POST tambem ja e velho
        return s, d

    def toggle_target(self, did):
        """Inverte o alvo no bridge ({"toggle": id}): a direcao sai da lista que o bridge tem AGORA, nao de uma copia local
        possivelmente velha. A resposta traz a lista nova; num erro (status 0 ou outro) a lista local fica como estava
        e o erro volta a quem chamou."""
        local = [x for x in self.targets if x != did] + ([] if did in self.targets else [did])
        return self._mandar_alvos({"toggle": did}, local)

    def set_targets(self, ids):
        """Muda a lista toda de alvos. Recusa (status 0) com a lista de devices desconhecida: um bridge que nao
        respondeu ou rows vazias nunca limpam os alvos."""
        if not self.conhecido():
            return 0, {"error": "device list unknown (bridge not answering)"}
        return self._mandar_alvos({"targets": list(ids)}, ids)

    def alvos_todos(self):
        """Todos os devices como alvos (incluindo o F6). Recusa se a lista nao for conhecida ou estiver vazia."""
        ids = self.ids()
        if not ids:
            return 0, {"error": "device list unknown (bridge not answering)"}
        return self.set_targets(ids)

    @staticmethod
    def device_rec(did):
        """REC/STOP de um so device (como o T da consola). O bridge sabe de que tipo e: o cliente nao o adivinha pelo id.
        Uma GoPro confirma em ate ~6 s."""
        return api("POST", "/api/devices/%s/rec" % did, timeout=12)

    @staticmethod
    def mark(kind):
        """Marca no shoot log: good | no-good | mark."""
        return api("POST", "/api/log/mark", {"kind": kind})

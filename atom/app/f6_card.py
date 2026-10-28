"""O cartao do Zoom F6 visto pelo bridge (F6 em MENU > SYSTEM > USB > SD Card
Reader, cabo USB ao Atom: o F6 aparece como um disco exFAT).

  - scan(): as takes com os metadados do F6 (cena, take, TC de inicio, duracao,
    CIRCLED, nota, nomes das pistas), cruzadas com o shoot log do bridge pelo
    timecode; escreve um CSV para a montagem;
  - explorador: listar pastas, mudar o nome, mover, criar pastas, apagar.

Seguranca: o cartao e o som da rodagem. Monta-se so durante cada operacao (so
de leitura para ver; com escrita so para mexer) e desmonta-se logo a seguir.
"Apagar" MOVE para a pasta F6_TRASH do proprio cartao; so o "esvaziar o lixo",
pedido a parte, apaga de vez. Os caminhos nunca saem do cartao.
"""
import csv
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time

MOUNT = "/mnt/f6sd"
TRASH = "F6_TRASH"
TC_TOL = 2.0                  # s: F6 e shoot log a contar a mesma take
_last = {"report": None, "csv": None}
# Nomes validos no cartao (exFAT) e sem surpresas no F6: sem / \\ : * ? " < > |
NOME_OK = re.compile(r"^[^/\\\\:*?\"<>|\x00-\x1f]{1,120}$")


class CardError(Exception):
    pass


# --- o disco -----------------------------------------------------------------
ZOOM_VID = "1686"


def e_zoom(dev, sys_block="/sys/class/block"):
    """True se o disco `dev` (/dev/sdb ou /dev/sdb1) e um aparelho Zoom (idVendor 1686) pela USB, False se e
    outro, None se nao se sabe. Uma pen de copias tambem e exFAT/FAT pela USB: sem isto o explorador do F6
    podia mexer na pen e a copia de seguranca podia escrever no cartao do som."""
    try:
        p = os.path.realpath(os.path.join(sys_block, os.path.basename(dev)))
    except (OSError, TypeError):
        return None
    while p and p != "/":
        try:
            with open(os.path.join(p, "idVendor")) as f:
                return f.read().strip().lower() == ZOOM_VID
        except OSError:
            p = os.path.dirname(p)
    return None


def find_device():
    """(dispositivo, ponto de montagem actual ou None) da particao exFAT/FAT de
    um disco USB (o F6 em SD Card Reader), ou (None, None). Um disco que se sabe
    nao ser Zoom (uma pen) fica de fora."""
    try:
        out = subprocess.run(["lsblk", "-J", "-p", "-o", "NAME,FSTYPE,MOUNTPOINT,TRAN,RM,SIZE,MODEL"],
                             capture_output=True, text=True, timeout=5).stdout
        devs = json.loads(out).get("blockdevices", [])
    except Exception:
        return None, None
    for d in devs:
        if d.get("tran") != "usb" or e_zoom(d["name"]) is False:
            continue
        for p in d.get("children") or [d]:
            if (p.get("fstype") or "").lower() in ("exfat", "vfat"):
                return p["name"], p.get("mountpoint")
    return None, None


class Mounted:
    """with Mounted(rw) as raiz: o cartao montado enquanto houver quem o use.

    Leituras partilham a mesma montagem (ver pastas enquanto um ficheiro toca);
    a escrita so entra com o cartao livre, e monta com escrita so durante ela.
    Desmonta quando o ultimo utilizador sai (se fomos nos a monta-lo)."""

    _m = threading.Lock()
    _users = 0
    _root = None
    _ours = False
    _rw = False
    _remontado = None

    def __init__(self, rw=False):
        self.rw = rw

    def __enter__(self):
        c = Mounted
        with c._m:
            if c._users and (self.rw or c._rw):
                raise CardError("the card is busy (playing a file?): stop it first")
            if not c._users:
                dev, mp = find_device()
                if not dev:
                    raise CardError("no card: on the F6, MENU > SYSTEM > USB > SD Card Reader, "
                                    "then plug the USB cable into the bridge")
                c._ours, c._remontado = False, None
                if mp:
                    if self.rw and not os.access(mp, os.W_OK):
                        r = subprocess.run(["mount", "-o", "remount,rw", mp], capture_output=True,
                                           text=True, timeout=10)
                        if r.returncode:
                            raise CardError("card mounted read-only at %s: %s" % (mp, r.stderr.strip()))
                        c._remontado = mp
                    c._root = mp
                else:
                    os.makedirs(MOUNT, exist_ok=True)
                    r = subprocess.run(["mount", "-o", "rw" if self.rw else "ro", dev, MOUNT],
                                       capture_output=True, text=True, timeout=15)
                    if r.returncode:
                        raise CardError("mount failed: %s" % (r.stderr.strip() or r.returncode))
                    c._root, c._ours = MOUNT, True
                c._rw = self.rw
            c._users += 1
            return c._root

    def __exit__(self, *a):
        c = Mounted
        with c._m:
            if self.rw:
                os.sync()
            c._users -= 1
            if c._users:
                return
            if c._ours:
                subprocess.run(["umount", MOUNT], capture_output=True, timeout=30)
            elif c._remontado:
                subprocess.run(["mount", "-o", "remount,ro", c._remontado], capture_output=True, timeout=10)
            c._rw, c._ours, c._remontado = False, False, None


def status():
    dev, mp = find_device()
    return {"device": dev, "mounted": mp, "busy": Mounted._users > 0, "player": player.state(),
            "report_at": (_last["report"] or {}).get("at"), "csv": bool(_last["csv"])}


def _dentro(root, rel):
    """O caminho absoluto de `rel` dentro do cartao, ou CardError."""
    rel = (rel or "").replace("\\", "/").strip("/")
    p = os.path.realpath(os.path.join(root, rel))
    r = os.path.realpath(root)
    if p != r and not p.startswith(r + os.sep):
        raise CardError("path outside the card")
    return p


def _nome(n):
    n = (n or "").strip()
    if not NOME_OK.match(n) or n in (".", "..") or n.startswith("."):
        raise CardError("invalid name: %r (no / \\ : * ? \" < > | and not starting with a dot)" % n)
    return n


# --- WAV: metadados do F6 --------------------------------------------------------
def wav_info(path):
    """{sr, block, frames, tref, ixml, bext_desc} sem ler o audio (RIFF e RF64)."""
    info = {"sr": None, "block": None, "frames": None, "tref": None, "ixml": "", "desc": ""}
    data_size = None
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"ds64":
                b = f.read(size)
                data_size = struct.unpack("<Q", b[8:16])[0]
            elif cid == b"fmt ":
                b = f.read(size)
                info["sr"] = struct.unpack("<I", b[4:8])[0]
                info["block"] = struct.unpack("<H", b[12:14])[0]
            elif cid == b"bext":
                b = f.read(size)
                info["desc"] = b[:256].split(b"\0")[0].decode("latin-1", "replace")
                if len(b) >= 346:
                    info["tref"] = struct.unpack("<Q", b[338:346])[0]
            elif cid == b"iXML":
                info["ixml"] = f.read(size).decode("utf-8", "replace")
            elif cid == b"data":
                n = data_size if (size == 0xFFFFFFFF and data_size is not None) else size
                if info["block"]:
                    info["frames"] = n // info["block"]
                f.seek(n, 1)
                size = n
            else:
                f.seek(size, 1)
            if size % 2:
                f.seek(1, 1)
    return info


def _tag(xml, nome):
    m = re.search(r"<%s>(.*?)</%s>" % (nome, nome), xml, re.S)
    return m.group(1).strip() if m else ""


def _tc(secs, fps=25):
    if secs is None:
        return ""
    n = int(round(secs * fps)) % (86400 * fps)
    return "%02d:%02d:%02d:%02d" % (n // (fps * 3600), n // (fps * 60) % 60, n // fps % 60, n % fps)


def _tc_secs(txt, fps=25):
    m = re.search(r"(\d\d):(\d\d):(\d\d)[:;](\d\d)", txt or "")
    if not m:
        return None
    h, mi, s, f = map(int, m.groups())
    return h * 3600 + mi * 60 + s + f / float(fps)


def _takes(root):
    """[(pasta relativa, base, [wavs])]: uma take = os WAV com a mesma base
    (antes de _Tr / _LR) na mesma pasta."""
    out = {}
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x != TRASH and not x.startswith(".")]
        for f in sorted(fs):
            if not f.lower().endswith(".wav") or f.startswith("._"):
                continue
            base = re.split(r"_(?:Tr\d+|LR|Tr[LR])\b", os.path.splitext(f)[0])[0]
            out.setdefault((os.path.relpath(d, root), base), []).append(os.path.join(d, f))
    return [(k[0], k[1], v) for k, v in sorted(out.items())]


# --- o relatorio --------------------------------------------------------------------
def scan(shoot_log_path, out_dir):
    """Le o cartao (so leitura) e cruza com o shoot log. Devolve o relatorio e
    escreve f6_card_AAAAMMDD-HHMM.csv em out_dir."""
    with Mounted(rw=False) as root:
        takes = []
        for rel, base, wavs in _takes(root):
            t = {"folder": rel, "base": base, "files": [os.path.basename(w) for w in wavs],
                 "scene": "", "take": "", "start_tc": "", "start_s": None, "duration_s": None,
                 "circled": "", "note": "", "tracks": "", "size_mb": 0.0, "error": ""}
            nomes = []
            for w in wavs:
                t["size_mb"] += os.path.getsize(w) / 1e6
                try:
                    i = wav_info(w)
                except (OSError, ValueError) as e:
                    t["error"] = "%s: %s" % (os.path.basename(w), e)
                    continue
                x = i["ixml"]
                nomes += re.findall(r"<TRACK>.*?<NAME>(.*?)</NAME>.*?</TRACK>", x, re.S)
                if not t["scene"] and x:
                    t["scene"], t["take"] = _tag(x, "SCENE"), _tag(x, "TAKE")
                    t["circled"], t["note"] = _tag(x, "CIRCLED"), _tag(x, "NOTE")
                if t["start_s"] is None and i["tref"] is not None and i["sr"]:
                    t["start_s"] = i["tref"] / float(i["sr"])
                    t["start_tc"] = _tc(t["start_s"])
                if t["duration_s"] is None and i["frames"] is not None and i["sr"]:
                    t["duration_s"] = round(i["frames"] / float(i["sr"]), 1)
            t["tracks"] = ", ".join(n for n in nomes if n)
            t["size_mb"] = round(t["size_mb"], 1)
            takes.append(t)
    log = _ler_shoot_log(shoot_log_path)
    _cruzar(takes, log)
    rep = {"at": time.time(), "takes": takes,
           "log_only": [r for r in log["f6_starts"] if not r.get("_usada")]}
    for r in rep["log_only"]:
        r.pop("_usada", None)
    _last["report"] = rep
    _last["csv"] = _csv(rep, out_dir)
    return rep


def _acao(r):
    """A accao de uma linha do shoot log: `action` (o nome de hoje) ou `event` (o nome dos ficheiros arquivados antes de 2026-10-23)."""
    return r.get("action") or r.get("event") or ""


def _ler_shoot_log(path):
    linhas = []
    try:
        with open(path, newline="") as f:
            linhas = list(csv.DictReader(f))
    except OSError:
        pass
    starts = []
    for r in linhas:
        if r.get("source") == "F6" and _acao(r) == "start":
            s = _tc_secs(r.get("note"))
            if s is not None:
                starts.append({"take": r.get("take", ""), "time": r.get("timestamp", ""),
                               "tc": _tc(s), "secs": s})
    return {"rows": linhas, "f6_starts": starts}


def _cruzar(takes, log):
    """Cada take do cartao recebe a take do shoot log que comecou ao mesmo TC
    (+-TC_TOL), e as marcas, notas e camaras dessa take."""
    for t in takes:
        t.update({"log_take": "", "log_time": "", "marks": "", "log_notes": "", "cameras": ""})
        if t["start_s"] is None:
            continue
        melhor = None
        for r in log["f6_starts"]:
            d = abs(r["secs"] - t["start_s"])
            d = min(d, 86400 - d)
            if d <= TC_TOL and (melhor is None or d < melhor[0]):
                melhor = (d, r)
        if not melhor:
            continue
        r = melhor[1]
        r["_usada"] = True
        n = r["take"]
        t["log_take"], t["log_time"] = n, r["time"]
        if not n:
            continue
        mesmas = [x for x in log["rows"] if x.get("take") == n]
        t["marks"] = " ".join(_acao(x) for x in mesmas if x.get("source") == "MARK")
        t["log_notes"] = " | ".join(x.get("note", "") for x in mesmas
                                    if _acao(x) == "note" and x.get("note"))
        t["cameras"] = " ".join(sorted({x["source"] for x in mesmas
                                        if _acao(x) == "start" and x.get("source") not in ("F6",)}))


CSV_COLS = ["folder", "base", "scene", "take", "start_tc", "duration_s", "circled", "note", "tracks",
            "size_mb", "log_take", "log_time", "marks", "log_notes", "cameras", "error"]


def _csv(rep, out_dir):
    path = os.path.join(out_dir, time.strftime("f6_card_%Y%m%d-%H%M.csv"))
    try:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_COLS)
            for t in rep["takes"]:
                w.writerow([t.get(c, "") for c in CSV_COLS])
            for r in rep["log_only"]:
                w.writerow(["(not on the card)", "", "", "", r["tc"], "", "", "", "", "",
                            r["take"], r["time"], "", "", "", "shoot log has an F6 take not on this card"])
        return path
    except OSError:
        return None


def last_csv():
    return _last["csv"]


# --- explorador ---------------------------------------------------------------
def ls(rel=""):
    with Mounted(rw=False) as root:
        p = _dentro(root, rel)
        if not os.path.isdir(p):
            raise CardError("not a folder: %s" % rel)
        out = []
        for n in sorted(os.listdir(p), key=lambda x: (not os.path.isdir(os.path.join(p, x)), x.lower())):
            if n.startswith("._"):
                continue
            q = os.path.join(p, n)
            st = os.stat(q)
            out.append({"name": n, "dir": os.path.isdir(q), "size": st.st_size if not os.path.isdir(q) else None,
                        "mtime": int(st.st_mtime)})
        try:
            du = shutil.disk_usage(root)
            livre = {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
        except OSError:
            livre = {}
        rel = os.path.relpath(p, root)
        return {"path": "" if rel == "." else rel.replace(os.sep, "/"), "entries": out, **livre}


def op(kind, path="", name="", dest="", confirm=""):
    """Operacoes de escrita. Devolve uma frase do que se fez (para o event log)."""
    with Mounted(rw=True) as root:
        if kind == "mkdir":
            p = os.path.join(_dentro(root, path), _nome(name))
            if os.path.exists(p):
                raise CardError("already exists: %s" % name)
            os.mkdir(p)
            return "F6 card: folder %s created in /%s" % (name, path)
        if kind == "empty_trash":
            if confirm != "EMPTY":
                raise CardError("type EMPTY to empty the trash")
            t = os.path.join(root, TRASH)
            n = len(os.listdir(t)) if os.path.isdir(t) else 0
            if n:
                shutil.rmtree(t)
            return "F6 card: trash emptied (%d items deleted for good)" % n
        src = _dentro(root, path)
        if src == os.path.realpath(root) or not os.path.exists(src):
            raise CardError("nothing to act on: /%s" % path)
        if kind == "rename":
            dst = os.path.join(os.path.dirname(src), _nome(name))
            if os.path.exists(dst):
                raise CardError("already exists: %s" % name)
            os.rename(src, dst)
            return "F6 card: /%s renamed to %s" % (path, name)
        if kind == "move":
            d = _dentro(root, dest)
            if not os.path.isdir(d):
                raise CardError("destination is not a folder: /%s" % dest)
            if d == src or d.startswith(src + os.sep):
                raise CardError("cannot move a folder into itself")
            dst = os.path.join(d, os.path.basename(src))
            if os.path.exists(dst):
                raise CardError("already exists in /%s: %s" % (dest, os.path.basename(src)))
            shutil.move(src, dst)
            return "F6 card: /%s moved to /%s" % (path, dest)
        if kind == "trash":
            t = os.path.join(root, TRASH)
            os.makedirs(t, exist_ok=True)
            if src == os.path.realpath(t) or src.startswith(os.path.realpath(t) + os.sep):
                raise CardError("already in the trash (use empty trash)")
            dst = os.path.join(t, time.strftime("%Y%m%d-%H%M%S_") + os.path.basename(src))
            shutil.move(src, dst)
            return "F6 card: /%s moved to the trash (%s)" % (path, TRASH)
        if kind == "rename_take":
            # A take inteira (path = a pasta da take, name = a base antiga, dest =
            # a base nova): cada WAV que comeca pela base, e a pasta se comecar
            # por ela. Os metadados dentro dos WAV (SCENE...) nao mudam.
            if not os.path.isdir(src):
                raise CardError("rename_take needs the take folder")
            antiga, nova = _nome(name), _nome(dest)
            alvo = [f for f in os.listdir(src) if f.startswith(antiga) and f.lower().endswith(".wav")]
            if not alvo:
                raise CardError("no WAV starting with %s in /%s" % (antiga, path))
            novos = [nova + f[len(antiga):] for f in alvo]
            if any(os.path.exists(os.path.join(src, n)) for n in novos):
                raise CardError("a file with the new name already exists")
            for a, n in zip(alvo, novos):
                os.rename(os.path.join(src, a), os.path.join(src, n))
            pasta = os.path.basename(src)
            if pasta.startswith(antiga):
                nova_pasta = os.path.join(os.path.dirname(src), nova + pasta[len(antiga):])
                if not os.path.exists(nova_pasta):
                    os.rename(src, nova_pasta)
            return "F6 card: take %s renamed to %s (%d files)" % (antiga, nova, len(alvo))
        raise CardError("unknown operation: %s" % kind)


# --- leitor (play pela saida de som do Atom) ------------------------------------
# Formato do aplay pelo fmt do WAV: (tag, bits) -> -f. Tag 0xFFFE (extensible)
# usa o subformato. O F6 grava 24 bits inteiros ou 32 bits float.
APLAY_FMT = {(1, 16): "S16_LE", (1, 24): "S24_3LE", (1, 32): "S32_LE", (3, 32): "FLOAT_LE", (3, 64): "FLOAT64_LE"}


def wav_layout(path):
    """(formato aplay, canais, taxa, bloco, inicio dos dados, tamanho dos dados)."""
    fmt = None
    data_size = None
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise CardError("not a WAV file")
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"ds64":
                b = f.read(size)
                data_size = struct.unpack("<Q", b[8:16])[0]
            elif cid == b"fmt ":
                b = f.read(size)
                tag, ch, sr = struct.unpack("<HHI", b[:8])
                block, bits = struct.unpack("<HH", b[12:16])
                if tag == 0xFFFE and len(b) >= 26:
                    tag = struct.unpack("<H", b[24:26])[0]
                fmt = (tag, ch, sr, block, bits)
            elif cid == b"data":
                n = data_size if (size == 0xFFFFFFFF and data_size is not None) else size
                if not fmt:
                    raise CardError("WAV without a fmt chunk before the audio")
                af = APLAY_FMT.get((fmt[0], fmt[4]))
                if not af:
                    raise CardError("unsupported WAV format (tag %d, %d bits)" % (fmt[0], fmt[4]))
                return af, fmt[1], fmt[2], fmt[3], f.tell(), n
            else:
                f.seek(size, 1)
            if size % 2:
                f.seek(1, 1)
    raise CardError("WAV without audio data")


class Player:
    """Toca um WAV do cartao pela saida de som do Atom (a dos auscultadores).

    configure(device, pause): o dispositivo ALSA e a funcao que faz o gerador de
    LTC largar a saida (pause(True)) e retoma-la (pause(False))."""

    def __init__(self):
        self.device = "plughw:0,0"
        self._pause = None
        self._stop = threading.Event()
        self._thread = None
        self._st = {"file": None, "pos": 0.0, "dur": None, "error": None, "playing": False}

    def configure(self, device, pause):
        self.device, self._pause = device, pause

    def state(self):
        return dict(self._st)

    def play(self, rel, start=0.0):
        self.stop()
        with Mounted(rw=False) as root:            # so para validar antes de arrancar
            p = _dentro(root, rel)
            if not os.path.isfile(p) or not p.lower().endswith(".wav"):
                raise CardError("not a WAV file: %s" % rel)
            af, ch, sr, block, ini, n = wav_layout(p)
        if self._pause:
            ok, err = self._pause(True)
            if not ok:
                raise CardError(err)
        self._stop.clear()
        dur = n / float(block * sr)
        start = max(0.0, min(float(start or 0), dur))
        self._st = {"file": rel, "pos": start, "dur": round(dur, 1), "error": None, "playing": True}
        self._thread = threading.Thread(target=self._run, args=(rel, start), daemon=True, name="f6-play")
        self._thread.start()
        return {"file": rel, "dur": round(dur, 1)}

    def stop(self):
        t = self._thread
        if t and t.is_alive():
            self._stop.set()
            t.join(5)
        return {"stopped": True}

    def _run(self, rel, start):
        proc = None
        try:
            with Mounted(rw=False) as root:
                p = _dentro(root, rel)
                af, ch, sr, block, ini, n = wav_layout(p)
                salto = int(start * sr) * block
                proc = subprocess.Popen(["aplay", "-q", "-D", self.device, "-t", "raw", "-f", af,
                                         "-c", str(ch), "-r", str(sr), "-"],
                                        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                passo = block * (sr // 10)              # 0,1 s de cada vez
                with open(p, "rb") as f:
                    f.seek(ini + salto)
                    falta = n - salto
                    enviado = 0
                    while falta > 0 and not self._stop.is_set():
                        b = f.read(min(passo, falta))
                        if not b:
                            break
                        proc.stdin.write(b)
                        falta -= len(b)
                        enviado += len(b)
                        self._st["pos"] = round(start + enviado / float(block * sr), 1)
                proc.stdin.close()
                if self._stop.is_set():
                    proc.terminate()
                proc.wait(timeout=10)
        except (OSError, CardError, subprocess.SubprocessError, ValueError) as e:
            self._st["error"] = str(e)
            if proc:
                proc.kill()
        finally:
            self._st["playing"] = False
            if self._pause:
                self._pause(False)


player = Player()

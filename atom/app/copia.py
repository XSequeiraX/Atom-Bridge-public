"""Copia de seguranca da configuracao do rig num so ficheiro (2026-10-14): guardar numa pen USB e repor (no mesmo Atom
ou num Atom novo). Se o disco do Atom morrer, o rig volta em minutos.

O ficheiro e JSON legivel: {"kind": "bridge-config", "version": 1, "made", "host", "files": {nome: conteudo}}.
Leva a configuracao (camaras, iPhones, gateways, perfis, USB, definicoes) e NUNCA segredos: sem o token dos
gateways (fica no Atom; num Atom novo, os gateways configuram-se outra vez pela USB), sem passwords de Wi-Fi, sem o PIN
da pagina e sem a password do SFTP (ao repor, fica a que o Atom ja tiver).
"""
import json
import os
import re
import socket
import subprocess
import time

import f6_card
import gravar

AQUI = os.path.dirname(os.path.abspath(__file__))
FICHEIROS = ("settings.json", "c3.json", "bmpccs.json", "iphones.json", "gopros.json", "devices.json",
             "usb_map.json", "gw_fw.json", "profiles.json", "sftp.json", "sonys.json")
SEGREDOS = {"sftp.json": ("password",), "sonys.json": ()}     # chaves que nunca saem do Atom (sonys: a de cada camara)
TIPO = "bridge-config"
MONTAGEM = "/mnt/bridge-backup"
NOME_RE = re.compile(r"bridge-config-[\w.\-]+\.json")


def fazer(pasta=AQUI):
    files = {}
    for n in FICHEIROS:
        try:
            d = gravar.ler_json(os.path.join(pasta, n))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            for k in SEGREDOS.get(n, ()):
                d.pop(k, None)
            if n == "sonys.json":
                d = _sony_sem_pw(d)
        files[n] = d
    return {"kind": TIPO, "version": 1, "made": time.strftime("%Y-%m-%d %H:%M:%S"), "host": socket.gethostname(),
            "files": files}


# O tipo de cada ficheiro (todos objectos JSON, como a app os carrega) e o das chaves que, se existirem, tem de ter forma
# certa: um "c3.json": [] ou null da pen deitava abaixo o arranque seguinte.
TIPOS = {
    "settings.json": {"rec_targets": list},
    "c3.json": {},
    "bmpccs.json": {"cams": dict},
    "iphones.json": {"iphones": dict},
    "gopros.json": {"gateways": list},
    "devices.json": {},
    "usb_map.json": {"devices": dict},
    "gw_fw.json": {},
    "profiles.json": {},
    "sftp.json": {"host": str, "user": str, "path": str},
    "sonys.json": {"sonys": dict, "removed": dict},
}


def _sony_sem_pw(d):
    """sonys.json sem a password SSH de cada camara (nunca vai para a pen)."""
    out = dict(d)
    for grupo in ("sonys", "removed"):
        if isinstance(d.get(grupo), dict):
            out[grupo] = {k: ({kk: vv for kk, vv in v.items() if kk != "password"} if isinstance(v, dict) else v)
                          for k, v in d[grupo].items()}
    return out


def _sony_pw_atual(conteudo, atual):
    """As passwords SSH que este Atom ja tem ficam, na camara com o mesmo id, endereco e utilizador."""
    tem = {}
    for grupo in ("sonys", "removed"):
        for k, v in (atual.get(grupo) or {}).items() if isinstance(atual.get(grupo), dict) else ():
            if isinstance(v, dict) and v.get("password"):
                tem[k] = v
    for grupo in ("sonys", "removed"):
        for k, v in (conteudo.get(grupo) or {}).items() if isinstance(conteudo.get(grupo), dict) else ():
            a = tem.get(k)
            if isinstance(v, dict) and a and a.get("host") == v.get("host") and a.get("user") == v.get("user"):
                v["password"] = a["password"]
    return conteudo
HOSTKEYS = "sftp_known_hosts"                    # a chave do servidor SFTP em que o Atom confia (bridge.py)


def _nome_tipo(t):
    return {dict: "an object", list: "a list", str: "a string"}.get(t, t.__name__)


def validar(copia):
    """Os ficheiros a repor ({nome: conteudo}) ou levanta ValueError. Nada se escreve antes de tudo estar validado."""
    if not isinstance(copia, dict) or copia.get("kind") != TIPO or not isinstance(copia.get("files"), dict):
        raise ValueError("not a bridge configuration backup")
    if copia.get("version") != 1:
        raise ValueError("backup version %s is not supported" % copia.get("version"))
    files = {n: c for n, c in copia["files"].items() if n in FICHEIROS}
    if not files:
        raise ValueError("the backup has no configuration files")
    for n, c in files.items():
        if not isinstance(c, dict):
            raise ValueError("%s in the backup must be a JSON object, not %s" % (
                n, "null" if c is None else type(c).__name__))
        for chave, tipo in TIPOS.get(n, {}).items():
            if chave in c and not isinstance(c[chave], tipo):
                raise ValueError("%s in the backup: \"%s\" must be %s" % (n, chave, _nome_tipo(tipo)))
        if n == "profiles.json" and not all(isinstance(v, dict) for v in c.values()):
            raise ValueError("profiles.json in the backup: every profile must be an object")
        if n == "sftp.json":
            porta = c.get("port", 22)
            if isinstance(porta, bool) or not isinstance(porta, int) or not 1 <= porta <= 65535:
                raise ValueError("sftp.json in the backup: \"port\" must be a number from 1 to 65535")
    return files


def _modo(final, segredo):
    """O modo do ficheiro: 0600 nos que levam segredos; senao o que o ficheiro actual ja tem (ou 0644)."""
    if segredo:
        return 0o600
    try:
        return os.stat(final).st_mode & 0o777
    except OSError:
        return 0o644


def _preparar(final, conteudo, segredo):
    """Escreve o ficheiro temporario (modo certo desde a criacao, no disco) e devolve o seu caminho."""
    tmp = "%s.restore-%d.tmp" % (final, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _modo(final, segredo))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(conteudo, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _modo(final, segredo))            # o umask nao pode tirar ou dar mais do que o pedido
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


def _sftp_conta(d):
    # O nome do servidor nao distingue maiusculas nem o ponto final ("H" e "h." sao o mesmo): senao a palavra-passe e a chave
    # de confianca perdiam-se por uma copia escrita de outra forma. O utilizador distingue-as; a porta "22" e 22 sao a mesma.
    host = str(d.get("host") or "").strip().lower().rstrip(".")
    porta = d.get("port", 22)
    try:
        porta = int(porta)
    except (TypeError, ValueError):
        pass
    return (host, str(d.get("user") or ""), porta)


def repor(copia, pasta=AQUI):
    """Escreve os ficheiros da copia (os que ela tem; os outros ficam). Devolve os nomes escritos (o do sftp.json diz o
    que se perdeu, se o servidor mudou). Tudo ou nada: valida tudo, prepara todos os ficheiros temporarios e so depois os
    troca; se algo falhar, os que ja tinham sido trocados voltam ao que eram e os temporarios saem."""
    files = validar(copia)
    preparados, avisos, descartar = {}, {}, []
    try:
        for n, conteudo in files.items():
            final = os.path.join(pasta, n)
            segredo = n in SEGREDOS
            if segredo:
                conteudo = dict(conteudo)
                try:
                    atual = gravar.ler_json(final)        # os segredos que este Atom ja tem ficam
                except (OSError, ValueError):
                    atual = {}
                atual = atual if isinstance(atual, dict) else {}
                for k in SEGREDOS[n]:
                    conteudo.pop(k, None)                 # um segredo nunca vem da pen
                if n == "sonys.json":
                    conteudo = _sony_pw_atual(_sony_sem_pw(conteudo), atual)
                if n == "sftp.json":
                    hostkeys = os.path.join(pasta, HOSTKEYS)
                    if _sftp_conta(atual) == _sftp_conta(conteudo):
                        for k in SEGREDOS[n]:
                            if k in atual:
                                conteudo[k] = atual[k]
                    else:
                        # Outro servidor: a palavra-passe e a chave de confianca do anterior nao vao para ele.
                        perdeu = [k for k in SEGREDOS[n] if atual.get(k)]
                        if os.path.exists(hostkeys):
                            perdeu.append("trusted host key")
                            descartar.append(hostkeys)
                        if perdeu:
                            avisos[n] = ("different server: dropped the saved %s; enter the password again and the "
                                         "server key is trusted on the first connection" % " and ".join(perdeu))
            preparados[n] = _preparar(final, conteudo, segredo)
        antes = {}
        for n in preparados:                              # o que havia, para voltar atras
            try:
                with open(os.path.join(pasta, n), "rb") as f:
                    antes[n] = f.read()
            except OSError:
                antes[n] = None
        feitos = []
        try:
            for n, tmp in list(preparados.items()):
                final = os.path.join(pasta, n)
                gravar.trocar(tmp, final)
                del preparados[n]
                feitos.append(n)
                if n in SEGREDOS:
                    try:
                        os.chmod(final + ".bak", 0o600)   # a copia anterior tambem levava a palavra-passe
                    except OSError:
                        pass
        except BaseException:
            for n in feitos:                              # desfaz os ja trocados
                final = os.path.join(pasta, n)
                try:
                    if antes[n] is None:
                        os.remove(final)
                    else:
                        with open(final, "wb") as f:
                            f.write(antes[n])
                            f.flush()
                            os.fsync(f.fileno())
                except OSError:
                    pass
            raise
    finally:
        for tmp in preparados.values():                   # os temporarios que sobraram (erro a meio)
            try:
                os.remove(tmp)
            except OSError:
                pass
    for caminho in descartar:
        try:
            os.remove(caminho)
        except OSError:
            pass
    return ["%s (%s)" % (n, avisos[n]) if n in avisos else n for n in files]


def nome_ficheiro(copia):
    host = re.sub(r"[^\w.\-]", "", copia.get("host") or "atom")[:30] or "atom"
    return "bridge-config-%s-%s.json" % (host, time.strftime("%Y%m%d-%H%M%S"))


# ---- pen USB -------------------------------------------------------------------------------------------------------
def _correr(cmd, timeout=20):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or "%s failed" % cmd[0]).strip()[:200])
    return r.stdout


def pens(lsblk_json=None):
    """As particoes de pens USB: [{dev, label, fstype, mountpoint}] (lsblk)."""
    if lsblk_json is None:
        lsblk_json = _correr(["lsblk", "-J", "-p", "-o", "NAME,TRAN,RM,TYPE,FSTYPE,LABEL,MOUNTPOINT"])
    out = []

    def ver(no, usb):
        usb = usb or no.get("tran") == "usb"
        if no.get("tran") == "usb" and f6_card.e_zoom(no["name"]):
            return                          # o F6 em SD Card Reader: o cartao do som nao e uma pen
        if no.get("type") == "part" and usb and no.get("fstype"):
            out.append({"dev": no["name"], "label": no.get("label") or "", "fstype": no["fstype"],
                        "mountpoint": no.get("mountpoint")})
        for f in no.get("children") or []:
            ver(f, usb)
    for d in json.loads(lsblk_json).get("blockdevices") or []:
        ver(d, False)
    return out


class Pen:
    """Monta a pen (se ainda nao estiver montada) e desmonta no fim: with Pen() as pasta: ..."""

    def __init__(self, escrever=False):
        self.escrever, self._montei, self.pasta, self.label = escrever, False, None, ""

    def __enter__(self):
        ps = pens()
        if not ps:
            raise OSError("no USB stick found: plug one into the Atom")
        p = ps[0]
        self.label = p["label"] or p["dev"]
        if p["mountpoint"]:
            self.pasta = p["mountpoint"]
        else:
            os.makedirs(MONTAGEM, exist_ok=True)
            _correr(["mount", "-o", "rw" if self.escrever else "ro", p["dev"], MONTAGEM])
            self._montei, self.pasta = True, MONTAGEM
        return self.pasta

    def __exit__(self, *a):
        if self._montei:
            try:
                _correr(["sync"])
                _correr(["umount", MONTAGEM])
            except (OSError, subprocess.SubprocessError):
                pass


def guardar_na_pen(copia):
    with Pen(escrever=True) as pasta:
        nome = nome_ficheiro(copia)
        tmp = os.path.join(pasta, nome + ".tmp")
        with open(tmp, "w") as f:
            json.dump(copia, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(pasta, nome))
        return nome


def listar_na_pen():
    with Pen() as pasta:
        out = []
        for n in sorted(os.listdir(pasta), reverse=True):
            if NOME_RE.fullmatch(n):
                out.append({"name": n, "size": os.path.getsize(os.path.join(pasta, n))})
        return out


def ler_da_pen(nome):
    if not isinstance(nome, str) or not NOME_RE.fullmatch(nome):
        raise ValueError("not a backup file name")
    with Pen() as pasta:
        with open(os.path.join(pasta, nome)) as f:
            return json.load(f)

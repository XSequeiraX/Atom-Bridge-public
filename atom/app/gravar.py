"""Gravar e ler os ficheiros de configuracao sem os perder num corte de energia (2026-10-14).

O Atom pode ficar sem corrente a qualquer momento. Escrever para um .tmp e trocar (os.replace) evita um ficheiro
meio escrito, mas sem fsync o conteudo pode ainda nao estar no disco: depois de um corte o ficheiro aparecia vazio e
as Canons, os iPhones ou o endereco do C3 desapareciam sem aviso. Aqui:
  - trocar(tmp, final): forca o .tmp para o disco, guarda o anterior em final.bak, troca e forca a pasta;
  - ler_json(final): le o ficheiro; se estiver vazio ou estragado, usa o .bak.
"""
import itertools
import json
import os
import shutil
import threading

_lock = threading.Lock()             # um trocar() de cada vez (o .bak e o replace de dois escritores nao se misturam)
_n = itertools.count(1)


def tmp_unico(final):
    """Um nome de .tmp so deste pedido (pid + thread + contador): duas gravacoes ao mesmo tempo do mesmo ficheiro nunca
    partilham o .tmp (partilhado, uma escrevia por cima da outra e o os.replace da segunda dava ENOENT)."""
    return "%s.%d.%d.%d.tmp" % (final, os.getpid(), threading.get_ident(), next(_n))


def _json_ok(caminho):
    """True se `caminho` e um JSON valido; um ficheiro que nao e .json conta como bom se nao estiver vazio."""
    try:
        if not caminho.endswith(".json"):
            return os.path.getsize(caminho) > 0
        with open(caminho) as f:
            json.load(f)
        return True
    except (OSError, ValueError):
        return False


def _fsync_pasta(caminho):
    try:
        fd = os.open(os.path.dirname(os.path.abspath(caminho)) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def trocar(tmp, final):
    """Substitui `final` por `tmp` (ja escrito e fechado), com o conteudo no disco e uma copia do anterior em .bak. So se
    copia para .bak um `final` que ainda le: um estragado nunca apaga o bom que la estava."""
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    with _lock:
        if os.path.isfile(final) and _json_ok(final):
            try:
                shutil.copy2(final, final + ".bak")
            except OSError:
                pass
        os.replace(tmp, final)
    _fsync_pasta(final)


def ler_json(final):
    """O JSON de `final`; se faltar ou estiver estragado e houver .bak bom, o do .bak. Senao levanta o erro original
    (OSError ou ValueError), como o json.load."""
    try:
        with open(final) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        erro = e
    try:
        with open(final + ".bak") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise erro

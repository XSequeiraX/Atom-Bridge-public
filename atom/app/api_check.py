#!/usr/bin/env python3
"""Verifica a API do bridge sem o arrancar: as regras de nomes das rotas, e se todos os clientes chamam rotas que existem.

  python3 api_check.py            verifica (sai com 1 se houver erros); tambem diz se docs/API.md esta desactualizado
  python3 api_check.py --docs     reescreve docs/API.md a partir das rotas do bridge (nucleo.py e rotas_*.py)

Le o bridge (nucleo.py e rotas_*.py) com o ast (nao o importa: precisa de dbus) e os clientes (ui.html e as outras paginas, bridge-ctl, bridge_client,
stress_bridge...) com um leitor simples de expressoes de texto: 'canon/' + id + '/af' fica canon/<x>/af.

REGRAS (a API cresce sem ambiguidade):
  1. /api/<dominio>/...  Os dominios sao os de DOMINIOS. Paginas HTML ficam na raiz (/view, /shootlog...), nunca em /api.
  2. Segmentos em minusculas e snake_case. O parametro de caminho chama-se <id> (o id do device; <name> so em
     /setting/<name>).
  3. Todo o aparelho tem UM id unico e estavel; o tipo e um campo (`type`), nunca parte do caminho. Tipos: canon, iphone,
     gopro, bmpcc (a principal, id fixo "bmpcc"), bmpcc_extra (as de cada C3), sony, f6 (id fixo "f6"). Tudo o que e de um
     aparelho vive em /api/devices/<id>/<accao> (GET le, POST muda; nao ha DELETE: remove, restore e forget sao POST).
     O dominio devices tem so: GET|POST /api/devices (lista | acrescenta, corpo {"type": ...}), GET /api/devices/<id>,
     POST /api/devices/batch (accoes sobre varios: order, reset, settings_polling, lead) e as accoes /api/devices/<id>/<accao>.
     A docstring de cada accao comeca por [tipos] (ex.: "[canon, bmpcc] Liga ...") e o verificador confere que os tipos
     existem, que sao os que o handler aceita (_dev_exige) e que cada (metodo, accao) aparece uma so vez.
     Ids reservados (nenhum device novo os leva): bmpcc, f6, batch e os que as frotas geram (iphone*, bmN, gpN, sony*).
  4. Gateways: /api/gateways/<c3|s3|usb|firmware>/...  Tempo: /api/time/...  Registos: /api/log/...  Claquete: /api/slate/...
     Os nomes de firmware usam "firmware" (nunca "fw"). O mesmo caminho pode ter GET (le) e POST (muda).
  5. Respostas: todo o POST e todo o erro devolvem JSON {"ok": bool, "error": texto}. Estados HTTP: 400 pedido mal feito,
     404 id desconhecido, 409 o estado nao deixa (a gravar, desligado, ocupado), 502/504 o aparelho nao respondeu.
"""
import ast
import glob
import os
import re
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(AQUI))
DOCS = os.path.join(RAIZ, "docs", "API.md")

DOMINIOS = {
    "login", "logout", "security",                    # acesso
    "status", "preflight", "system", "services", "demo", "profiles", "backup", "fault", "net",
    "rec", "devices", "slate", "log", "time",
    "gateways", "bluetooth",
}
TIPOS = ("canon", "mobile", "gopro", "bmpcc", "bmpcc_extra", "sony", "f6")
PARAMETROS = {"id", "name"}
PAGINAS_FORA_DE_API = {"/login", "/", "/view", "/shootlog", "/eventlog", "/f6card", "/gopro_qr", "/manifest.json",
                       "/sw.js", "/icon-192.png", "/icon-512.png", "/favicon.ico", "/fonts/<path:nome>"}

CLIENTES = ["ui.html", "shootlog.html", "eventlog.html", "f6card.html", "view.html", "gopro_qr.html",
            "bridge_client.py", "usb_bridge.py", "stress_bridge.py", "acesso.py"]
CLIENTES_RAIZ = ["bridge-ctl.py"]
# Funcoes de JavaScript que recebem um caminho: nome -> posicao do argumento (os restantes pedem /api/ + argumento)
JS_AJUDANTES = {"api": 0, "iphAct": 2, "bmAct": 2, "gpAct": 2, "soAct": 2, "f6Act": 1, "flashResult": 1, "savedAct": 1}


# ---- as rotas ---------------------------------------------------------------------------------
def rotas():
    """[{path, methods, func, doc, file}] das @app.route do bridge (nucleo.py e os modulos rotas_*.py)."""
    out = []
    for f in FICHEIROS_BRIDGE():
        out += _rotas_de(f)
    return sorted(out, key=lambda r: (r["path"], r["methods"]))


def FICHEIROS_BRIDGE():
    return [os.path.join(AQUI, "nucleo.py")] + sorted(glob.glob(os.path.join(AQUI, "rotas_*.py")))


def _rotas_de(caminho):
    arv = ast.parse(open(caminho, encoding="utf-8").read())
    out = []
    for n in ast.walk(arv):
        if not isinstance(n, ast.FunctionDef):
            continue
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route":
                metodos = ["GET"]
                for k in d.keywords:
                    if k.arg == "methods":
                        metodos = [e.value for e in k.value.elts]
                doc = " ".join((ast.get_docstring(n) or "").strip().split("\n\n")[0].split())
                out.append({"path": d.args[0].value, "methods": metodos, "func": n.name, "doc": doc, "line": n.lineno,
                            "file": os.path.basename(caminho), "exige": _exige(n)})
    return out


def _exige(func):
    """(tipos | None, accao) da chamada a _dev_exige(id, tipos, accao, ...) dentro do handler; None se nao a chama.
    `tipos` None quer dizer todos os tipos."""
    for no in ast.walk(func):
        if isinstance(no, ast.Call) and getattr(no.func, "id", "") == "_dev_exige" and len(no.args) >= 3:
            t, a = no.args[1], no.args[2]
            tipos = None if isinstance(t, ast.Constant) else tuple(e.value for e in getattr(t, "elts", []))
            return tipos, getattr(a, "value", None)
    return None


IDS_FIXOS = {"bmpcc", "f6", "batch"}


def segmentos(caminho):
    return [s for s in caminho.split("/") if s]


def e_parametro(seg):
    return seg.startswith("<") and seg.endswith(">")


def combina(cliente, rota):
    """O caminho do cliente (com <x> onde ha uma variavel) cabe na rota?"""
    a, b = segmentos(cliente.split("?")[0]), segmentos(rota)
    if len(a) != len(b):
        return False
    return all(x == y or e_parametro(x) or e_parametro(y) for x, y in zip(a, b))


# ---- regras de nomes --------------------------------------------------------------------------
def erros_de_nomes(rs):
    erros = []
    vistos = {}
    for r in rs:
        p, m = r["path"], r["methods"]
        for met in m:
            chave = (p, met)
            if chave in vistos:
                erros.append("%s %s: declarada duas vezes (%s e %s)" % (met, p, vistos[chave], r["func"]))
            vistos[chave] = r["func"]
        if not p.startswith("/api/"):
            if p not in PAGINAS_FORA_DE_API:
                erros.append("%s: fora de /api e nao e uma pagina conhecida (ver PAGINAS_FORA_DE_API)" % p)
            continue
        segs = segmentos(p)
        if segs[1] not in DOMINIOS:
            erros.append("%s: dominio '%s' nao esta em DOMINIOS" % (p, segs[1]))
        for s in segs:
            if e_parametro(s):
                nome = s[1:-1].split(":")[-1]
                if nome not in PARAMETROS:
                    erros.append("%s: parametro <%s> (usar <id>)" % (p, nome))
            elif not re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*(\.[a-z0-9]+)?", s):
                erros.append("%s: segmento '%s' nao e snake_case minusculo" % (p, s))
        if segs[1] == "devices":
            erros += _erros_devices(r, segs)
        elif any(e_parametro(x) and x[1:-1] == "id" for x in segs):
            erros.append("%s: <id> so existe em /api/devices/<id>/..." % p)
        if "fw" in segs:
            erros.append("%s: usar 'firmware', nao 'fw'" % p)
    # um device so tem uma forma de REC individual
    toggles = [r["path"] for r in rs if re.search(r"/(toggle|toggle_rec)$", r["path"])]
    for t in toggles:
        erros.append("%s: usar POST /api/devices/<id>/rec" % t)
    # cada (metodo, accao) aparece uma so vez (a accao e tudo o que vem depois de <id>)
    acoes = {}
    for r in rs:
        segs = segmentos(r["path"])
        if len(segs) >= 4 and segs[1] == "devices" and segs[2] == "<id>":
            acao = _acao(segs)
            for met in r["methods"]:
                if (met, acao) in acoes:
                    erros.append("%s %s: accao repetida (%s e %s)" % (met, acao, acoes[(met, acao)], r["func"]))
                acoes[(met, acao)] = r["func"]
    return erros


def _acao(segs):
    """/api/devices/<id>/setting/<name> -> setting/<name>; /api/devices/<id>/card/ls -> card/ls."""
    return "/".join(segs[3:])


def tipos_da_doc(doc):
    """('[canon, bmpcc] Liga ...') -> (['canon', 'bmpcc'], 'Liga ...'); (None, doc) se nao comeca por [tipos]."""
    m = re.match(r"\[([a-z0-9_, ]+)\]\s*(.*)", doc)
    if not m:
        return None, doc
    return [t.strip() for t in m.group(1).split(",") if t.strip()], m.group(2)


def _erros_devices(r, segs):
    """O dominio devices: /api/devices, /api/devices/batch, /api/devices/<id>, /api/devices/<id>/<accao>."""
    p, erros = r["path"], []
    if len(segs) == 2:
        return erros
    if segs[2] == "batch":
        if len(segs) != 3 or r["methods"] != ["POST"]:
            erros.append("%s: batch so existe como POST /api/devices/batch" % p)
        return erros
    if segs[2] != "<id>":
        erros.append("%s: o 2.o segmento de devices e <id> (ou o literal batch)" % p)
        return erros
    if len(segs) == 3:
        return erros
    acao = _acao(segs)
    tipos, _ = tipos_da_doc(r["doc"])
    if tipos is None:
        erros.append("%s: a docstring tem de comecar por [tipos] (ex.: [canon, bmpcc] Liga ...)" % p)
        return erros
    for t in tipos:
        if t not in TIPOS:
            erros.append("%s: tipo '%s' da docstring nao existe (%s)" % (p, t, ", ".join(TIPOS)))
    ex = r.get("exige")
    if ex is None:
        erros.append("%s: o handler %s nao chama _dev_exige (404/409 uniformes)" % (p, r["func"]))
    else:
        usados, nome = ex
        if sorted(usados or TIPOS) != sorted(tipos):
            erros.append("%s: a docstring diz [%s] mas _dev_exige aceita %s" % (p, ", ".join(tipos), list(usados or TIPOS)))
        if nome != acao.split("/<")[0]:
            erros.append("%s: _dev_exige(... '%s') nao e a accao da rota (%s)" % (p, nome, acao))
    return erros


# ---- os clientes ------------------------------------------------------------------------------
def _texto_js(expr):
    """'canon/' + id + '/af'  ->  canon/<x>/af.  Texto entre aspas fica; o resto e uma variavel (<x>)."""
    partes, atual, aspa, i, prof = [], "", None, 0, 0
    while i < len(expr):
        c = expr[i]
        if aspa:
            if c == "\\":
                atual += expr[i:i + 2]
                i += 2
                continue
            atual += c
            if c == aspa:
                aspa = None
            i += 1
            continue
        if c in "'\"`":
            aspa = c
            atual += c
        elif c in "([{":
            prof += 1
            atual += c
        elif c in ")]}":
            prof -= 1
            atual += c
        elif c == "+" and prof == 0:
            partes.append(atual.strip())
            atual = ""
        else:
            atual += c
        i += 1
    partes.append(atual.strip())
    out = ""
    for p in partes:
        if len(p) >= 2 and p[0] in "'\"`" and p[-1] == p[0]:
            out += re.sub(r"\$\{[^}]*\}", "<x>", p[1:-1])
        elif p:
            out += "<x>"
    return out


def _argumentos(texto, ini):
    """Os argumentos de topo da chamada cujo '(' esta em texto[ini]; (lista, fim)."""
    args, atual, prof, aspa, i = [], "", 0, None, ini
    while i < len(texto):
        c = texto[i]
        if aspa:
            atual += c
            if c == "\\":
                atual += texto[i + 1]
                i += 1
            elif c == aspa:
                aspa = None
        elif c in "'\"`":
            aspa = c
            atual += c
        elif c in "([{":
            prof += 1
            if prof > 1:
                atual += c
        elif c in ")]}":
            prof -= 1
            if prof == 0:
                args.append(atual)
                return args, i
            atual += c
        elif c == "," and prof == 1:
            args.append(atual)
            atual = ""
        else:
            atual += c
        i += 1
    return args, len(texto)


def chamadas_js(texto):
    """[(caminho, linha)] dos pedidos a API num HTML/JS: as chamadas dos ajudantes, e qualquer texto '/api/...' entre aspas
    (href, call('/api/x'), url = '/api/x'), para nada escapar por ter outro nome de funcao."""
    out = []
    for m in re.finditer(r"""(['"`])(/api/[A-Za-z0-9_./\-]*)""", texto):
        resto = texto[m.end():m.end() + 40]
        t = m.group(2)
        if re.match(r"\s*['\"`]?\s*\+", resto):          # '/api/x/' + id + '/y': o fim e dinamico (ja visto pelos ajudantes)
            continue
        if t.rstrip("/") != "/api":
            out.append((t, texto.count("\n", 0, m.start()) + 1))
    for m in re.finditer(r"\b(fetch|%s)\s*\(" % "|".join(JS_AJUDANTES), texto):
        nome = m.group(1)
        args, _ = _argumentos(texto, m.end() - 1)
        pos = 0 if nome == "fetch" else JS_AJUDANTES[nome]
        if pos >= len(args):
            continue
        t = _texto_js(args[pos])
        if nome != "fetch" and not t.startswith("/api/"):
            t = "/api/" + t.lstrip("/")
        linha = texto.count("\n", 0, m.start()) + 1
        t = re.sub(r"(?<=[^/>])<x>$", "", t)             # '/api/x' + (cond ? '?a=1' : ''): so uma query
        if t.startswith("/api/") and t != "/api/<x>":
            out.append((t, linha))
    return out


def _texto_py(no):
    """Reconstroi o texto de uma expressao ('/api/%s/x' % i, f-strings, somas); None se nao for texto."""
    if isinstance(no, ast.Constant) and isinstance(no.value, str):
        return no.value
    if isinstance(no, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "<x>" for v in no.values)
    if isinstance(no, ast.BinOp) and isinstance(no.op, ast.Mod):
        e = _texto_py(no.left)
        return re.sub(r"%[sdr]", "<x>", e) if e is not None else None
    if isinstance(no, ast.BinOp) and isinstance(no.op, ast.Add):
        a, b = _texto_py(no.left), _texto_py(no.right)
        if a is None and b is None:
            return None
        return (a if a is not None else "<x>") + (b if b is not None else "<x>")
    return None


def chamadas_py(texto):
    """[(caminho, linha)]. As chamadas com base= falam com outro servico (um gateway...), nao com o bridge."""
    out, vistos = [], set()
    arv = ast.parse(texto)
    for no in ast.walk(arv):
        if isinstance(no, ast.Call) and any(k.arg == "base" for k in no.keywords):
            for f in ast.walk(no):
                vistos.add(id(f))
    for no in ast.walk(arv):
        if id(no) in vistos:
            continue
        if isinstance(no, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            t = _texto_py(no)
            if t and t.startswith("/api/"):
                out.append((re.sub(r"\{[^}]*\}", "<x>", t), no.lineno))
                for f in ast.walk(no):
                    vistos.add(id(f))
    return out


def clientes():
    """{ficheiro: [(caminho, linha)]}"""
    out = {}
    for nome in CLIENTES + CLIENTES_RAIZ:
        f = os.path.join(RAIZ if nome in CLIENTES_RAIZ else AQUI, nome)
        if not os.path.isfile(f):
            continue
        t = open(f, encoding="utf-8", errors="replace").read()
        out[nome] = chamadas_py(t) if nome.endswith(".py") else chamadas_js(t)
    return out


def erros_de_clientes(rs):
    erros = []
    for nome, chamadas in clientes().items():
        for caminho, linha in chamadas:
            c = caminho.split("?")[0].rstrip("/")
            segs = segmentos(c)
            # Um nome de tipo (ou outro literal) no lugar do <id> casava com a rota /api/devices/<id>/... e passava: so os ids
            # fixos (bmpcc, f6) e a palavra batch podem ser literais nessa posicao.
            if len(segs) >= 3 and segs[1] == "devices" and not e_parametro(segs[2]) and segs[2] not in IDS_FIXOS:
                erros.append("%s:%d chama %s: '%s' no lugar do <id> (so %s sao literais ali)"
                             % (nome, linha, caminho, segs[2], ", ".join(sorted(IDS_FIXOS))))
                continue
            if c in ("/api", "/api/<x>") or not any(combina(c, r["path"]) for r in rs):
                if c.rstrip("/") + "/" and any(r["path"].startswith(c + "/") for r in rs) and c.count("/") < 3:
                    continue                    # so um prefixo a que se acrescenta o resto
                erros.append("%s:%d chama %s, que nao existe no bridge" % (nome, linha, caminho))
    return erros


# ---- documentacao -----------------------------------------------------------------------------
def _linha(r):
    return "| %s | `%s` | %s |" % (",".join(r["methods"]), r["path"], r["doc"].replace("|", "/"))


def documentacao(rs):
    grupos, acoes = {}, {}
    for r in rs:
        segs = segmentos(r["path"])
        if len(segs) >= 4 and segs[1] == "devices" and segs[2] == "<id>":
            acoes.setdefault(_acao(segs), []).append(r)
            continue
        chave = segs[1] if r["path"].startswith("/api/") and len(segs) > 1 else "(paginas)"
        grupos.setdefault(chave, []).append(r)
    L = ["# API do bridge", "",
         "Gerado por `atom/app/api_check.py --docs` a partir das rotas do bridge (`nucleo.py`, `rotas_*.py`): nao se edita a mao. As regras estao no topo do script.", ""]
    for g in sorted(grupos, key=lambda k: (k == "(paginas)", k)):
        L += ["## " + g, "", "| Metodo | Caminho | O que faz |", "|---|---|---|"]
        for r in sorted(grupos[g], key=lambda r: (r["path"], r["methods"])):
            L.append(_linha(r))
        L.append("")
        if g == "devices":
            L += ["### `devices/<id>/<accao>`", "",
                  "Tudo o que e de um aparelho. O id e o do `GET /api/devices`; 404 se nao existe, 409 se o tipo nao tem a accao ou o "
                  "aparelho esta retirado (so restore e forget valem). Tipos: " + ", ".join(TIPOS) + ".", "",
                  "| Metodo | Accao | Tipos | O que faz |", "|---|---|---|---|"]
            for acao in sorted(acoes):
                for r in sorted(acoes[acao], key=lambda r: r["methods"]):
                    tipos, texto = tipos_da_doc(r["doc"])
                    L.append("| %s | `%s` | %s | %s |" % (",".join(r["methods"]), acao, ", ".join(tipos or []),
                                                         texto.replace("|", "/")))
            L.append("")
    return "\n".join(L)


def main():
    rs = rotas()
    if "--docs" in sys.argv:
        os.makedirs(os.path.dirname(DOCS), exist_ok=True)
        open(DOCS, "w", encoding="utf-8").write(documentacao(rs))
        print("escrito", DOCS)
        return 0
    erros = erros_de_nomes(rs) + erros_de_clientes(rs)
    esperado = documentacao(rs)
    try:
        atual = open(DOCS, encoding="utf-8").read()
    except OSError:
        atual = ""
    if atual != esperado:
        erros.append("docs/API.md desactualizado: correr python3 atom/app/api_check.py --docs")
    for e in erros:
        print("ERRO", e)
    print("%d rotas, %d clientes, %d erro(s)" % (len(rs), len(clientes()), len(erros)))
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())

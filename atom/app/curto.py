"""Erros de rede em texto curto, para a consola, a pagina e o registo de eventos.

As bibliotecas (requests, urllib3, urllib, http.client) devolvem textos como
"HTTPConnectionPool(host='10.255.255.1', port=8080): Max retries exceeded with url: /x
(Caused by ConnectTimeoutError(<...>, 'Connection to ... timed out. (connect timeout=1)'))",
que nao cabem numa linha e nao dizem nada a quem esta a filmar. `curto()` troca o texto
cru pela causa ("no answer (timeout)", "connection refused", ...) e deixa o resto da
mensagem como esta ("Connect failed: no answer (timeout)").

A consola (bridge-ctl.py) e um ficheiro so e tem uma copia desta funcao; o CI confirma
que as duas dao o mesmo resultado.
"""

import re

# Ordem importa: a primeira causa que aparece no texto ganha.
_CAUSAS = [
    (re.compile(r"timed? ?out|ConnectTimeout|ReadTimeout|TimeoutError", re.I), "no answer (timeout)"),
    (re.compile(r"Connection refused|Errno 111\b|ConnectionRefusedError", re.I), "connection refused"),
    (re.compile(r"No route to host|Network is unreachable|Errno 11[03]\b|Errno 101\b", re.I), "unreachable"),
    (re.compile(r"Name or service not known|Temporary failure in name resolution|getaddrinfo failed|"
                r"nodename nor servname|NameResolutionError|Errno -[23]\b", re.I), "name not found"),
    (re.compile(r"Connection reset|RemoteDisconnected|Connection aborted|ConnectionResetError|"
                r"BrokenPipe|Broken pipe|closed connection without response", re.I), "connection dropped"),
]
_HTTP = re.compile(r"\b(\d{3}) (?:Client|Server) Error: ([^\n]*?) for url: \S+")
_URLOPEN = re.compile(r"<urlopen error (.*)>", re.S)
_HTTP2 = re.compile(r"HTTP Error (\d{3}): ")
_TIPO = re.compile(r"\b[A-Za-z_][\w.]*(?:Error|Exception|Timeout|Disconnected): ")
# Onde comeca o texto cru das bibliotecas (o que se troca pela causa).
_CRU = re.compile(r"HTTPS?ConnectionPool\(|<urlopen error|\(Caused by|Max retries exceeded|\('Connection aborted|"
                  r"\[Errno -?\d+\]|[A-Za-z_][\w.]*(?:Error|Exception|Timeout|Disconnected)\(|"
                  r"\b(?:[A-Za-z_][\w.]*(?:Error|Timeout|Disconnected)): ")


def curto(texto):
    """O texto com o erro de rede encurtado. Sem erro de rede reconhecido, fica igual."""
    if texto is None:
        return texto
    s = str(texto)
    m = _HTTP.search(s)
    if m:
        return (s[:m.start()] + "HTTP %s %s" % (m.group(1), m.group(2).strip())).strip()
    for padrao, causa in _CAUSAS:
        if padrao.search(s):
            c = _CRU.search(s)
            if c is None:
                return s
            antes = s[:c.start()].rstrip(" :-(")
            return (antes + ": " + causa) if antes else causa
    m = _URLOPEN.search(s)
    if m:
        s = s[:m.start()] + m.group(1).strip() + s[m.end():]
    return _HTTP2.sub(r"HTTP \1 ", _TIPO.sub("", s))


def erro(e):
    """O texto curto de uma excepcao. Um erro de rede fica so com a causa; outro qualquer leva o tipo (um KeyError
    so com o texto "'x'" nao dizia nada)."""
    s = str(e)
    c = curto(s)
    if c != s:
        return c
    return "%s: %s" % (type(e).__name__, s) if s else type(e).__name__

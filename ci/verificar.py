"""Verificacao do repositorio, igual no GitHub (workflow Verificar) e no portatil: python3 ci/verificar.py

1. Todo o Python compila e o pyflakes nao encontra nomes indefinidos nem erros (avisos de estilo nao contam).
2. api_check: regras das rotas, clientes que chamam rotas que existem, docs/API.md em dia.
3. O JavaScript de todas as paginas do bridge tem sintaxe valida (node --check).
4. A consola arranca e responde no simulador (ci/consola_sim.py), nos cenarios do dia a dia.
5. Sony: o sony_ctl do repositorio saiu do sony_ctl.cpp actual e arranca com as bibliotecas da Sony ao lado; a frota
   (sony.py) com o simulador do ajudante (sony_sim.py), incluindo os termos da Sony antes da primeira camara.
Sai com 1 ao primeiro grupo que falhe, a dizer o que falhou.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "ci"))

PYTHON = sorted(glob.glob(os.path.join(RAIZ, "atom", "app", "*.py"))) + [os.path.join(RAIZ, "bridge-ctl.py")] \
    + sorted(glob.glob(os.path.join(RAIZ, "firmware", "*.py"))) + sorted(glob.glob(os.path.join(RAIZ, "ci", "*.py")))
# Avisos que nao sao erros: importacoes sem uso (ha as opcionais), f-strings sem campos, nomes redefinidos de proposito.
IGNORAR = ("imported but unused", "f-string is missing placeholders", "redefinition of unused", "unable to detect undefined names")

# (nome, tamanho, cenario, teclas, textos que tem de haver no ecra)
CONSOLA = [
    ("dashboard", "100x40", "normal", [], ["REC  VIA  DEVICE", "< STATE >", "GATEWAYS"]),
    ("dashboard 80x24", "80x24", "normal", [], ["DEVICE", "STATE"]),
    ("vista IMAGE", "100x40", "normal", ["\\eOC"], ["< IMAGE >", "SETTINGS"]),
    ("falhas", "100x40", "falhas", [], ["offline", "FAULT"]),
    ("a gravar", "100x40", "rec", [], ["REC  00:03:1", "take 7", "cameras"]),
    ("rodape", "100x40", "normal", [], ["SPACE target", "ESC menu", "F1 help"]),
    ("menu DEVICES", "100x40", "normal", ["\\e", "2"], ["DEVICES", "MOBILE", "Add mobile"], ["Add iPhone", "IPHONE"]),
    ("menu DEVICES Sony", "100x50", "normal", ["\\e", "2"], ["SONY", "Add a Sony", "Sony SDK and search"]),
    ("linha Sony", "100x40", "normal", [], ["A7 IV"]),
    # O nome do device aparece inteiro no dashboard (ate 12 caracteres), sem "iPh"/"GP"/"BM".
    ("nomes inteiros", "100x40", "normal", [], ["iPhone Cabin", "GoPro 1"], ["iPh ", " GP 1"]),
    # As Canon so gravam em modo video: uma em modo foto aparece como "not video" (nao como "ready") e nao ha nada de fotos.
    ("Canon em modo foto", "100x40", "normal", [], ["not video"], ["PHOTO", "shots"]),
    ("ajuda", "100x40", "normal", ["?"], ["HELP", "Keys"]),
    # Issue #35: o nome ou o alias novo aparece logo no menu aberto (antes so depois de sair e voltar a entrar)
    ("alias novo no menu", "100x40", "normal", ["\\d", "\\d", "\\d", "\\r", "6", "\x7f" * 8 + "B-cam", "\\r", "!wait:2"],
     ["Alias  B-cam"], ["Alias  A-cam"]),
    ("nome novo no Config", "100x40", "normal", ["\\d", "\\d", "\\d", "\\r", "5", "2", "\x7f" * 20 + "iPhone Two", "\\r", "!wait:2"],
     ["Config  iPhone Two", "Rename  iPhone Two"], ["iPhone Cabine"]),
    ("nome novo no More", "100x40", "normal", ["\\d"] * 6 + ["\\r", "5", "2", "\x7f" * 20 + "FX3 B", "\\r", "!wait:2"],
     ["More  FX3 B"], ["More  A7 IV"]),
    ("dados da take", "100x40", "fim", ["!wait:4"], ["TAKE 7 DONE"]),
    # Os avisos de licenca: sem aceitar nao se entra; a consola so carrega depois do "y".
    ("avisos de licenca", "100x30", "termos", [], ["LICENSES AND NOTICES"], ["GATEWAYS"]),
    ("avisos aceites", "100x40", "termos", ["\\r", "y", "!wait:2"], ["GATEWAYS", "REC  VIA"], ["Accept these notices"]),
    ("avisos recusados", "100x30", "termos", ["\\r", "n", "!wait:2"], ["Accept these notices"], ["GATEWAYS", "REC  VIA"]),
]


def grupo(nome, erros):
    print(("OK    " if not erros else "FALHA ") + nome)
    for e in erros[:40]:
        print("      " + e)
    return not erros


def python():
    erros = []
    for f in PYTHON:
        r = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True, text=True)
        if r.returncode:
            erros.append(r.stderr.strip().splitlines()[-1])
    r = subprocess.run([sys.executable, "-m", "pyflakes"] + PYTHON, capture_output=True, text=True)
    if "No module named pyflakes" in r.stderr:
        return erros + ["pyflakes nao instalado (pip install pyflakes)"]
    erros += [os.path.relpath(x, RAIZ) for x in r.stdout.splitlines() if x.strip() and not any(i in x for i in IGNORAR)]
    return erros


def api():
    r = subprocess.run([sys.executable, os.path.join(RAIZ, "atom", "app", "api_check.py")], capture_output=True, text=True)
    return [] if r.returncode == 0 else [x for x in (r.stdout + r.stderr).splitlines() if x.strip()]


def javascript():
    erros = []
    for f in sorted(glob.glob(os.path.join(RAIZ, "atom", "app", "*.html"))):
        js = "\n".join(re.findall(r"<script>(.*?)</script>", open(f, encoding="utf-8").read(), re.S))
        if not js.strip():
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as t:
            t.write(js)
        try:
            r = subprocess.run(["node", "--check", t.name], capture_output=True, text=True)
        except FileNotFoundError:
            return ["node nao instalado"]
        finally:
            os.unlink(t.name)
        if r.returncode:
            erros.append("%s: %s" % (os.path.basename(f), " ".join(r.stderr.split())[:300]))
    return erros


def consola():
    import consola_sim
    erros = []
    for nome, tam, cen, teclas, esperado, *ausente in CONSOLA:
        c, l = (int(x) for x in tam.split("x"))
        ecra = "\n".join(consola_sim.correr(c, l, cen, teclas))
        for mau in ("SCREEN ERROR", "Traceback", "error:"):
            if mau in ecra:
                erros.append("%s: '%s' no ecra" % (nome, mau))
        for t in esperado:
            if t not in ecra:
                erros.append("%s: falta '%s'" % (nome, t))
        for t in (ausente[0] if ausente else ()):
            if t in ecra:
                erros.append("%s: nao devia haver '%s'" % (nome, t))
        if any(e.startswith(nome + ":") for e in erros):
            erros.append("ecra de '%s':\n%s" % (nome, ecra))
    return erros


def sony():
    """A frota Sony contra o simulador do ajudante: ligar, REC/STOP confirmados, timecode, erros, retirar e repor."""
    import time
    app = os.path.join(RAIZ, "atom", "app")
    sys.path.insert(0, app)
    import sony as so
    erros = []
    tmp = tempfile.mkdtemp(prefix="sony-")
    f = so.Fleet(lambda m: None, os.path.join(tmp, "sonys.json"), comando=[sys.executable, os.path.join(app, "sony_sim.py")])
    f.start()

    def espera(cond, t=6.0):
        fim = time.time() + t
        while time.time() < fim:
            if cond():
                return True
            time.sleep(0.1)
        return False
    if not espera(f.disponivel):
        return ["o simulador do ajudante nao arrancou"]
    for args, quer in ((("x", "ILCE-7M4", "ip", "300.1.1.1"), "host"), (("x", "nada", "ip", "1.2.3.4"), "model"),
                       (("x", "ILME-FX6", "ip", "1.2.3.4"), "SSH"), (("x", "ILME-FX6", "usb", None, "ABC"), "USB")):
        ok, err = f.add(*args)
        if ok or quer not in str(err):
            erros.append("add%r devia ser recusado por %s: %r" % (args, quer, err))
    f.termos_ok = lambda: False
    ok, err = f.add("A7 IV", "ILCE-7M4", "ip", "192.168.4.80")
    if ok or err != so.TERMOS_FALTA:
        erros.append("uma Sony entrou sem os avisos de licenca aceites: %r" % err)
    f.termos_ok = lambda: True
    ok, cid = f.add("A7 IV", "ILCE-7M4", "ip", "192.168.4.80")
    ok2, mau = f.add(None, "ILCE-1", "ip", "192.168.4.99")
    if not ok or not ok2:
        return erros + ["add falhou: %s %s" % (cid, mau)]
    cam = f.get(cid)
    if not espera(cam.connected):
        return erros + ["a Sony do simulador nao ligou"]
    if not espera(lambda: f.get(mau).error):
        erros.append("a camara que nao responde nao tem erro")
    ok, err = cam.set_running(True)
    if not ok or not espera(cam.running):
        erros.append("REC nao confirmado: %s" % err)
    if f.remove(cid)[0]:
        erros.append("retirou-se uma Sony a gravar")
    if cam.set_timecode("01:00:00:00")[0]:
        erros.append("escreveu-se o timecode a gravar")
    ok, err = cam.set_running(False)
    if not ok or not espera(lambda: not cam.running()):
        erros.append("STOP nao confirmado: %s" % err)
    if not cam.set_timecode("01:00:00:00")[0]:
        erros.append("o timecode parado foi recusado")
    st = cam.status()
    if st.get("battery") != 87 or st.get("iso") != "800" or st.get("fnumber") != "2.8" or st.get("shutter") != "1/50":
        erros.append("estado mal lido: %r" % {k: st.get(k) for k in ("battery", "iso", "fnumber", "shutter")})
    if not f.remove(cid)[0] or cid in f.ids() or cid not in f.removed_list():
        erros.append("remove nao a pos na lista dos guardados")
    ok, novo = f.restore(cid)
    if not ok or novo != cid or not espera(f.get(cid).connected):
        erros.append("restore falhou: %s" % novo)
    return erros + sony_ctl()


def iphone_obturador():
    """O angulo do obturador do iPhone (app Blackmagic Camera) mostra-se com 1 casa decimal, truncado como a app; um valor
    impossivel da None e fica no registo."""
    import time
    app = os.path.join(RAIZ, "atom", "app")
    sys.path.insert(0, app)
    import bmcam
    erros, logs = [], []
    tmp = tempfile.mkdtemp(prefix="iph-")
    f = bmcam.Fleet(logs.append, os.path.join(tmp, "iphones.json"))
    ok, cid = f.add("iPhone", "192.168.4.50", 4444)
    cam = f.get(cid)
    cam._ok_at = cam._st_at = time.time()
    # O angulo do obturador: a app manda todas as casas (272.2727...); mostra-se com 1 casa, truncado como a app (272.2).
    for v, quer in ((272.2727272727273, 272.2), (272.72727272727275, 272.7), ("272,2", 272.2), (180.0, 180), (172.8, 172.8),
                    (0, None), (361, None), (None, None), ("x", None), (272272727272727272727272, None)):
        if bmcam.shutter_normal(v) != quer:
            erros.append("shutter_normal(%r) = %r, devia ser %r" % (v, bmcam.shutter_normal(v), quer))
    cam._set("shutter", {"shutterAngle": 272.2727272727273})
    if cam.status().get("shutter") != 272.2:
        erros.append("o estado devia dar 272.2 para 272.2727...: %r" % cam.status().get("shutter"))
    return erros


def erros_curtos():
    """Os erros de rede chegam curtos a consola, a pagina e ao registo (issue #34), com as mensagens reais das bibliotecas;
    a copia do bridge-ctl.py da o mesmo que o atom/app/curto.py."""
    import importlib.util
    app = os.path.join(RAIZ, "atom", "app")
    sys.path.insert(0, app)
    import curto as c
    spec = importlib.util.spec_from_file_location("bridge_ctl_curto", os.path.join(RAIZ, "bridge-ctl.py"))
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    casos = [
        ("HTTPConnectionPool(host='10.255.255.1', port=8080): Max retries exceeded with url: /x (Caused by "
         "ConnectTimeoutError(<urllib3.connection.HTTPConnection object at 0x7f3a2c1d0>, 'Connection to 10.255.255.1 "
         "timed out. (connect timeout=1)'))", "no answer (timeout)"),
        ("Connect failed: HTTPConnectionPool(host='127.0.0.1', port=8771): Read timed out. (read timeout=1)",
         "Connect failed: no answer (timeout)"),
        ("<urlopen error timed out>", "no answer (timeout)"),
        ("<urlopen error [Errno 111] Connection refused>", "connection refused"),
        ("ConnectTimeout: HTTPConnectionPool(host='192.168.4.50', port=4444): Max retries exceeded with url: "
         "/control/api/v1/transports/0/record", "no answer (timeout)"),
        ("HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded with url: /x (Caused by NewConnectionError("
         "'<urllib3.connection.HTTPConnection object at 0x7f>: Failed to establish a new connection: [Errno 111] "
         "Connection refused'))", "connection refused"),
        ("OSError: [Errno 113] No route to host", "unreachable"),
        ("<urlopen error [Errno -2] Name or service not known>", "name not found"),
        ("Record stop failed: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without "
         "response'))", "Record stop failed: connection dropped"),
        ("Set ISO failed: 404 Client Error: Not Found for url: http://192.168.4.20:8080/ccapi/ver100/shooting/settings/iso",
         "Set ISO failed: HTTP 404 Not Found"),
        ("Connect failed: HTTPError: HTTP Error 403: Forbidden", "Connect failed: HTTP 403 Forbidden"),
        # O que nao e erro de rede fica como esta
        ("REC: canon1 skipped \u2014 not in video mode", "REC: canon1 skipped \u2014 not in video mode"),
        ("Shoot log write failed: [Errno 28] No space left on device", "Shoot log write failed: [Errno 28] No space left on device"),
        ("REC ALL: iPhone did not confirm (timeout)", "REC ALL: iPhone did not confirm (timeout)"),
    ]
    erros = []
    for txt, quer in casos:
        for nome, f in (("curto.py", c.curto), ("bridge-ctl.py", ctl.curto)):
            v = f(txt)
            if v != quer:
                erros.append("%s: %r -> %r, devia ser %r" % (nome, txt[:60], v, quer))
    return erros


def instalador():
    """instalar_app (atom-install/lib_app.sh) copia a app por cima de uma instalacao com um programa A CORRER (o sony_ctl,
    que o bridge mantem em execucao): nao pode falhar com "Text file busy", e o processo antigo segue com a sua versao."""
    import shutil
    import time
    t = tempfile.mkdtemp(prefix="instalador-")
    erros, proc = [], None
    try:
        app, opt = os.path.join(t, "app"), os.path.join(t, "opt")
        for d in (app, opt):
            os.makedirs(os.path.join(d, "sony"))
        for d in (app, opt):
            shutil.copy("/bin/sleep", os.path.join(d, "sony", "sony_ctl"))
        open(os.path.join(app, "bridge.py"), "w").write("import nucleo\n")
        proc = subprocess.Popen([os.path.join(opt, "sony", "sony_ctl"), "30"])
        time.sleep(0.3)
        with open(os.path.join(app, "sony", "sony_ctl"), "ab") as f:      # o programa novo e diferente do que corre
            f.write(b"\0nova versao")
        r = subprocess.run(["bash", "-c", 'source "$1"; NO_WAIT_REC=1 instalar_app "$2" "$3"', "x",
                            os.path.join(RAIZ, "atom-install", "lib_app.sh"), app, opt], capture_output=True, text=True)
        if r.returncode:
            erros.append("instalar_app falhou (rc %d) com um programa a correr: %s" % (r.returncode, (r.stdout + r.stderr)[-300:]))
        elif open(os.path.join(opt, "sony", "sony_ctl"), "rb").read() != open(os.path.join(app, "sony", "sony_ctl"), "rb").read():
            erros.append("o programa novo nao ficou instalado")
        elif proc.poll() is not None:
            erros.append("o processo antigo foi morto pela copia")
    finally:
        if proc:
            proc.kill()
            proc.wait()
        shutil.rmtree(t, ignore_errors=True)
    return erros


def sony_ctl():
    """O sony_ctl do repositorio: corresponde ao sony_ctl.cpp (sony_ctl.fonte) e arranca (SDK, modelos) neste Linux x86_64."""
    import hashlib
    import json
    import platform
    pasta = os.path.join(RAIZ, "atom", "app", "sony")
    erros = []
    try:
        fonte = open(os.path.join(pasta, "sony_ctl.fonte")).read().split()[0]
    except (OSError, IndexError):
        return ["falta atom/app/sony/sony_ctl.fonte: recompilar com sony/compilar.sh"]
    if fonte != hashlib.sha256(open(os.path.join(pasta, "sony_ctl.cpp"), "rb").read()).hexdigest():
        erros.append("o sony_ctl.cpp mudou e o sony_ctl nao foi recompilado (sony/compilar.sh <SDK> atom/app/sony)")
    if platform.machine() != "x86_64":
        return erros
    try:
        r = subprocess.run([os.path.join(pasta, "sony_ctl")], input="models\nquit\n", capture_output=True, text=True,
                           timeout=30, cwd=pasta)
    except (OSError, subprocess.TimeoutExpired) as e:
        return erros + ["o sony_ctl nao correu: %s" % e]
    evs = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    pronto = next((e for e in evs if e.get("ev") == "ready"), None)
    modelos = next((e.get("models") for e in evs if e.get("ev") == "models"), None) or []
    if not pronto or "ILCE-7M4" not in modelos:
        erros.append("o sony_ctl nao arrancou bem (rc %s): %s %s" % (r.returncode, r.stdout[-300:], r.stderr[-300:]))
    return erros


if __name__ == "__main__":
    ok = True
    for nome, f in (("Python (compila, pyflakes)", python), ("API (api_check)", api),
                    ("JavaScript das paginas", javascript), ("Consola no simulador", consola),
                    ("Sony com o simulador do ajudante", sony), ("Instalador: copia com um programa a correr", instalador),
                    ("iPhone: obturador com 1 casa decimal", iphone_obturador),
                    ("Erros de rede curtos (#34)", erros_curtos)):
        ok = grupo(nome, f()) and ok
    sys.exit(0 if ok else 1)

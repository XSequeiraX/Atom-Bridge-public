"""As paginas (/, /view, /shootlog...), os ficheiros que elas pedem e o login com o PIN.

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import acesso
import os
from flask import jsonify, request, send_from_directory
from nucleo import (NO_CACHE, _body, app, log_event)


@app.route('/login')
def pagina_login():
    return acesso.LOGIN_PAGE, 200, NO_CACHE


@app.route('/api/login', methods=['POST'])
def api_login():
    d = _body()
    token, err, code = acesso.login(request.remote_addr or "", str(d.get("pin") or ""))
    if not token:
        if code == 401:
            log_event("Web PIN: wrong PIN from %s" % (request.remote_addr or "?"))
        return jsonify({"ok": False, "error": err}), code
    log_event("Web PIN: session opened from %s" % (request.remote_addr or "?"))
    resp = jsonify({"ok": True})
    # HttpOnly (o JavaScript nao o le) e SameSite=Strict (nao vai em pedidos de outros sitios). Sem Secure: a pagina e
    # servida por HTTP na rede local (com Secure o browser nunca o devolvia).
    resp.set_cookie(acesso.COOKIE, token, max_age=acesso.SESSAO_S, httponly=True, samesite="Strict", path="/")
    return resp


@app.route('/api/logout', methods=['POST'])
def api_logout():
    acesso.logout(request.cookies.get(acesso.COOKIE))
    resp = jsonify({"ok": True})
    resp.delete_cookie(acesso.COOKIE, path="/")
    return resp


@app.route('/api/security/pin', methods=['GET', 'POST'])
def api_security_pin():
    """O PIN da pagina: GET diz se ha PIN; POST {"pin": "1234"} define-o, {"pin": ""} apaga-o. So do Atom ou do
    Tailscale (quem entrou com o PIN nao o muda). O PIN nunca volta na resposta nem vai para o registo."""
    if not acesso.de_confianca(request.remote_addr or ""):
        return jsonify({"ok": False, "error": "only from the Atom or Tailscale"}), 403
    if request.method == 'GET':
        return jsonify({"ok": True, "pin_set": acesso.tem_pin()})
    d = _body()
    ok, err = acesso.definir_pin(str(d.get("pin") or ""))
    if ok:
        log_event("Web PIN %s" % ("set" if d.get("pin") else "removed"))
    return jsonify({"ok": ok, "error": err, "pin_set": acesso.tem_pin()}), (200 if ok else 400)


# Read-only status page — no controls, meant for someone (e.g. a director/
# producer) to glance at battery/recording state over Tailscale or the
# local hotspot without any risk of them touching REC by accident.

# The pages live in ui.html / view.html beside this script rather than inside
# it. Two reasons: they are plain static HTML with no template placeholders at
# all (render_template_string would recompile ~41KB per hit for nothing), and
# keeping them out of the .py means a UI change is a file copy — no service
# restart, and therefore no dropped BLE link to the BMPCC, which
# is what every interface tweak used to cost.
_PAGE_CACHE = {}


def load_page(name):
    """Read a page, re-reading only when the file actually changed on disk."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return f"<h1>{name} missing</h1>"
    cached = _PAGE_CACHE.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        body = f.read()
    _PAGE_CACHE[name] = (mtime, body)
    return body


@app.route('/')
def index():
    return load_page("ui.html"), 200, NO_CACHE


@app.route('/view')
def view():
    return load_page("view.html"), 200, NO_CACHE


@app.route('/shootlog')
def shootlog_page():
    return load_page("shootlog.html"), 200, NO_CACHE


@app.route('/eventlog')
def eventlog_page():
    return load_page("eventlog.html"), 200, NO_CACHE


@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Camera Bridge",
        "short_name": "Camera",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a0a",
        "theme_color": "#0a0a0a",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


@app.route('/fonts/<path:nome>')
def fonts(nome):
    """Serve os tipos de letra a partir do proprio Atom.

    Estavam a vir do Google por @import dentro do CSS, o que bloqueia a
    renderizacao ate resolver -- numa locacao sem uplink a pagina ficava
    pendurada a cada carregamento. Nada no que se desenha pode depender de
    haver internet.

    O send_from_directory trata do caminho com seguranca; ficheiros de tipos de
    letra nunca mudam, por isso levam cache longa, ao contrario do HTML.
    """
    resp = send_from_directory(FONTS_DIR, nome)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route('/sw.js')
def service_worker():
    sw = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
    if (e.request.url.includes('/api/')) {
        e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', {headers:{'Content-Type':'application/json'}})));
    }
});
"""
    return sw, 200, {'Content-Type': 'application/javascript'}

_icone = []          # (corpo, tipo) gerado uma vez: converter o SVG a cada pedido gastava CPU no Atom


@app.route('/icon-192.png')
@app.route('/icon-512.png')
@app.route('/favicon.ico')   # o browser pede-o sozinho em todas as paginas (sem ele, um 404 na consola de cada uma)
def icon():
    if _icone:
        return _icone[0][0], 200, {'Content-Type': _icone[0][1]}
    # Generate a simple SVG-based PNG icon
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" viewBox="0 0 192 192">
    <rect width="192" height="192" rx="40" fill="#0a0a0a"/>
    <circle cx="96" cy="86" r="30" fill="none" stroke="#e63946" stroke-width="6"/>
    <circle cx="96" cy="86" r="10" fill="#e63946"/>
    <rect x="56" y="130" width="80" height="8" rx="4" fill="#333"/>
    <rect x="56" y="130" width="50" height="8" rx="4" fill="#e63946"/>
    </svg>'''
    try:
        import cairosvg
        _icone.append((cairosvg.svg2png(bytestring=svg.encode()), 'image/png'))
    except Exception:
        _icone.append((svg, 'image/svg+xml'))     # sem cairosvg: o SVG serve de icone
    return _icone[0][0], 200, {'Content-Type': _icone[0][1]}


@app.route('/f6card')
def f6card_page():
    return load_page("f6card.html"), 200, NO_CACHE


@app.route('/gopro_qr')
def gopro_qr_page():
    """Codigo QR animado com a hora do bridge (GoPro Labs: 'oT...'): a camara le-o e acerta o relogio."""
    return load_page("gopro_qr.html"), 200, NO_CACHE

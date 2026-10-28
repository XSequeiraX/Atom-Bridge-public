"""Wi-Fi do gateway C3: rede guardada, AP de configuracao (como o Tasmota), modo e API HTTP.

Sequencia (poll() chama-se do ciclo principal; nada aqui bloqueia mais do que um scan):
  1. Tenta as redes guardadas por ordem (ate 2), 15 s cada, em ciclo. Ligou -> modo STA.
  2. Sem redes guardadas, ou a falhar ha mais de 3 min seguidos -> abre o AP "FIZ-C3-XXXX" (aberto, como o
     Tasmota) com portal em 192.168.4.1: lista as redes que ve, escolhe-se uma e escreve-se a password.
     Uma queda curta do Wi-Fi nao abre o AP: o C3 continua a tentar as redes guardadas.
  3. O AP fica 3 min (como o WifiConfig 2 do Tasmota); se ha redes guardadas volta a tentar-as.

Firmware por Wi-Fi (OTA): o bridge envia main.py, app.mpy e netcfg.mpy (/api/fw/put, um de cada vez, com o SHA-256;
o programa vai ja compilado, ver main.py),
depois /api/fw/apply: os actuais ficam em .bak e o C3 reinicia em teste (boot.py, fw_trial). Se o novo nao
chegar a ligar o Wi-Fi, ao 3.o arranque voltam os .bak. /api/fw diz o SHA-256 de cada ficheiro instalado.
Com o ficheiro "gw_token" no C3 (o Atom grava-o pela USB), todos os POST (fw, rec, bm/write, cam, mode, save...) e o
/api/scan?fresh=1 exigem o cabecalho X-GW-Token igual (so o portal /save, /wifi_reset e /api/mode dispensa-o no AP do
proprio C3); sem o ficheiro aceitam tudo, como antes. /api/status e /api/bm/poll dizem "token": true|false.

Modo (escolhe-se na pagina, fica guardado):
  "standalone"  autonomo: o C3 nao fala com o Atom (ate a fiz_bm_c3 21 chamava-se "fiz": le-se e aceita-se ainda).
  "bridge"  a BMPCC conversa com o Atom por Wi-Fi (/api/bm/poll e /api/bm/write): o Atom ve o estado dela
            e manda-lhe o REC com o resto do grupo.
O FIZ e so deste C3 e funciona igual nos dois modos: o botao dele dispara sempre a BMPCC ligada a este C3.
O Atom nao recebe nada do FIZ.
"""
import network, socket, json, time, os, ubinascii, hashlib, machine

CFG_FILE = "wifi.json"
AP_IP = "192.168.4.1"
AP_WINDOW_MS = 180000
STA_TRY_MS = 15000
STA_LOST_MS = 30000
STA_GIVEUP_MS = 180000      # redes guardadas a falhar ha tanto tempo seguido: abre o AP
BEACON_PORT = 5172          # "C3GW ip=... " em UDP broadcast, como o "GPGW" do gateway S3
BEACON_EVERY_MS = 5000
BRIDGE_STALE_MS = 10000

cfg = {"nets": [], "mode": "standalone"}
MODOS = ("standalone", "bridge")


def _modo(m):
    """O nome do modo; "fiz" (o nome antigo do autonomo) passa a "standalone". None se nao e um modo."""
    m = "standalone" if m == "fiz" else m
    return m if m in MODOS else None
state = "init"              # sta_try | sta_up | ap
_status_fn = _rec_fn = _bm_poll_fn = _bm_write_fn = _ext_fn = None
_sta = network.WLAN(network.STA_IF)
_ap = network.WLAN(network.AP_IF)
_srv = _dns = _beacon = None
_try_idx = 0
_since = 0
_lost_since = 0
_last_beacon = 0
_scan = []
_last_poll = None           # ticks do ultimo pedido do Atom
_fail_since = None          # desde quando as redes guardadas falham sem parar (None: ligado ou no AP)
_one_pass = False           # redes acabadas de gravar no portal: uma volta e, se falhar, o AP outra vez
_wdt = None                 # o watchdog do main.py (alimenta-se nas partes demoradas: scan, upload)


def _feed():
    if _wdt is not None:
        _wdt.feed()


def _load():
    global cfg
    try:
        with open(CFG_FILE) as f:
            c = json.load(f)
        mode = _modo(c.get("mode") or ("bridge" if c.get("bridge") else "standalone")) or "standalone"
        cfg = {"nets": [tuple(n) for n in c.get("nets", [])][:2], "mode": mode}
    except Exception:
        cfg = {"nets": [], "mode": "standalone"}


def _save():
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"nets": cfg["nets"], "mode": cfg["mode"]}, f)
    os.rename(tmp, CFG_FILE)


def ap_name():
    return "FIZ-C3-" + ubinascii.hexlify(_sta.config("mac")[-2:]).decode().upper()


def bridge_mode():
    return cfg["mode"] == "bridge"


def atom_alive():
    return (state == "sta_up" and _last_poll is not None
            and time.ticks_diff(time.ticks_ms(), _last_poll) < BRIDGE_STALE_MS)


# ---- estados ----
def _start_sta(idx):
    global state, _try_idx, _since, _fail_since
    if _ap.active():
        _ap.active(False)
    _stop_dns()
    now = time.ticks_ms()
    if _fail_since is None:
        _fail_since = now
    if idx >= len(cfg["nets"]):
        # Acabou uma volta: recomeca, a nao ser que nao haja redes ou falhem ha mais de STA_GIVEUP_MS
        if not cfg["nets"] or _one_pass or time.ticks_diff(now, _fail_since) > STA_GIVEUP_MS:
            _start_ap()
            return
        idx = 0
    _try_idx = idx
    ssid, pw = cfg["nets"][idx]
    _sta.active(True)
    try:
        _sta.config(dhcp_hostname="fiz-c3")
    except Exception:
        pass
    try:
        _sta.disconnect()
    except Exception:
        pass
    _sta.connect(ssid, pw)
    state = "sta_try"
    _since = time.ticks_ms()


def _do_scan():
    global _scan
    _feed()                         # o scan bloqueia alguns segundos
    try:
        best = {}
        res = _sta.scan()
        _feed()
        for r in res:
            s = r[0].decode("utf-8", "ignore")
            if s and (s not in best or r[3] > best[s][0]):
                best[s] = (r[3], r[4] != 0)
        _scan = sorted(((s, v[0], v[1]) for s, v in best.items()), key=lambda x: -x[1])[:15]
    except Exception:
        _scan = []


def _start_ap():
    global state, _since, _fail_since, _one_pass
    _fail_since, _one_pass = None, False    # passada a janela do AP, mais STA_GIVEUP_MS a tentar as redes
    _sta.active(True)
    try:
        _sta.disconnect()
    except Exception:
        pass
    _do_scan()
    _ap.active(True)
    _ap.config(essid=ap_name(), authmode=network.AUTH_OPEN)
    _ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
    _start_dns()
    state = "ap"
    _since = time.ticks_ms()


def _start_dns():
    global _dns
    try:
        _dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _dns.bind(("0.0.0.0", 53))
        _dns.setblocking(False)
    except Exception:
        _dns = None


def _stop_dns():
    global _dns
    if _dns is not None:
        try:
            _dns.close()
        except Exception:
            pass
        _dns = None


def _answer_dns():
    """Responde a qualquer nome com o IP do AP (captive portal)."""
    try:
        q, who = _dns.recvfrom(256)
    except OSError:
        return
    if len(q) < 12:
        return
    end = 12
    while end < len(q) and q[end] != 0:
        end += q[end] + 1
    question = q[12:end + 5]
    ip = bytes(int(x) for x in AP_IP.split("."))
    r = q[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + question \
        + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + ip
    try:
        _dns.sendto(r, who)
    except OSError:
        pass


# ---- HTTP ----
def _unq(s):
    b = s.replace("+", " ").encode()
    out = bytearray()
    i = 0
    while i < len(b):
        if b[i] == 37 and i + 2 < len(b):
            try:
                out.append(int(b[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(b[i])
        i += 1
    return out.decode("utf-8", "ignore")


def _form(body):
    d = {}
    for kv in body.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[_unq(k)] = _unq(v)
    return d


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _full_status():
    d = _status_fn() if _status_fn else {}
    d.update({"ok": True, "wifi": state, "mode": cfg["mode"], "atom": atom_alive(), "token": _tem_token(),
              "ap": ap_name(), "nets": [n[0] for n in cfg["nets"]],
              "uptime_s": time.ticks_ms() // 1000})
    if state == "sta_up":
        d["ssid"] = _sta.config("essid")
        d["ip"] = _sta.ifconfig()[0]
        try:
            d["rssi"] = _sta.status("rssi")
        except Exception:
            pass
    return d


# Pagina estatica: o estado chega por /api/status (refresca sozinho).
PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FIZ C3</title><style>
:root{--bg:#0f1115;--card:#181b22;--line:#262b35;--tx:#e8eaf0;--mu:#8b93a3;--ok:#3ecf8e;--wa:#f5b942;--er:#ef5b5b;--ac:#5b9dff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.4 -apple-system,system-ui,sans-serif}
main{max-width:460px;margin:0 auto;padding:16px}h1{font-size:19px;margin:6px 0 14px;display:flex;align-items:center;gap:8px}
h1 small{color:var(--mu);font-weight:400;font-size:13px;margin-left:auto}
.c{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.c h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mu);margin:0 0 10px}
.row{display:flex;align-items:center;justify-content:space-between;padding:6px 0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:var(--mu)}
.ok{background:var(--ok)}.wa{background:var(--wa)}.er{background:var(--er)}
.rec{color:var(--er);font-weight:700}.mu{color:var(--mu);font-size:13px}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.seg button{background:#11141a;color:var(--tx);border:1px solid var(--line);border-radius:12px;padding:12px 8px;text-align:left;cursor:pointer}
.seg button b{display:block;font-size:15px;margin-bottom:2px}.seg button span{color:var(--mu);font-size:12px}
.seg button.on{border-color:var(--ac);background:#15233a;box-shadow:0 0 0 1px var(--ac) inset}
input{width:100%;background:#11141a;color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:11px;margin:4px 0;font-size:15px}
.btn{width:100%;border:0;border-radius:10px;padding:12px;font-size:15px;font-weight:600;background:var(--ac);color:#fff;margin-top:6px;cursor:pointer}
.btn.gh{background:transparent;color:var(--mu);border:1px solid var(--line)}
.net{display:flex;justify-content:space-between;padding:9px 10px;border-radius:9px;cursor:pointer}
.net:hover,.net.sel{background:#11141a}.bars{letter-spacing:-2px;color:var(--ac)}
details summary{cursor:pointer;color:var(--mu);font-size:13px;margin-top:8px}
</style><main>
<h1>FIZ &rarr; BMPCC <small id=up></small></h1>
<div class=c><h2>Status</h2>
<div class=row><span><i class=dot id=dfiz></i>FIZ</span><span id=fiz class=mu></span></div>
<div class=row><span><i class=dot id=dbm></i>BMPCC</span><span id=bm class=mu></span></div>
<div class=row><span><i class=dot id=datom></i>Atom (Bridge)</span><span id=atom class=mu></span></div></div>
<div class=c><h2>Mode</h2><div class=seg>
<button id=m_alone onclick="modo('standalone')"><b>Standalone</b><span>The C3 works on its own</span></button>
<button id=m_bridge onclick="modo('bridge')"><b>Bridge</b><span>The BMPCC talks to the Atom</span></button></div>
<p class=mu id=mnote></p></div>
<div class=c><h2>Wi-Fi</h2><div class=row><span id=wnow></span><span id=wip class=mu></span></div>
<div id=list></div><button class="btn gh" onclick="scan()" id=sb>Find networks</button>
<form method=post action=/save><input id=s1 name=s1 placeholder="Network name (SSID)" autocomplete=off>
<input name=p1 type=password placeholder="Password">
<details><summary>Second network (optional)</summary><input name=s2 placeholder="SSID 2"><input name=p2 type=password placeholder="Password 2"></details>
<button class=btn>Save and connect</button></form>
<form method=post action=/wifi_reset onsubmit="return confirm('Forget the saved networks? The C3 opens its own AP again.')"><button class="btn gh">Forget networks</button></form></div>
<p class=mu style=text-align:center id=foot></p></main><script>
const $=i=>document.getElementById(i);const PH={ready:['ok','connected'],idle:['er','searching'],queued:['wa','connecting'],connecting:['wa','connecting'],encrypting:['wa','encrypting'],discovering:['wa','preparing'],gave_up:['er','not paired'],broken:['er','error'],off:['','off (Bridge mode: the Atom records)'],ble_error:['er','C3 Bluetooth did not start']};
function dot(id,c){$(id).className='dot '+c}
function bars(r){return r>-55?'▂▄▆█':r>-67?'▂▄▆':r>-78?'▂▄':'▂'}
async function st(){try{const s=await(await fetch('/api/status')).json();
const f=PH[s.fiz]||['',s.fiz];let b=PH[s.bm]||['',s.bm];dot('dfiz',f[0]);$('fiz').textContent=f[1];dot('dbm',b[0]);if(s.ble_error)b=[b[0],b[1]+' - '+String(s.ble_error).replace(/[<>&]/g,'')];
$('bm').innerHTML=s.bm=='ready'&&s.recording?'<span class=rec>● RECORDING</span>':b[1];
dot('datom',s.mode!='bridge'?'':s.atom?'ok':'wa');$('atom').textContent=s.mode!='bridge'?'off (standalone mode)':s.atom?'talking':'waiting for the Atom';
$('m_alone').className=s.mode!='bridge'?'on':'';$('m_bridge').className=s.mode=='bridge'?'on':'';
$('mnote').textContent=ER||(s.mode=='bridge'?(s.atom?'The Atom sees the BMPCC and sends it REC with the group. The FIZ is not used in this mode.':'Waiting for the Atom.'):'The Atom gets nothing from the BMPCC. The FIZ triggers it.');
$('wnow').innerHTML=s.wifi=='sta_up'?'<i class="dot ok"></i>'+esc(s.ssid):s.wifi=='ap'?'<i class="dot wa"></i>AP '+esc(s.ap):'<i class="dot wa"></i>connecting...';
$('wip').textContent=s.wifi=='sta_up'?s.ip+(s.rssi?' · '+s.rssi+' dBm':''):s.wifi=='ap'?'192.168.4.1':'';
$('up').textContent=s.fw||'';$('foot').textContent='up for '+Math.floor(s.uptime_s/60)+' min'+(s.mem_free!=null?' · memory '+Math.round(s.mem_free/1024)+' KB'+(s.mem_max_block!=null?' (block '+Math.round(s.mem_max_block/1024)+' KB)':''):'')+(s.bm_rssi!=null?' · BMPCC '+s.bm_rssi+' dBm':'');}catch(e){}}
function esc(t){return String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
const TK='Needs the gateway token: change it from the Atom console or over USB, or from the C3 own AP.';let ER='';
async function modo(m){const r=await fetch('/api/mode?m='+m,{method:'POST'});ER=r.status==403?TK:'';st()}
async function scan(f){$('sb').textContent='Searching...';try{const r=await fetch('/api/scan'+(f===0?'':'?fresh=1'));const l=await r.json();
if(!Array.isArray(l))$('list').textContent=r.status==403?TK:String(l.error||'');else $('list').innerHTML=l.map(n=>'<div class=net onclick="pick(this)" data-s="'+esc(n[0])+'"><span>'+(n[2]?'🔒 ':'')+esc(n[0])+'</span><span class=bars>'+bars(n[1])+'</span></div>').join('')}catch(e){}$('sb').textContent='Search again'}
function pick(e){document.querySelectorAll('.net').forEach(x=>x.classList.remove('sel'));e.classList.add('sel');$('s1').value=e.dataset.s;document.querySelector('[name=p1]').focus()}
st();setInterval(st,2000);scan(0);
</script></html>"""


def _reply(c, code, body, ctype="text/html; charset=utf-8", extra=""):
    if isinstance(body, str):
        body = body.encode()
    c.send(("HTTP/1.0 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\n"
            "Connection: close\r\n%s\r\n" % (code, ctype, len(body), extra)).encode())
    mv = memoryview(body)
    i = 0
    while i < len(body):                    # aos bocados: o socket do C3 aceita pouco de cada vez
        i += c.send(mv[i:i + 1024])


def _json(c, d, code="200 OK"):
    _reply(c, code, json.dumps(d), "application/json")


FW_FILES = ("main.py", "app.mpy", "netcfg.mpy")      # o boot.py (a guarda do OTA) so muda pela USB
FW_MAX = 64000
TOKEN_FILE = "gw_token"


def _token_ok(got):
    """put/apply/rollback: com o gw_token no C3 o pedido traz o mesmo (comparacao em tempo constante); sem ele aceita."""
    try:
        with open(TOKEN_FILE) as f:
            want = f.read().strip()
    except OSError:
        return True
    if not want:
        return True
    a, b = got.encode(), want.encode()
    r = len(a) ^ len(b)
    for i in range(len(b)):
        r |= (a[i] if i < len(a) else 0) ^ b[i]
    return r == 0


def _pw_ok(pw):
    """Password do Wi-Fi: vazia (aberta), 8..63 BYTES, ou 64 hexadecimais (PSK em bruto)."""
    n = len(pw.encode())
    if n == 64:
        return all(ch in "0123456789abcdefABCDEF" for ch in pw)
    return n == 0 or 8 <= n <= 63


def _tem_token():
    try:
        with open(TOKEN_FILE) as f:
            return bool(f.read().strip())
    except OSError:
        return False


def _existe(n):
    try:
        os.stat(n)
        return True
    except OSError:
        return False


def _sha(n):
    h = hashlib.sha256()
    with open(n, "rb") as f:
        while True:
            b = f.read(512)
            if not b:
                break
            h.update(b)
    return ubinascii.hexlify(h.digest()).decode()


def _a_gravar():
    return bool(_status_fn and _status_fn().get("recording"))


def _fw_put(c, q, body, clen):
    """Recebe um ficheiro do firmware em <nome>.new, aos bocados (nao cabe na RAM de uma vez)."""
    name = q.get("name", "")
    if name not in FW_FILES:
        return _json(c, {"ok": False, "error": "name: " + " | ".join(FW_FILES)}, "400 Bad Request")
    if _a_gravar():
        return _json(c, {"ok": False, "error": "the BMPCC is recording"}, "409 Conflict")
    if clen <= 0 or clen > FW_MAX:
        return _json(c, {"ok": False, "error": "size"}, "400 Bad Request")
    h, got, tmp = hashlib.sha256(), 0, name + ".new"
    c.settimeout(5)
    try:
        with open(tmp, "wb") as f:
            if body:
                body = body[:clen]
                f.write(body)
                h.update(body)
                got = len(body)
            while got < clen:
                _feed()                 # um upload lento passa dos 30 s do watchdog
                chunk = c.recv(min(1024, clen - got))
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
    except Exception:
        got = -1                        # a ligacao caiu a meio: o .new incompleto nao pode ficar (o apply instalava-o)
    sha = ubinascii.hexlify(h.digest()).decode()
    if got != clen or sha != q.get("sha", ""):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return _json(c, {"ok": False, "error": "incomplete or checksum mismatch"}, "400 Bad Request")
    return _json(c, {"ok": True, "name": name, "sha": sha})


def _fw_apply(c):
    novos = [n for n in FW_FILES if _existe(n + ".new")]
    if not novos:
        return _json(c, {"ok": False, "error": "nothing uploaded"}, "409 Conflict")
    if _a_gravar():
        return _json(c, {"ok": False, "error": "the BMPCC is recording"}, "409 Conflict")
    # O fw_trial vai antes das trocas: um corte a meio delas arranca ja em teste (e o boot.py repoe os .bak).
    with open("fw_trial", "w") as f:
        f.write("0")
    for n in FW_FILES:
        if n in novos:
            if _existe(n):
                os.rename(n, n + ".bak")
            os.rename(n + ".new", n)
            if n.endswith(".mpy") and _existe(n[:-4] + ".py"):
                os.remove(n[:-4] + ".py")      # o MicroPython prefere o .py ao .mpy: um .py antigo tapava o novo
        elif _existe(n + ".bak"):
            os.remove(n + ".bak")          # .bak de um OTA antigo: repor so este ficaria desencontrado
    _json(c, {"ok": True, "applied": novos})
    c.close()
    time.sleep_ms(500)
    machine.reset()


def _fw_rollback(c):
    velhos = [n for n in FW_FILES if _existe(n + ".bak")]
    if not velhos:
        return _json(c, {"ok": False, "error": "no previous firmware"}, "409 Conflict")
    if _a_gravar():
        return _json(c, {"ok": False, "error": "the BMPCC is recording"}, "409 Conflict")
    for n in velhos:
        os.rename(n + ".bak", n)
    if _existe("fw_trial"):
        os.remove("fw_trial")
    _json(c, {"ok": True, "restored": velhos})
    c.close()
    time.sleep_ms(500)
    machine.reset()


def _msg_page(title, text):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='background:#0f1115;color:#e8eaf0;font:16px system-ui;padding:24px;max-width:460px;margin:auto'>"
            "<h2>%s</h2><p>%s</p><p><a style='color:#5b9dff' href=/>Back</a></p>" % (title, text))


def _handle(c):
    global _last_poll, _fail_since, _one_pass
    c.settimeout(2)
    req = b""
    while b"\r\n\r\n" not in req and len(req) < 3000:
        chunk = c.recv(512)
        if not chunk:
            break
        req += chunk
    head, _, body = req.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "ignore").split("\r\n")
    try:
        method, target, _v = lines[0].split(" ")
    except ValueError:
        return
    clen = 0
    token = ""
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith("content-length:"):
            clen = int(ln.split(":")[1])
        elif low.startswith("x-gw-token:"):
            token = ln.split(":", 1)[1].strip()
    while len(body) < min(clen, 1500):
        chunk = c.recv(512)
        if not chunk:
            break
        body += chunk
    path, _, qs = target.partition("?")
    q = _form(qs) if qs else {}

    # -- firmware por Wi-Fi (OTA) --
    if path == "/api/fw":
        return _json(c, {"ok": True, "fw": (_status_fn() if _status_fn else {}).get("fw"),
                         "files": {n: _sha(n) for n in FW_FILES if _existe(n)}, "trial": _existe("fw_trial"),
                         "rollback": any(_existe(n + ".bak") for n in FW_FILES)})
    # Todos os POST pedem o X-GW-Token (se o C3 tem gw_token); so o portal do AP de configuracao (/save, /wifi_reset, /api/mode)
    # dispensa-o enquanto o C3 esta no seu proprio AP (quem la esta acabou de se ligar a ele, e o portal nao tem o token).
    if method == "POST" and not (state == "ap" and path in ("/save", "/wifi_reset", "/api/wifi_reset", "/api/mode")) \
            and not _token_ok(token):
        if path in ("/save", "/wifi_reset"):
            return _reply(c, "403 Forbidden", _msg_page("Token needed", "The C3 has a gateway token: change the network from its own AP (FIZ-C3-XXXX)."))
        return _json(c, {"ok": False, "error": "wrong or missing gateway token (set the C3 up again by USB)"},
                     "403 Forbidden")
    if path == "/api/fw/put" and method == "POST":
        return _fw_put(c, q, body, clen)
    if path == "/api/fw/apply" and method == "POST":
        return _fw_apply(c)
    if path == "/api/fw/rollback" and method == "POST":
        return _fw_rollback(c)

    # -- o Atom (modo Bridge) --
    if path == "/api/bm/poll":
        if not bridge_mode():
            return _json(c, {"ok": False, "error": "standalone mode", "mode": "standalone"}, "409 Conflict")
        _last_poll = time.ticks_ms()
        try:
            since, tcs = int(q.get("since", "-1")), int(q.get("tcs", "-1"))
        except ValueError:
            since, tcs = -1, -1
        d = _bm_poll_fn(since, tcs) if _bm_poll_fn else {}
        d["ok"], d["mode"], d["token"] = True, "bridge", _tem_token()
        return _json(c, d)
    if path == "/api/bm/write" and method == "POST":
        if not bridge_mode():
            return _json(c, {"ok": False, "error": "standalone mode"}, "409 Conflict")
        _last_poll = time.ticks_ms()
        try:
            data = bytes.fromhex(q.get("hex") or body.decode().strip())
        except Exception:
            return _json(c, {"ok": False, "error": "invalid hex"}, "400 Bad Request")
        ok = _bm_write_fn(data) if _bm_write_fn else False
        return _json(c, {"ok": bool(ok)} if ok else {"ok": False, "error": "BMPCC not ready"},
                     "200 OK" if ok else "409 Conflict")

    # -- a camera: procurar e emparelhar (main.net_cam) --
    if path.startswith("/api/cam") and _ext_fn:
        r = _ext_fn(method, path, q)
        if r is not None:
            return _json(c, r[1], r[0])

    # -- estado e controlo --
    if path == "/api/status":
        return _json(c, _full_status())
    if path == "/api/scan":
        if q.get("fresh") or not _scan:
            # O scan mexe no radio: ligado a uma rede (STA) so com o token, e nunca a meio de um take.
            if state != "ap" and not _token_ok(token):
                if q.get("fresh"):
                    return _json(c, {"ok": False, "error": "wrong or missing gateway token"}, "403 Forbidden")
            elif _a_gravar():                   # (sem token num C3 com token: so a lista em cache, nunca uma procura)
                if q.get("fresh"):
                    return _json(c, {"ok": False, "error": "the BMPCC is recording"}, "409 Conflict")
            else:
                _do_scan()
        return _json(c, [list(n) for n in _scan])
    # Mudar de modo ou de rede a meio de um take deixa o bridge sem a BMPCC (o C3 sai da rede ou muda de dono).
    if method == "POST" and path in ("/api/mode", "/api/wifi_reset", "/wifi_reset", "/save") and _a_gravar():
        if path in ("/wifi_reset", "/save"):
            return _reply(c, "409 Conflict", _msg_page("The BMPCC is recording", "Change the network after the take."))
        return _json(c, {"ok": False, "error": "the BMPCC is recording"}, "409 Conflict")
    if path == "/api/mode" and method == "POST":
        m = _modo(q.get("m"))
        if not m:
            return _json(c, {"ok": False, "error": "mode: standalone | bridge"}, "400 Bad Request")
        cfg["mode"] = m
        _save()
        return _json(c, {"ok": True, "mode": m})
    if path == "/api/rec" and method == "POST":
        ok = _rec_fn(q.get("on") == "1") if _rec_fn else False
        return _json(c, {"ok": bool(ok)})
    if path in ("/api/wifi_reset", "/wifi_reset") and method == "POST":
        cfg["nets"] = []
        _save()
        if path == "/wifi_reset":
            _reply(c, "200 OK", _msg_page("Networks forgotten", "The C3 now opens its AP <b>%s</b>." % _esc(ap_name())))
        else:
            _json(c, {"ok": True})
        c.close()
        time.sleep_ms(300)
        _start_ap()
        return
    if path == "/save" and method == "POST":
        f = _form(body.decode("utf-8", "ignore"))
        nets = []
        for s, p in (("s1", "p1"), ("s2", "p2")):
            if f.get(s):
                ssid, pw = f[s], f.get(p, "")
                if not 1 <= len(ssid.encode()) <= 32 or not _pw_ok(pw) \
                        or any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid + pw):
                    return _reply(c, "400 Bad Request", _msg_page("Invalid network", "Network name: 1 to 32 bytes. Password: empty (open network), 8 to 63 bytes or 64 hex characters. No control characters."))
                nets.append((ssid, pw))
        if not nets:
            return _reply(c, "400 Bad Request", _msg_page("Network name missing", "Pick a network from the list or type its SSID."))
        cfg["nets"] = nets
        _save()
        _reply(c, "200 OK", _msg_page("Connecting to %s" % _esc(nets[0][0]),
                                      "If it connects, the C3 stays on that network (the AP closes). If not, it opens the AP <b>%s</b> again."
                                      % _esc(ap_name())))
        c.close()
        time.sleep_ms(500)
        _fail_since, _one_pass = None, True     # como a pagina diz: se nenhuma ligar, o AP volta logo
        _start_sta(0)
        return
    if path in ("/", "/index.html"):
        return _reply(c, "200 OK", PAGE)
    if state == "ap":   # captive portal: tudo o resto vai para a pagina de configuracao
        return _reply(c, "302 Found", "", extra="Location: http://%s/\r\n" % AP_IP)
    _reply(c, "404 Not Found", "not found", "text/plain")


def _serve():
    for _ in range(3):              # o Atom pergunta 10x/s; a pagina pede outras coisas pelo meio
        try:
            c, _a = _srv.accept()
        except OSError:
            return
        try:
            _handle(c)
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass


def _send_beacon():
    global _beacon, _last_beacon
    now = time.ticks_ms()
    if time.ticks_diff(now, _last_beacon) < BEACON_EVERY_MS:
        return
    _last_beacon = now
    try:
        if _beacon is None:
            _beacon = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ip, mask, _gw, _dns_ = _sta.ifconfig()
        ib = [int(x) for x in ip.split(".")]
        mb = [int(x) for x in mask.split(".")]
        bc = ".".join(str(ib[i] | (255 - mb[i])) for i in range(4))
        st = _status_fn() if _status_fn else {}
        _beacon.sendto(("C3GW ip=%s bm=%s mode=%s" % (
            ip, st.get("bm", "?"), cfg["mode"])).encode(), (bc, BEACON_PORT))
    except Exception:
        pass


# ---- interface para o ciclo principal ----
def init(status_fn, rec_fn, bm_poll_fn=None, bm_write_fn=None, ext_fn=None, wdt=None):
    global _srv, _status_fn, _rec_fn, _bm_poll_fn, _bm_write_fn, _ext_fn, _wdt
    _status_fn, _rec_fn, _bm_poll_fn, _bm_write_fn, _ext_fn = status_fn, rec_fn, bm_poll_fn, bm_write_fn, ext_fn
    _wdt = wdt
    _load()
    # Um .new que ficou de um envio anterior (de antes deste arranque) nunca se aplica: o apply so leva o que chegou agora.
    for n in FW_FILES:
        if _existe(n + ".new"):
            os.remove(n + ".new")
    _srv = socket.socket()
    _srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _srv.bind(("0.0.0.0", 80))
    _srv.listen(4)
    _srv.setblocking(False)
    _start_sta(0)


def poll():
    global state, _lost_since, _fail_since, _one_pass
    now = time.ticks_ms()
    if state == "sta_try":
        if _sta.isconnected():
            state = "sta_up"
            _lost_since = 0
            _fail_since, _one_pass = None, False
        elif time.ticks_diff(now, _since) > STA_TRY_MS:
            _start_sta(_try_idx + 1)
    elif state == "sta_up":
        if _sta.isconnected():
            _lost_since = 0
            _send_beacon()
        else:
            _lost_since = _lost_since or now
            if time.ticks_diff(now, _lost_since) > STA_LOST_MS:
                _fail_since = _lost_since          # o tempo ate ao AP conta desde a queda
                _start_sta(0)
    elif state == "ap":
        if _dns is not None:
            _answer_dns()
        # Como o Tasmota: passada a janela volta a tentar as redes guardadas (se houver)
        if cfg["nets"] and time.ticks_diff(now, _since) > AP_WINDOW_MS:
            _start_sta(0)
    _serve()

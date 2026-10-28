#!/usr/bin/env python3
"""Stress test do bridge no Atom (2026-10-16). So biblioteca padrao.

  python3 /opt/camera-bridge/stress_bridge.py                      1 min de teste leve (modo de treino, camaras DEMO)
  python3 /opt/camera-bridge/stress_bridge.py --cycles 100 --hold 2 --clients 8 --duration 600
  sudo python3 /opt/camera-bridge/stress_bridge.py --real --cycles 30    EQUIPAMENTO VERDADEIRO (grava a serio)
  python3 /opt/camera-bridge/stress_bridge.py --cycles 0 --clients 0 --duration 300    linha de base, sem gravar

O que faz, tudo contra o bridge verdadeiro (http://127.0.0.1:8080), com as camaras DEMO do modo de treino
(GLOBAL SETTINGS > TRAINING > Demo mode), sem equipamento a gravar:
  - ciclos de REC/STOP do grupo (/api/rec) a --hold s de take e --pause s entre takes (--cycles 0: so mede o
    bridge parado, sem gravar nada, para ter uma linha de base);
  - --clients leitores a pedir /api/status em paralelo (iPads, telemoveis, a consola);
  - de 2 em 2 s amostra o processo do bridge (memoria, CPU, fios, ficheiros abertos), a carga e a temperatura do Atom;
  - no fim junta os erros do journal e escreve o relatorio.
Relatorio em ~/stress-results/<data>/: report.txt (resumo e veredicto), samples.csv (amostras) e report.html (graficos).

Por omissao so aceita camaras do modo de treino: recusa-se a correr se este estiver desligado ou se os alvos de REC
tiverem algo que nao seja DEMO (F6, BMPCC principal, Canon, iPhones, GoPro verdadeiras). Com --real testa o equipamento
verdadeiro dos alvos de REC (pede SIM, lembra o que isso faz: cartoes, baterias, shoot log) e, com ou sem --real, confirma
no /api/status que CADA aparelho passa a gravar e pára dentro de --confirm s, e regista quanto demora (por aparelho).
Cada ciclo escreve takes no shoot log: apaga-as no fim. Corre-o com sudo para tambem medir os ficheiros abertos do bridge.
Nao toca em gateways nem em firmware.
"""
import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
SERVICO = "camera-bridge"


def api(method, path, body=None, timeout=15):
    """(status HTTP, JSON, segundos). Status 0 quando nao responde ou da timeout."""
    data = json.dumps(body or {}).encode() if method == "POST" else None
    cab = {"X-Bridge-Service": "1"}          # um servico do Atom, nao uma pessoa a ver (nao acorda as Canon)
    if data:
        cab["Content-Type"] = "application/json"
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=cab)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}"), time.monotonic() - t0
        except ValueError:
            return e.code, {}, time.monotonic() - t0
    except Exception:                         # pylint: disable=broad-except
        return 0, {}, time.monotonic() - t0


def pct(valores, p):
    if not valores:
        return 0.0
    v = sorted(valores)
    return v[min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1))))]


def resumo(valores):
    if not valores:
        return {"n": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "media": 0}
    return {"n": len(valores), "p50": pct(valores, 50), "p95": pct(valores, 95), "p99": pct(valores, 99),
            "max": max(valores), "media": statistics.fmean(valores)}


# ---- o processo do bridge ----------------------------------------------------------------------------------------------
def pid_bridge():
    try:
        out = subprocess.run(["systemctl", "show", SERVICO, "-p", "MainPID", "--value"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return int(out) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ler(caminho):
    try:
        with open(caminho) as f:
            return f.read()
    except OSError:
        return ""


def amostra_processo(pid):
    """(rss_mb, fios, ficheiros, ticks_cpu) do processo, de /proc."""
    rss = fios = 0
    for l in ler("/proc/%d/status" % pid).splitlines():
        if l.startswith("VmRSS:"):
            rss = int(l.split()[1]) / 1024.0
        elif l.startswith("Threads:"):
            fios = int(l.split()[1])
    try:
        fds = len(os.listdir("/proc/%d/fd" % pid))
    except OSError:
        fds = None                       # o bridge corre como root: sem sudo nao se lista (ver o aviso no relatorio)
    stat = ler("/proc/%d/stat" % pid)
    try:
        campos = stat[stat.rindex(")") + 2:].split()
        ticks = int(campos[11]) + int(campos[12])        # utime + stime
    except (ValueError, IndexError):
        ticks = 0
    return rss, fios, fds, ticks


def temperatura():
    """A temperatura mais alta que o Atom diz (thermal_zone ou hwmon), em graus; None se nao diz nenhuma."""
    import glob
    vals = []
    for caminho in glob.glob("/sys/class/thermal/thermal_zone*/temp") + glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
        t = ler(caminho).strip()
        if t.lstrip("-").isdigit() and 0 < int(t) < 150000:
            vals.append(int(t) / 1000.0)
    return max(vals) if vals else None


class Amostrador(threading.Thread):
    def __init__(self, pid, intervalo=2.0):
        super().__init__(daemon=True, name="amostras")
        self.pid, self.intervalo, self.parar = pid, intervalo, threading.Event()
        self.linhas, self.pid_mudou = [], False
        self.hz = os.sysconf("SC_CLK_TCK")

    def run(self):
        t0 = time.time()
        ant_ticks, ant_t = None, None
        while not self.parar.is_set():
            pid = pid_bridge()
            if pid and pid != self.pid:
                self.pid_mudou = True                    # o bridge reiniciou durante o teste
                self.pid = pid
                ant_ticks = None
            rss, fios, fds, ticks = amostra_processo(self.pid)
            agora = time.time()
            cpu = 0.0
            if ant_ticks is not None and agora > ant_t:
                cpu = (ticks - ant_ticks) / self.hz / (agora - ant_t) * 100.0
            ant_ticks, ant_t = ticks, agora
            carga = ler("/proc/loadavg").split()[0] if ler("/proc/loadavg") else ""
            livre = 0
            for l in ler("/proc/meminfo").splitlines():
                if l.startswith("MemAvailable:"):
                    livre = int(l.split()[1]) / 1024.0
            self.linhas.append({"t": round(agora - t0, 1), "rss_mb": round(rss, 1), "cpu_pct": round(cpu, 1),
                                "fios": fios, "ficheiros": fds, "carga1": carga, "mem_livre_mb": round(livre),
                                "temp_c": temperatura()})
            self.parar.wait(self.intervalo)


# ---- carga ---------------------------------------------------------------------------------------------------------------
class Leitor(threading.Thread):
    """Um iPad / telemovel / consola a pedir o estado."""

    def __init__(self, fim, intervalo):
        super().__init__(daemon=True, name="leitor")
        self.fim, self.intervalo = fim, intervalo
        self.tempos, self.erros = [], 0

    def run(self):
        while time.time() < self.fim:
            s, _d, dt = api("GET", "/api/status", timeout=10)
            if s == 200:
                self.tempos.append(dt)
            else:
                self.erros += 1
            time.sleep(self.intervalo)


def a_gravar(st, cid):
    """True / False se o /api/status diz que o aparelho grava; None se nao se consegue saber."""
    if cid == "bmpcc":
        return bool(st.get("is_recording"))
    if cid == "f6":
        f6 = st.get("f6")
        return bool(f6.get("running")) if isinstance(f6, dict) and "running" in f6 else None
    for chave in ("gopros", "bmpcc_extras", "iphones", "sonys", "cameras", "canons"):
        c = (st.get(chave) or {}).get(cid)
        if isinstance(c, dict):
            for k in ("running", "recording", "is_recording"):
                if k in c:
                    return bool(c[k])
    return None


def confirmar(alvos, quer, limite, t0):
    """Espera que cada alvo diga que grava (quer=True) ou parou. {id: segundos desde t0 | None se nao confirmou}.
    Os que o status nao deixa saber ficam fora (n/v)."""
    feito, fim = {}, time.monotonic() + limite
    while True:
        s, st, _ = api("GET", "/api/status", timeout=8)
        if s == 200:
            for cid in alvos:
                v = a_gravar(st, cid)
                if v is not None and cid not in feito and v == quer:
                    feito[cid] = time.monotonic() - t0
        if len(feito) == len([c for c in alvos if c in res_conhecidos]) or time.monotonic() > fim:
            break
        time.sleep(0.15)
    return {cid: feito.get(cid) for cid in alvos if cid in res_conhecidos}


res_conhecidos = set()          # os alvos cujo estado de gravacao o status mostra (preenchido no inicio)


def ciclos(args, res, parar):
    """REC / STOP do grupo. Cada chamada tem de responder 200 com 'recording' a alternar e cada aparelho tem de o
    confirmar no status dentro de --confirm s (o tempo que demora fica por aparelho)."""
    alvos = res["alvos"]
    for n in range(1, args.cycles + 1):
        if parar.is_set():
            break
        for quer in (True, False):
            t0 = time.monotonic()
            s, d, dt = api("POST", "/api/rec", timeout=20)
            res["rec" if quer else "stop"].append(dt)
            if s == 200 and d.get("ok"):
                res["gravando"] = d.get("recording") is True
            if s != 200 or not d.get("ok") or d.get("recording") != quer:
                res["falhas"].append("ciclo %d %s: HTTP %s %s" % (n, "REC" if quer else "STOP", s, json.dumps(d)[:120]))
                if s == 409 and "ecord" in json.dumps(d):      # ficou algo a gravar: tenta repor
                    api("POST", "/api/rec", timeout=20)
            if s == 200 and d.get("ok") and d.get("recording") == quer and res_conhecidos:
                conf = confirmar(alvos, quer, args.confirm, t0)
                for cid, t in conf.items():
                    por = res["aparelhos"].setdefault(cid, {"rec": [], "stop": [], "falhou": 0})
                    if t is None:
                        por["falhou"] += 1
                        res["falhas"].append("ciclo %d: %s nao confirmou %s em %.0f s" % (n, cid, "REC" if quer else "STOP", args.confirm))
                    else:
                        por["rec" if quer else "stop"].append(t)
            res["ciclos_feitos"] += 0.5
            time.sleep(args.hold if quer else args.pause)
        if res["falhas"] and len(res["falhas"]) >= args.max_falhas:
            res["falhas"].append("abortado: demasiadas falhas (%d)" % len(res["falhas"]))
            break


def erros_journal(desde):
    try:
        out = subprocess.run(["journalctl", "-u", SERVICO, "--since", "@%d" % int(desde), "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None, []
    ruins = [l for l in out.splitlines() if "Traceback" in l or "Error" in l or "ERROR" in l or "Exception" in l]
    return len(out.splitlines()), ruins


# ---- relatorio -----------------------------------------------------------------------------------------------------------
def veredicto(rel):
    falhas, avisos = [], []
    if rel["falhas_rec"]:
        falhas.append("%d chamadas REC/STOP falharam" % len(rel["falhas_rec"]))
    if rel["status"]["erros"]:
        falhas.append("%d pedidos de estado falharam" % rel["status"]["erros"])
    if rel["bridge_reiniciou"]:
        falhas.append("o bridge REINICIOU durante o teste")
    if rel["journal_erros"]:
        avisos.append("%d linhas de erro no journal" % len(rel["journal_erros"]))
    if rel["status"]["p99"] > 1.0:
        avisos.append("p99 do estado acima de 1 s (%.2f s)" % rel["status"]["p99"])
    if rel["rec"]["p95"] > 5.0:
        avisos.append("p95 do REC acima de 5 s (%.2f s)" % rel["rec"]["p95"])
    r = rel["recursos"]
    if r.get("rss_cresceu_pct", 0) > 20:
        avisos.append("memoria do bridge cresceu %.0f%% (possivel fuga)" % r["rss_cresceu_pct"])
    if r.get("fios_cresceram", 0) > 10:
        avisos.append("o bridge ganhou %d fios (possivel fuga)" % r["fios_cresceram"])
    if r.get("ficheiros_cresceram", 0) > 20:
        avisos.append("o bridge ganhou %d ficheiros abertos (possivel fuga)" % r["ficheiros_cresceram"])
    if (r.get("temp_max") or 0) > 80:
        avisos.append("temperatura acima de 80 C (%.0f)" % r["temp_max"])
    return ("FALHOU" if falhas else ("ATENCAO" if avisos else "PASSOU")), falhas, avisos


def grafico(titulo, xs, ys, unidade):
    if not ys:
        return ""
    w, h, m = 560, 130, 30
    lo, hi = min(ys), max(ys)
    if hi == lo:
        hi = lo + 1
    pts = " ".join("%.1f,%.1f" % (m + (x - xs[0]) / max(1e-9, xs[-1] - xs[0]) * (w - m - 8),
                                  h - 18 - (y - lo) / (hi - lo) * (h - 34)) for x, y in zip(xs, ys))
    return ('<figure><figcaption>%s <small>(min %.1f, max %.1f %s)</small></figcaption>'
            '<svg viewBox="0 0 %d %d" role="img" aria-label="%s"><polyline fill="none" stroke="currentColor" '
            'stroke-width="1.6" points="%s"/><line x1="%d" y1="%d" x2="%d" y2="%d" stroke="currentColor" opacity=".3"/></svg>'
            '</figure>' % (titulo, lo, hi, unidade, w, h, titulo, pts, m, h - 18, w - 8, h - 18))


def escrever(pasta, rel, linhas):
    os.makedirs(pasta, exist_ok=True)
    with open(os.path.join(pasta, "samples.csv"), "w", newline="") as f:
        if linhas:
            w = csv.DictWriter(f, fieldnames=list(linhas[0]))
            w.writeheader()
            w.writerows(linhas)
    with open(os.path.join(pasta, "report.json"), "w") as f:
        json.dump(rel, f, indent=1)
    v = rel["veredicto"]
    t = []
    t.append("STRESS TEST DO BRIDGE: %s" % v)
    t.append("inicio %s, duracao %.0f s, modo %s, %d leitores, %d ciclos pedidos (%d feitos)"
             % (rel["inicio"], rel["duracao_s"], rel["modo"].upper(), rel["clientes"], rel["ciclos_pedidos"], rel["ciclos_feitos"]))
    for x in rel["falhas"]:
        t.append("  FALHA: " + x)
    for x in rel["avisos"]:
        t.append("  AVISO: " + x)
    t.append("")
    t.append("%-22s %6s %8s %8s %8s %8s" % ("latencia (s)", "n", "p50", "p95", "p99", "max"))
    for nome, k in (("REC (grupo)", "rec"), ("STOP (grupo)", "stop"), ("estado (/api/status)", "status")):
        s = rel[k]
        t.append("%-22s %6d %8.3f %8.3f %8.3f %8.3f" % (nome, s["n"], s["p50"], s["p95"], s["p99"], s["max"]))
    t.append("pedidos de estado falhados: %d" % rel["status"]["erros"])
    if rel["aparelhos"]:
        t.append("")
        t.append("%-14s %24s %24s %9s" % ("confirmacao (s)", "REC  p50 / max", "STOP p50 / max", "falhas"))
        for cid, ap_ in sorted(rel["aparelhos"].items()):
            t.append("%-14s %12.2f / %-9.2f %12.2f / %-9.2f %6d" % (cid, ap_["rec"]["p50"], ap_["rec"]["max"],
                                                                 ap_["stop"]["p50"], ap_["stop"]["max"], ap_["falhou"]))
    r = rel["recursos"]
    if r:
        t.append("")
        nd = lambda v: "n/d" if v is None else v
        t.append("bridge: memoria %.0f -> %.0f MB (max %.0f, %+.0f%%) | CPU media %.1f%% max %.1f%% | fios %d -> %d | ficheiros %s -> %s"
                 % (r["rss_ini"], r["rss_fim"], r["rss_max"], r["rss_cresceu_pct"], r["cpu_media"], r["cpu_max"],
                    r["fios_ini"], r["fios_fim"], nd(r["ficheiros_ini"]), nd(r["ficheiros_fim"])))
        if r["ficheiros_ini"] is None:
            t.append("  (os ficheiros abertos nao se leem sem root: corre com  sudo python3 %s  para os medir)" % sys.argv[0])
        t.append("Atom: carga max %.2f | memoria livre min %.0f MB | temperatura max %s"
                 % (r["carga_max"], r["mem_livre_min"], ("%.0f C" % r["temp_max"]) if r["temp_max"] else "?"))
    if rel["falhas_rec"]:
        t.append("")
        t.append("Falhas de REC/STOP (primeiras 10):")
        t += ["  " + x for x in rel["falhas_rec"][:10]]
    if rel["journal_erros"]:
        t.append("")
        t.append("Linhas de erro no journal (primeiras 15 de %d):" % len(rel["journal_erros"]))
        t += ["  " + x[:160] for x in rel["journal_erros"][:15]]
    with open(os.path.join(pasta, "report.txt"), "w") as f:
        f.write("\n".join(t) + "\n")
    xs = [l["t"] for l in linhas]
    graficos = "".join([
        grafico("Memoria do bridge", xs, [l["rss_mb"] for l in linhas], "MB"),
        grafico("CPU do bridge", xs, [l["cpu_pct"] for l in linhas], "%"),
        grafico("Fios do bridge", xs, [l["fios"] for l in linhas], ""),
        grafico("Ficheiros abertos", xs, [l["ficheiros"] for l in linhas], "") if linhas and linhas[0]["ficheiros"] is not None else "",
        grafico("Temperatura", xs, [l["temp_c"] for l in linhas if l["temp_c"] is not None],
                "C") if any(l["temp_c"] is not None for l in linhas) else ""])
    cor = {"PASSOU": "#2e7d32", "ATENCAO": "#b26a00", "FALHOU": "#c62828"}[v]
    with open(os.path.join(pasta, "report.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Stress test do bridge</title><style>:root{color-scheme:light dark}body{font:15px system-ui;margin:16px;'
                'max-width:640px}h1{color:%s}pre{white-space:pre-wrap;font-size:13px}figure{margin:12px 0}small{opacity:.7}'
                'svg{width:100%%;height:auto}</style><h1>%s</h1><pre>%s</pre>%s'
                % (cor, v, "\n".join(t).replace("<", "&lt;"), graficos))
    return "\n".join(t)


# ---- principal -----------------------------------------------------------------------------------------------------------
def verificar_seguro(real):
    s, d, _ = api("GET", "/api/demo", timeout=8)
    if s != 200:
        sys.exit("O bridge nao responde em %s (HTTP %s). Esta a correr?  systemctl status camera-bridge" % (BASE, s))
    if not d.get("on") and not real:
        sys.exit("O modo de treino esta desligado. Liga-o em GLOBAL SETTINGS > TRAINING > Demo mode (camaras DEMO) "
                 "e volta a correr, ou usa --real para testar com o equipamento verdadeiro (grava a serio).")
    s, st, _ = api("GET", "/api/status", timeout=8)
    return (list(st.get("rec_targets") or []), st) if s == 200 else ([], {})


def nao_demo(alvos, st):
    """Os alvos de REC que nao sao camaras do modo de treino. DEMO = o gateway/C3 simulado (127.0.0.1) ou o nome DEMO-*.
    O F6, a BMPCC principal, as Canon e os iPhones nunca sao DEMO."""
    out = []
    for cid in alvos:
        c = (st.get("gopros") or {}).get(cid)
        host = (c or {}).get("gateway")
        if c is None:
            c = (st.get("bmpcc_extras") or {}).get(cid)
            host = (c or {}).get("host")
        demo = c is not None and (str(c.get("name") or "").upper().startswith("DEMO") or str(host or "").startswith("127.0.0.1"))
        if not demo:
            out.append(cid if c is None else "%s (%s)" % (cid, c.get("name") or "?"))
    return out


def main():
    ap = argparse.ArgumentParser(description="Stress test do bridge (modo de treino).")
    ap.add_argument("--cycles", type=int, default=20, help="ciclos de REC+STOP (por omissao 20)")
    ap.add_argument("--real", action="store_true",
                    help="testar com o equipamento VERDADEIRO nos alvos de REC (grava a serio: cartoes, baterias, shoot log)")
    ap.add_argument("--hold", type=float, default=None, help="segundos a gravar em cada take (2 em treino, 6 com --real)")
    ap.add_argument("--pause", type=float, default=None, help="segundos entre takes (1 em treino, 4 com --real)")
    ap.add_argument("--confirm", type=float, default=8.0, help="segundos para cada aparelho confirmar REC/STOP no status")
    ap.add_argument("--clients", type=int, default=4, help="leitores de /api/status em paralelo")
    ap.add_argument("--poll", type=float, default=0.5, help="intervalo de cada leitor (s)")
    ap.add_argument("--duration", type=float, default=0, help="duracao minima (s): continua a ler o estado depois dos ciclos")
    ap.add_argument("--max-falhas", type=int, default=10)
    ap.add_argument("--yes", action="store_true", help="nao perguntar")
    ap.add_argument("--url", default=BASE)
    ap.add_argument("--out", default=os.path.expanduser("~/stress-results"))
    args = ap.parse_args()
    globals()["BASE"] = args.url.rstrip("/")
    if args.hold is None:
        args.hold = 6.0 if args.real else 2.0
    if args.pause is None:
        args.pause = 4.0 if args.real else 1.0

    alvos, st0 = verificar_seguro(args.real)
    if args.cycles > 0:
        print("Alvos de REC neste momento: %s" % (", ".join(alvos) or "nenhum"))
        if not alvos:
            sys.exit("Nao ha alvos de REC. Poe as camaras DEMO nos alvos (consola: DEVICES, ou a pagina) e volta a correr.")
        reais = nao_demo(alvos, st0)
        if reais and not args.real:
            sys.exit("RECUSADO: estes alvos de REC nao sao camaras DEMO e iam gravar a serio: %s\n"
                     "Tira-os dos alvos (pagina ou consola: o REC dos alvos) e volta a correr; para testar de proposito com o equipamento "
                     "verdadeiro usa --real; ou --cycles 0 para medir so o bridge parado." % ", ".join(reais))
        ja = [c for c in alvos if a_gravar(st0, c)]
        if ja:
            sys.exit("Ja ha alvos a gravar (%s): pára-os primeiro. Um REC de grupo pararia essas e o teste ia ao contrario." % ", ".join(ja))
        if args.real:
            takes = args.cycles
            print("\nTESTE COM EQUIPAMENTO VERDADEIRO. Vai gravar %d takes de ~%.0f s em: %s" % (takes, args.hold, ", ".join(alvos)))
            print("  - fica em cada cartao ~%.0f min de material de teste (apaga-o depois) e gasta bateria e aquece as camaras;" % (takes * args.hold / 60.0))
            print("  - o shoot log ganha as takes de teste (e a numeracao das takes avanca): apaga-as no fim;")
            print("  - o F6, se estiver nos alvos e armado, grava tambem. Ctrl+C a meio manda STOP.")
            if not args.yes and input("Escreve SIM para continuar: ").strip() != "SIM":
                sys.exit("Cancelado.")
        res_conhecidos.update(c for c in alvos if a_gravar(st0, c) is not None)
        desconhecidos = [c for c in alvos if c not in res_conhecidos]
        if desconhecidos:
            print("Nota: o status nao mostra se estes gravam (so se mede a chamada): %s" % ", ".join(desconhecidos))
    pid = pid_bridge()
    if not pid:
        print("Aviso: nao achei o processo do bridge (systemctl show %s): sem amostras de memoria/CPU." % SERVICO)
    t_ini = time.time()
    estimado = args.cycles * (args.hold + args.pause + 0.5)
    fim_leitores = t_ini + max(args.duration, estimado) + 5
    leitores = [Leitor(fim_leitores, args.poll) for _ in range(args.clients)]
    amostrador = Amostrador(pid) if pid else None
    if amostrador:
        amostrador.start()
    for l in leitores:
        l.start()
    res = {"rec": [], "stop": [], "falhas": [], "ciclos_feitos": 0.0, "gravando": False, "alvos": alvos, "aparelhos": {}}
    parar = threading.Event()
    print("A correr (~%.0f s). Ctrl+C para parar." % max(args.duration, estimado))
    try:
        ciclos(args, res, parar)
        while time.time() < t_ini + args.duration:
            time.sleep(1)
    except KeyboardInterrupt:
        parar.set()
        print("Interrompido: a repor (STOP) e a fechar o relatorio ...")
    if res["gravando"]:                       # interrompido a meio de uma take: o proximo toque do grupo e um STOP
        s2, d2, _ = api("POST", "/api/rec", timeout=20)
        print("STOP final: HTTP %s %s" % (s2, "ok" if d2.get("ok") else json.dumps(d2)[:100]))
    for l in leitores:
        l.fim = 0
    for l in leitores:
        l.join(timeout=12)
    if amostrador:
        amostrador.parar.set()
        amostrador.join(timeout=5)
    todos = [x for l in leitores for x in l.tempos]
    status = resumo(todos)
    status["erros"] = sum(l.erros for l in leitores)
    n_journal, ruins = erros_journal(t_ini)
    linhas = amostrador.linhas if amostrador else []
    r = {}
    if linhas:
        rss = [l["rss_mb"] for l in linhas]
        cpu = [l["cpu_pct"] for l in linhas[1:]] or [0.0]
        cargas = [float(l["carga1"]) for l in linhas if l["carga1"]] or [0.0]
        temps = [l["temp_c"] for l in linhas if l["temp_c"] is not None]
        r = {"rss_ini": rss[0], "rss_fim": rss[-1], "rss_max": max(rss),
             "rss_cresceu_pct": (rss[-1] - rss[0]) / rss[0] * 100.0 if rss[0] else 0.0,
             "cpu_media": statistics.fmean(cpu), "cpu_max": max(cpu),
             "fios_ini": linhas[0]["fios"], "fios_fim": linhas[-1]["fios"],
             "fios_cresceram": linhas[-1]["fios"] - linhas[0]["fios"],
             "ficheiros_ini": linhas[0]["ficheiros"], "ficheiros_fim": linhas[-1]["ficheiros"],
             "ficheiros_cresceram": (linhas[-1]["ficheiros"] - linhas[0]["ficheiros"])
             if linhas[0]["ficheiros"] is not None and linhas[-1]["ficheiros"] is not None else 0,
             "carga_max": max(cargas), "mem_livre_min": min(l["mem_livre_mb"] for l in linhas),
             "temp_max": max(temps) if temps else None}
    rel = {"inicio": time.strftime("%F %T", time.localtime(t_ini)), "duracao_s": time.time() - t_ini,
           "clientes": args.clients, "ciclos_pedidos": args.cycles, "ciclos_feitos": int(res["ciclos_feitos"]),
           "rec": resumo(res["rec"]), "stop": resumo(res["stop"]), "status": status, "recursos": r,
           "falhas_rec": res["falhas"], "modo": "real" if args.real else "treino",
           "aparelhos": {cid: {"rec": resumo(v["rec"]), "stop": resumo(v["stop"]), "falhou": v["falhou"]}
                         for cid, v in res["aparelhos"].items()},
           "journal_linhas": n_journal, "journal_erros": ruins,
           "bridge_reiniciou": bool(amostrador and amostrador.pid_mudou)}
    rel["veredicto"], rel["falhas"], rel["avisos"] = veredicto(rel)
    pasta = os.path.join(args.out, time.strftime("%Y%m%d-%H%M%S", time.localtime(t_ini)))
    print()
    print(escrever(pasta, rel, linhas))
    print("\nFicheiros em %s (report.txt, report.html, samples.csv, report.json)" % pasta)
    sys.exit(0 if rel["veredicto"] != "FALHOU" else 1)


if __name__ == "__main__":
    main()

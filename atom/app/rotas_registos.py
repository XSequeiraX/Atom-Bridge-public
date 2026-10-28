"""Shoot log, event log, notas e marcas, copias por SFTP e na pen (/api/log..., /api/backup...).

Parte do bridge (o arranque e o estado partilhado estao em nucleo.py). As rotas registam-se no app ao
importar o modulo, no fim do nucleo."""
import copia
import csv
import json
import os
import re
import subprocess
import threading
import time
from flask import jsonify, request
from nucleo import (EVENT_LOG_PATH, SHOOT_LOG_FIELDS, SHOOT_LOG_PATH, _body, _marcar, _nota_no_log,
    _rewrite_shoot_log, _run, _shoot_log_lock, _take_ativos, _take_lock, anything_recording, app, log_event, state)


@app.route('/api/log/mark', methods=['POST'])
def api_mark():
    """Assinala a take que acabou de parar.

    Escreve no shoot log com o timecode do instante, para em pos se encontrar a
    take pelo numero em vez de a rever inteira. Nao pergunta nada nem confirma:
    se obrigasse a escrever alguma coisa no momento, nao seria usado quando e
    preciso.
    """
    dados = _body()
    nota = str(dados.get("note") or "")[:80]
    # good = take a aproveitar, no-good = a descartar, mark = so um instante.
    # A distincao vai para uma coluna propria do log em vez de ficar diluida na
    # nota: e por ela que se filtra o ficheiro em pos.
    especie = dados.get("kind") if dados.get("kind") in ("good", "no-good", "mark") else "mark"
    tc = _marcar(especie, nota)
    return jsonify({"ok": True, "tc": tc, "kind": especie})


def _digitos(t):
    """True se `t` (sem espacos) sao so algarismos ASCII, no maximo 9: int() nunca rebenta com ele ('\u00b2'.isdigit() e
    True mas int() levanta ValueError; milhares de algarismos tambem) -- era um 500 em vez de um 400."""
    t = str(t).strip()
    return t.isascii() and t.isdigit() and len(t) <= 9


def _digitos_pos(t):
    return _digitos(t) and int(str(t).strip()) > 0


@app.route('/api/log/shoot', methods=['GET'])
def api_shoot_log_json():
    """As ultimas linhas do log, para a grelha na propria pagina.

    So o fim do ficheiro interessa: quem esta a filmar quer ver o que acabou de
    acontecer, e mandar o historico todo para o telemovel a cada abertura seria
    desperdicio. O CSV completo continua a poder ser descarregado.
    """
    try:
        limit = max(1, min(500, int(request.args.get("limit", 60))))
    except (TypeError, ValueError):
        limit = 60
    try:
        rows = _shoot_log_linhas()
    except Exception as e:
        return jsonify({"error": str(e), "rows": []}), 500
    return jsonify({"rows": rows[-limit:][::-1], "total": len(rows)})


_shoot_log_cache = {"chave": None, "rows": []}


def _shoot_log_linhas():
    """Todas as linhas do shoot log (so leitura: nao mexer na lista). So se rele o CSV quando ele muda (tamanho, data,
    inode): a consola, a pagina e o ecra do shoot log pedem-no de poucos em poucos segundos e o ficheiro cresce o dia todo."""
    with _shoot_log_lock:
        try:
            st = os.stat(SHOOT_LOG_PATH)
        except FileNotFoundError:
            return []
        chave = (st.st_ino, st.st_size, st.st_mtime_ns)
        if _shoot_log_cache["chave"] != chave:
            with open(SHOOT_LOG_PATH, newline="") as f:
                rows = [{k: r.get(k, "") for k in SHOOT_LOG_FIELDS} for r in csv.DictReader(f)]
            _shoot_log_cache.update(chave=chave, rows=rows)
        return _shoot_log_cache["rows"]


# Eventos que uma linha pode ter. E lista fechada de proposito: o CSV vai para
# pos e um evento inventado a mao nao seria reconhecido por nada do outro
# lado. Todas as que o codigo escreve, e nao so as bonitas: sem "fault" aqui,
# a linha de uma paragem inesperada -- justamente a que mais se quer anotar --
# nao era corrigivel. "stop-unexpected" foi fundido em "fault" a 2026-09-17
# (o proprio "fault" ja cobre "algo correu mal"); "keep"->"good",
# "ng"->"no-good" no mesmo dia, a pedido do operador.
SHOOT_LOG_EVENTS = {"start", "stop", "slate", "mark",
                    "good", "no-good", "fault", "note"}


def _linha_do_take_aberto(rid):
    """A linha `rid` e do take que esta a gravar agora? Apaga-la ou mudar-lhe o evento/numero estraga a contagem dos
    takes e a retoma depois de um reinício do bridge."""
    if not anything_recording():
        return False
    n = str(int(state.get("_take_no") or 0))
    try:
        with _shoot_log_lock:
            with open(SHOOT_LOG_PATH, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("id") == rid:
                        return (r.get("take") or "").strip() == n
    except OSError:
        pass
    return False


@app.route('/api/log/shoot/edit', methods=['POST'])
def api_shoot_log_edit():
    """Corrige uma linha do log: o evento, a nota, ou ambos.

    Corrige no sitio em vez de acrescentar uma linha de correccao, porque o CSV
    e lido em pos por quem nao esteve ca e uma entrada errada ao lado da certa
    e pior do que nao ter nenhuma. A alteracao fica registada no EVENT LOG, que
    e onde se ve o que se andou a fazer nesta sessao.
    """
    d = _body()
    rid = str(d.get("id") or "").strip()
    if not rid:
        return jsonify({"error": "no id"}), 400
    evento = d.get("action", d.get("event"))               # "event" e o nome antigo: os clientes de antes continuam a funcionar
    if evento is not None:
        evento = str(evento).strip().lower()
        if evento not in SHOOT_LOG_EVENTS:
            return jsonify({"error": f"unknown event '{evento}'"}), 400
    nota = d.get("note")
    if nota is not None:
        # Uma so linha: o CSV cita virgulas sozinho, mas uma quebra de linha
        # partia a leitura de quem o abre com ferramentas mais simples.
        nota = " ".join(str(nota).split())[:200]
    take = d.get("take")
    if take is not None:
        take = str(take).strip()
        # Vazio apaga o numero; qualquer outra coisa tem de ser um inteiro
        # positivo, senao a numeracao deixa de ordenar.
        if take and not _digitos_pos(take):
            return jsonify({"error": f"take must be a positive number, got '{take}'"}), 400
    if evento is None and nota is None and take is None:
        return jsonify({"error": "nothing to change"}), 400
    if (evento is not None or take is not None) and _linha_do_take_aberto(rid):
        return jsonify({"error": "this take is recording: change its event or number after it stops (the note is fine)"}), 409

    def mutate(rows):
        for r in rows:
            if r["id"] != rid:
                continue
            antes = f"{r['action']}" + (f" take {r['take']}" if r.get("take") else "")
            if evento is not None:
                r["action"] = evento
            if nota is not None:
                r["note"] = nota
            if take is not None:
                r["take"] = take
            depois = f"{r['action']}" + (f" take {r['take']}" if r.get("take") else "")
            return rows, f"{r['source']} {antes} -> {depois}"
        return None

    desc, erro = _rewrite_shoot_log(mutate)
    if erro:
        return jsonify({"error": erro}), 404
    log_event(f"Shoot log edited: {desc}")
    return jsonify({"ok": True})


@app.route('/api/log/shoot/delete', methods=['POST'])
def api_shoot_log_delete():
    """Apaga uma linha do log.

    Existe porque uma entrada a mais -- uma gravacao arrancada por engano, uma
    marca posta fora de sitio -- desalinha a contagem de tomadas com o material.
    """
    rid = str(_body().get("id") or "").strip()
    if not rid:
        return jsonify({"error": "no id"}), 400
    if _linha_do_take_aberto(rid):
        return jsonify({"error": "this take is recording: delete the entry after it stops"}), 409

    def mutate(rows):
        fica = [r for r in rows if r["id"] != rid]
        if len(fica) == len(rows):
            return None
        ida = next(r for r in rows if r["id"] == rid)
        return fica, f"{ida['source']} {ida['action']} at {ida['timestamp'][11:]}"

    desc, erro = _rewrite_shoot_log(mutate)
    if erro:
        return jsonify({"error": erro}), 404
    log_event(f"Shoot log entry deleted: {desc}")
    return jsonify({"ok": True})


# === COPIA DE SEGURANCA POR SFTP ===
# As credenciais vivem FORA do codigo, num ficheiro so legivel pelo root, e
# nunca aparecem na interface nem numa linha de comandos. Usa-se o paramiko e
# nao o sshpass por isso mesmo: com o sshpass a palavra-passe passa pelo
# argv e fica visivel a quem corra `ps`.
SFTP_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sftp.json")

# A chave do servidor guarda-se na primeira ligacao que autentica e as seguintes recusam-se se ela mudar (sem isto, um
# intruso na rede passava por o servidor e levava a palavra-passe e os registos).
SFTP_HOSTKEYS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sftp_known_hosts")


def sftp_config():
    """A configuracao guardada, ou None se ainda nao houver."""
    try:
        with open(SFTP_CONF_PATH, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or not c.get("host") or not c.get("user"):
        return None
    c.setdefault("port", 22)
    c.setdefault("path", ".")
    return c


def _ficheiros_a_enviar():
    """O shoot log e o event log actuais, mais os arquivos ainda em disco.

    Os actuais vao com um carimbo de data no nome remoto. Os arquivos ja o
    trazem no nome de origem, por isso vao como estao. Assim nada se sobrepoe
    no servidor e nao ha que pensar em versoes -- foi a razao de o operador
    querer o sufixo logo no arquivo.
    """
    carimbo = time.strftime("%Y%m%d-%H%M%S")
    pares = []
    for caminho, base, ext in ((SHOOT_LOG_PATH, "shoot_log", ".csv"),
                               (EVENT_LOG_PATH, "events", ".log")):
        if os.path.exists(caminho) and os.path.getsize(caminho) > 0:
            pares.append((caminho, f"{base}-{carimbo}{ext}"))
    pasta = os.path.dirname(os.path.abspath(SHOOT_LOG_PATH))
    for nome in sorted(os.listdir(pasta)):
        if re.match(r"^(shoot_log|events)-\d{8}-\d{6}\.(csv|log)$", nome):
            pares.append((os.path.join(pasta, nome), nome))
    return pares


# O envio em curso (ou o ultimo), para a consola e a pagina mostrarem a barra e o resultado em vez de mandarem ver o
# event log.
_sftp_estado = {"running": False}

_sftp_lock = threading.Lock()


def sftp_estado():
    with _sftp_lock:
        return dict(_sftp_estado)


def _sftp_mexe(**k):
    with _sftp_lock:
        _sftp_estado.update(k)


def enviar_por_sftp():
    """Envia os registos. Devolve (enviados, erro). Vai pondo o progresso em _sftp_estado (bytes e ficheiro)."""
    c = sftp_config()
    if not c:
        return [], "no SFTP server configured"
    ficheiros = _ficheiros_a_enviar()
    if not ficheiros:
        return [], "nothing to send"
    total = sum(os.path.getsize(l) for l, _r in ficheiros)
    _sftp_mexe(total_files=len(ficheiros), total_bytes=total, done_bytes=0, done_files=0, skipped=0, file=None)
    import paramiko
    cli = paramiko.SSHClient()
    try:
        cli.load_host_keys(SFTP_HOSTKEYS_PATH)
    except IOError:
        pass                                    # ainda nao ha: a primeira ligacao fixa-a
    # A primeira ligacao a uma maquina nova nao pode falhar por nao a
    # conhecermos: isto corre sem ninguem a quem perguntar "aceitas a chave?". Uma chave DIFERENTE da guardada
    # levanta BadHostKeyException (ver em baixo) em vez de ser aceite.
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    enviados = []
    try:
        cli.connect(c["host"], port=int(c["port"]), username=c["user"],
                    password=c.get("password") or None, timeout=20,
                    allow_agent=False, look_for_keys=False)
        try:                                    # autenticou: a chave deste servidor passa a ser a de confianca
            fd = os.open(SFTP_HOSTKEYS_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
            cli.save_host_keys(SFTP_HOSTKEYS_PATH)
        except (OSError, IOError):
            pass
        sftp = cli.open_sftp()
        # sftp robusto: a pasta que falta cria-se, uma parte de cada vez.
        try:
            sftp.chdir(c["path"])
        except IOError:
            caminho = c["path"].replace("\\", "/")
            if caminho.startswith("/"):
                sftp.chdir("/")
            for parte in [p for p in caminho.split("/") if p and p != "."]:
                try:
                    sftp.chdir(parte)
                except IOError:
                    try:
                        sftp.mkdir(parte)
                        sftp.chdir(parte)
                    except IOError as e:
                        return [], f"cannot create the remote folder {c['path']!r} ({parte!r}: {e})"
            log_event(f"SFTP: remote folder {c['path']} ready")
        feito = 0
        for n, (local, remoto) in enumerate(ficheiros, 1):
            tam = os.path.getsize(local)
            _sftp_mexe(file=remoto, file_no=n)
            # Um arquivo (nome com data) que ja esta no servidor com o mesmo tamanho nao vai outra vez: antes subiam
            # todos a cada envio. Os actuais levam um carimbo novo, por isso vao sempre.
            if remoto == os.path.basename(local):
                try:
                    if sftp.stat(remoto).st_size == tam:
                        feito += tam
                        with _sftp_lock:
                            _sftp_estado["skipped"] = _sftp_estado.get("skipped", 0) + 1
                            _sftp_estado["done_files"] = n
                            _sftp_estado["done_bytes"] = feito
                        continue
                except IOError:
                    pass
            base = feito
            sftp.put(local, remoto, callback=lambda a, _t, b=base: _sftp_mexe(done_bytes=b + a))
            feito += tam
            enviados.append(remoto)
            _sftp_mexe(done_files=n, done_bytes=feito)
        sftp.close()
    except paramiko.BadHostKeyException:
        return enviados, ("the SFTP server's key is NOT the one saved on the first connection: nothing was sent. If the "
                          "server was reinstalled, save the SFTP settings again (that forgets the old key)")
    except Exception as e:
        return enviados, f"{type(e).__name__}: {e}"
    finally:
        try:
            cli.close()
        except Exception:
            pass
    return enviados, None


def _arquivar(caminho, etiqueta):
    """Poe o ficheiro de lado com a data no nome e devolve o novo nome.

    ARQUIVA em vez de apagar, e isto nao e escrupulo a mais: um shoot log e o
    que liga o material a linha temporal, e quem carrega em "comecar de novo" a
    meio de um dia esta a um clique de perder a manha. O disco tem 23 GB
    livres; um CSV de uma rodagem tem alguns kilobytes.
    """
    if not os.path.exists(caminho):
        return None
    base, ext = os.path.splitext(caminho)
    destino = f"{base}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    os.replace(caminho, destino)
    log_event(f"{etiqueta} archived as {os.path.basename(destino)}")
    return os.path.basename(destino)


@app.route('/api/backup/sftp', methods=['GET', 'POST'])
def api_backup_config():
    """Le e grava a configuracao do servidor SFTP.

    A leitura NUNCA devolve a palavra-passe -- so se ha uma guardada. Sem isso,
    qualquer pessoa com acesso a pagina a podia ler, e a pagina nao tem
    autenticacao nenhuma.
    """
    if request.method == "GET":
        c = sftp_config() or {}
        return jsonify({"host": c.get("host", ""), "port": c.get("port", 22),
                        "user": c.get("user", ""), "path": c.get("path", "."),
                        "has_password": bool(c.get("password"))})
    d = request.get_json(silent=True)
    d = d if isinstance(d, dict) else {}
    host = str(d.get("host") or "").strip()
    user = str(d.get("user") or "").strip()
    # Enganos frequentes no campo do servidor: "sftp://", "utilizador@" e ":porta" colados ao nome.
    porta_no_host = None
    for pre in ("sftp://", "ssh://"):
        if host.lower().startswith(pre):
            host = host[len(pre):]
    host = host.rstrip("/")
    if "@" in host:
        quem, host = host.rsplit("@", 1)
        user = user or quem.strip()
    if host.startswith("[") and "]" in host:              # IPv6: [endereco]:porta
        nome, _, resto = host[1:].partition("]")
        if resto.startswith(":") and _digitos(resto[1:]):
            porta_no_host = int(resto[1:])
        host = nome.strip()
    elif host.count(":") == 1:
        nome, p = host.split(":")
        if _digitos(p):
            host, porta_no_host = nome.strip(), int(p)
    if not host or not user:
        return jsonify({"error": "host and user are required"}), 400
    try:
        porta = porta_no_host if porta_no_host is not None else int(str(d.get("port") or 22).strip())
    except ValueError:
        return jsonify({"error": "port: a number, e.g. 22"}), 400
    if not 1 <= porta <= 65535:
        return jsonify({"error": "port: between 1 and 65535"}), 400
    antiga = sftp_config() or {}
    nova = {
        "host": host, "user": user,
        "port": porta,
        "path": str(d.get("path") or ".").strip() or ".",
        # Campo vazio mantem a que ja la estava, para se poder corrigir o
        # caminho sem ter de reescrever a palavra-passe.
        "password": str(d.get("password") or "") or antiga.get("password", ""),
    }
    try:
        # Tem a palavra-passe: cria-se ja com 0600 (nao fica legivel nem um instante) e troca-se de uma vez.
        tmp = SFTP_CONF_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(nova, f, indent=2)
        os.replace(tmp, SFTP_CONF_PATH)
        if (antiga.get("host"), antiga.get("port")) != (host, porta):
            try:
                os.remove(SFTP_HOSTKEYS_PATH)       # outro servidor: a chave do anterior ja nao serve
            except OSError:
                pass
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    log_event(f"SFTP backup set to {user}@{host} port {porta}, folder {nova['path']}")
    return jsonify({"ok": True})


@app.route('/api/backup/send', methods=['POST'])
def api_backup_send():
    """Envia os registos para o servidor. Corre em segundo plano.

    Nao se recusa a gravar: enviar ficheiros nao toca no radio nem nas camaras,
    e e precisamente a meio de um dia longo que faz falta ter copia.
    """
    if not sftp_config():
        return jsonify({"error": "no SFTP server configured"}), 400
    with _sftp_lock:
        if _sftp_estado.get("running"):
            return jsonify({"error": "a send is already running"}), 409
        _sftp_estado.clear()
        _sftp_estado.update(running=True, started=time.time(), total_files=0, done_files=0,
                            total_bytes=0, done_bytes=0, skipped=0, file=None)

    def trabalho():
        log_event("SFTP backup starting...")
        try:
            enviados, erro = enviar_por_sftp()
        except Exception as e:                      # pylint: disable=broad-except
            enviados, erro = [], "%s: %s" % (type(e).__name__, e)
        salt = sftp_estado().get("skipped", 0)
        if erro:
            log_event(f"SFTP backup failed after {len(enviados)} file(s): {erro}")
        else:
            log_event(f"SFTP backup sent {len(enviados)} file(s)" + (f", {salt} already on the server" if salt else ""))
        _sftp_mexe(running=False, finished=time.time(), ok=not erro, error=erro, sent=len(enviados), file=None)

    threading.Thread(target=trabalho, daemon=True).start()
    return jsonify({"ok": True})


@app.route('/api/log/shoot/note', methods=['POST'])
def api_shoot_log_note():
    """Escreve uma nota no shoot log, na take que estiver a decorrer.

    Existe para se escrever a serio -- com um teclado Bluetooth ligado ao
    telemovel -- e nao para carimbar veredictos, que e o que os botoes
    KEEP/NG/MARK ja fazem. Por isso guarda TEXTO e nao um simbolo, e herda o
    numero da take para a nota dizer de que tomada fala.
    """
    d = _body()
    texto = str(d.get("note") or "").strip()
    if not texto:
        return jsonify({"error": "empty note"}), 400
    # Uma linha so, e com tecto: o CSV cita virgulas sozinho, mas uma quebra de
    # linha parte a leitura de quem o abra com ferramentas mais simples.
    texto = " ".join(texto.split())[:300]
    # Uma nota pode referir-se a uma take PASSADA -- lembra-se de uma coisa
    # duas takes depois, e o sitio dela e junto a take de que fala, nao junto
    # aquela em que se estava a pensar nela.
    take = str(d.get("take") or "").strip()
    if take and not _digitos_pos(take):                         # antes de escrever a linha: um pedido recusado nao escreve nada
        return jsonify({"ok": False, "error": f"take must be a positive number, got '{take}'"}), 400
    rid = _nota_no_log(texto, take)
    return jsonify({"ok": True, "id": rid})


@app.route('/api/log/shoot/new', methods=['POST'])
def api_shoot_log_new():
    """Arquiva o shoot log actual e comeca um vazio.

    Recusa a meio de uma gravacao: a linha de `start` ficaria num ficheiro e a
    de `stop` noutro, e a take deixava de ter duracao em qualquer um deles.
    """
    if anything_recording():
        return jsonify({"error": "Recording — finish the take first"}), 409
    with _shoot_log_lock:
        antigo = _arquivar(SHOOT_LOG_PATH, "Shoot log")
        with open(SHOOT_LOG_PATH, "w", newline="") as f:
            csv.writer(f).writerow(SHOOT_LOG_FIELDS)
            f.flush()
            os.fsync(f.fileno())
    # A numeracao de takes recomeca com o ficheiro: um log novo e uma folha
    # nova, e continuar na take 47 num ficheiro que comeca do zero nao ajuda
    # ninguem em pos.
    state["_take_no"] = 0
    state["_take_day"] = None
    with _take_lock:
        _take_ativos.clear()
    log_event("Shoot log started fresh — takes numbered from 1")
    return jsonify({"ok": True, "archived": antigo})


@app.route('/api/log/events/new', methods=['POST'])
def api_events_new():
    """Arquiva o event log em disco e comeca um vazio.

    Limpa tambem o que esta em memoria, senao a interface continuava a mostrar
    linhas que ja nao existem no ficheiro que se descarrega -- duas versoes da
    mesma coisa a discordar.
    """
    antigo = _arquivar(EVENT_LOG_PATH, "Event log")
    state["log"] = []
    state["last_event"] = ""
    log_event("Event log started fresh")
    return jsonify({"ok": True, "archived": antigo})


@app.route('/api/log/shoot.csv', methods=['GET'])
def api_shoot_log():
    """Downloadable CSV of every recording start/stop this session —
    see log_shot() / SHOOT_LOG_PATH above."""
    try:
        with open(SHOOT_LOG_PATH, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        content = ",".join(SHOOT_LOG_FIELDS) + "\n"
    return content, 200, {
        'Content-Type': 'text/csv',
        'Content-Disposition': 'attachment; filename="shoot_log.csv"',
    }


@app.route('/api/log/events.log', methods=['GET'])
def api_events_log():
    """O event log inteiro, do disco -- e nao as 50 entradas em memoria.

    E a diferenca que importa: as entradas em memoria desaparecem no reinicio
    que muitas vezes se faz POR CAUSA do problema que se esta a investigar. O
    que se descarrega daqui atravessa esses reinicios, e e isso que serve para
    reconstruir o que aconteceu depois de a filmagem acabar.
    """
    try:
        with open(EVENT_LOG_PATH, encoding="utf-8") as f:
            conteudo = f.read()
    except OSError:
        conteudo = ""
    if not conteudo:
        # Nao devolver vazio sem explicacao: um ficheiro de zero bytes parece
        # avaria e e so ausencia de historico.
        conteudo = "# no events recorded yet\n"
    return conteudo, 200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": 'attachment; filename="events.log"',
    }


# === Copia de seguranca da configuracao (copia.py) ===
@app.route('/api/backup/usb', methods=['GET', 'POST'])
def api_backup_usb():
    """GET: as copias que estao na pen USB. POST: guarda uma copia nova na pen."""
    try:
        if request.method == 'GET':
            return jsonify({"ok": True, "files": copia.listar_na_pen()})
        nome = copia.guardar_na_pen(copia.fazer())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    log_event(f"Configuration backup saved on the USB stick: {nome}")
    return jsonify({"ok": True, "file": nome})


@app.route('/api/backup/restore', methods=['POST'])
def api_restore():
    """{"usb_file": nome} (da pen) ou {"backup": {...}}: repoe a configuracao e reinicia o bridge e o servico USB.
    Recusado a gravar."""
    if anything_recording():
        return jsonify({"ok": False, "error": "recording: restore after the take"}), 409
    b = _body()
    if b.get("usb_file") is not None and not isinstance(b.get("usb_file"), str):
        return jsonify({"ok": False, "error": "usb_file: the file name (text)"}), 400
    if not b.get("usb_file") and not isinstance(b.get("backup"), dict):
        return jsonify({"ok": False, "error": "usb_file (a name) or backup (an object) needed"}), 400
    try:
        c = copia.ler_da_pen(b["usb_file"]) if b.get("usb_file") else b.get("backup")
        escritos = copia.repor(c)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    log_event("Configuration restored (%s): %s. Restarting the bridge" % (
        b.get("usb_file") or "uploaded", ", ".join(escritos)))

    def depois():
        time.sleep(1.5)
        if anything_recording():                          # um REC pode ter arrancado desde o pedido
            log_event("Restart after the restore cancelled: a recording started in the meantime "
                      "(the restored configuration applies on the next restart)")
            return
        _run(["systemctl", "restart", "bridge-usb"], timeout=30)
        _run(["systemctl", "restart", "camera-bridge"], timeout=30)
    threading.Thread(target=depois, daemon=True).start()
    return jsonify({"ok": True, "restored": escritos, "note": "the bridge restarts now"})

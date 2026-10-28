#!/bin/bash
# Instala o codigo do repositorio (atom/app) em $OPT, com copia de seguranca e reposicao (2026-10-13).
# O repositorio e a fonte unica do codigo: o instalador copia-o em vez de aplicar patches. A configuracao e os
# dados do operador (settings.json, shoot_log.csv, c3.json, ...) nao estao em atom/app e nunca se tocam.
#
#   instalar_app <pasta-da-app> <opt>   verifica, guarda uma copia do que corre agora (BK) e copia a app
#   reverter_app <opt>                  repoe o CODIGO da copia (BK) e reinicia os servicos; os dados do operador ficam
#   esperar_stop [texto]                espera pelo STOP se ha uma take a decorrer (NO_WAIT_REC=1 salta)
# Devolve 0 se instalou; nada alterado se a verificacao falhar.

PORT="${PORT:-8080}"

# O que o bridge e os servicos escrevem em runtime (dados e configuracao do operador, chaves, registos, copias .bak/.tmp):
# nunca se repoe a partir da copia, que e de antes de o operador os mudar. Lista a manter com o copia.py (FICHEIROS) e com
# o bridge-repor-codigo.sh. O nome e relativo a $opt.
e_dado() {
    case "$1" in
        *.csv|*.log) return 0 ;;                     # shoot_log.csv, events.log e os arquivos com data
        settings.json|devices.json|usb_map.json|sftp.json|sftp_known_hosts|c3.json) return 0 ;;
        bmpccs.json|iphones.json|gopros.json|sonys.json|termos.json|gw_fw.json|gw_fw_pendente.json|profiles.json) return 0 ;;
        pin.json|ap.conf|repo.path|version.json|take_aberto.json|wifi.json|gw_token) return 0 ;;
        escrito.json|*/escrito.json|*.bak|*.bak-*|*.tmp|*.falhou|__pycache__/*|*/__pycache__/*) return 0 ;;
    esac
    return 1
}

gravar_agora() {
    curl -fs -m 5 "http://localhost:$PORT/api/status" 2>/dev/null \
        | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("anything_recording") else 1)' 2>/dev/null
}

# Reiniciar o bridge a meio de uma take corta-a: espera-se pelo STOP (o bridge diz "anything_recording" no status; um
# bridge antigo que nao o diz conta como parado). NO_WAIT_REC=1 reinicia na mesma.
esperar_stop() {
    local oque="${1:-restarting the bridge}"
    if [ -z "${NO_WAIT_REC:-}" ] && gravar_agora; then
        echo "recording now: $oque after the STOP (waiting; NO_WAIT_REC=1 to skip)"
        while gravar_agora; do sleep 5; done
        echo "stopped: $oque"
    fi
    return 0
}

instalar_app() {
    local app="$1" opt="$2" f parcial mx m
    BK=""
    APP_SRC=""
    [ -f "$app/bridge.py" ] || { echo "atom/app has no bridge.py"; return 1; }
    # verificacao: todo o python da app tem de ser valido antes de se tocar em nada (ast: nao escreve ficheiros)
    for f in "$app"/*.py; do
        python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$f" 2>/dev/null \
            || { echo "ERROR: $f is not valid python: nothing changed"; return 2; }
    done
    # a copia do que corre agora, para repor se o novo nao arrancar (ficam as 3 ultimas). Faz-se para um nome que NAO e
    # .bak-* e so depois se renomeia: uma copia a meio (disco cheio, corte de energia) nunca pode ser escolhida como "a
    # ultima copia boa" (bridge-repor-codigo.sh).
    rm -rf "$opt".parcial-* 2>/dev/null || true
    if [ -d "$opt" ]; then
        parcial="$opt.parcial-$(date +%Y%m%d-%H%M%S)"
        if ! cp -a "$opt" "$parcial" || ! mv "$parcial" "${parcial/.parcial-/.bak-}"; then
            rm -rf "$parcial" "${parcial/.parcial-/.bak-}"
            echo "ERROR: could not save the copy of $opt (disk full?): nothing changed"
            return 3
        fi
        BK="${parcial/.parcial-/.bak-}"
    fi
    APP_SRC="$app"
    mkdir -p "$opt"
    # --remove-destination: apaga o ficheiro antigo antes de criar o novo. Sem isso o cp escreve POR CIMA e um programa que
    # esta a correr (o sony_ctl, que o bridge mantem a correr) da "Text file busy"; assim o processo antigo continua com a
    # sua versao ate o bridge reiniciar, e o ficheiro novo ja fica no sitio.
    cp -a --remove-destination "$app"/. "$opt"/ || { echo "ERROR: copying the app failed"; return 4; }
    # Poda: a copia nova ($BK) nunca sai; das outras ficam as 2 mais recentes pela data de modificacao (ls -dt). Pelo nome
    # (a data esta nele) um relogio atrasado fazia a copia nova ficar a mais velha e ser apagada. O cp -a guarda a data da
    # origem, por isso marca-se a copia nova com a hora de agora, e nunca antes da mais recente das outras (relogio atrasado):
    # fica sempre a mais recente tambem para o bridge-repor-codigo.sh, que repoe a primeira de ls -dt.
    if [ -n "$BK" ] && [ -d "$BK" ]; then
        touch "$BK" 2>/dev/null || true
        mx=0
        for f in "$opt".bak-*; do
            [ -d "$f" ] || continue
            [ "$f" = "$BK" ] && continue
            m=$(stat -c %Y "$f" 2>/dev/null || echo 0)
            [ "$m" -gt "$mx" ] && mx="$m"
        done
        [ "$(stat -c %Y "$BK" 2>/dev/null || echo 0)" -gt "$mx" ] || touch -d "@$((mx + 1))" "$BK" 2>/dev/null || true
    fi
    ls -dt "$opt".bak-* 2>/dev/null | grep -vxF -- "$BK" | tail -n +3 | xargs -r rm -rf
    return 0
}

reverter_app() {
    # Repoe os ficheiros de CODIGO da copia (os da app, o que o instalador copiou) e tira os que o codigo novo acrescentou;
    # os dados do operador escritos desde a copia (shoot log, events, settings, usb/sftp/c3...) ficam como estao.
    # Antes espera pelo STOP, como o passo 7: nao se corta uma take para repor codigo. Reinicia os servicos.
    local opt="$1" rel d falhas=0
    [ -n "${BK:-}" ] && [ -d "$BK" ] || { echo "no copy to restore"; return 1; }
    [ -n "${APP_SRC:-}" ] && [ -d "$APP_SRC" ] || { echo "no app folder to compare with: cannot restore"; return 1; }
    esperar_stop "restoring the previous code"
    systemctl stop camera-bridge bridge-usb 2>/dev/null || true
    while IFS= read -r -d '' rel; do
        rel="${rel#./}"
        e_dado "$rel" && continue
        if [ -e "$BK/$rel" ]; then
            d="$opt/$(dirname "$rel")"
            mkdir -p "$d" && cp -a "$BK/$rel" "$opt/$rel.rev" && mv -f "$opt/$rel.rev" "$opt/$rel" \
                || { echo "ERROR: could not restore $rel"; rm -f "$opt/$rel.rev"; falhas=1; }
        else
            rm -f "$opt/$rel"                 # acrescentado pelo codigo novo
        fi
    done < <(cd "$APP_SRC" && find . -type f -print0)
    systemctl restart camera-bridge
    for u in bridge-usb bridge-brilho; do       # so os que estao ligados (a consola pode te-los desligado)
        systemctl is-enabled --quiet "$u" 2>/dev/null && systemctl restart "$u" 2>/dev/null
    done
    [ "$falhas" = 0 ]
}

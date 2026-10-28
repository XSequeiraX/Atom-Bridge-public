#!/bin/bash
# Antes de o bridge arrancar (ExecStartPre do camera-bridge): se o codigo em /opt/camera-bridge estiver estragado
# (falta o bridge.py ou o nucleo.py, ou algum .py nao compila: um corte de energia a meio de uma actualizacao), repoe a copia mais recente que
# compila (/opt/camera-bridge.bak-*; a mais recente pela data de modificacao, que o instalador garante crescente mesmo
# com o relogio atrasado). Sai sempre com 0: nunca impede o arranque.
# Os dados do operador que estavam no codigo estragado (settings, shoot log...) voltam por cima da copia: a copia e de
# antes de o operador os mudar. NAO voltam o version.json nem o repo.path: dizem que codigo corre, e o que corre e o da copia.
# (Uma copia a meio chama-se .parcial-*, nunca .bak-*, por isso nao e escolhida aqui.)
# A copia faz-se para um nome temporario ($OPT.novo) e so se troca (mv, atomico) depois de copiada inteira e de o bridge.py
# compilar: um disco cheio a meio nunca deixa um $OPT meio reposto nem apaga o $OPT.falhou (o codigo estragado) antes do tempo.
OPT=/opt/camera-bridge
# Bom = tem o bridge.py, todo o python compila (o bridge esta partido em nucleo.py e rotas_*.py) e, se o bridge.py arranca
# o nucleo, o nucleo.py existe. Uma copia antiga (so com o bridge.py) continua a contar como boa.
ok() {
    [ -f "$1/bridge.py" ] || return 1
    if grep -q '^import nucleo' "$1/bridge.py" && [ ! -f "$1/nucleo.py" ]; then return 1; fi
    python3 -c 'import ast,sys
for f in sys.argv[1:]: ast.parse(open(f, encoding="utf-8").read())' "$1"/*.py 2>/dev/null
}
# um corte entre os dois mv da troca deixou o estragado em $OPT.falhou-novo e nada em $OPT: volta para o seu sitio
[ ! -e "$OPT" ] && [ -d "$OPT.falhou-novo" ] && mv "$OPT.falhou-novo" "$OPT"
ok "$OPT" && exit 0
for bk in $(ls -dt "$OPT".bak-* 2>/dev/null); do
    if ok "$bk"; then
        echo "bridge-repor-codigo: $OPT is damaged, restoring $bk"
        rm -rf "$OPT.novo"
        if ! cp -a "$bk" "$OPT.novo" || ! ok "$OPT.novo"; then
            rm -rf "$OPT.novo"
            echo "bridge-repor-codigo: could not copy $bk (disk full?): $OPT left as it was"
            exit 0
        fi
        if [ -d "$OPT" ]; then
            for f in "$OPT"/*.json "$OPT"/*.csv "$OPT"/*.log "$OPT"/ap.conf \
                     "$OPT"/sftp_known_hosts "$OPT"/fw/s3/escrito.json; do
                [ -s "$f" ] || continue          # so ficheiros com conteudo
                case "${f#$OPT/}" in version.json|repo.path) continue ;; esac     # o que corre e o codigo da copia
                mkdir -p "$(dirname "$OPT.novo/${f#$OPT/}")" 2>/dev/null
                cp -a "$f" "$OPT.novo/${f#$OPT/}" 2>/dev/null || true
            done
            rm -rf "$OPT.falhou-novo"
            if ! mv "$OPT" "$OPT.falhou-novo"; then
                rm -rf "$OPT.novo"
                echo "bridge-repor-codigo: could not set $OPT aside: left as it was"
                exit 0
            fi
        fi
        if mv "$OPT.novo" "$OPT"; then
            rm -rf "$OPT.falhou"                 # so agora, com a troca feita, vai o estragado de outra vez
            [ -d "$OPT.falhou-novo" ] && mv "$OPT.falhou-novo" "$OPT.falhou"
        else
            [ -d "$OPT.falhou-novo" ] && mv "$OPT.falhou-novo" "$OPT"
            rm -rf "$OPT.novo"
            echo "bridge-repor-codigo: swap failed: $OPT left as it was"
        fi
        exit 0
    fi
done
echo "bridge-repor-codigo: $OPT is damaged and there is no good copy"
exit 0

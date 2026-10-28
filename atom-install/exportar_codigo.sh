#!/bin/bash
# Junta o codigo VIVO do Atom e manda-o a um PC da tailnet por Taildrop (sem passwords), para completar o repositorio.
#   bash exportar_codigo.sh [pc-na-tailnet] [pasta-do-repo]      (corre como root: o instalador chama-o)
# So vai codigo e paginas, por extensao. A configuracao, os registos, os dados do operador e as chaves ficam de fora
# (json, csv, conf, log, bak, e tudo o que o instalador nao escreve): do /opt so se levam .py .html .js .css e
# imagens/fontes. O inventario.txt traz o resto em texto (nomes e tamanhos, nunca conteudos).
set -u
OPT="${OPT:-/opt/camera-bridge}"
PC="${1:-movie}"
REPO="${2:-$(cd "$(dirname "$0")/.." && pwd)}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/opt-camera-bridge" "$T/fora"

cd "$OPT" || exit 1
levar() {   # os ficheiros de codigo e paginas (por extensao); nunca copias de seguranca, caches nem ambientes virtuais
    find . -type f \( -name '*.py' -o -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.png' -o -name '*.svg' \
                      -o -name '*.ico' -o -name '*.ttf' -o -name '*.woff' -o -name '*.woff2' -o -name 'manifest.json' \) \
         ! -name '*.bak*' ! -name '*.antes_*' ! -path '*/__pycache__/*' ! -path './venv/*' ! -path './.venv/*' "$@"
}
levar -print0 | tar --null -T - -cf - | tar -xf - -C "$T/opt-camera-bridge"
levar | sort > "$T/levados.lst"

# Fora do /opt: o que muda o comportamento e o instalador nao deriva do codigo (drop-ins do systemd, regras, ligacoes)
for f in /etc/systemd/system/camera-bridge.service /etc/systemd/system/camera-bridge.service.d/*.conf \
         /etc/systemd/system/btusb-*.service /etc/systemd/system/bridge-*.service \
         /etc/udev/rules.d/*bridge* /etc/udev/rules.d/*camera* /etc/systemd/network/10-bridge-*.link \
         /etc/NetworkManager/conf.d/*bridge* /etc/sudoers.d/*bridge* /usr/local/bin/bridge-*; do
    [ -f "$f" ] && { mkdir -p "$T/fora$(dirname "$f")"; cp "$f" "$T/fora$f"; }
done

{
    echo "# inventario do Atom em $(date '+%F %T')  (so nomes e tamanhos, nunca conteudos)"
    echo; echo "== /opt/camera-bridge (nivel 1)"
    ls -la --time-style=+%F\ %R "$OPT" | awk '$1 != "total" {printf "%-10s %9s %s %s %s\n", $1, $5, $6, $7, $8}'
    echo; echo "== o que NAO foi levado (configuracao e dados do operador)"
    for f in $(find . -maxdepth 1 -type f | sort); do
        grep -qxF "$f" "$T/levados.lst" || printf '%s  %s\n' "${f#./}" "$(stat -c %s "$f")"
    done
    echo; echo "== codigo levado: soma md5 (8) | no repo (atom/app/) se houver"
    for f in $(cd "$T/opt-camera-bridge" && find . -maxdepth 1 -type f | sort); do
        a=$(md5sum "$OPT/$f" | cut -c1-8); b=$(md5sum "$REPO/atom/app/$(basename "$f")" 2>/dev/null | cut -c1-8)
        printf "%-26s atom=%s repo=%s %s\n" "$f" "$a" "${b:--}" "$([ -z "$b" ] && echo '(so no Atom)' || { [ "$a" = "$b" ] && echo igual || echo DIFERENTE; })"
    done
    echo; echo "== linhas de codigo"; wc -l "$T"/opt-camera-bridge/*.py 2>/dev/null | tail -20
    echo; echo "== pacotes python3 e outros instalados a mao"
    dpkg -l 2>/dev/null | awk '/^ii/ && ($2 ~ /^python3-/ || $2 ~ /espeak|bluez|alsa|chrony|ntp|surf|matchbox|dialog|nmcli|network-manager/) {print $2, $3}'
    pip list 2>/dev/null | head -40
    echo; echo "== servicos do rig"
    systemctl list-unit-files 2>/dev/null | grep -iE 'bridge|camera|btusb|kiosk|usb'
    echo; echo "== fora do /opt (copiado em fora/)"; (cd "$T/fora" && find . -type f | sort)
} > "$T/inventario.txt" 2>&1

ARQ="/tmp/atom-codigo-$(date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$ARQ" -C "$T" .
echo "export: $(du -h "$ARQ" | cut -f1)  $ARQ  ($(tar -tzf "$ARQ" | wc -l) entradas)"
if [ "${ENVIAR:-1}" = 1 ]; then
    if tailscale file cp "$ARQ" "$PC:" 2>&1; then
        echo "export: enviado a $PC por Taildrop"
    else
        echo "AVISO: o Taildrop para $PC falhou (a tailscale esta ligada? o PC esta na tailnet?). O ficheiro ficou em $ARQ"
        exit 1
    fi
fi

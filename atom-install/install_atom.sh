#!/bin/bash
# Instala ou reinstala o Camera Bridge no Toshiba NB500 (Atom, Debian 13).
#
# Idempotente: pode correr-se outra vez sem estragar nada -- cada passo ve se
# ja esta feito. Corre-se a partir da pasta atom-install copiada para o Atom:
#
#   sudo bash install_atom.sh                          # so aplica o que falta
#
# Variaveis opcionais:
#   ATOM_USER       o utilizador do Atom (por defeito quem chamou o sudo)
#   AP_SSID         nome do AP das cameras; se faltar, e pedido
#   AP_PSK          password do AP das cameras; se faltar e o AP nao existir,
#                   e pedida (tem de ser a que as cameras ja tem guardada)
#
# De fora, de proposito: a regra udev que desliga a Ethernet (cortava o SSH) e
# os scripts de Wi-Fi do Pi (ps-wifi-fallback, ps-portal, ps-ap-channel).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OPT=/opt/camera-bridge
ATOM_USER="${ATOM_USER:-${KIOSK_USER:-${SUDO_USER:-sequeira}}}"
PORT=8080
# Nomes fixos dos dois radios Wi-Fi (ate 15 caracteres; evitar wlanN, e nao
# usar o nome de um perfil do NetworkManager: a troca de nome no bridge.py e
# global e apanharia tambem o perfil).
UP_NAME="${UP_NAME:-ps-uplink}"
AP_NAME="${AP_NAME:-ps-control}"
# O codigo do bridge vive no repositorio (atom/app) e o instalador copia-o, com copia de seguranca e reposicao
# automatica se o bridge novo nao arrancar (lib_app.sh). (O modo antigo, a partir de um .tar.gz com patches por cima,
# saiu a 2026-10-14 com os patches e a pasta deployed/: levava ainda codigo do FIZ e do WR-03, que ja nao existem.)
APP="$HERE/../atom/app"
. "$HERE/lib_app.sh"

step() { echo; echo "=== $* ==="; }
die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run it with sudo"
id "$ATOM_USER" >/dev/null 2>&1 || die "user '$ATOM_USER' does not exist"
[ -f "$APP/bridge.py" ] || die "no bridge code in $APP (run the installer from the repository clone)"

# ----------------------------------------------------------------------------
step "1/7 Packages"
# Os pacotes so se revem quando esta seccao do instalador mudou (lista nova...) ou falta alguma peca: um
# Update from GitHub que so traz codigo nao passa pelo apt-get update. FORCE_PKGS=1 obriga a rever tudo.
PKG_STAMP=/var/lib/camera-bridge/pacotes.sig
PKG_SIG="$(sed -n '/^step "1\/7 Packages"/,/^step "2\/7/p' "$0" | sha256sum | cut -c1-16)"
pacotes_ok() {
    [ "$(cat "$PKG_STAMP" 2>/dev/null)" = "$PKG_SIG" ] || return 1
    python3 -c "import flask, dbus, bleak, serial" 2>/dev/null || return 1      # o esptool vive no /opt/gw-tools (abaixo)
    command -v mpremote >/dev/null 2>&1 || return 1
    [ -x /opt/gw-tools/bin/esptool.py ] || return 1
}
if [ -z "${FORCE_PKGS:-}" ] && pacotes_ok; then
    echo "packages: nothing changed since the last install (FORCE_PKGS=1 to check them again)"
else
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    python3-flask python3-dbus python3-requests python3-paramiko python3-cairosvg \
    python3-pip bluez alsa-utils iw curl network-manager nftables dialog
# Gateways por USB, sempre a partir do Atom (configuracao inicial e actualizacao): esptool (grava o ESP32), mpremote (copia os
# ficheiros do C3) e pyserial (configura o Wi-Fi do S3). Opcionais: sem eles o bridge corre na mesma.
apt-get install -y -q python3-serial >/dev/null 2>&1 || pip install pyserial --break-system-packages >/dev/null 2>&1 \
    || echo "WARNING: pyserial not installed (the S3 Wi-Fi cannot be set by USB)"
command -v mpremote >/dev/null 2>&1 || apt-get install -y -q mpremote >/dev/null 2>&1 \
    || pip install mpremote --break-system-packages >/dev/null 2>&1 || echo "WARNING: mpremote not installed (the C3 cannot be written by USB from the Atom)"
# esptool e mpremote oficiais num ambiente proprio (/opt/gw-tools): o esptool do Debian vem sem os "stubs" (o programa que
# o esptool envia ao chip para ler e gravar a flash) e falha. O gw_usb.py usa estes primeiro.
apt-get install -y -q --no-install-recommends python3-venv >/dev/null 2>&1 || true
if [ ! -x /opt/gw-tools/bin/esptool.py ] || [ ! -x /opt/gw-tools/bin/mpremote ]; then
    { python3 -m venv /opt/gw-tools && /opt/gw-tools/bin/pip install -q "esptool==4.8.1" mpremote pyserial; } >/dev/null 2>&1 \
        && echo "esptool and mpremote in /opt/gw-tools" \
        || echo "WARNING: official esptool/mpremote not installed in /opt/gw-tools (the gateways may not be writable by USB)"
fi
if ! python3 -c "import bleak" 2>/dev/null; then
    apt-get install -y -q python3-bleak || pip install bleak --break-system-packages
fi
apt-get install -y -q --no-install-recommends python3-zeroconf >/dev/null \
    || true
# Firmware do dongle do AP (AR9271): sem o 1.4.0 o driver cai para o 1.3 e a interface nao sobe. "manual" para o autoremove nao o tirar.
apt-get install -y -q firmware-ath9k-htc >/dev/null 2>&1 \
    || echo "WARNING: firmware-ath9k-htc not installed (the AP dongle needs it: apt install firmware-ath9k-htc)"
apt-mark manual firmware-ath9k-htc >/dev/null 2>&1 || true
mkdir -p "$(dirname "$PKG_STAMP")" && echo "$PKG_SIG" > "$PKG_STAMP"
fi
# O kernel nao sobe sozinho (o 6.12.113 deixou o dongle do AP sem subir a interface): os metapacotes ficam com prioridade -1 no apt.
# Os pacotes do sistema actualizam-se so pelo terminal. Para actualizar o kernel de proposito:
#   sudo rm /etc/apt/preferences.d/bridge-kernel && sudo apt full-upgrade     (e depois volta a criar o ficheiro, ou apagar o marcador abaixo)
# Poe-se uma vez; depois e a escolha do operador.
KPIN=/etc/apt/preferences.d/bridge-kernel
KMARK=/var/lib/camera-bridge/kernel-pin
if [ ! -e "$KMARK" ]; then
    printf 'Package: linux-image-amd64 linux-headers-amd64\nPin: release *\nPin-Priority: -1\n' > "$KPIN" \
        && mkdir -p "$(dirname "$KMARK")" && : > "$KMARK" && echo "kernel upgrades held ($KPIN; remove it to upgrade the kernel)"
fi
# O kernel em uso fica "manual": um autoremove nunca o apaga (o 6.12.111 ja nao esta nos repositorios do Debian).
apt-mark manual "linux-image-$(uname -r)" >/dev/null 2>&1 || true

# A voz nos headphones Bluetooth saiu (2026-10-14: o audio A2DP atrasava os REC dos iPhones). Tira-se o que ficou dela:
# a BlueALSA, o Piper, a configuracao e as frases guardadas. O BlueZ fica (plano B da BMPCC).
# So fala quando ha mesmo algo para tirar: o "dpkg -s" tambem da sucesso a um pacote ja removido que deixou a configuracao
# (config-files), e o "systemctl is-enabled" a unidades "static"/"alias"; por isso as duas coisas se leem pelo estado exacto.
for u in bluealsa.service bluealsad.service; do
    [ "$(systemctl is-enabled "$u" 2>/dev/null)" = enabled ] && systemctl disable --now "$u" >/dev/null 2>&1 && echo "$u disabled" || true
done
for pk in bluez-alsa-utils libasound2-plugin-bluez; do
    case "$(dpkg-query -W -f='${db:Status-Status}' "$pk" 2>/dev/null || true)" in
        installed)    apt-get purge -y -q "$pk" >/dev/null 2>&1 && echo "$pk removed" || true ;;
        config-files) apt-get purge -y -q "$pk" >/dev/null 2>&1 || true ;;      # so a configuracao que ficou: sem barulho
    esac
done
if [ -d /opt/piper ]; then rm -rf /opt/piper && echo "Piper removed"; fi
rm -rf "$OPT/voz.json" "$OPT/voz.json.bak" "$OPT/voz.py" "$OPT/voz_cache"

# ----------------------------------------------------------------------------
step "2/7 Bridge code in $OPT"
mkdir -p "$OPT"
# O trap fica ANTES da copia: um cp -a que falha a meio (disco cheio) deixava uma arvore meio escrita sem reposicao. Daqui ate
# ao arranque (passo 7), qualquer erro repoe o codigo anterior (so o codigo: os dados do operador escritos entretanto
# ficam; espera pelo STOP se se grava): sem isto o codigo novo, que nunca arrancou, ficava em /opt e so rebentava no
# proximo arranque do Atom. Se a propria copia de seguranca falhar (rc 3) nao ha BK e nada foi alterado.
trap 'rc=$?; if [ "$rc" -ne 0 ] && [ "${CONCLUIDO:-0}" != 1 ] && [ -n "${BK:-}" ]; then
          echo "ERROR during the install: restoring the previous code ($BK)"; reverter_app "$OPT" || true; fi' EXIT
instalar_app "$APP" "$OPT" || die "could not install the code from the repository (see above)"
echo "code from the repository installed; copy of the previous one: ${BK:-none}"
# Onde esta o clone: e dai que a actualizacao pela pagina/consola puxa o codigo (bridge.py le o repo.path).
(cd "$HERE/.." && pwd) > "$OPT/repo.path"
# Firmware do gateway C3 para o OTA (fw_ota.py): o bridge envia-o ao C3 pelo Wi-Fi quando se pede.
# O programa vai ja compilado (app.mpy, netcfg.mpy: o GitHub compila-os); compilar no C3 deixava-o sem memoria.
if [ -f "$HERE/../c3_fiz_bm/app.mpy" ]; then
    mkdir -p "$OPT/fw/c3"
    rm -f "$OPT/fw/c3/"{netcfg,app}.py
    cp "$HERE/../c3_fiz_bm/"{boot,main}.py "$HERE/../c3_fiz_bm/"{app,netcfg}.mpy "$OPT/fw/c3/" 2>/dev/null \
        && echo "C3 firmware in $OPT/fw/c3"
fi
# Sony (Camera Remote SDK): o sony_ctl vem ja compilado no repositorio, com as bibliotecas da Sony ao lado (a licenca do
# SDK deixa distribui-las so como parte da aplicacao: atom/app/sony/AVISO-SONY.md); a copia da app pô-lo em $OPT/sony.
# Para o recompilar aqui (outra versao do SDK, descarregado da Sony): SONY_SDK=<pasta ou .zip do SDK> FORCE_SONY=1.
if [ "${FORCE_SONY:-0}" = 1 ] && [ -n "${SONY_SDK:-}" ]; then
    command -v g++ >/dev/null 2>&1 || apt-get install -y -q --no-install-recommends g++ >/dev/null 2>&1 || true
    bash "$APP/sony/compilar.sh" "$SONY_SDK" "$OPT/sony" || echo "WARNING: the Sony helper did not build (the shipped one stays)"
fi
# As Sony por USB: o SDK pede mais memoria para o USB do que os 16 MB do kernel. Escreve-se em cada arranque com o
# systemd-tmpfiles (o usbcore costuma vir dentro do kernel do Debian: as "options" do modprobe.d nao chegam la).
if [ ! -f /etc/tmpfiles.d/sony-usbfs.conf ]; then
    echo "w /sys/module/usbcore/parameters/usbfs_memory_mb - - - - 150" > /etc/tmpfiles.d/sony-usbfs.conf
    rm -f /etc/modprobe.d/sony-usbfs.conf             # a versao anterior do instalador
    systemd-tmpfiles --create /etc/tmpfiles.d/sony-usbfs.conf 2>/dev/null || true
fi
# O MicroPython do C3 (para gravar um C3 em branco por USB a partir do Atom): descarrega-se uma vez, se houver Internet.
python3 "$OPT/gw_usb.py" --fetch-mp 2>&1 | tail -1 || true

# ----------------------------------------------------------------------------
step "3/7 Wi-Fi interfaces (fixed names: $UP_NAME and $AP_NAME)"
# O AP das cameras e o dongle AR9271 (driver ath9k_htc); o uplink e o outro
# radio Wi-Fi da maquina. Os nomes que o sistema da sozinho (wlp7s0,
# wlx90f65217bfd1) vem do barramento e do MAC; ficam nomes fixos com ficheiros
# .link do systemd: o uplink pelo caminho PCI do radio interno (nao muda), o AP
# pelo driver do dongle (vale em qualquer porta USB). Nao se usa wlanN: e o
# nome que o kernel da sozinho e pode chocar no arranque.
AP_IF="$(for d in /sys/class/net/*; do
    [ "$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)")" = ath9k_htc ] && basename "$d"
done | head -1 || true)"
UP_IF="$(for d in /sys/class/net/*; do
    n="$(basename "$d")"; [ -d "$d/wireless" ] && [ "$n" != "$AP_IF" ] && echo "$n"
done | head -1 || true)"
echo "now: cameras AP ${AP_IF:-(dongle not found)}, uplink ${UP_IF:-(none)}"

NEED_REBOOT=0
write_link() {   # ficheiro, linha do [Match], nome
    local new
    new="$(printf '[Match]\n%s\nType=wlan\n\n[Link]\nName=%s' "$2" "$3")"
    if [ "$(cat "$1" 2>/dev/null)" != "$new" ]; then
        printf '%s\n' "$new" > "$1"
        echo "written $1 ($3)"
    fi
}
mkdir -p /etc/systemd/network
if [ -n "$AP_IF" ]; then
    write_link /etc/systemd/network/10-bridge-camap.link "Driver=ath9k_htc" "$AP_NAME"
    [ "$AP_IF" = "$AP_NAME" ] || NEED_REBOOT=1
else
    AP_NAME=""
fi
if [ -n "$UP_IF" ]; then
    UP_PATH="$(udevadm info -q property -p "/sys/class/net/$UP_IF" 2>/dev/null | sed -n 's/^ID_PATH=//p' | head -1 || true)"
    if [ -n "$UP_PATH" ]; then
        write_link /etc/systemd/network/10-bridge-uplink.link "Path=$UP_PATH" "$UP_NAME"
        [ "$UP_IF" = "$UP_NAME" ] || NEED_REBOOT=1
    else
        echo "WARNING: no device path for $UP_IF; the uplink keeps that name"
        UP_NAME="$UP_IF"
    fi
else
    UP_NAME=""
fi

# O bridge le os nomes das interfaces do ap.conf (AP_IF, UP_IF; escrito no passo 5). Antes um sed trocava-os em todo o
# bridge.py, e apanhava tambem palavras iguais nos comentarios.

# Perfis do NetworkManager presos ao nome antigo (o perfil do AP esta preso ao
# dongle) passam para o nome novo. So os presos: os outros nao se tocam.
for u in $(nmcli -g UUID connection show 2>/dev/null); do
    ifn="$(nmcli -g connection.interface-name connection show "$u" 2>/dev/null || true)"
    [ -n "$ifn" ] || continue
    to=""
    if [ -n "$AP_IF" ] && [ "$ifn" = "$AP_IF" ] && [ "$AP_IF" != "$AP_NAME" ]; then to="$AP_NAME"; fi
    if [ -n "$UP_IF" ] && [ "$ifn" = "$UP_IF" ] && [ "$UP_IF" != "$UP_NAME" ]; then to="$UP_NAME"; fi
    if [ -n "$to" ]; then
        if nmcli connection modify "$u" connection.interface-name "$to"; then
            echo "NetworkManager: $(nmcli -g connection.id connection show "$u") -> $to"
        else
            echo "WARNING: could not change the profile $u to $to"
        fi
    fi
done

# ----------------------------------------------------------------------------
step "4/7 camera-bridge service"
cp "$HERE/system/camera-bridge.service" /etc/systemd/system/camera-bridge.service
# A calibracao do LTC desta placa (LTC_LATENCY): estava so no Atom, uma instalacao nova perdia-a.
mkdir -p /etc/systemd/system/camera-bridge.service.d
cp "$HERE/system/ltc-latency.conf" /etc/systemd/system/camera-bridge.service.d/ltc-latency.conf
# Servicos a parte, que falam com o bridge pela API: USB (controladores class compliant).
# O codigo (usb_bridge.py, bridge_client.py) vem em atom/app. A configuracao (usb_map.json) e do operador: nunca por cima.
# O servico USB pode ter sido desligado na consola (GLOBAL SETTINGS > Services): so se liga na primeira
# instalacao (quando a unidade ainda nao existia); depois fica a escolha do operador.
NOVOS_SERV=""
[ -f /etc/systemd/system/bridge-usb.service ] || NOVOS_SERV="bridge-usb"
BRILHO_NOVO=0; [ -f /etc/systemd/system/bridge-brilho.service ] || BRILHO_NOVO=1
cp "$HERE/system/bridge-usb.service" "$HERE/system/bridge-brilho.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable camera-bridge $NOVOS_SERV >/dev/null
# Brilho do ecra (Fn+F6/F7): so se liga na primeira instalacao; depois fica a escolha do operador.
[ "$BRILHO_NOVO" = 1 ] && systemctl enable bridge-brilho >/dev/null 2>&1 || true
# Tampa do portatil: o logind ignora-a (senao suspende e o Atom perde as camaras a meio de uma take). Quem a trata a seguir
# (bloquear a consola, apagar o ecra) e o bridge-brilho. O logind relê a configuracao com um HUP (nao se reinicia: nao mexe nas sessoes).
mkdir -p /etc/systemd/logind.conf.d
cp "$HERE/system/bridge-lid.conf" /etc/systemd/logind.conf.d/bridge-lid.conf
systemctl kill -s HUP systemd-logind >/dev/null 2>&1 || true

# ----------------------------------------------------------------------------
step "5/7 Cameras AP"
# O nome do AP escolhe-se aqui (ou por AP_SSID=... sudo -E bash install_atom.sh), e nao fica no codigo.
# Fica em /opt/camera-bridge/ap.conf, que o bridge le.
AP_CON="${AP_SSID:-}"
# O perfil ja existe? O nome compara-se como texto (-F: um "Rig (5G)" ou "A*B" nao e uma expressao regular), linha inteira
# (-x), e o nmcli em modo terse com "-e no" da um nome por linha SEM o escape de ':' e de barra invertida do -t sozinho.
# O "--" porque um nome pode comecar por "-".
tem_perfil_ap() { nmcli -t -e no -f NAME connection show 2>/dev/null | grep -Fxq -- "$AP_CON"; }
# Numa atualizacao reaproveita-se o nome ja escolhido (ap.conf), sem perguntar outra vez.
if [ -z "$AP_CON" ] && [ -f "$OPT/ap.conf" ]; then
    AP_CON="$(sed -n 's/^AP_CON=//p' "$OPT/ap.conf" | head -n1)"
fi
if [ -z "$AP_CON" ]; then
    # Sem terminal (a actualizacao pela pagina/consola) nao se pergunta: sai antes de mexer em nada daqui
    [ -t 0 ] || die "the AP name is missing (ap.conf): run the installer once in a terminal, or AP_SSID=..."
    read -rp "Name (SSID) of the cameras AP, the one the cameras already have saved: " AP_CON
fi
[ -n "$AP_CON" ] && [ "${#AP_CON}" -le 32 ] || die "the AP name must be 1 to 32 characters"
mkdir -p "$OPT"
{
    echo "AP_CON=$AP_CON"
    [ -n "$AP_NAME" ] && echo "AP_IF=$AP_NAME"
    [ -n "$UP_NAME" ] && echo "UP_IF=$UP_NAME"
    true
} > "$OPT/ap.conf"
chmod 644 "$OPT/ap.conf"
if tem_perfil_ap; then
    echo "profile $AP_CON already exists -- left as it is"
elif [ -z "$AP_IF" ]; then
    echo "WARNING: no AR9271 dongle plugged in, the profile is not created (run the script again with it plugged in)"
else
    PSK="${AP_PSK:-}"
    if [ -z "$PSK" ]; then
        [ -t 0 ] || die "the password of the AP $AP_CON is missing: run the installer once in a terminal"
        read -rsp "Password of the AP $AP_CON (the one the cameras have saved): " PSK; echo
    fi
    [ "${#PSK}" -ge 8 ] || die "the WPA password must have at least 8 characters"
    # Preso ao nome fixo do dongle; se ele so o ganhar depois de reiniciar, o
    # AP sobe sozinho nesse arranque (autoconnect).
    # O perfil escreve-se como ficheiro do NetworkManager (0600, so o root le) e nao com "nmcli ... wifi-sec.psk":
    # assim a password nunca aparece na linha de comando (ps, /proc/*/cmdline).
    NMDIR=/etc/NetworkManager/system-connections
    NMFILE="$NMDIR/$(printf '%s' "$AP_CON" | tr -c 'A-Za-z0-9._-' '_').nmconnection"
    esc() { printf '%s' "$1" | sed 's/\\/\\\\/g'; }   # o formato do NetworkManager: \ -> \\
    (
        umask 077
        TMPF="$(mktemp "$NMDIR/.ap.XXXXXX")"
        {
            printf '[connection]\nid=%s\nuuid=%s\ntype=wifi\ninterface-name=%s\nautoconnect=true\n\n' \
                "$(esc "$AP_CON")" "$(cat /proc/sys/kernel/random/uuid)" "$AP_NAME"
            printf '[wifi]\nmode=ap\nssid=%s\nband=bg\nchannel=11\n\n' "$(esc "$AP_CON")"
            printf '[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n' "$(esc "$PSK")"
            printf '[ipv4]\nmethod=shared\naddress1=192.168.4.1/24\n\n[ipv6]\nmethod=auto\n'
        } > "$TMPF"
        chown root:root "$TMPF"
        chmod 600 "$TMPF"
        mv -f "$TMPF" "$NMFILE"
    )
    unset PSK
    nmcli connection reload >/dev/null
    tem_perfil_ap || die "NetworkManager did not load the AP profile ($NMFILE)"
    if [ -e "/sys/class/net/$AP_NAME" ]; then
        nmcli connection up "$AP_CON" ifname "$AP_NAME" >/dev/null \
            || echo "WARNING: the AP did not come up now; try 'nmcli connection up $AP_CON'"
    else
        echo "the AP comes up after a restart, once the dongle is named $AP_NAME"
    fi
    echo "profile $AP_CON created on $AP_NAME (channel 11, 192.168.4.1/24)"
fi
# O AP das cameras nao partilha a internet do uplink: cada telemovel/gateway a falar com a internet por aqui gastava
# tempo de antena num radio de 2.4 GHz que serve as cameras. O NetworkManager continua em "shared" (e dele o DHCP e
# as reservas das Canon); a regra apenas deita fora o que viria do AP para outra interface. Para voltar a partilhar:
# sudo systemctl disable --now bridge-ap-nonat
AP_NONAT_NOVO=0; [ -f /etc/systemd/system/bridge-ap-nonat.service ] || AP_NONAT_NOVO=1
install -m 755 "$HERE/system/bridge-ap-nonat.sh" /usr/local/bin/bridge-ap-nonat.sh
cp "$HERE/system/bridge-ap-nonat.service" /etc/systemd/system/bridge-ap-nonat.service
systemctl daemon-reload
# So se liga na primeira instalacao; depois fica a escolha do operador (disable --now para partilhar outra vez).
if [ "$AP_NONAT_NOVO" = 1 ]; then
    systemctl enable --now bridge-ap-nonat >/dev/null 2>&1 \
        || echo "WARNING: could not load the no-sharing rule (see 'journalctl -u bridge-ap-nonat')"
elif systemctl is-enabled --quiet bridge-ap-nonat; then
    systemctl restart bridge-ap-nonat || true
fi
# ----------------------------------------------------------------------------
step "6/7 Sound and boot menu"
# O Atom nao precisa de servidor de som (a claquete fica so no telemovel), e o
# arecord do LTC precisa do ADC em exclusivo. O PipeWire do Debian segurava o
# cartao e bloqueava o arranque.
systemctl --global mask pipewire.service pipewire.socket pipewire-pulse.service \
    pipewire-pulse.socket wireplumber.service filter-chain.service >/dev/null 2>&1 || true
pkill -u "$ATOM_USER" -x pipewire 2>/dev/null || true
pkill -u "$ATOM_USER" -x pipewire-pulse 2>/dev/null || true
pkill -u "$ATOM_USER" -x wireplumber 2>/dev/null || true

# O quiosque (Xorg + surf no ecra do Atom) saiu a 2026-10-15: era o que mais pesava no Atom, e a consola faz o mesmo.
# Num Atom que o tinha, tira-se tudo: o servico, os scripts, a configuracao do X e os pacotes (o WebKit e o X sao
# centenas de MB). O ecra fica com o menu de arranque e a consola.
if [ -e /etc/systemd/system/bridge-kiosk.service ] || [ -e /usr/local/bin/bridge-kiosk.sh ]; then
    systemctl disable --now bridge-kiosk >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/bridge-kiosk.service /usr/local/bin/bridge-kiosk.sh \
          /usr/local/bin/bridge-kiosk-session.sh /etc/default/bridge-kiosk /etc/X11/xorg.conf.d/10-sem-aceleracao.conf
    echo "kiosk removed"
fi
KIOSK_PKGS=""
for pk in surf matchbox-window-manager xinit xserver-xorg-core xserver-xorg-input-libinput x11-xserver-utils; do
    # So os instalados: o "dpkg -s" tambem devolve 0 a um pacote ja removido mas com a configuracao no disco (estado "rc"), e
    # a mensagem "kiosk packages removed" voltava em todas as instalacoes.
    dpkg-query -W -f='${Status}' "$pk" 2>/dev/null | grep -q "^install ok installed" && KIOSK_PKGS="$KIOSK_PKGS $pk"
done
if [ -n "$KIOSK_PKGS" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -q $KIOSK_PKGS >/dev/null 2>&1 \
        && DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -q >/dev/null 2>&1 \
        && echo "kiosk packages removed:$KIOSK_PKGS" \
        || echo "WARNING: could not remove the kiosk packages ($KIOSK_PKGS)"
fi

# Menu no arranque (tty1): consola, Wi-Fi do uplink, NetworkManager ou linha de
# comandos. Sem resposta segue para a consola (bridge-ctl), para o rig continuar a
# arrancar sozinho no terreno.
install -m 755 "$HERE/system/bridge-boot-menu.sh" /usr/local/bin/bridge-boot-menu.sh
# A consola que o menu abre (/usr/local/bin/bridge-ctl): o bridge-ctl.py vem ao lado deste
# script ou na pasta de cima (a do projecto). Sem ela, o menu de arranque nao tem consola.
for c in "$HERE/bridge-ctl.py" "$HERE/../bridge-ctl.py"; do
    if [ -f "$c" ]; then install -m 755 "$c" /usr/local/bin/bridge-ctl; break; fi
done
[ -x /usr/local/bin/bridge-ctl ] || echo "WARNING: bridge-ctl.py (console) missing: copy it to /usr/local/bin/bridge-ctl"

# Mensagens do kernel (por exemplo do driver do Wi-Fi, "rtl8192se: switch case not processed") nao aparecem no ecra do Atom, por
# cima da consola: so as de emergencia. Continuam todas no dmesg e no journal (journalctl -k).
cat > /etc/sysctl.d/20-bridge-console.conf <<'SYSCTL'
# nivel de log da consola 1 (so emergencias); por omissao 4, minimo 1, nivel de arranque 7
kernel.printk = 1 4 1 7
SYSCTL
sysctl -q -p /etc/sysctl.d/20-bridge-console.conf >/dev/null 2>&1 || dmesg -n 1 2>/dev/null || true
echo "kernel messages hidden from the screen (journalctl -k shows them)"

cat > /etc/systemd/system/bridge-boot-menu.service <<'EOF'
[Unit]
Description=Camera Bridge: menu no arranque (consola, rede ou linha de comandos)
After=systemd-user-sessions.service getty@tty1.service
Conflicts=getty@tty1.service

[Service]
# simple e nao oneshot: a consola (bridge-ctl) corre dentro do menu e pode ficar
# aberta horas; um oneshot por acabar deixava o arranque "a meio" para o systemd.
Type=simple
ExecStart=/usr/local/bin/bridge-boot-menu.sh
StandardInput=tty
StandardOutput=tty
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
Environment=TERM=linux

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# Sem quiosque nao ha nada grafico: arranque em modo texto. O reenable passa a ligacao do menu do graphical.target
# (de antes) para o multi-user.target.
systemctl set-default multi-user.target >/dev/null
systemctl reenable bridge-boot-menu >/dev/null 2>&1

# ----------------------------------------------------------------------------
step "7/7 Start"
CONCLUIDO=1             # daqui em diante este passo trata da reposicao ele proprio (o bridge novo nao respondeu)
install -m 755 "$HERE/system/bridge-repor-codigo.sh" /usr/local/bin/bridge-repor-codigo.sh
systemctl daemon-reload
# Reiniciar o bridge a meio de uma take corta-a: a actualizacao so foi recusada se ja se gravava ao pedi-la, e o REC
# pode ter comecado durante o git pull ou o apt. Espera-se pelo STOP (esperar_stop, em lib_app.sh). NO_WAIT_REC=1
# reinicia na mesma.
esperar_stop "restarting the bridge"
systemctl restart camera-bridge
for u in bridge-usb bridge-brilho; do       # depois da consola nova (passo 6): leem os devices pelo modulo dela
    if systemctl is-enabled --quiet "$u"; then systemctl restart "$u"; else echo "$u is off (console: GLOBAL SETTINGS > Services)"; fi
done
printf "waiting for the bridge"
for i in $(seq 1 45); do
    curl -fs -o /dev/null "http://localhost:$PORT/" && break
    printf "."; sleep 2
done
echo
FALHOU=0
if curl -fs -o /dev/null "http://localhost:$PORT/"; then
    echo "bridge answering on :$PORT"
else
    echo "WARNING: the bridge is not answering yet; see 'journalctl -u camera-bridge -n 40'"
    if [ -n "${BK:-}" ]; then
        # O codigo novo nao arrancou e ninguem esta ao teclado: repoe-se o anterior para o rig nao ficar sem bridge.
        echo "RESTORING THE PREVIOUS CODE ($BK)"
        reverter_app "$OPT" || echo "ERROR: could not restore"
        for _ in $(seq 1 15); do curl -fs -o /dev/null "http://localhost:$PORT/" && break; sleep 2; done
        if curl -fs -o /dev/null "http://localhost:$PORT/"; then
            echo "previous bridge restored and answering"
        else
            echo "ERROR: the bridge is still not answering"
        fi
        FALHOU=1
    fi
fi
# O ecra passa pelo menu, como num arranque normal (a consola e a escolha por defeito ao fim de 10 s).
systemctl start --no-block bridge-boot-menu

# A versao instalada (o commit do repositorio), para a consola e a pagina a mostrarem: so quando o bridge novo
# arrancou. Se a instalacao falhou, fica a de antes (a copia reposta traz o version.json dela) e a consola ve que o
# clone do GitHub esta a frente do que corre. O git corre como root numa pasta de outro utilizador: safe.directory.
if [ "$FALHOU" = 0 ]; then
    REPO_DIR="$(cd "$HERE/.." && pwd)"
    if V_COMMIT="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)"; then
        V_DATA="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" log -1 --format=%cI 2>/dev/null || true)"
        V_ASSUNTO="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" log -1 --format=%s 2>/dev/null || true)"
        V_COMMIT="$V_COMMIT" V_DATA="$V_DATA" V_ASSUNTO="$V_ASSUNTO" python3 - "$OPT/version.json" <<'PYV'
import json, os, sys, time
d = {"commit": os.environ["V_COMMIT"], "date": os.environ["V_DATA"], "subject": os.environ["V_ASSUNTO"][:120],
     "installed": time.strftime("%Y-%m-%d %H:%M:%S")}
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f)
os.replace(tmp, sys.argv[1])
PYV
        echo "installed version: $V_COMMIT  $V_ASSUNTO"
    fi
fi

echo
echo "=== Summary ==="
printf "%-18s %s\n" camera-bridge "$(systemctl is-active camera-bridge)" \
                    bridge-boot-menu "$(systemctl is-enabled bridge-boot-menu) (the console starts from the menu)"
if [ "$NEED_REBOOT" = 1 ]; then
    echo
    echo "*** RESTART THE ATOM for the new adapter names ($UP_NAME, $AP_NAME)"
    echo "*** to take effect:  sudo reboot"
    echo "*** Until then the bridge network panel and the AP may not answer."
fi

# Uma tarefa pontual, pedida pelo repositorio: se existir atom-install/EXPORTAR (com o nome do PC la dentro),
# o codigo vivo vai para esse PC por Taildrop (exportar_codigo.sh). Sem o ficheiro, nao faz nada.
if [ -s "$HERE/EXPORTAR" ]; then
    echo
    echo "=== Exporting the code to $(head -n1 "$HERE/EXPORTAR")"
    bash "$HERE/exportar_codigo.sh" "$(head -n1 "$HERE/EXPORTAR")" "$HERE/.." || echo "WARNING: the export failed"
fi

[ "$FALHOU" = 0 ] || { echo; echo "*** THE NEW CODE DID NOT START: the previous one stays. See the log above."; exit 1; }

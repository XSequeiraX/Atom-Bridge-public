#!/bin/bash
# Menu de arranque no tty1: consola (bridge-ctl), Wi-Fi do uplink,
# NetworkManager ou linha de comandos. Sem resposta em 10 s (so na primeira vez)
# segue para a consola, para o rig continuar a arrancar sozinho no terreno. Ao
# sair da consola (Q) volta-se a este menu.
#
# O Wi-Fi so mexe no radio do uplink (o interno), nunca no dongle AR9271 do AP
# das cameras (driver ath9k_htc): ligar o uplink ali derrubava o AP das cameras.

TIMEOUT=10

# A bridge-ctl e curses: precisa de UTF-8 para os simbolos, e o servico no tty1
# arranca sem locale.
run_console() {
    stty sane 2>/dev/null; kbd_mode -u 2>/dev/null    # outro programa pode ter deixado o terminal/teclado estragado
    clear
    LANG=C.UTF-8 LC_ALL=C.UTF-8 /usr/local/bin/bridge-ctl
    clear
}

uplink_if() {
    local d drv
    for d in /sys/class/net/*; do
        [ -d "$d/wireless" ] || continue
        drv="$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)")"
        [ "$drv" = ath9k_htc ] && continue
        echo "${d##*/}"
        return
    done
}

status_text() {
    local up ssid ips
    up="$(uplink_if)"
    ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v ':' | grep -v '^$' | head -3 | tr '\n' ' ')"
    if [ -n "$up" ]; then
        ssid="$(nmcli -g GENERAL.CONNECTION device show "$up" 2>/dev/null)"
    fi
    printf '%s   %s\nUplink %s: %s' "$(hostname)" "${ips:-no IP}" "${up:-none}" "${ssid:-not connected}"
}

perfil_wifi() {   # ssid, password, interface, seguranca (do nmcli): escreve o perfil 0600 e recarrega
    local ssid="$1" psk="$2" ifn="$3" sec="$4" km=wpa-psk dir=/etc/NetworkManager/system-connections f tmp
    [ "${#psk}" -ge 8 ] || { echo "the Wi-Fi password must have at least 8 characters"; return 1; }
    # So WPA3 (sem WPA2 ao lado): SAE; o resto (WPA2, WPA2/WPA3 misto) vai por WPA-PSK
    case "$sec" in *WPA3*) case "$sec" in *WPA2*) ;; *) km=sae ;; esac ;; esac
    esc() { printf '%s' "$1" | sed 's/\\/\\\\/g'; }   # o formato do NetworkManager: \ -> \\
    f="$dir/$(printf '%s' "$ssid" | tr -c 'A-Za-z0-9._-' '_').nmconnection"
    (
        umask 077
        tmp="$(mktemp "$dir/.uplink.XXXXXX")" || exit 1
        {
            printf '[connection]\nid=%s\nuuid=%s\ntype=wifi\ninterface-name=%s\nautoconnect=true\n\n' \
                "$(esc "$ssid")" "$(cat /proc/sys/kernel/random/uuid)" "$ifn"
            printf '[wifi]\nmode=infrastructure\nssid=%s\n\n' "$(esc "$ssid")"
            printf '[wifi-security]\nkey-mgmt=%s\npsk=%s\n\n' "$km" "$(esc "$psk")"
            printf '[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n'
        } > "$tmp"
        chmod 600 "$tmp"
        mv -f "$tmp" "$f"
    ) || { echo "could not write the Wi-Fi profile"; return 1; }
    nmcli connection reload >/dev/null 2>&1
    nmcli -g NAME connection show | grep -qxF "$ssid" || { echo "NetworkManager did not load the profile"; return 1; }
}

wifi_pick() {
    local up line ssid sig sec freq band choice psk out i=0
    local -a items=() names=() secs=()
    local -A seen=()
    up="$(uplink_if)"
    if [ -z "$up" ]; then
        dialog --title " Wi-Fi " --msgbox "No uplink wifi adapter found." 7 50
        return
    fi
    dialog --infobox "Scanning for networks on $up..." 4 52
    # nmcli -t escapa os ':' dentro dos valores com uma barra; troca-se por
    # um marcador antes de partir a linha nos ':' que separam os campos.
    while IFS= read -r line; do
        line="${line//\\:/%%C%%}"
        IFS=: read -r ssid sig sec freq <<<"$line"
        ssid="${ssid//%%C%%/:}"
        [ -n "$ssid" ] || continue
        [ -n "${seen[$ssid]:-}" ] && continue
        seen[$ssid]=1
        band="2.4 GHz"
        [ "${freq%% *}" -gt 4000 ] 2>/dev/null && band="5 GHz"
        [ -n "$sec" ] && [ "$sec" != "--" ] || sec="open"
        i=$((i + 1))
        names[$i]="$ssid"
        secs[$i]="$sec"
        items+=("$i" "$(printf '%-28.28s %3s%%  %-7s %s' "$ssid" "$sig" "$band" "$sec")")
    done < <(nmcli -t -f SSID,SIGNAL,SECURITY,FREQ device wifi list ifname "$up" --rescan yes 2>/dev/null \
             | sed 's/\\:/%%C%%/g' | sort -t: -k2,2nr | sed 's/%%C%%/\\:/g')
    if [ "$i" -eq 0 ]; then
        dialog --title " Wi-Fi " --msgbox "No networks found on $up." 7 50
        return
    fi
    choice="$(dialog --stdout --title " Wi-Fi uplink ($up) " \
        --menu "Strongest first. 5 GHz keeps the uplink off the Bluetooth band." 20 72 12 \
        "${items[@]}")" || return
    ssid="${names[$choice]}"
    # Rede ja guardada: liga com o perfil que existe, sem pedir password.
    if nmcli -g NAME connection show | grep -qxF "$ssid"; then
        dialog --infobox "Connecting to $ssid..." 4 52
        out="$(nmcli -w 30 connection up id "$ssid" ifname "$up" 2>&1)"
    elif [ "${secs[$choice]}" = open ]; then
        dialog --infobox "Connecting to $ssid..." 4 52
        out="$(nmcli -w 30 device wifi connect "$ssid" ifname "$up" 2>&1)"
    else
        psk="$(dialog --stdout --insecure --title " Wi-Fi " \
            --passwordbox "Password for $ssid" 9 60)" || return
        dialog --infobox "Connecting to $ssid..." 4 52
        # A password nunca vai na linha de comandos (ps, /proc/*/cmdline): o perfil escreve-se como ficheiro do
        # NetworkManager (0600, so o root le), como o instalador faz com o AP.
        if out="$(perfil_wifi "$ssid" "$psk" "$up" "${secs[$choice]}" 2>&1)"; then
            out="$(nmcli -w 30 connection up id "$ssid" ifname "$up" 2>&1)"
        fi
        psk=""
    fi
    dialog --title " Wi-Fi " --msgbox "$(printf '%s\n\n%s' "$(echo "$out" | tail -3)" "$(status_text)")" 12 72
}

network_settings() {
    if command -v nmtui >/dev/null 2>&1; then
        dialog --title " Network settings " --msgbox \
            "Opening NetworkManager.\n\nCareful with the camera AP: it is the access point on the USB dongle. Changing or deleting it drops the Canon bodies." 10 62
        nmtui
    else
        dialog --title " Network settings " --msgbox "nmtui is not installed." 7 40
    fi
}

network_menu() {
    local c
    while :; do
        c="$(dialog --stdout --no-cancel --default-item wifi --title " Network " \
            --menu "\n$(status_text)\n" 15 66 3 \
            wifi "Wi-Fi uplink: scan and connect" \
            net  "Network settings (NetworkManager)" \
            back "Back")" || return
        case "$c" in
            wifi) wifi_pick ;;
            net)  network_settings ;;
            *)    return ;;
        esac
    done
}

if ! command -v dialog >/dev/null 2>&1; then
    run_console
    systemctl start --no-block getty@tty1.service
    exit 0
fi

first=1
while :; do
    # Ecra bloqueado na consola (Win+L / F12): sem menu, que dava a rede e a linha de comandos. A consola abre bloqueada.
    if [ -e /run/bridge-ctl.locked ]; then
        run_console
        sleep 1
        continue
    fi
    msg="\n$(status_text)\n\n"
    tflag=()
    if [ "$first" = 1 ]; then
        tflag=(--timeout "$TIMEOUT")
        msg+="The console starts by itself in $TIMEOUT s."
    fi
    choice="$(dialog --stdout --no-cancel "${tflag[@]}" --default-item console \
        --title " Camera Bridge " --menu "$msg" 18 66 4 \
        console "Bridge console (bridge-ctl)" \
        wifi  "Wi-Fi uplink: scan and connect" \
        net   "Network settings (NetworkManager)" \
        shell "Command line (login)")" || true
    first=0
    case "$choice" in
        wifi) wifi_pick ;;
        net)  network_settings ;;
        shell)
            clear
            systemctl start --no-block getty@tty1.service
            exit 0 ;;
        *)
            # consola, Esc ou tempo esgotado; ao sair dela volta a este menu
            run_console ;;
    esac
done

#!/bin/sh
# AP das cameras sem partilha de internet: o NetworkManager (ipv4.method=shared) continua a dar o DHCP, as reservas
# das Canon e o gateway 192.168.4.1, mas nada que venha do AP sai por outra interface (sem NAT para o uplink).
# O trafego do proprio Atom (bridge, NTP, Tailscale, OTA dos gateways) nao passa pela cadeia "forward": nao e afectado.
# Uso: bridge-ap-nonat.sh start|stop
AP_IF=ps-control
# Le-se com sed e nao com ". ap.conf": o AP_CON e o nome da rede, sem aspas (um "Rig (5G)" seria um erro de sintaxe, e um
# "$(...)" correria como root).
AP_CONF_IF="$(sed -n 's/^AP_IF=//p' /opt/camera-bridge/ap.conf 2>/dev/null | head -n1)"
[ -n "$AP_CONF_IF" ] && AP_IF="$AP_CONF_IF"
case "$1" in
    start)
        nft delete table inet bridge_ap_nonat 2>/dev/null || true
        # priority -10: corre antes das regras que o NetworkManager poe para o "shared"; um drop ganha a qualquer accept.
        nft -f - <<NFT
table inet bridge_ap_nonat {
    chain forward {
        type filter hook forward priority -10; policy accept;
        # reject e nao drop: a app (updates, iCloud) recebe a recusa logo e desiste, em vez de esperar e repetir (cada
        # repeticao gasta tempo de antena). TCP leva reset, o resto leva ICMP "proibido".
        iifname "$AP_IF" oifname != "$AP_IF" meta l4proto tcp counter reject with tcp reset
        iifname "$AP_IF" oifname != "$AP_IF" counter reject with icmpx type admin-prohibited
    }
}
NFT
        ;;
    stop)
        nft delete table inet bridge_ap_nonat 2>/dev/null || true
        ;;
    *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

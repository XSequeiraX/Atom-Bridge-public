#!/usr/bin/env python3
"""Grava um firmware no S3 (ESP32-S3) ou na M5StickS3 com o esptool. Uso:

  python gravar.py COM4                  firmware estavel do S3, so a aplicacao (o Wi-Fi e as GoPro do S3 ficam)
  python gravar.py COM4 --completo       firmware estavel do S3, imagem completa (apaga tudo: Wi-Fi e GoPro; S3 novo ou avariado)
  python gravar.py COM4 --f6-probe       teste (testes/): sonda BLE por porta serie
  python gravar.py COM4 --dry-run        mostra os comandos sem os correr

Comando de bolso do F6 (M5Stack M5StickS3; so com o S3 do F6 em modo F6 standalone):
  python gravar.py COM7 --m5stick --ssid F6-REMOTE-1A2B     imagem completa + nome e palavra-passe do AP (a palavra-passe e pedida
                                                            no terminal, sem aparecer; ou --psk ...)
  python gravar.py COM7 --m5stick                           so a aplicacao (actualizar; o Wi-Fi guardado fica)
  python gravar.py COM7 --m5stick --so-wifi --ssid X        so muda o nome/palavra-passe do AP (a aplicacao fica)
  (--host IP muda o endereco do S3; por omissao 192.168.4.1)

Requisitos: python 3 e  pip install esptool . O S3 tem de estar ligado ao PC por USB e, se estiver no modo F6 (USB host),
ligado com o botao BOOT carregado (a porta serie so existe assim)."""
import argparse
import getpass
import os
import subprocess
import sys
import tempfile

AQUI = os.path.dirname(os.path.abspath(__file__))
ESTAVEL_APP = os.path.join(AQUI, "estavel", "gopro_gw-21_only_program.bin")
ESTAVEL_COMPLETO = os.path.join(AQUI, "estavel", "gopro_gw-21_complete.bin")
M5_APP = os.path.join(AQUI, "m5stick", "f6_remote-6_only_program.bin")
M5_COMPLETO = os.path.join(AQUI, "m5stick", "f6_remote-6_complete.bin")
M5_CFG_OFFSET = "0x310000"           # particao "cfg" (partitions.csv do _esp32_tools/m5stick_f6_remote)
# Os testes vivem em ../testes (so no repositorio, nao nesta pasta se a levares sozinha para outro PC)
EXPERIMENTAIS = {"--f6-probe": os.path.join(AQUI, "..", "testes", "f6_ble_probe", "f6_ble_probe.merged.bin")}


def esptool(porta, *args):
    return [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", porta, *args]


def cfg_blob(ssid, psk, host):
    """Os 128 bytes da particao "cfg" da M5StickS3 (formato no cabecalho do m5stick_f6_remote.ino)."""
    s, p, h = ssid.encode(), psk.encode(), host.encode()
    if not 1 <= len(s) <= 32:
        sys.exit("o nome do Wi-Fi (--ssid) tem de ter de 1 a 32 bytes")
    if not 8 <= len(p) <= 63:
        sys.exit("a palavra-passe do Wi-Fi tem de ter de 8 a 63 caracteres (WPA2)")
    if not 1 <= len(h) <= 16:
        sys.exit("o endereco (--host) tem de ter de 1 a 16 caracteres")
    b = bytearray(128)
    b[0:4] = b"F6RC"
    b[4], b[5], b[6], b[7] = 1, len(s), len(p), len(h)
    b[8:8 + len(s)] = s
    b[40:40 + len(p)] = p
    b[104:104 + len(h)] = h
    soma = sum(b[:126]) & 0xFFFF
    b[126], b[127] = soma & 0xFF, soma >> 8
    return bytes(b)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("porta", help="porta serie do S3 ou da M5StickS3 (COM4, /dev/ttyACM0, ...)")
    ap.add_argument("--completo", action="store_true", help="imagem completa do firmware estavel do S3 (apaga tudo)")
    ap.add_argument("--f6-probe", action="store_true")
    ap.add_argument("--m5stick", action="store_true", help="firmware do comando de bolso do F6 (M5StickS3)")
    ap.add_argument("--ssid", help="--m5stick: nome do Wi-Fi do S3 do F6 (F6-REMOTE-xxxx)")
    ap.add_argument("--psk", help="--m5stick: palavra-passe desse Wi-Fi (se faltar, pergunta-se no terminal)")
    ap.add_argument("--host", default="192.168.4.1", help="--m5stick: endereco do S3 (omissao 192.168.4.1)")
    ap.add_argument("--so-wifi", action="store_true", help="--m5stick: so escreve o nome e a palavra-passe do Wi-Fi")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    exp = [k for k in EXPERIMENTAIS if getattr(a, k[2:].replace("-", "_"))]
    if len(exp) + a.completo + a.m5stick > 1:
        ap.error("escolhe so um destes: --completo, --f6-probe, --m5stick")
    if (a.ssid or a.psk or a.so_wifi) and not a.m5stick:
        ap.error("--ssid, --psk e --so-wifi so servem com --m5stick")
    if a.so_wifi and not a.ssid:
        ap.error("--so-wifi precisa do --ssid")
    cfg_tmp = None
    if a.m5stick:
        ficheiros = [] if a.so_wifi else [M5_COMPLETO if a.ssid else M5_APP]
        if a.ssid:
            psk = a.psk if a.psk is not None else getpass.getpass("Palavra-passe do Wi-Fi %s: " % a.ssid)
            fd, cfg_tmp = tempfile.mkstemp(suffix=".bin")          # 0600; apaga-se no fim
            with os.fdopen(fd, "wb") as f:
                f.write(cfg_blob(a.ssid, psk, a.host))
        passos = []
        if not a.so_wifi:
            passos.append(esptool(a.porta, "write_flash", "0x0" if a.ssid else "0x10000", ficheiros[0]))
        if cfg_tmp:
            passos.append(esptool(a.porta, "write_flash", M5_CFG_OFFSET, cfg_tmp))
    elif exp:
        passos = [esptool(a.porta, "erase_flash"), esptool(a.porta, "write_flash", "0x0", EXPERIMENTAIS[exp[0]])]
        ficheiros = [EXPERIMENTAIS[exp[0]]]
    elif a.completo:
        passos = [esptool(a.porta, "erase_flash"), esptool(a.porta, "write_flash", "0x0", ESTAVEL_COMPLETO)]
        ficheiros = [ESTAVEL_COMPLETO]
    else:                       # como o Atom: a aplicacao no 0x10000 depois de apagar o otadata (0xe000) para arrancar dela
        passos = [esptool(a.porta, "erase_region", "0xe000", "0x2000"), esptool(a.porta, "write_flash", "0x10000", ESTAVEL_APP)]
        ficheiros = [ESTAVEL_APP]
    try:
        for f in ficheiros:
            if not os.path.exists(f):
                sys.exit("falta o ficheiro: " + f)
        for p in passos:
            print("$", " ".join(p if p[-1] != cfg_tmp else p[:-1] + ["<wifi.cfg: %s>" % a.ssid]))     # a palavra-passe nunca aparece
            if not a.dry_run and subprocess.call(p) != 0:
                sys.exit("o esptool falhou (porta certa, cabo de dados, modo de gravacao)")
        print("feito. Desliga e volta a ligar o aparelho." if not a.dry_run else "")
    finally:
        if cfg_tmp and os.path.exists(cfg_tmp):
            os.remove(cfg_tmp)


if __name__ == "__main__":
    main()

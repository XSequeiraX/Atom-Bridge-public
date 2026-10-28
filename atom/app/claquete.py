"""Cena, reel e take do clip seguinte, iguais em todas as camaras (2026-10-14).

O bridge numera os takes (shoot log). Quando um take fecha, o clip SEGUINTE de cada camara recebe a cena, o reel e o
numero do take que vai ser: a BMPCC (principal e extra) pelo protocolo Blackmagic, os iPhones pela claquete da app
(PUT /slates/nextClip). Os ficheiros chegam a montagem ja identificados e coerentes entre camaras. Nada disto se envia
a gravar nem atrasa o REC: e entre takes.

Protocolo Blackmagic (Camera Control, categoria 12 Metadata; operacao 0 = atribuir):
  12.0 Reel   int16  [0] 0-999
  12.2 Scene  string ate 5 caracteres
  12.3 Take   int8   [0] take 1-99, [1] etiqueta (-1 nenhuma)
Pacote: [destino 255, tamanho (4 + dados), comando 0, 0, categoria, parametro, tipo, operacao, dados...], com zeros ate
multiplo de 4 (como o REC: 255 5 0 0 10 1 1 0 2 0 0 0).
"""
import re
import struct

TIPO_INT8, TIPO_INT16, TIPO_STRING = 1, 2, 5
CENA_MAX = 5
REEL_MAX = 999


def pacote(categoria, parametro, tipo, dados):
    corpo = bytes([categoria, parametro, tipo, 0]) + bytes(dados)
    p = bytes([255, len(corpo), 0, 0]) + corpo
    return p + b"\x00" * (-len(p) % 4)


def take_camara(n):
    """O numero que a camara aceita (1-99): o take 100 do bridge e o 1 da camara, e assim por diante."""
    return (int(n) - 1) % 99 + 1


def validar(cena, reel):
    """(cena, reel) normalizados ou levanta ValueError. Cena vazia/None: sem cena. Reel None: sem reel."""
    if cena is not None:
        cena = str(cena).strip()
        if cena == "":
            cena = None
        elif len(cena) > CENA_MAX or not re.fullmatch(r"[A-Za-z0-9]+", cena) or not re.search(r"[A-Za-z1-9]", cena):
            raise ValueError("scene: 1 to %d letters or digits (with a letter or a non-zero digit)" % CENA_MAX)
    if reel is not None and reel != "":
        try:
            reel = int(reel)
        except (TypeError, ValueError):
            raise ValueError("reel: a number from 1 to %d" % REEL_MAX) from None
        if not 1 <= reel <= REEL_MAX:
            raise ValueError("reel: a number from 1 to %d" % REEL_MAX)
    else:
        reel = None
    return cena, reel


def comandos_bm(cena, reel, take):
    """Os pacotes para a BMPCC: reel, cena e take do clip seguinte (os que existirem)."""
    out = []
    if reel is not None:
        out.append(pacote(12, 0, TIPO_INT16, struct.pack("<h", int(reel))))
    if cena:
        out.append(pacote(12, 2, TIPO_STRING, cena.encode("utf-8")[:CENA_MAX]))
    if take:
        out.append(pacote(12, 3, TIPO_INT8, struct.pack("<bb", take_camara(take), -1)))
    return out


def chave(cena, reel, take):
    """O que identifica o que se enviou (para nao repetir o mesmo a mesma camara)."""
    return "%s|%s|%s" % (cena or "", reel or "", take or "")

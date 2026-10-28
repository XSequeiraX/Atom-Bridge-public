#!/usr/bin/env python3
"""Mostra os metadados de WAVs do Zoom F6: cena, take, nota, nomes das pistas.

O F6 escreve o nome da cena nos METADADOS (iXML <SCENE> e a descricao BWF),
nao no nome do ficheiro. Isto le-os sem programas extra (so Python 3).

  python3 f6_meta.py /media/sd/F6/ZOOM0001.WAV
  python3 f6_meta.py /media/sd            (todos os WAV da pasta, recursivo)
  python3 f6_meta.py ficheiro.WAV --tudo  (o iXML inteiro e a descricao BWF)
  python3 f6_meta.py /mnt/f6sd --ultima   (so a take mais recente da pasta)
"""
import os
import re
import struct
import sys


def chunks(path):
    """{id: bytes} dos chunks bext e iXML (RIFF e RF64), sem ler o audio."""
    out = {}
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid in (b"bext", b"iXML"):
                out[cid.decode()] = f.read(size)
            else:
                if cid == b"data" and size == 0xFFFFFFFF:     # RF64: o tamanho real esta no ds64
                    break
                f.seek(size, 1)
            if size % 2:
                f.seek(1, 1)
    return out


def tag(xml, nome):
    m = re.search(r"<%s>(.*?)</%s>" % (nome, nome), xml, re.S)
    return m.group(1).strip() if m else None


def mostra(path, tudo=False):
    try:
        c = chunks(path)
    except (OSError, ValueError) as e:
        print("%s: %s" % (path, e))
        return
    xml = c.get("iXML", b"").decode("utf-8", "replace")
    print("== %s" % path)
    if not xml:
        print("   no iXML")
    else:
        for nome in ("PROJECT", "SCENE", "TAKE", "TAPE", "NOTE", "CIRCLED", "FILE_UID"):
            v = tag(xml, nome)
            if v is not None:
                print("   %-9s %s" % (nome, v or "(vazio)"))
        pistas = re.findall(r"<TRACK>.*?<CHANNEL_INDEX>(\d+)</CHANNEL_INDEX>.*?<NAME>(.*?)</NAME>.*?</TRACK>",
                            xml, re.S)
        if pistas:
            print("   PISTAS    " + ", ".join("%s=%s" % (i, n or "-") for i, n in pistas))
    if "bext" in c:
        desc = c["bext"][:256].split(b"\0")[0].decode("latin-1", "replace")
        linhas = [l for l in desc.replace("\r", "\n").split("\n") if l.strip()]
        cena = [l for l in linhas if "SCENE" in l.upper()]
        if cena:
            print("   BWF       " + " | ".join(cena))
        if tudo:
            print("   -- descricao BWF:\n      " + "\n      ".join(linhas))
    if tudo and xml:
        print("   -- iXML:\n" + xml)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tudo = "--tudo" in sys.argv
    if not args:
        sys.exit(__doc__)
    wavs = []
    for a in args:
        if os.path.isdir(a):
            for raiz, _, fs in os.walk(a):
                wavs += [os.path.join(raiz, f) for f in sorted(fs)
                         if f.lower().endswith(".wav") and not f.startswith("._")]
        else:
            wavs.append(a)
    if "--ultima" in sys.argv and wavs:
        # A take mais recente: a pasta (ou ficheiro) com a data de alteracao maior.
        pasta = os.path.dirname(max(wavs, key=os.path.getmtime))
        wavs = [w for w in wavs if os.path.dirname(w) == pasta]
    for w in wavs:
        mostra(w, tudo)


if __name__ == "__main__":
    main()

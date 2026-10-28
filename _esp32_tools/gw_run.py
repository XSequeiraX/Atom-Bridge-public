#!/usr/bin/env python3
"""Fala com o gopro_gw pela serie USB numa so ligacao: gw_run.py COM4 "SCAN 8" "WAIT:10" "FOUND" ..."""
import sys, time, serial
s = serial.Serial(); s.port, s.baudrate, s.timeout = sys.argv[1], 115200, 0.2
s.dtr = s.rts = False
s.open(); time.sleep(0.4)
def ler(seg):
    fim = time.time() + seg
    while time.time() < fim:
        ln = s.readline().decode("utf-8", "replace").rstrip()
        if ln: print(ln, flush=True)
for p in sys.argv[2:]:
    print(">>>", p, flush=True)
    if p.startswith("WAIT:"): ler(float(p[5:])); continue
    s.write((p + "\n").encode()); ler(2.5 if not p.upper().startswith("REC") else 8)
s.close()

# Gateway C3: o programa e o app.mpy, ja compilado (o GitHub compila o app.py e o netcfg.py com o mpy-cross do
# MicroPython 1.24.1: .github/workflows/c3-firmware.yml). Compilar o codigo-fonte no proprio C3 gastava a memoria que
# o Wi-Fi e o Bluetooth precisam (2026-10-14: "Wifi Unknown Error 0x0101", sem memoria, e o Bluetooth sem arrancar).
import gc
gc.collect()
import app

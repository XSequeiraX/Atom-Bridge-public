# Arranque do gateway C3: guarda do firmware novo (OTA, 2026-10-13).
#
# Depois de um OTA (netcfg: /api/fw/apply) fica o ficheiro "fw_trial" com o numero de arranques do firmware novo.
# Enquanto ele existir liga-se o watchdog: se o main.py novo rebentar (cai na REPL) ou encravar, o C3 reinicia
# sozinho. Ao terceiro arranque sem o main.py dar o firmware por bom (apaga o fw_trial quando o Wi-Fi esta ligado
# ha 60 s), repoe as copias .bak e arranca o firmware anterior.
import os
import machine

TRIAL = "fw_trial"
FILES = ("main.py", "app.mpy", "netcfg.mpy")      # o programa vai compilado (.mpy): ver main.py


def _log(txt):
    try:
        with open("diag.log", "a") as f:
            f.write(txt + "\n")
    except OSError:
        pass


# Um corte de energia a meio da troca dos ficheiros (OTA) pode deixar o main.py em falta: repoem-se as copias .bak.
try:
    os.stat("main.py")
except OSError:
    for _f in FILES:
        try:
            os.stat(_f + ".bak")
            os.rename(_f + ".bak", _f)
        except OSError:
            pass
    _log("boot without main.py: .bak copies restored")

try:
    with open(TRIAL) as f:
        _n = int(f.read().strip() or "0")
except (OSError, ValueError):
    _n = None

if _n is not None:
    if _n >= 2:
        for _f in FILES:
            try:
                os.stat(_f + ".bak")
                os.rename(_f + ".bak", _f)
            except OSError:
                pass
        try:
            os.remove(TRIAL)
        except OSError:
            pass
        _log("OTA: new firmware did not start, .bak copies restored")
    else:
        with open(TRIAL, "w") as f:
            f.write(str(_n + 1))
        machine.WDT(timeout=60000)

# (fiz_bm_c3 16 a 18 ligavam aqui o Bluetooth, antes de o main.py ser compilado. Na instalacao pela USB o C3 deixou de
# arrancar de todo, nem o Wi-Fi: 2026-10-14, voltou a ligar-se so no main.py, como na 15, onde o Wi-Fi arranca sempre.)
# Um marcador ble_try deixado pela versao anterior apaga-se: aqui ja nao se tenta nada.
try:
    with open("ble_try") as _f:
        _m = _f.read().strip()
    if _m == "boot":
        os.remove("ble_try")
except OSError:
    pass

import bluetooth, time, json, os, sys, select

BM_MAC = bytes([0xCC, 0x86, 0xEC, 0x2F, 0x15, 0x50])
SECRETS_FILE = "ble_secrets.json"

def load_secrets():
    try:
        with open(SECRETS_FILE) as f:
            return {(t, bytes.fromhex(k)): bytes.fromhex(v) for t, k, v in json.load(f)}
    except Exception:
        return {}

def save_secrets():
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([[k[0], k[1].hex(), v.hex()] for k, v in secrets.items()], f)
    os.rename(tmp, SECRETS_FILE)

secrets = load_secrets()
print("saved bonds:", len(secrets))

ble = bluetooth.BLE()
ble.active(True)
# KEYBOARD_ONLY: a BM mostra o codigo, nos "escrevemos" -> Passkey Entry
ble.config(bond=True, mitm=True, le_secure=False, io=2)

st = {"at": None, "conn": None, "pending": None, "enc": False, "down": False}

def irq(ev, data):
    if ev == 5:
        at, addr, _, rssi, _ = data
        if bytes(addr) == BM_MAC and st["at"] is None:
            st["at"] = at
    elif ev == 6:
        if st["at"] is not None:
            ble.gap_connect(st["at"], BM_MAC)
    elif ev == 7:
        st["conn"] = data[0]
        print("CONNECTED")
    elif ev == 8:
        print("DROPPED")
        st["down"] = True
    elif ev == 28:
        conn, enc, auth, bonded, ks = data
        print("ENCRYPTION enc=%d auth=%d bonded=%d ks=%d" % (enc, auth, bonded, ks))
        st["enc"] = bool(enc)
    elif ev == 29:
        t, index, key = data
        if key is None:
            i = 0
            for (kt, k), v in secrets.items():
                if kt == t:
                    if i == index:
                        return v
                    i += 1
            return None
        return secrets.get((t, bytes(key)), None)
    elif ev == 30:
        t, key, value = data
        k = (t, bytes(key))
        if value is None:
            secrets.pop(k, None)
        else:
            secrets[k] = bytes(value)
        save_secrets()
        print("SET_SECRET saved (type %d)" % t)
        return True
    elif ev == 31:
        conn, action, passkey = data
        print("PASSKEY action=%d value=%s" % (action, passkey))
        if action == 2:
            st["pending"] = conn
            print(">>> PASSKEY_INPUT: read the code on the BM screen")
        elif action == 4:
            ble.gap_passkey(conn, action, 1)

ble.irq(irq)
ble.gap_scan(5000, 30000, 30000, True)
t0 = time.ticks_ms()
while st["conn"] is None and time.ticks_diff(time.ticks_ms(), t0) < 12000:
    time.sleep_ms(100)

if st["conn"] is None:
    print("DID NOT CONNECT")
else:
    time.sleep_ms(800)
    print("pairing...")
    ble.gap_pair(st["conn"])
    # O MicroPython nao entrega _IRQ_PASSKEY_ACTION em ligacoes iniciadas por
    # nos (peripheral_gap_event_cb nao trata o evento), mas o NimBLE fica a
    # espera do codigo na mesma -- aceitar digitos logo a partir daqui.
    st["pending"] = st["conn"]
    print(">>> READY FOR THE CODE")
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    digits = ""
    t0 = time.ticks_ms()
    while not st["down"] and time.ticks_diff(time.ticks_ms(), t0) < 120000:
        if st["pending"] is not None and poller.poll(0):
            c = sys.stdin.read(1)
            if c.isdigit():
                digits += c
            if len(digits) == 6:
                print("sending code", digits)
                ble.gap_passkey(st["pending"], 2, int(digits))
                st["pending"] = None
                digits = ""
        if st["enc"]:
            print("BOND OK")
            time.sleep(2)
            break
        time.sleep_ms(20)
    if not st["down"]:
        ble.gap_disconnect(st["conn"])
        time.sleep(1)
print("END")

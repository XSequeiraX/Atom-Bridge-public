import bluetooth, time, json, os

BM_MAC = bytes([0xCC, 0x86, 0xEC, 0x2F, 0x15, 0x50])
INCOMING = bluetooth.UUID("b864e140-76a0-416a-bf30-5876504537d9")
OUTGOING = bluetooth.UUID("5dd3465f-1aee-4299-8493-d2eca2f8e1bb")
SECRETS_FILE = "ble_secrets.json"

with open(SECRETS_FILE) as f:
    secrets = {(t, bytes.fromhex(k)): bytes.fromhex(v) for t, k, v in json.load(f)}
print("bonds loaded:", len(secrets))

def save_secrets():
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([[k[0], k[1].hex(), v.hex()] for k, v in secrets.items()], f)
    os.rename(tmp, SECRETS_FILE)

ble = bluetooth.BLE()
ble.active(True)
ble.config(bond=True, mitm=True, le_secure=False, io=2)

st = {"at": None, "conn": None, "enc": False, "down": False, "svcs": [], "sdone": False,
      "in_h": None, "out_h": None, "cdone": False, "notifies": 0, "transport": [], "t_conn": None}

def irq(ev, data):
    if ev == 5:
        at, addr, _, _, _ = data
        if bytes(addr) == BM_MAC and st["at"] is None:
            st["at"] = at
    elif ev == 6:
        if st["at"] is not None:
            ble.gap_connect(st["at"], BM_MAC)
    elif ev == 7:
        st["conn"] = data[0]
        st["t_conn"] = time.ticks_ms()
        print("CONNECTED")
    elif ev == 8:
        print("DROPPED")
        st["down"] = True
    elif ev == 9:
        st["svcs"].append((data[1], data[2]))
    elif ev == 10:
        st["sdone"] = True
    elif ev == 11:
        _, _, vh, _, uuid = data
        if uuid == INCOMING:
            st["in_h"] = vh
        elif uuid == OUTGOING:
            st["out_h"] = vh
    elif ev == 12:
        st["cdone"] = True
    elif ev == 18:
        _, vh, d = data
        if vh == st["in_h"]:
            st["notifies"] += 1
            d = bytes(d)
            if len(d) > 8 and d[4] == 10 and d[5] == 1:
                st["transport"].append(d[8])
    elif ev == 28:
        conn, enc, auth, bonded, ks = data
        print("ENCRYPTION enc=%d auth=%d bonded=%d ks=%d  (%d ms after connecting)" %
              (enc, auth, bonded, ks, time.ticks_diff(time.ticks_ms(), st["t_conn"])))
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
        print("SET_SECRET (type %d)" % t)
        return True
    elif ev == 31:
        print("PASSKEY asked?! action=%d" % data[1])

def wait(cond, ms):
    t0 = time.ticks_ms()
    while not cond() and not st["down"] and time.ticks_diff(time.ticks_ms(), t0) < ms:
        time.sleep_ms(20)
    return cond()

ble.irq(irq)
ble.gap_scan(5000, 30000, 30000, True)
if not wait(lambda: st["conn"] is not None, 12000):
    print("DID NOT CONNECT")
else:
    time.sleep_ms(300)
    ble.gap_pair(st["conn"])
    if not wait(lambda: st["enc"], 10000):
        print("NO ENCRYPTION with the saved bond")
    else:
        ble.gattc_discover_services(st["conn"])
        wait(lambda: st["sdone"], 5000)
        for s, e in st["svcs"]:
            st["cdone"] = False
            ble.gattc_discover_characteristics(st["conn"], s, e)
            wait(lambda: st["cdone"], 5000)
        print("handles: incoming=%s outgoing=%s" % (st["in_h"], st["out_h"]))
        if st["in_h"]:
            ble.gattc_write(st["conn"], st["in_h"] + 1, b"\x01\x00", 1)
            t0 = time.ticks_ms()
            while not st["down"] and time.ticks_diff(time.ticks_ms(), t0) < 15000:
                time.sleep_ms(200)
            print("notifications in 15 s:", st["notifies"])
            print("transport states seen:", st["transport"][:10])
            print("link held 15 s" if not st["down"] else "link dropped while listening")
    if not st["down"]:
        ble.gap_disconnect(st["conn"])
        time.sleep(1)
print("END")

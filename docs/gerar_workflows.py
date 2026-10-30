"""Bridge-Control: os diagramas de cada parte, uma pagina A4 deitada por parte. Escreve workflows.html ao lado; o
imprimir_pdf.py passa-o a PDF (Chromium): python3 imprimir_pdf.py workflows.html Bridge-Control-workflows.pdf"""
import html
import os

W, H = 1123, 794            # A4 landscape at 96 dpi
COR = {
    "rec": "#c93b3b", "time": "#22895c", "log": "#b8860b", "cams": "#1784a6", "net": "#3a64c8",
    "sys": "#5b6472", "f6": "#7151b8", "gw": "#d0701c", "ok": "#2e8b57", "bad": "#c93b3b",
}
FUNDO = {k: v + "14" for k, v in COR.items()}   # 8 % tint


def esc(t):
    return html.escape(str(t))


class Diagram:
    def __init__(self, h=560):
        self.parts, self.h, self.nodes = [], h, {}

    def node(self, nid, x, y, w, h, title, lines=(), cor="sys", shape="box"):
        self.nodes[nid] = (x, y, w, h)
        c, f = COR[cor], FUNDO[cor]
        if shape == "diamond":
            cx, cy = x + w / 2, y + h / 2
            self.parts.append('<polygon points="%s,%s %s,%s %s,%s %s,%s" fill="%s" stroke="%s" stroke-width="1.6"/>'
                              % (cx, y, x + w, cy, cx, y + h, x, cy, f, c))
        elif shape == "pill":
            self.parts.append('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="%s" stroke="%s" stroke-width="1.6"/>'
                              % (x, y, w, h, h / 2, c, c))
        else:
            self.parts.append('<rect x="%s" y="%s" width="%s" height="%s" rx="8" fill="%s" stroke="%s" stroke-width="1.6"/>'
                              % (x, y, w, h, f, c))
            self.parts.append('<rect x="%s" y="%s" width="5" height="%s" rx="2" fill="%s"/>' % (x, y + 6, h - 12, c))
        n = 1 + len(lines)
        y0 = y + h / 2 - (n - 1) * 7.5 + 4
        cor_t = "#fff" if shape == "pill" else c
        util = (w * (0.62 if shape == "diamond" else 1)) - 16
        # O texto encolhe para caber na caixa (a largura media de um caracter: ~7.6 px a 13 px, ~6.3 px a 11 px)
        ft = min(13.0, 13.0 * util / max(1, 8.3 * len(title)))
        self.parts.append('<text x="%s" y="%s" text-anchor="middle" class="nt" fill="%s" style="font-size:%.1fpx">%s</text>'
                          % (x + w / 2 + (2 if shape == "box" else 0), y0, cor_t, ft, esc(title)))
        for i, l in enumerate(lines):
            fl = min(11.0, 11.0 * util / max(1, 6.3 * len(l)))
            self.parts.append('<text x="%s" y="%s" text-anchor="middle" class="nl" fill="%s" style="font-size:%.1fpx">%s</text>'
                              % (x + w / 2 + (2 if shape == "box" else 0), y0 + 15 * (i + 1),
                                 "#fff" if shape == "pill" else "#333", fl, esc(l)))

    def _borda(self, nid, lado):
        if isinstance(lado, tuple):          # um ponto exacto (para caixas altas com varias entradas)
            return lado
        x, y, w, h = self.nodes[nid]
        return {"r": (x + w, y + h / 2), "l": (x, y + h / 2), "t": (x + w / 2, y), "b": (x + w / 2, y + h)}[lado]

    def arrow(self, a, la, b, lb, label="", cor="sys", dash=False, via=None, lpos=0.5, both=False):
        x1, y1 = self._borda(a, la)
        x2, y2 = self._borda(b, lb)
        pts = [(x1, y1)] + (via or []) + [(x2, y2)]
        d = "M %s %s " % pts[0] + " ".join("L %s %s" % p for p in pts[1:])
        c = COR[cor]
        self.parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.7"%s marker-end="url(#m_%s)"%s/>'
                          % (d, c, ' stroke-dasharray="6 4"' if dash else "", cor,
                             ' marker-start="url(#ms_%s)"' % cor if both else ""))
        if label:
            # o rotulo no meio do troco escolhido
            seg = max(0, min(len(pts) - 2, int(lpos * (len(pts) - 1))))
            (ax, ay), (bx, by) = pts[seg], pts[seg + 1]
            t = lpos * (len(pts) - 1) - seg if len(pts) > 2 else lpos
            mx, my = ax + (bx - ax) * t, ay + (by - ay) * t
            larg = 6.2 * len(label) + 10
            self.parts.append('<rect x="%s" y="%s" width="%s" height="17" rx="4" fill="#fff" stroke="%s" stroke-width="0.8"/>'
                              % (mx - larg / 2, my - 9, larg, c))
            self.parts.append('<text x="%s" y="%s" text-anchor="middle" class="al" fill="%s">%s</text>'
                              % (mx, my + 4, c, esc(label)))

    def text(self, x, y, t, cls="nl", cor="#333", anchor="start"):
        self.parts.append('<text x="%s" y="%s" text-anchor="%s" class="%s" fill="%s">%s</text>'
                          % (x, y, anchor, cls, cor, esc(t)))

    def zone(self, x, y, w, h, label, cor="sys"):
        self.parts.append('<rect x="%s" y="%s" width="%s" height="%s" rx="12" fill="none" stroke="%s" stroke-width="1.2" '
                          'stroke-dasharray="3 4"/>' % (x, y, w, h, COR[cor]))
        larg = 7.4 * len(label) + 14
        self.parts.append('<rect x="%s" y="%s" width="%s" height="18" rx="5" fill="#fff"/>' % (x + 8, y + h - 9, larg))
        self.parts.append('<text x="%s" y="%s" class="zl" fill="%s">%s</text>' % (x + 15, y + h + 4, COR[cor], esc(label)))

    def svg(self):
        defs = "".join(
            '<marker id="m_%s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
            '<path d="M0,0 L10,5 L0,10 z" fill="%s"/></marker>'
            '<marker id="ms_%s" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
            '<path d="M10,0 L0,5 L10,10 z" fill="%s"/></marker>' % (k, v, k, v) for k, v in COR.items())
        return '<svg viewBox="0 0 1043 %s" width="1043" height="%s"><defs>%s</defs>%s</svg>' % (
            self.h, self.h, defs, "".join(self.parts))


PAGES = []


def page(num, title, subtitle, diagram, notes=(), cor="sys"):
    notas = "".join("<li>%s</li>" % n for n in notes)
    PAGES.append('''<section class="page">
  <header><div class="num" style="background:%s">%s</div><div><h1>%s</h1><p>%s</p></div></header>
  <div class="dia">%s</div>
  %s
  <footer>Bridge-Control · visual workflows · 2026-10-27</footer>
</section>''' % (COR[cor], num, esc(title), esc(subtitle), diagram.svg() if diagram else "",
                 '<ul class="notes">%s</ul>' % notas if notas else ""))


# ---------------------------------------------------------------- 1 cover
def capa():
    d = Diagram(500)
    areas = [("rec", "REC / STOP", "group and per-device recording"), ("time", "TIME", "timecode, clocks, jam"),
             ("cams", "CAMERAS", "BMPCC, Canon, Sony, mobiles, GoPro"), ("gw", "GATEWAYS", "C3 (BMPCC) and S3 (GoPro)"),
             ("f6", "ZOOM F6", "FRC-8 link, card, jam"), ("log", "LOGS", "shoot log, takes, SFTP"),
             ("net", "NETWORK", "camera AP, uplink, web"), ("sys", "SYSTEM", "services, update, access")]
    for i, (k, t, s) in enumerate(areas):
        x = 40 + (i % 4) * 245
        y = 30 + (i // 4) * 90
        d.node("a%d" % i, x, y, 225, 66, t, [s], k)
    idx = ["2  System map (hardware)", "3  Software: services and ports", "4  Power on and daily start",
           "5  Arming and the group REC", "6  REC on one device, per camera type", "7  Timecode and clocks",
           "8  BMPCC: C3 gateway or Atom Bluetooth (plan B)", "9  GoPro through the S3 gateway",
           "10  Zoom F6", "11  Shoot log, takes, slate, send logs", "12  Control surfaces: console, web, USB",
           "13  Update from GitHub and gateway firmware", "14  Access, services and warnings",
           "15  Demo mode", "16  Code check: what was found and fixed"]
    for i, t in enumerate(idx):
        d.text(60 + (i // 8) * 480, 250 + (i % 8) * 28, t, "idx")
    d.text(40, 225, "Pages", "zl", COR["sys"])
    page(1, "Bridge-Control — visual workflows",
         "How each part of the rig works, from button to camera. Colors match the console areas (legend above the index).",
         d, ["Read top to bottom, left to right. Solid arrows: commands and data. Dashed arrows: optional or fallback paths.",
             "Names are the ones used in the console and on the web page."])


# ---------------------------------------------------------------- 2 hardware
def hardware():
    d = Diagram(600)
    d.zone(320, 165, 410, 290, "ATOM (the bridge computer)", "sys")
    d.node("atom", 340, 180, 370, 70, "Bridge + console", ["Debian 13 · camera-bridge, USB services"], "sys")
    d.node("ap", 340, 275, 180, 60, "AR9271 dongle", ["camera Wi-Fi AP 192.168.4.1"], "net")
    d.node("up", 530, 275, 180, 60, "Internal Wi-Fi", ["uplink (5 GHz)"], "net")
    d.node("bt", 340, 360, 180, 60, "BT dongle", ["plan B BMPCC · TC-1 scan"], "cams")
    d.node("aud", 530, 360, 180, 60, "Audio jacks", ["mic in: LTC · phones out: jam"], "time")
    d.node("canon", 30, 14, 200, 56, "Canon R10 / R6 II", ["CCAPI over Wi-Fi"], "cams")
    d.node("sony", 30, 76, 200, 56, "Sony Alpha / FX / ZV", ["Wi-Fi (camera AP) or USB"], "cams")
    d.node("iph", 30, 138, 200, 56, "Mobiles", ["Blackmagic Camera app"], "cams")
    d.node("c3", 30, 205, 200, 56, "C3 gateway", ["Wi-Fi ↔ BLE · MicroPython"], "gw")
    d.node("bmpcc", 30, 295, 200, 56, "BMPCC", ["Bluetooth LE"], "cams")
    d.node("s3", 30, 385, 200, 56, "S3 gateway", ["Wi-Fi ↔ BLE · Arduino"], "gw")
    d.node("gp", 30, 475, 200, 56, "GoPros", ["Bluetooth LE"], "cams")
    d.node("phone", 810, 115, 200, 56, "Phones / laptops", ["web page (PIN)"], "net")
    d.node("ts", 810, 190, 200, 56, "Internet / Tailscale", ["NTP, GitHub, remote"], "net")
    d.node("f6", 810, 300, 200, 56, "Zoom F6", ["USB: FRC-8 link or card"], "f6")
    d.node("tc1", 810, 390, 200, 56, "Deity TC-1", ["LTC out · BLE adverts"], "time")
    d.node("usb", 810, 480, 200, 56, "USB controllers", ["MIDI, pedals, keypads"], "sys")
    d.arrow("canon", "r", "ap", "l", "", "net", via=[(300, 42), (300, 295)])
    d.arrow("sony", "r", "ap", "l", "", "net", via=[(300, 104), (300, 285)])
    d.arrow("iph", "r", "ap", "l", "", "net", via=[(300, 166), (300, 305)])
    d.arrow("c3", "r", "ap", "l", "Wi-Fi", "net", via=[(300, 233), (300, 315)], lpos=0.2)
    d.arrow("s3", "r", "ap", "l", "", "net", via=[(300, 413), (300, 325)])
    d.arrow("c3", "b", "bmpcc", "t", "BLE", "gw")
    d.arrow("s3", "b", "gp", "t", "BLE", "gw")
    d.arrow("bt", "l", "bmpcc", "r", "plan B", "cams", dash=True, via=[(300, 385), (300, 323)], lpos=0.1)
    d.arrow("phone", "l", "ap", "t", "", "net", via=[(750, 143), (750, 150), (445, 150)], dash=True)
    d.arrow("up", "r", "ts", "l", "", "net", via=[(740, 305), (740, 218)])
    d.arrow("f6", "l", "atom", "r", "USB", "f6", via=[(720, 328), (720, 220)], lpos=0.1)
    d.arrow("tc1", "l", "aud", "r", "LTC", "time")
    d.arrow("tc1", "l", "bt", "b", "BLE", "time", dash=True, via=[(760, 430), (760, 440), (435, 440)], lpos=0.6)
    d.arrow("aud", "r", "f6", "l", "jam", "time", dash=True, via=[(700, 385), (700, 340)], lpos=0.6)
    d.arrow("usb", "l", "atom", "b", "USB", "sys", via=[(745, 508), (745, 465), (520, 465)], lpos=0.3)
    page(2, "System map", "What talks to what. Everything on set meets on the camera Wi-Fi of the Atom's AR9271 dongle.",
         d, ["Cameras without Wi-Fi control (BMPCC, GoPro) are reached through an ESP32 gateway that turns Wi-Fi commands into Bluetooth LE.",
             "The internal Wi-Fi is only the uplink (internet, NTP, GitHub, Tailscale); the camera network never depends on it.",
             "Plan B: the main BMPCC can be driven straight from the Atom's Bluetooth dongle when the C3 is not available."], "sys")


# ---------------------------------------------------------------- 3 software
def software():
    d = Diagram(560)
    d.zone(345, 20, 490, 390, "camera-bridge.service  (bridge.py → nucleo + rotas · Flask · port 8080)", "sys")
    d.node("api", 365, 40, 150, 340, "HTTP API", ["/api/*", "", "the one place", "with the rules"], "sys")
    mods = [("rec", "Group REC", "targets, intent", "rec"), ("cam", "Cameras", "canon, bmcam, sony, gopro…", "cams"),
            ("f6m", "F6", "frc8, f6_ctl, f6_ltc", "f6"), ("tc", "Time", "ltc_audio, tc1_ble", "time"),
            ("lg", "Logs", "shoot log, events", "log"), ("acc", "Access", "PIN, web on/off", "sys"),
            ("pg", "Web pages", "/ /shootlog /f6card", "net"), ("cfg", "Files", "/opt/camera-bridge", "log")]
    for i, (k, t, l, c) in enumerate(mods):
        d.node(k, 535 + (i % 2) * 150, 40 + (i // 2) * 88, 135, 66, t, [l], c)
    d.node("boot", 20, 40, 145, 70, "Boot menu", ["tty1"], "sys")
    d.node("con", 185, 40, 145, 70, "Console", ["bridge-ctl"], "sys")
    d.node("ctl", 20, 300, 145, 70, "USB controllers", ["MIDI, pedals"], "sys")
    d.node("usb", 185, 300, 145, 70, "bridge-usb", ["API 8091 (local)"], "sys")
    d.node("br", 870, 40, 150, 70, "Browser", ["phone, iPad, laptop"], "net")
    d.node("gw", 870, 160, 150, 70, "Gateways", ["C3, S3 (Wi-Fi)"], "gw")
    d.node("cm", 870, 280, 150, 70, "Canon, mobiles, Sony", ["Wi-Fi APIs / SDK"], "cams")
    d.node("f6", 870, 400, 150, 60, "Zoom F6", ["USB"], "f6")
    d.arrow("boot", "r", "con", "l", "starts", "sys")
    d.arrow("con", "r", "api", (365, 75), "HTTP", "sys")
    d.arrow("ctl", "r", "usb", "l", "", "sys")
    d.arrow("usb", "r", "api", (365, 335), "HTTP", "sys")
    d.arrow("br", "l", "api", (835, 75), "HTTP + PIN", "net", both=True)
    d.arrow("gw", "l", "api", (835, 195), "", "gw", both=True)
    d.arrow("cm", "l", "api", (835, 315), "", "cams", both=True)
    d.arrow("f6", "l", "api", (835, 395), "", "f6", both=True, via=[(850, 430), (850, 395)])
    page(3, "Software: services and ports", "Two systemd services plus the bridge. Only the bridge talks to cameras; the console and USB use its API.",
         d, ["The console, the USB service and the web page all go through the same HTTP API: one set of rules, no duplicated logic.",
             "The USB service and the web page for other devices can be switched off in GLOBAL SETTINGS > Services; the console keeps working.",
             "The local API (8091) only listens on 127.0.0.1; the console reaches it for its USB menus."], "sys")


# ---------------------------------------------------------------- 4 boot
def arranque():
    d = Diagram(560)
    passos = [("p1", "Power on", ["Atom boots (text mode)"], "sys", "pill"),
              ("p2", "systemd starts services", ["camera-bridge · bridge-usb"], "sys", "box"),
              ("p3", "Code check before start", ["broken /opt? restore last good copy"], "sys", "box"),
              ("p4", "Bridge up on :8080", ["reads settings, devices, ap.conf"], "sys", "box"),
              ("p5", "Boot menu on tty1", ["10 s, then the console"], "sys", "box")]
    for i, (k, t, l, c, s) in enumerate(passos):
        d.node(k, 30 + i * 205, 30, 185, 70, t, l, c, s)
        if i:
            d.arrow(passos[i - 1][0], "r", k, "l", "", "sys")
    d.node("m1", 60, 170, 180, 56, "Bridge console", ["default"], "sys")
    d.node("m2", 270, 170, 180, 56, "Wi-Fi uplink", ["scan + join"], "net")
    d.node("m3", 480, 170, 180, 56, "Network settings", ["NetworkManager"], "net")
    d.node("m4", 690, 170, 180, 56, "Command line", ["login shell"], "sys")
    for k in ("m1", "m2", "m3", "m4"):
        d.arrow("p5", "b", k, "t", "", "sys", via=[(942, 140), (self_x(d, k), 140)])
    d.text(30, 270, "Daily start in the console (W = start-of-session wizard; its 17 steps, grouped)", "zl", COR["sys"])
    w = [("w1", "Atom", ["power, RAM"], "sys"), ("w2", "Wi-Fi", ["AP + uplink"], "net"),
         ("w3", "Clocks", ["source + time"], "time"), ("w4", "TC-1 / LTC", ["GET LTC jam"], "time"),
         ("w5", "Cameras", ["connect, battery"], "cams"), ("w6", "F6", ["FRC-8, tracks"], "f6"),
         ("w7", "GoPro", ["gateway, clock"], "gw"), ("w8", "Targets", ["arm with SPACE"], "rec"),
         ("w9", "Shoot log", ["new day?"], "log"), ("w10", "Test take", ["REC, STOP, check"], "rec")]
    for i, (k, t, l, c) in enumerate(w):
        x = 30 + (i % 5) * 205
        y = 290 + (i // 5) * 95
        d.node(k, x, y, 185, 62, t, l, c)
        if i % 5:
            d.arrow(w[i - 1][0], "r", k, "l", "", "sys")
    d.arrow("w5", "b", "w6", "t", "", "sys", via=[(902, 370), (122, 370)])
    d.node("pre", 380, 490, 280, 56, "Preflight: READY TO SHOOT / NOT READY", ["GLOBAL SETTINGS > Preflight"], "ok")
    d.arrow("w10", "b", "pre", "r", "", "ok", via=[(902, 518)])
    page(4, "Power on and daily start", "From the power button to the first test take.", d,
         ["If the code in /opt is damaged (power cut during an update), the service restores the newest copy that compiles before starting.",
          "The console reopens by itself after an update, even with a page open.",
          "Preflight lists every check (cameras, time source, disk, memory, power) and gives one verdict, the same as the web page."], "sys")


def self_x(d, k):
    x, y, w, h = d.nodes[k]
    return x + w / 2


# ---------------------------------------------------------------- 5 group REC
def grupo():
    d = Diagram(580)
    d.node("in", 20, 30, 210, 90, "REC pressed", ["console R · web REC", "USB pedal"], "rec", "box")
    d.node("lock", 270, 45, 190, 60, "One at a time", ["group lock"], "sys")
    d.node("q", 500, 25, 200, 100, "Anything recording?", ["armed or not"], "rec", "diamond")
    d.node("stop", 760, 30, 260, 90, "STOP everything recording", ["incl. devices disarmed mid-take", "('also stopping …' in the log)"], "rec")
    d.node("arm", 500, 175, 200, 60, "START armed devices", ["only those ready"], "rec")
    d.node("f6q", 500, 265, 200, 90, "F6 armed with no track?", [], "f6", "diamond")
    d.node("refuse", 760, 280, 260, 60, "Refused", ["'F6: no track armed'"], "bad")
    d.node("fire", 270, 390, 500, 66, "Fire all in parallel", ["Canons · Sony · mobiles · GoPros · BMPCC extras · main BMPCC · F6"], "rec")
    d.node("conf", 270, 490, 240, 60, "Each confirms", ["own state, timeout"], "ok")
    d.node("fault", 540, 490, 230, 60, "No confirm → FAULT", ["warning + shoot log"], "bad")
    d.node("log", 800, 490, 220, 60, "Shoot log", ["start / stop per device, take no."], "log")
    d.arrow("in", "r", "lock", "l", "", "rec")
    d.arrow("lock", "r", "q", "l", "", "rec")
    d.arrow("q", "r", "stop", "l", "yes", "rec")
    d.arrow("q", "b", "arm", "t", "no", "rec")
    d.arrow("arm", "b", "f6q", "t", "", "rec")
    d.arrow("f6q", "r", "refuse", "l", "yes", "bad")
    d.arrow("f6q", "b", "fire", "t", "no", "rec")
    d.arrow("stop", "b", "fire", "r", "", "rec", via=[(890, 423)])
    d.arrow("fire", "b", "conf", "t", "", "ok")
    d.arrow("fire", "b", "fault", "t", "", "bad", dash=True)
    d.arrow("fire", (740, 456), "log", "l", "", "log", via=[(740, 470), (785, 470), (785, 520)])
    d.text(20, 175, "Arming (targets)", "zl", COR["rec"])
    d.text(20, 197, "SPACE / target buttons send one change:", "nl")
    d.text(20, 214, "{id, on} — applied to the bridge's list,", "nl")
    d.text(20, 231, "so two taps from two places never undo", "nl")
    d.text(20, 248, "each other.", "nl")
    d.text(20, 280, "Late telemetry", "zl", COR["rec"])
    d.text(20, 302, "The last group command is remembered", "nl")
    d.text(20, 319, "a few seconds, so a second press still", "nl")
    d.text(20, 336, "stops while a camera reports late.", "nl")
    page(5, "Arming and the group REC", "One rule: if anything is rolling, the next press stops everything. Stopping is always one press away.", d,
         ["Only devices in video mode and ready are started; skipped ones are named in the event log.",
          "A device that accepts the command but does not change state raises a visible FAULT and a 'fault' line in the shoot log.",
          "Per-device REC buttons (next page) never touch other devices."], "rec")


# ---------------------------------------------------------------- 6 per device
def por_aparelho():
    d = Diagram(580)
    lanes = [("Canon (R10, R6 II)", "cams", ["CCAPI recbutton (video mode)", "reply lost → probe confirms", "periodic check: stopped on its own?"]),
             ("Sony (Camera Remote SDK)", "cams", ["SDK MovieRecord (sony_ctl)", "RecordingState confirms (6 s)", "toggle models handled"]),
             ("Mobile (Blackmagic app)", "cams", ["REST /transports/0/record", "confirm within 3 s", "reads older than the command ignored"]),
             ("GoPro (via S3)", "gw", ["S3 queues the command (seq)", "poll until done, then real state", "out-of-order replies dropped"]),
             ("BMPCC extra (own C3)", "gw", ["C3 writes the BLE command", "transport packet confirms", "3 retries on Wi-Fi hiccups"]),
             ("Main BMPCC", "cams", ["BLE write (C3 or plan B)", "confirm within 5 s", "drop handled once"]),
             ("Zoom F6", "f6", ["FRC-8 REC / STOP button", "F6 reports rec state", "no confirm in 1.5 s → FAULT"])]
    for i, (t, c, ls) in enumerate(lanes):
        y = 10 + i * 80
        d.node("h%d" % i, 20, y, 210, 70, t, [], c)
        for j, l in enumerate(ls):
            d.node("s%d_%d" % (i, j), 270 + j * 255, y + 6, 235, 60, l, [], c if j < 2 else "sys")
            d.arrow("h%d" % i if j == 0 else "s%d_%d" % (i, j - 1), "r", "s%d_%d" % (i, j), "l", "", c)
    page(6, "REC on one device", "Console: T (or 2 in the option bar) on the device row · web: the device's REC button.", d,
         ["A per-device REC only changes that device, and decides start or stop from that device's own state.",
          "On the GATEWAYS lines of the console, SPACE / T explain where the cameras are instead of doing nothing.",
          "While a device records, its REC option reads 'REC (recording: press to STOP)'."], "rec")


# ---------------------------------------------------------------- 7 time
def tempo():
    d = Diagram(580)
    d.node("tc1", 20, 40, 200, 70, "Deity TC-1", ["timecode generator"], "time")
    d.node("ltc", 280, 20, 230, 60, "LTC by cable", ["mic input · 3 s read"], "time")
    d.node("ble", 280, 100, 230, 60, "TC-1 by Bluetooth", ["15 s scan · Sidus app open"], "time")
    d.node("jam", 570, 50, 220, 70, "GET LTC → jam", ["offset kept vs Atom clock"], "time")
    d.node("src", 570, 170, 220, 80, "Time source in use", ["BM TC · LTC · NTP · mobile"], "time", "diamond")
    d.node("ntp", 850, 140, 170, 56, "NTP", ["via uplink"], "net")
    d.node("iph", 850, 215, 170, 56, "Mobile time", ["operator offer"], "net")
    d.node("cams", 280, 300, 230, 60, "Set camera clocks", ["Canon (clock), BMPCC, Sony (TC)"], "cams")
    d.node("gp", 20, 300, 220, 60, "GoPro", ["QR (GoPro Labs) · check clock"], "gw")
    d.node("f6", 570, 300, 220, 60, "F6 jam", ["LTC out of headphones, ~5 s"], "f6")
    d.node("dji", 850, 300, 170, 60, "DJI Mic 3", ["same jam output"], "f6")
    d.node("vig", 280, 420, 510, 66, "Drift watch (tc_vigia)", ["each camera's TC vs the Atom's LTC · 60 s window · warns on drift"], "time")
    d.node("rec", 20, 420, 220, 66, "Takes", ["TC of start/stop in the shoot log"], "log")
    d.arrow("tc1", "r", "ltc", "l", "", "time")
    d.arrow("tc1", "r", "ble", "l", "", "time", dash=True)
    d.arrow("ltc", "r", "jam", "l", "", "time")
    d.arrow("ble", "r", "jam", "l", "", "time", dash=True)
    d.arrow("jam", "b", "src", "t", "", "time")
    d.arrow("ntp", "l", "src", "r", "", "net", dash=True)
    d.arrow("iph", "l", "src", "r", "", "net", dash=True)
    d.arrow("src", "b", "cams", "t", "", "time", via=[(680, 275), (395, 275)])
    d.arrow("src", "b", "f6", "t", "", "time")
    d.arrow("f6", "r", "dji", "l", "", "f6", dash=True)
    d.arrow("src", "l", "gp", "t", "", "time", via=[(130, 210)])
    d.arrow("cams", "b", "vig", "t", "", "time")
    d.arrow("vig", "l", "rec", "r", "", "log")
    page(7, "Timecode and clocks", "One reference (the TC-1), one jam on the Atom, everything else set or checked against it.", d,
         ["The jam stores the difference between the TC-1 and the Atom clock; the Atom then free-runs (it has a real-time clock).",
          "Bluetooth reading (no cable) averages ~90 adverts over 15 s and corrects a measured +0.045 s offset.",
          "Nothing sets camera clocks automatically: it is always an operator action."], "time")


# ---------------------------------------------------------------- 8 BMPCC
def bmpcc():
    d = Diagram(580)
    d.node("br", 400, 20, 240, 60, "Bridge", ["c3.json: via = c3 | bluez"], "sys")
    d.node("q", 420, 110, 200, 90, "Which path?", [], "cams", "diamond")
    d.node("c3", 120, 240, 250, 70, "C3 gateway (bridge mode)", ["HTTP /api/bm/poll · /api/bm/write"], "gw")
    d.node("bz", 680, 240, 250, 70, "Atom Bluetooth (plan B)", ["BlueZ · bonded on the dongle"], "cams")
    d.node("bm", 400, 370, 240, 66, "BMPCC", ["REC, TC, battery, slate"], "cams")
    d.node("ev", 20, 370, 250, 66, "Ordered event stream", ["seq numbers · only the last state counts"], "ok")
    d.node("sa", 20, 480, 300, 80, "C3 standalone (no Atom)", ["own network FIZ-C3-XXXX", "FIZ only lives here"], "gw")
    d.node("cmd", 700, 480, 280, 66, "Console switch", ["BMPCC row > Connection"], "sys")
    d.arrow("br", "b", "q", "t", "", "sys")
    d.arrow("q", "l", "c3", "t", "c3", "gw", via=[(245, 155)])
    d.arrow("q", "r", "bz", "t", "bluez", "cams", via=[(805, 155)], dash=True)
    d.arrow("c3", "r", "bm", (470, 370), "BLE", "gw", via=[(470, 275)], lpos=0.3)
    d.arrow("bz", (770, 310), "bm", "r", "BLE", "cams", dash=True, via=[(770, 403)], lpos=0.5)
    d.arrow("c3", (180, 310), "ev", (180, 370), "", "ok")
    d.arrow("cmd", (840, 480), "bz", (840, 310), "", "sys")
    d.arrow("sa", "r", "bm", "b", "", "gw", dash=True, via=[(520, 520)])
    page(8, "BMPCC: C3 gateway or Atom Bluetooth", "The main BMPCC goes through the C3 by default; plan B drives it from the Atom's own dongle.", d,
         ["Switching path is refused while recording; the old session closes and the new one starts.",
          "The C3 reports memory and Bluetooth signal; low memory raises a warning (the firmware is precompiled to fit).",
          "Extra BMPCCs each have their own C3 (bm2, bm3 …) and use the same bridge-mode API."], "cams")


# ---------------------------------------------------------------- 9 GoPro
def gopro():
    d = Diagram(580)
    d.node("usb", 20, 30, 230, 70, "First setup by USB", ["from the Atom: firmware, Wi-Fi, token"], "gw")
    d.node("s3", 400, 30, 240, 70, "S3 gateway", ["joins the camera Wi-Fi", "token in every request"], "gw")
    d.node("gp", 780, 30, 240, 70, "GoPros", ["BLE · up to several per S3"], "cams")
    d.node("poll", 400, 160, 240, 66, "Status poll every 1 s", ["/api/status: cams, cmd, RSSI"], "gw")
    d.node("ord", 400, 270, 240, 66, "Reply order check", ["older than last applied → dropped"], "ok")
    d.node("rec", 20, 160, 230, 66, "REC / STOP", ["POST /api/rec {id, on}"], "rec")
    d.node("seq", 20, 270, 230, 66, "Command queued", ["returns seq"], "rec")
    d.node("done", 20, 380, 230, 66, "cmd seq done?", ["poll every 0.15 s"], "rec", "diamond")
    d.node("ok", 20, 490, 230, 56, "Confirmed", ["shoot log start/stop"], "ok")
    d.node("real", 400, 400, 240, 66, "No answer in time", ["the camera's real state decides"], "sys")
    d.node("by", 780, 270, 240, 66, "Changed by itself?", ["only if not commanded recently"], "log")
    d.node("reset", 780, 400, 240, 66, "Gateway stuck", ["UDP 'GPGW-RESET <token>' :5171"], "bad")
    d.arrow("usb", "r", "s3", "l", "once", "gw")
    d.arrow("s3", "r", "gp", "l", "BLE", "gw", both=True)
    d.arrow("s3", "b", "poll", "t", "", "gw")
    d.arrow("poll", "b", "ord", "t", "", "ok")
    d.arrow("ord", "r", "by", "l", "", "log")
    d.arrow("rec", "b", "seq", "t", "", "rec")
    d.arrow("seq", "b", "done", "t", "", "rec")
    d.arrow("done", "b", "ok", "t", "yes", "ok")
    d.arrow("done", "r", "real", "l", "timeout", "sys")
    d.arrow("rec", "r", "s3", (400, 88), "", "rec", via=[(340, 193), (340, 88)])
    d.arrow("poll", "r", "reset", "r", "", "bad", dash=True, via=[(1032, 193), (1032, 433)])
    page(9, "GoPro through the S3 gateway", "The S3 turns HTTP on the camera Wi-Fi into Bluetooth commands for the GoPros.", d,
         ["The token is set only by USB / serial; every request on the Wi-Fi then carries it in the X-GW-Token header.",
          "Firmware updates always go through the Atom (GLOBAL SETTINGS > Update gateways).",
          "GoPro time: show the camera the QR from /gopro_qr (GoPro Labs); Check clock reads it back to about 1 s."], "gw")


# ---------------------------------------------------------------- 10 F6
def f6():
    d = Diagram(580)
    d.node("q", 420, 20, 200, 90, "F6 USB mode", [], "f6", "diamond")
    d.node("frc", 120, 160, 280, 70, "FRC-8 link (USB-MIDI)", ["the Atom acts as the FRC-8 remote"], "f6")
    d.node("card", 640, 160, 280, 70, "SD Card Reader", ["F6 as a USB disk (Zoom vendor)"], "f6")
    d.node("rec", 20, 280, 200, 60, "REC / STOP", ["button notes"], "rec")
    d.node("st", 240, 280, 200, 60, "State back", ["rec, armed tracks, meters"], "f6")
    d.node("usr", 20, 370, 200, 60, "Scene, note, marks", ["FRC-8 keyboard keys"], "log")
    d.node("blk", 240, 370, 200, 60, "No track armed", ["group REC refused"], "bad")
    d.node("scan", 560, 280, 200, 60, "Scan takes", ["iXML scene/take, TC"], "f6")
    d.node("csv", 780, 280, 220, 60, "Match shoot log", ["CSV for the edit"], "log")
    d.node("exp", 560, 370, 200, 60, "Explorer", ["rename, move, trash"], "f6")
    d.node("play", 780, 370, 220, 60, "Play a WAV", ["through the Atom output"], "f6")
    d.node("jam", 340, 480, 360, 60, "Timecode jam", ["LTC out of the headphone jack, F6 in Int Free Run"], "time")
    d.arrow("q", "l", "frc", "t", "F-Control", "f6", via=[(260, 65)])
    d.arrow("q", "r", "card", "t", "card reader", "f6", via=[(780, 65)])
    d.arrow("frc", "b", "rec", "t", "", "f6")
    d.arrow("frc", "b", "st", "t", "", "f6")
    d.arrow("st", "b", "blk", "t", "", "bad")
    d.arrow("rec", "b", "usr", "t", "", "log", dash=True)
    d.arrow("card", "b", "scan", "t", "", "f6")
    d.arrow("scan", "r", "csv", "l", "", "log")
    d.arrow("card", "l", "exp", "l", "", "f6", via=[(540, 195), (540, 400)], dash=True)
    d.arrow("exp", "r", "play", "l", "", "f6", dash=True)
    page(10, "Zoom F6", "Two USB modes, chosen on the F6. Timecode comes in by audio, not USB.", d,
         ["FRC-8 order that works: press connect on the F6 with the cable out, then plug it in.",
          "The card is only mounted during each operation; delete moves to F6_TRASH; a backup stick is never mistaken for the card (USB vendor check).",
          "If the F6 does not confirm REC/STOP within 1.5 s, a FAULT is raised."], "f6")


# ---------------------------------------------------------------- 11 logs
def logs():
    d = Diagram(580)
    d.node("start", 20, 30, 210, 60, "First start", ["opens take N"], "rec")
    d.node("take", 280, 30, 230, 60, "Take N open", ["take_aberto.json saved"], "log")
    d.node("stop", 560, 30, 210, 60, "Last stop", ["closes take N"], "rec")
    d.node("next", 820, 30, 200, 60, "Next take", ["N + 1"], "log")
    d.node("rs", 300, 140, 180, 70, "Bridge restarts", ["within 180 s, a start", "resumes the same take"], "sys")
    d.node("mk", 20, 140, 210, 70, "Marks and notes", ["G good · N no-good · M mark", "notes on the take"], "log")
    d.node("sl", 560, 140, 210, 70, "Slate between takes", ["scene, reel, take → BMPCC", "and mobiles every 5 s"], "log")
    d.node("csv", 280, 270, 230, 60, "shoot_log.csv", ["one line per event"], "log")
    d.node("ev", 560, 270, 210, 60, "events.log", ["what happened, trimmed"], "log")
    d.node("arc", 20, 270, 210, 60, "New log", ["archives with date"], "log")
    d.node("send", 280, 390, 230, 66, "Send (LOGS > SFTP)", ["progress bar · DONE / FAILED"], "net")
    d.node("srv", 560, 390, 210, 66, "SFTP server", ["remote folder made if missing"], "net")
    d.node("what", 20, 390, 210, 66, "What is sent", ["current logs (time-stamped)", "+ archives not there yet"], "net")
    d.node("usbk", 820, 270, 200, 60, "USB stick", ["config backup (no secrets)"], "log")
    for a, b in (("start", "take"), ("take", "stop"), ("stop", "next")):
        d.arrow(a, "r", b, "l", "", "log")
    d.arrow("take", (390, 90), "rs", (390, 140), "", "sys", dash=True)
    d.arrow("mk", "r", "take", (320, 90), "", "log", via=[(270, 175), (270, 115), (320, 115)], dash=True)
    d.arrow("stop", "b", "sl", "t", "", "log")
    d.arrow("take", (500, 90), "csv", (500, 330), "", "log")
    d.arrow("csv", "b", "send", "t", "", "net")
    d.arrow("ev", (620, 330), "send", (480, 390), "", "net", via=[(620, 360), (480, 360)])
    d.arrow("send", "r", "srv", "l", "SFTP", "net")
    d.arrow("arc", "r", "csv", "l", "", "log", dash=True)
    page(11, "Shoot log, takes, slate, send logs", "Every start and stop, from every device, lands in one CSV with the take number and TC.", d,
         ["Take numbers restart at 1 only when you start a new shoot log (the old one is archived, never deleted).",
          "A REC/STOP pressed by the operator after a restart always opens a new take instead of resuming.",
          "The SFTP password is never shown or exported; it stays in a 0600 file on the Atom."], "log")


# ---------------------------------------------------------------- 12 surfaces
def superficies():
    d = Diagram(580)
    d.node("con", 20, 40, 230, 80, "Console", ["R group · T one · SPACE arm", "F2 rename · [ ] move section"], "sys")
    d.node("web", 20, 160, 230, 80, "Web page", ["target buttons, REC", "per-camera cards"], "net")
    d.node("usb", 20, 400, 230, 80, "USB controllers", ["mapped keys/notes →", "rec, target, devrec, mark"], "sys")
    d.node("api", 400, 200, 240, 90, "Bridge API", ["/api/rec (group)", "/api/devices/<id>/rec (one)", "/api/rec/targets {id, on}"], "rec")
    d.node("fb", 790, 60, 230, 80, "Feedback", ["status polled ~1 s", "older replies ignored"], "ok")
    d.node("led", 790, 340, 230, 80, "Controller LEDs", ["MIDI note/CC follows", "rec / armed state"], "sys")
    for k in ("con", "web", "usb"):
        d.arrow(k, "r", "api", "l", "", "rec")
    d.arrow("api", "r", "fb", "l", "", "ok")
    d.arrow("api", "r", "led", "l", "", "sys")
    page(12, "Control surfaces", "Three ways to drive the rig, one set of rules in the bridge.", d,
         ["Fixed in this round: a status read that left before a tap and arrived after it no longer puts the old state back (page, console).",
          "Console menus: PgUp/PgDn move an item, [ and ] move a whole section, F2 renames an item or a section (kept across updates).",
          "Mappings for USB controllers live in usb_map.json and follow the device to any port."], "net")


# ---------------------------------------------------------------- 13 update
def update():
    d = Diagram(580)
    d.node("ask", 20, 30, 220, 70, "Update from GitHub", ["GLOBAL SETTINGS"], "sys")
    d.node("cmp", 280, 30, 230, 70, "Compare", ["GitHub vs clone vs installed"], "sys", "diamond")
    d.node("pull", 560, 30, 200, 70, "git pull", ["fast-forward only"], "sys")
    d.node("inst", 800, 30, 220, 70, "install_atom.sh", ["7 steps, logged"], "sys")
    steps = ["1 Packages", "2 Code to /opt (+ backup copy)", "3 Wi-Fi names", "4 Services", "5 Camera AP",
             "6 Sound + boot menu", "7 Start"]
    for i, s in enumerate(steps):
        d.node("s%d" % i, 800, 120 + i * 50, 220, 40, s, [], "sys")
        if i:
            d.arrow("s%d" % (i - 1), "b", "s%d" % i, "t", "", "sys")
    d.arrow("inst", "b", "s0", "t", "", "sys")
    d.node("wait", 500, 380, 240, 66, "Recording?", ["wait for STOP before restart"], "rec", "diamond")
    d.node("rst", 500, 490, 240, 60, "Restart bridge", ["check it answers"], "sys")
    d.node("bad", 790, 490, 230, 60, "Not answering", ["restore previous copy"], "bad")
    d.node("ver", 250, 490, 210, 60, "version.json written", ["Version page shows it"], "ok")
    d.node("re", 20, 490, 200, 60, "Console reopens", ["by itself"], "ok")
    d.node("pend", 280, 150, 230, 66, "Pulled, not installed?", ["offer to install now"], "log")
    d.node("gw", 20, 150, 220, 70, "Gateway firmware", ["always through the Atom", "C3/S3 over Wi-Fi, or USB"], "gw")
    d.arrow("ask", "r", "cmp", "l", "", "sys")
    d.arrow("cmp", "r", "pull", "l", "new", "sys")
    d.arrow("pull", "r", "inst", "l", "", "sys")
    d.arrow("cmp", "b", "pend", "t", "", "log", dash=True)
    d.arrow("s6", "l", "wait", "r", "", "rec", via=[(780, 433), (780, 413)])
    d.arrow("wait", "b", "rst", "t", "no / after STOP", "rec")
    d.arrow("rst", "r", "bad", "l", "", "bad", dash=True)
    d.arrow("rst", "l", "ver", "r", "ok", "ok")
    d.arrow("ver", "l", "re", "r", "", "ok")
    page(13, "Update from GitHub and gateway firmware", "The Atom updates itself and then the gateways; nothing is installed half-way.", d,
         ["The bridge is never restarted in the middle of a take: the installer waits for STOP (NO_WAIT_REC=1 skips it).",
          "The USB service stays off after an update if you switched it off.",
          "Gateway firmware: GitHub builds it, the Atom keeps it and sends it to each C3/S3 (Wi-Fi OTA, or USB for a blank board)."], "sys")


# ---------------------------------------------------------------- 14 access
def acesso():
    d = Diagram(580)
    d.node("req", 20, 40, 220, 60, "Request to :8080", [], "net")
    d.node("web", 290, 30, 220, 80, "Web page ON?", ["GLOBAL SETTINGS > Services"], "net", "diamond")
    d.node("off", 560, 40, 230, 60, "Only the Atom itself", ["others: 'turned off' notice"], "bad")
    d.node("tr", 290, 160, 220, 80, "Trusted?", ["the Atom or Tailscale"], "net", "diamond")
    d.node("in", 560, 170, 230, 60, "Allowed", [], "ok")
    d.node("pin", 290, 300, 220, 70, "PIN login", ["4–12 digits · 5 tries → 15 min lock"], "sys")
    d.node("ses", 560, 305, 230, 60, "Session cookie", ["12 h · HttpOnly"], "ok")
    d.node("org", 20, 300, 220, 70, "POST from another site", ["refused (Origin check)"], "bad")
    d.node("sv", 820, 40, 200, 140, "Services", ["Web page for others", "USB controllers", "on / off, kept"], "sys")
    d.node("warn", 820, 230, 200, 200, "Warnings", ["camera / Atom battery", "card space", "disk space", "bridge memory > 500 MB",
                                                   "C3 memory low", "TC drift"], "log")
    d.node("sec", 20, 430, 770, 70, "Secrets", ["Wi-Fi passwords only in 0600 files or by serial · gateway token only over USB/serial ·",
                                               "PIN stored as PBKDF2 hash · SFTP password never leaves the Atom"], "sys")
    d.arrow("req", "r", "web", "l", "", "net")
    d.arrow("web", "r", "off", "l", "no", "bad")
    d.arrow("web", "b", "tr", "t", "yes", "net")
    d.arrow("tr", "r", "in", "l", "yes", "ok")
    d.arrow("tr", "b", "pin", "t", "no", "sys")
    d.arrow("pin", "r", "ses", "l", "", "ok")
    d.arrow("ses", "t", "in", "b", "", "ok")
    page(14, "Access, services and warnings", "Who can drive the rig, what can be switched off, and what the bridge warns about.", d,
         ["Warnings are raised once per condition and cleared when it recovers; they appear on the page and the console."], "sys")


# ---------------------------------------------------------------- 15 demo
def demo():
    d = Diagram(520)
    d.node("on", 20, 40, 230, 70, "Demo mode ON", ["GLOBAL SETTINGS > TRAINING"], "sys")
    d.node("sim", 300, 40, 260, 70, "Simulated gateways", ["S3 on :8170 (DEMO-1, DEMO-2)", "C3 on :8172 (DEMO-BM)"], "gw")
    d.node("rig", 610, 40, 230, 70, "Joined to the rig", ["header shows DEMO MODE"], "sys")
    d.node("use", 300, 170, 260, 80, "Use as on set", ["arm with SPACE, R / REC,", "T on one, marks, notes"], "rec")
    d.node("log", 610, 170, 230, 80, "Shoot log", ["lines with the DEMO names"], "log")
    d.node("off", 300, 310, 260, 70, "Demo mode OFF", ["refused while recording"], "sys")
    d.node("clean", 610, 310, 230, 70, "Removed", ["from the rig and the targets"], "ok")
    d.arrow("on", "r", "sim", "l", "", "sys")
    d.arrow("sim", "r", "rig", "l", "", "gw")
    d.arrow("rig", "b", "use", "t", "", "sys", via=[(725, 140), (430, 140)])
    d.arrow("use", "r", "log", "l", "", "log")
    d.arrow("use", "b", "off", "t", "", "sys")
    d.arrow("off", "r", "clean", "l", "", "ok")
    page(15, "Demo mode", "Practice the whole flow with no equipment: simulated gateways answer like the real ones.", d,
         ["The simulators run inside the bridge on 127.0.0.1 only; nothing goes on the camera Wi-Fi.",
          "The DEMO gateway line in GATEWAYS is not a camera: the DEMO-1 / DEMO-2 / DEMO-BM rows are in the device list."], "sys")


# ---------------------------------------------------------------- 16 code check
def verificacao():
    fixed = [
        ("Interfaces renamed by sed over bridge.py", "Installer rewrote every matching word (it had damaged a comment). Now the names come from ap.conf."),
        ("SFTP send", "No feedback; archives re-sent each time. Now progress bar, result, archives already on the server skipped."),
        ("Stale reads after a command", "Page, console, GoPro, mobile, Canon: an old reply no longer undoes a new command."),
        ("Group REC with disarmed devices", "Next press stops everything recording, even devices disarmed mid-take."),
        ("Per-device REC", "Never stops other devices; second GoPro right after the first goes the right way."),
        ("Update looked stuck", "Version is recorded; 'pulled but not installed' is detected and offered."),
        ("Console after update", "Reopens by itself even with a page open."),
        ("Small leftovers", "Unused import/constant, F6 notes as raw numbers, stale TODOs and a wrong Bluetooth comment."),
    ]
    left = [
        ("Diagnostic tools in the repo root", "ltc_gen.py, analyze_f6_take.py (old F6 Ext Auto Rec), f6_s3.py + s3_frc8/ (F6 via S3), f6_frc.py, probes. Kept on purpose; could move to tools/."),
        ("Bluetooth adapter chosen by number", "hci0/hci1 change with hardware (ThinkPad). Choosing the dongle by its address would be safer."),
        ("Old 'Pi' wording in code comments", "History only; no visible text says Pi. Route name /api/time/ltc/set_system_clock is internal."),
    ]
    html_ok = "".join("<tr><td class='k ok'>FIXED</td><td><b>%s</b><br>%s</td></tr>" % (esc(a), esc(b)) for a, b in fixed)
    html_lf = "".join("<tr><td class='k lf'>OPEN</td><td><b>%s</b><br>%s</td></tr>" % (esc(a), esc(b)) for a, b in left)
    PAGES.append('''<section class="page">
  <header><div class="num" style="background:%s">16</div><div><h1>Code check: what was found and fixed</h1>
  <p>Static analysis (pyflakes, vulture, shellcheck), unused routes and JS, docs vs code, and a read of the risky paths.</p></div></header>
  <div class="cols"><table class="tb">%s</table><table class="tb">%s
  <tr><td class='k ok'>TESTS</td><td><b>111 automated tests</b><br>All pass locally and on GitHub on every commit.</td></tr></table></div>
  <footer>Bridge-Control · visual workflows · 2026-10-27</footer>
</section>''' % (COR["ok"], html_ok, html_lf))


for f in (capa, hardware, software, arranque, grupo, por_aparelho, tempo, bmpcc, gopro, f6, logs, superficies, update,
          acesso, demo, verificacao):
    f()

CSS = '''
@page { size: A4 landscape; margin: 0 }
* { box-sizing: border-box }
body { margin: 0; font-family: "DejaVu Sans", "Liberation Sans", Arial, sans-serif; color: #222; }
.page { width: 297mm; height: 210mm; padding: 9mm 11mm 7mm; position: relative; page-break-after: always; overflow: hidden }
header { display: flex; gap: 12px; align-items: center; margin-bottom: 6px }
.num { width: 38px; height: 38px; border-radius: 9px; color: #fff; font-weight: 700; font-size: 18px;
       display: flex; align-items: center; justify-content: center; flex: none }
h1 { font-size: 21px; margin: 0 }
header p { margin: 2px 0 0; font-size: 12.5px; color: #555 }
.dia svg { display: block; margin: 4px auto 0 }
.nt { font-size: 13px; font-weight: 700 }
.nl { font-size: 11px }
.al { font-size: 10.5px; font-weight: 700 }
.zl { font-size: 12px; font-weight: 700; letter-spacing: .04em }
.idx { font-size: 15px; fill: #333 }
.notes { margin: 6px 0 0 4px; padding-left: 16px; font-size: 11.5px; color: #333; columns: 1 }
.notes li { margin: 2px 0 }
footer { position: absolute; bottom: 5mm; right: 11mm; font-size: 9.5px; color: #999 }
.cols { display: flex; gap: 16px; margin-top: 10px }
.tb { border-collapse: separate; border-spacing: 0 6px; font-size: 11.5px; flex: 1 }
.tb td { vertical-align: top; padding: 6px 8px; background: #f6f7f9; border-radius: 6px }
.tb td.k { font-weight: 700; font-size: 10px; color: #fff; width: 62px; text-align: center }
.k.ok { background: #2e8b57 } .k.lf { background: #d0701c }
'''
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows.html")
with open(out, "w") as f:
    f.write("<!doctype html><html><head><meta charset='utf-8'><title>Bridge-Control workflows</title><style>%s</style></head>"
            "<body>%s</body></html>" % (CSS, "\n".join(PAGES)))
print(out, len(PAGES), "pages")

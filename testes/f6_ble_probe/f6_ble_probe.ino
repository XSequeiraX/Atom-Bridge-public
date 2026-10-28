// F6 BLE probe (ESP32-S3, Arduino + NimBLE): faz-se passar pelo modulo Bluetooth do Zoom F6 (BTA-1) e regista no
// terminal serie TUDO o que o iPhone (app F Control) fizer: ligar, MTU, subscrever, ler, escrever.
// Os UUIDs e os bytes do anuncio vieram do firmware do F6: nada disto foi confirmado contra um BTA-1 real.
// Nao tem Wi-Fi, nem GoPro, nem F6 por USB: e uma experiencia a parte do gateway (gopro_gw).
//
// Serie 115200 (a porta USB nativa do S3). Comandos (uma linha, terminada em Enter):
//   help                 os comandos
//   stats                contadores e ligacoes
//   name <texto>         o nome anunciado (fica guardado; por omissao F6_B13A1F59C25B)
//   adv service|orig     service = flags + UUID do servico; orig = os bytes iBeacon do firmware (fica guardado)
//   echo on|off          responder no TX com o que chegou (por omissao off)
//   send tx|flow <hex>   notifica estes bytes ao iPhone (para testar respostas), ex.: send tx 01 02 ff
//   bond on|off          pedir emparelhamento (por omissao off; fica guardado)
//   scan [segundos] [texto]  procura aparelhos BLE em redor e regista cada anuncio completo (hex); texto filtra pelo nome.
//                        Serve para ver o que o F6 / BTA-1 reais e o iPhone (com a app aberta) anunciam. 'scan stop' pára.
//   state                envia agora ao iPhone/Android a aceitacao (80 00 00) e o estado do F6 (ver DUMP_HEX)
//   auto on|off          quando a app escreve b6 ... (pedido de ligacao), responder 80 00 00 + estado (por omissao on)
//   framing msg|stream   msg = uma notificacao por mensagem MIDI; stream = fluxo de bytes em blocos de 20 (por omissao msg)
//   try <hex> ; <hex> ... envia estas notificacoes uma a uma (pausa = pace) e diz se a app escreveu algo novo logo a seguir
//   sweep                como try, com uma lista de candidatas ja feita (respostas possiveis a ba 00 / 91 00)
//   pace <ms>            pausa entre candidatas (por omissao 1500)
//   connect [texto]      o S3 liga-se, como cliente, ao F6/BTA-1 REAL (nome com "F6_" ou o texto), lista os servicos e
//                        subscreve todas as notificacoes. O iPhone/Android tem de estar desligado do aparelho real.
//   cw <car> <hex>       escreve no aparelho real; <car> = indice da lista, ou rx / tx / flow. Ex.: cw rx b6 00
//   cr <car>             le uma caracteristica do aparelho real
//   cdisc                desliga do aparelho real
//   reboot
#include <Arduino.h>
#include <Preferences.h>
#include <NimBLEDevice.h>
#include <set>
#include <string>
#include <vector>

static const char *VERSAO = "f6_ble_probe 6";

static const char *U_SVC  = "5e981594-cd7d-4201-86b9-560cf375abae";
static const char *U_TX   = "4076b47f-130b-407c-a2f4-d52945cb84b5";
static const char *U_RX   = "cbeb8809-028a-4195-b4a5-762ff6e500a9";
static const char *U_FLOW = "f0262f5f-3eba-4719-a265-31f126a9c66c";

// Bytes do firmware: o iBeacon (fabricante 0x00D2, UUID 585cde93-..., major 1, minor 2, potencia -59) e o aviso do RW-BLE
static const uint8_t IBEACON[] = {0x1a, 0xff, 0xd2, 0x00, 0x02, 0x15, 0x58, 0x5c, 0xde, 0x93, 0x1b, 0x01, 0x42, 0xcc, 0x9a, 0x13,
                                  0x25, 0x00, 0x9b, 0xed, 0xc6, 0x5e, 0x00, 0x01, 0x00, 0x02, 0xc5};
static const uint8_t SCANRSP_RWBLE[] = {0x09, 0xff, 0x00, 0x60, 'R', 'W', '-', 'B', 'L', 'E'};

// O que um F6 real manda, por USB-MIDI, a seguir ao pedido de ligacao do FRC-8 (captura de 2026-10-19 com o f6_frc.py: luzes
// e SysEx de estado). Parte-se do principio de que a app F Control espera o mesmo fluxo pelo BLE; nao esta confirmado.
static const char *DUMP_HEX[] = {
  "90 00 00", "90 18 00", "90 01 00", "90 19 00", "90 02 00", "90 1a 00", "90 03 00", "90 1b 00", "90 04 00", "90 1c 00",
  "90 05 00", "90 1d 00", "90 06 00", "90 1e 00", "90 07 00", "90 1f 00", "90 08 00", "90 09 00", "90 0a 00", "90 0b 00",
  "90 0c 00", "90 0d 00", "90 0e 00", "90 0f 00", "90 27 00", "90 26 7f", "90 28 00", "90 29 00", "90 10 7f", "90 11 00",
  "90 12 00",
  "f0 52 00 74 00 00 00 63 10 09 30 07 04 f7", "f0 52 00 74 00 00 00 63 11 00 f7", "f0 52 00 74 00 00 00 63 13 3c f7",
  "f0 52 00 74 00 00 00 63 16 00 f7", "f0 52 00 74 00 00 00 63 0d 00 f7", "f0 52 00 74 00 00 00 63 01 f7",
  "f0 52 00 74 00 00 00 63 02 f7", "f0 52 00 74 00 00 00 63 0e 00 64 f7"};
static const size_t DUMP_N = sizeof(DUMP_HEX) / sizeof(DUMP_HEX[0]);

static Preferences prefs;
static String nome = "F6_B13A1F59C25B";      // o nome que a app F Control mostra para o F6 (F6_ + 12 hex)
static bool advOrig = false, eco = false, bond = false, autoEstado = true, framingStream = false;
static NimBLECharacteristic *cTx, *cRx, *cFlow;
static uint32_t nLig = 0, nEsc = 0, nBytes = 0, nLe = 0;
static int ligados = 0;

static void log(const char *fmt, ...) {
  char b[300];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(b, sizeof b, fmt, ap);
  va_end(ap);
  Serial.printf("[%8lu] %s\n", (unsigned long)millis(), b);
}

static String hexs(const uint8_t *d, size_t n) {
  String s;
  char t[4];
  for (size_t i = 0; i < n; i++) {
    snprintf(t, sizeof t, "%02x", d[i]);
    if (i) s += ' ';
    s += t;
  }
  return s;
}

static String ascii(const uint8_t *d, size_t n) {
  String s;
  for (size_t i = 0; i < n; i++) s += (d[i] >= 32 && d[i] < 127) ? (char)d[i] : '.';
  return s;
}

static const char *nomeDe(NimBLECharacteristic *c) {
  if (c == cTx) return "TX";
  if (c == cRx) return "RX";
  if (c == cFlow) return "FLOW";
  return "?";
}

static void anunciar() {
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->stop();
  NimBLEAdvertisementData ad, rsp;
  if (advOrig) {
    ad.addData(IBEACON, sizeof IBEACON);
    rsp.addData(SCANRSP_RWBLE, sizeof SCANRSP_RWBLE);
    rsp.setName(std::string(nome.c_str()));
  } else {
    ad.setFlags(0x06);
    ad.setCompleteServices(NimBLEUUID(U_SVC));
    rsp.setName(std::string(nome.c_str()));
    rsp.addData(SCANRSP_RWBLE, sizeof SCANRSP_RWBLE);
  }
  adv->setAdvertisementData(ad);
  adv->setScanResponseData(rsp);
  adv->start();
  log("ADV %s name=\"%s\"", advOrig ? "orig" : "service", nome.c_str());
}

// ---- estado do F6 em notificacoes (enviado a ritmo no loop(): nunca bloquear a tarefa do BLE) -----------------------------
static std::vector<std::vector<uint8_t>> fila;
static std::vector<uint16_t> atraso;     // ms de espera ANTES de cada bloco
static volatile bool pedirEstado = false;   // posto pela tarefa do BLE (onWrite); a fila so se mexe no loop()
static bool tryAtivo = false;
static int tryN = 0;
static uint32_t tryMs = 0, paceMs = 1500;
static size_t filaIdx = 0;
static uint32_t filaProxMs = 0;

static std::vector<uint8_t> hexParaBytes(const char *h) {
  std::vector<uint8_t> b;
  const char *p = h;
  while (*p) {
    while (*p == ' ') p++;
    if (!*p) break;
    b.push_back((uint8_t)strtol(p, nullptr, 16));
    while (*p && *p != ' ') p++;
  }
  return b;
}

static void iniciarTry(const std::vector<String> &lista) {
  fila.clear();
  atraso.clear();
  filaIdx = 0;
  tryAtivo = true;
  tryN = 0;
  for (size_t i = 0; i < lista.size(); i++) {
    std::vector<uint8_t> b = hexParaBytes(lista[i].c_str());
    if (b.empty()) continue;
    fila.push_back(b);
    atraso.push_back(fila.size() == 1 ? 0 : (uint16_t)paceMs);
  }
  filaProxMs = millis();
  log("TRY queued: %u candidates, %u ms apart", (unsigned)fila.size(), (unsigned)paceMs);
}

static void iniciarEstado(bool comAceite) {
  tryAtivo = false;
  fila.clear();
  atraso.clear();
  filaIdx = 0;
  if (comAceite) {
    fila.push_back({0x80, 0x00, 0x00});
    atraso.push_back(0);
  }
  if (!framingStream) {
    for (size_t i = 0; i < DUMP_N; i++) {
      fila.push_back(hexParaBytes(DUMP_HEX[i]));
      atraso.push_back(i == 0 ? 100 : 10);
    }
  } else {
    std::vector<uint8_t> blk;
    bool primeiro = true;
    for (size_t i = 0; i < DUMP_N; i++) {
      std::vector<uint8_t> m = hexParaBytes(DUMP_HEX[i]);
      for (uint8_t x : m) {
        blk.push_back(x);
        if (blk.size() == 20) {
          fila.push_back(blk);
          atraso.push_back(primeiro ? 100 : 10);
          primeiro = false;
          blk.clear();
        }
      }
    }
    if (!blk.empty()) { fila.push_back(blk); atraso.push_back(primeiro ? 100 : 10); }
  }
  filaProxMs = millis() + (atraso.empty() ? 0 : atraso[0]);
  log("STATE queued: %u notifications (%s%s)", (unsigned)fila.size(), framingStream ? "stream" : "msg", comAceite ? ", with 80 00 00" : "");
}

static void bombearEstado() {
  if (filaIdx >= fila.size() || (int32_t)(millis() - filaProxMs) < 0) return;
  bool ok = cTx->notify(fila[filaIdx].data(), fila[filaIdx].size());
  if (!ok) {
    log("STATE not sent (nobody subscribed to TX); stopped");
    filaIdx = fila.size();
    return;
  }
  if (tryAtivo) {
    tryN = (int)filaIdx + 1;
    tryMs = millis();
    log("TRY %d/%u sent: %s", tryN, (unsigned)fila.size(), hexs(fila[filaIdx].data(), fila[filaIdx].size()).c_str());
  }
  filaIdx++;
  if (filaIdx < fila.size()) filaProxMs = millis() + atraso[filaIdx];
  else if (tryAtivo) log("TRY done");
  else log("STATE sent (%u notifications)", (unsigned)fila.size());
}

class ServerCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *s, NimBLEConnInfo &ci) override {
    nLig++;
    ligados = s->getConnectedCount();
    log("CONNECT handle=%u addr=%s itvl=%u lat=%u to=%u", ci.getConnHandle(), ci.getAddress().toString().c_str(),
        ci.getConnInterval(), ci.getConnLatency(), ci.getConnTimeout());
  }
  void onDisconnect(NimBLEServer *s, NimBLEConnInfo &ci, int reason) override {
    ligados = s->getConnectedCount();
    log("DISCONNECT handle=%u reason=%d (0x%02x)", ci.getConnHandle(), reason, reason);
    anunciar();
  }
  void onMTUChange(uint16_t mtu, NimBLEConnInfo &ci) override { log("MTU handle=%u mtu=%u", ci.getConnHandle(), mtu); }
  void onConnParamsUpdate(NimBLEConnInfo &ci) override {
    log("CONN_PARAMS handle=%u itvl=%u lat=%u to=%u", ci.getConnHandle(), ci.getConnInterval(), ci.getConnLatency(), ci.getConnTimeout());
  }
  void onAuthenticationComplete(NimBLEConnInfo &ci) override {
    log("AUTH handle=%u encrypted=%d authenticated=%d bonded=%d", ci.getConnHandle(), ci.isEncrypted(), ci.isAuthenticated(), ci.isBonded());
  }
  void onIdentity(NimBLEConnInfo &ci) override { log("IDENTITY handle=%u id=%s", ci.getConnHandle(), ci.getIdAddress().toString().c_str()); }
};

class CharCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c, NimBLEConnInfo &ci) override {
    NimBLEAttValue v = c->getValue();
    nEsc++;
    nBytes += v.size();
    log("WRITE  %-4s len=%u hex=%s ascii=\"%s\"", nomeDe(c), (unsigned)v.size(), hexs(v.data(), v.size()).c_str(),
        ascii(v.data(), v.size()).c_str());
    if (tryAtivo && tryN > 0 && millis() - tryMs < 1400) log("  ^ new write %u ms after TRY %d  <<< the app reacted", (unsigned)(millis() - tryMs), tryN);
    if (autoEstado && c == cRx && v.size() && v.data()[0] == 0xB6) {
      log("AUTO: link request (b6) -> accept + F6 state");
      pedirEstado = true;
    }
    if (eco && v.size() && cTx) {
      bool ok = cTx->notify(v.data(), v.size(), ci.getConnHandle());
      if (!ok) log("echo on TX: not sent (the phone has not subscribed to TX)");
    }
  }
  void onRead(NimBLECharacteristic *c, NimBLEConnInfo &ci) override {
    nLe++;
    NimBLEAttValue v = c->getValue();
    log("READ   %-4s handle=%u len=%u hex=%s", nomeDe(c), ci.getConnHandle(), (unsigned)v.size(), hexs(v.data(), v.size()).c_str());
  }
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &ci, uint16_t sub) override {
    log("SUBSCRIBE %-4s handle=%u value=%u (%s)", nomeDe(c), ci.getConnHandle(), sub,
        sub == 0 ? "off" : sub == 1 ? "notify" : sub == 2 ? "indicate" : "notify+indicate");
  }
};

static ServerCb serverCb;
static CharCb charCb;

// ---- scan: ver o que os outros aparelhos anunciam -------------------------------------------------------------------
static std::set<std::string> vistos;
static String filtroScan;

static bool scanQuieto = false;

class ScanCb : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice *d) override {
    if (scanQuieto) return;
    const std::vector<uint8_t> &pl = d->getPayload();
    String hex = hexs(pl.data(), pl.size());
    std::string chave = d->getAddress().toString() + "|" + hex.c_str();
    if (vistos.count(chave)) return;
    if (vistos.size() > 400) vistos.clear();
    vistos.insert(chave);
    String nm = d->haveName() ? String(d->getName().c_str()) : String("");
    if (filtroScan.length()) {
      String a = nm, b = filtroScan;
      a.toLowerCase();
      b.toLowerCase();
      if (a.indexOf(b) < 0) return;
    }
    log("SCAN addr=%s type=%u rssi=%d conn=%d name=\"%s\" payload=%s", d->getAddress().toString().c_str(), d->getAddressType(), d->getRSSI(),
        d->isConnectable(), nm.c_str(), hex.c_str());
  }
  void onScanEnd(const NimBLEScanResults &r, int reason) override { log("SCAN end (%d devices seen)", (int)r.getCount()); }
};
static ScanCb scanCb;

static void iniciarScan(uint32_t seg, const String &filtro) {
  NimBLEScan *sc = NimBLEDevice::getScan();
  sc->stop();
  vistos.clear();
  filtroScan = filtro;
  sc->setScanCallbacks(&scanCb, true);
  sc->setActiveScan(true);
  sc->setInterval(100);
  sc->setWindow(99);
  sc->clearResults();
  bool ok = sc->start(seg * 1000, false, false);
  log("SCAN %s for %us%s%s", ok ? "started" : "FAILED", (unsigned)seg, filtro.length() ? " name contains " : "", filtro.c_str());
}

// ---- cliente: ligar ao aparelho real e ver o que ele responde -----------------------------------------------------------
static NimBLEClient *cli = nullptr;
static std::vector<NimBLERemoteCharacteristic *> rch;

static int indiceDe(NimBLERemoteCharacteristic *c) {
  for (size_t i = 0; i < rch.size(); i++)
    if (rch[i] == c) return (int)i;
  return -1;
}

static void notifCb(NimBLERemoteCharacteristic *c, uint8_t *d, size_t n, bool isNotify) {
  log("%s [%d] %s len=%u hex=%s ascii=\"%s\"", isNotify ? "NOTIFY" : "INDICATE", indiceDe(c), c->getUUID().toString().c_str(), (unsigned)n,
      hexs(d, n).c_str(), ascii(d, n).c_str());
}

class CliCb : public NimBLEClientCallbacks {
  void onConnect(NimBLEClient *) override { log("CLIENT connected"); }
  void onConnectFail(NimBLEClient *, int r) override { log("CLIENT connect failed reason=%d (0x%x)", r, r); }
  void onDisconnect(NimBLEClient *, int r) override { log("CLIENT disconnected reason=%d (0x%x)", r, r); }
};
static CliCb cliCb;

static NimBLERemoteCharacteristic *escolher(const String &sel) {
  String t = sel;
  t.toLowerCase();
  const char *u = t == "rx" ? U_RX : t == "tx" ? U_TX : t == "flow" ? U_FLOW : nullptr;
  if (u) {
    for (auto *c : rch)
      if (c->getUUID() == NimBLEUUID(u)) return c;
    return nullptr;
  }
  int i = t.toInt();
  return (i >= 0 && (size_t)i < rch.size() && (t[0] >= '0' && t[0] <= '9')) ? rch[i] : nullptr;
}

static void ligarReal(String filtro) {
  if (cli && cli->isConnected()) { log("CLIENT already connected (cdisc first)"); return; }
  if (!filtro.length()) filtro = "F6_";
  filtro.toLowerCase();
  NimBLEScan *sc = NimBLEDevice::getScan();
  sc->stop();
  scanQuieto = true;
  sc->setScanCallbacks(&scanCb, true);
  sc->setActiveScan(true);
  sc->setInterval(100);
  sc->setWindow(99);
  sc->clearResults();
  log("CLIENT scanning 6 s for a connectable device whose name contains \"%s\"", filtro.c_str());
  NimBLEScanResults r = sc->getResults(6000, false);
  scanQuieto = false;
  const NimBLEAdvertisedDevice *alvo = nullptr;
  for (int i = 0; i < r.getCount(); i++) {
    const NimBLEAdvertisedDevice *d = r.getDevice(i);
    if (!d || !d->haveName() || !d->isConnectable()) continue;
    String nm = String(d->getName().c_str());
    nm.toLowerCase();
    if (nm.indexOf(filtro) < 0) continue;
    if (!alvo || d->getRSSI() > alvo->getRSSI()) alvo = d;
  }
  if (!alvo) { log("CLIENT no connectable device found (is it connected to a phone?)"); return; }
  log("CLIENT target %s \"%s\" rssi=%d", alvo->getAddress().toString().c_str(), alvo->getName().c_str(), alvo->getRSSI());
  if (!cli) {
    cli = NimBLEDevice::createClient();
    cli->setClientCallbacks(&cliCb, false);
    cli->setConnectTimeout(10000);
  }
  if (!cli->connect(alvo)) { log("CLIENT connect failed"); return; }
  rch.clear();
  for (NimBLERemoteService *sv : cli->getServices(true)) {
    log("SERVICE %s", sv->getUUID().toString().c_str());
    for (NimBLERemoteCharacteristic *c : sv->getCharacteristics(true)) {
      int idx = (int)rch.size();
      rch.push_back(c);
      log("  [%d] %s props:%s%s%s%s%s", idx, c->getUUID().toString().c_str(), c->canRead() ? " read" : "", c->canWrite() ? " write" : "",
          c->canWriteNoResponse() ? " write-no-resp" : "", c->canNotify() ? " notify" : "", c->canIndicate() ? " indicate" : "");
      if (c->canNotify() || c->canIndicate()) {
        bool ok = c->subscribe(c->canNotify(), notifCb);
        log("      subscribe %s", ok ? "ok" : "FAILED");
      }
    }
  }
  log("CLIENT ready. Use: cw rx b6 00   (or cw <index> <hex>)");
}

static std::vector<String> palavras(String l) {
  std::vector<String> w;
  l.trim();
  while (l.length()) {
    int i = l.indexOf(' ');
    if (i < 0) { w.push_back(l); break; }
    w.push_back(l.substring(0, i));
    l = l.substring(i + 1);
    l.trim();
  }
  return w;
}

static void comando(String l) {
  std::vector<String> w = palavras(l);
  if (w.empty()) return;
  String c = w[0];
  c.toLowerCase();
  if (c == "help") {
    Serial.println("help | stats | name <text> | adv service|orig | echo on|off | bond on|off | send tx|flow <hex...> | state | try <hex> ; <hex> | sweep | pace <ms> | auto on|off | framing msg|stream | scan [seconds] [name text] | scan stop | connect [text] | cw <rx|tx|flow|n> <hex...> | cr <rx|tx|flow|n> | cdisc | reboot");
  } else if (c == "stats") {
    log("STATS connections=%lu writes=%lu bytes=%lu reads=%lu now=%d echo=%s adv=%s name=\"%s\" bond=%s auto=%s framing=%s", (unsigned long)nLig,
        (unsigned long)nEsc, (unsigned long)nBytes, (unsigned long)nLe, ligados, eco ? "on" : "off", advOrig ? "orig" : "service",
        nome.c_str(), bond ? "on" : "off", autoEstado ? "on" : "off", framingStream ? "stream" : "msg");
  } else if (c == "name" && w.size() >= 2) {
    String n = l.substring(l.indexOf(' ') + 1);
    n.trim();
    if (n.length() > 20) n = n.substring(0, 20);
    nome = n;
    prefs.putString("name", nome);
    NimBLEDevice::setDeviceName(nome.c_str());
    anunciar();
  } else if (c == "adv" && w.size() >= 2) {
    advOrig = (w[1] == "orig");
    prefs.putBool("orig", advOrig);
    anunciar();
  } else if (c == "echo" && w.size() >= 2) {
    eco = (w[1] == "on");
    log("echo %s", eco ? "on" : "off");
  } else if (c == "bond" && w.size() >= 2) {
    bond = (w[1] == "on");
    prefs.putBool("bond", bond);
    log("bond %s (applied after reboot)", bond ? "on" : "off");
  } else if (c == "send" && w.size() >= 3) {
    NimBLECharacteristic *ch = (w[1] == "flow") ? cFlow : cTx;
    std::vector<uint8_t> b;
    for (size_t i = 2; i < w.size(); i++) b.push_back((uint8_t)strtol(w[i].c_str(), nullptr, 16));
    bool ok = ch->notify(b.data(), b.size());
    log("SEND %s %s -> %s", nomeDe(ch), hexs(b.data(), b.size()).c_str(), ok ? "sent" : "not sent (nobody subscribed)");
  } else if (c == "scan") {
    if (w.size() >= 2 && w[1] == "stop") {
      NimBLEDevice::getScan()->stop();
      log("SCAN stopped");
    } else {
      uint32_t seg = (w.size() >= 2) ? (uint32_t)w[1].toInt() : 30;
      if (seg < 1 || seg > 600) seg = 30;
      iniciarScan(seg, w.size() >= 3 ? w[2] : String(""));
    }
  } else if (c == "pace" && w.size() >= 2) {
    paceMs = (uint32_t)w[1].toInt();
    if (paceMs < 100) paceMs = 100;
    log("pace %u ms", (unsigned)paceMs);
  } else if (c == "try" && l.indexOf(' ') > 0) {
    std::vector<String> lst;
    String resto = l.substring(l.indexOf(' ') + 1);
    while (resto.length()) {
      int i = resto.indexOf(';');
      String it = i < 0 ? resto : resto.substring(0, i);
      it.trim();
      if (it.length()) lst.push_back(it);
      if (i < 0) break;
      resto = resto.substring(i + 1);
    }
    iniciarTry(lst);
  } else if (c == "sweep") {
    static const char *CAND[] = {
      "80 00 00", "80 00", "00 00 00", "ba 00 00", "ba 00", "ba 00 7f", "91 00 00", "91 00", "93 00 00", "ca 00 00", "b7 02 00",
      "b6 00 00", "90 29 00", "90 28 00", "90 00 00", "d0 00", "d0 7f", "fe", "01", "7f", "ff",
      "f0 52 00 74 00 00 00 63 00 01 f7", "f0 52 00 74 00 00 00 63 0e 00 64 f7", "f0 52 00 74 00 00 00 63 0e f7",
      "81 00 00", "80 7f 7f", "b0 00 00", "b0 3c 00", "e0 00 00", "c0 00"};
    std::vector<String> lst;
    for (const char *x : CAND) lst.push_back(String(x));
    iniciarTry(lst);
  } else if (c == "state") {
    iniciarEstado(true);
  } else if (c == "auto" && w.size() >= 2) {
    autoEstado = (w[1] == "on");
    log("auto %s", autoEstado ? "on" : "off");
  } else if (c == "framing" && w.size() >= 2) {
    framingStream = (w[1] == "stream");
    log("framing %s", framingStream ? "stream" : "msg");
  } else if (c == "connect") {
    ligarReal(w.size() >= 2 ? l.substring(l.indexOf(' ') + 1) : String(""));
  } else if (c == "cdisc") {
    if (cli && cli->isConnected()) cli->disconnect();
    else log("CLIENT not connected");
  } else if (c == "cw" && w.size() >= 3) {
    NimBLERemoteCharacteristic *ch = (cli && cli->isConnected()) ? escolher(w[1]) : nullptr;
    if (!ch) { log("CLIENT not connected or no such characteristic"); }
    else {
      std::vector<uint8_t> b;
      for (size_t i = 2; i < w.size(); i++) b.push_back((uint8_t)strtol(w[i].c_str(), nullptr, 16));
      bool ok = ch->writeValue(b.data(), b.size(), ch->canWrite());
      log("CW [%d] %s -> %s", indiceDe(ch), hexs(b.data(), b.size()).c_str(), ok ? "written" : "FAILED");
    }
  } else if (c == "cr" && w.size() >= 2) {
    NimBLERemoteCharacteristic *ch = (cli && cli->isConnected()) ? escolher(w[1]) : nullptr;
    if (!ch) { log("CLIENT not connected or no such characteristic"); }
    else {
      NimBLEAttValue v = ch->readValue();
      log("CR [%d] len=%u hex=%s ascii=\"%s\"", indiceDe(ch), (unsigned)v.size(), hexs(v.data(), v.size()).c_str(), ascii(v.data(), v.size()).c_str());
    }
  } else if (c == "reboot") {
    delay(100);
    ESP.restart();
  } else {
    Serial.println("unknown command (help)");
  }
}

void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) delay(10);
  prefs.begin("f6probe", false);
  nome = prefs.getString("name", nome);
  advOrig = prefs.getBool("orig", false);
  bond = prefs.getBool("bond", false);

  NimBLEDevice::init(nome.c_str());
  if (bond) {
    NimBLEDevice::setSecurityAuth(true, false, false);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  }
  NimBLEServer *srv = NimBLEDevice::createServer();
  srv->setCallbacks(&serverCb);
  NimBLEService *svc = srv->createService(U_SVC);
  cTx = svc->createCharacteristic(U_TX, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);
  cRx = svc->createCharacteristic(U_RX, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  cFlow = svc->createCharacteristic(U_FLOW, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR | NIMBLE_PROPERTY::NOTIFY);
  cTx->setCallbacks(&charCb);
  cRx->setCallbacks(&charCb);
  cFlow->setCallbacks(&charCb);
  svc->start();

  log("%s", VERSAO);
  log("MAC %s", NimBLEDevice::getAddress().toString().c_str());
  log("SVC %s", U_SVC);
  log("TX %s (read, notify)", U_TX);
  log("RX %s (write, write without response)", U_RX);
  log("FLOW %s (read, write, write without response, notify)", U_FLOW);
  anunciar();
  Serial.println("Waiting for the iPhone (F Control). Type help for commands.");
}

void loop() {
  static String linha;
  static uint32_t statsMs = 0;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (linha.length()) comando(linha);
      linha = "";
    } else if (linha.length() < 200) {
      linha += ch;
    }
  }
  if (millis() - statsMs > 10000) {
    statsMs = millis();
    comando("stats");
  }
  if (pedirEstado) {
    pedirEstado = false;
    iniciarEstado(true);
  }
  bombearEstado();
  delay(5);
}

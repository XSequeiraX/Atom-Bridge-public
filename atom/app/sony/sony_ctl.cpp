// sony_ctl: as camaras Sony do rig pelo Camera Remote SDK da Sony (2026-10-27).
//
// O bridge (sony.py) arranca este programa e fala com ele por linhas: comandos no stdin, eventos JSON no stdout (um por
// linha). O SDK nao vem no repositorio: cada um descarrega-o da Sony (licenca propria) e o instalador compila isto contra
// ele (compilar.sh). Sem o SDK a Sony fica desligada e o resto do rig funciona igual.
//
// Comandos (campos separados por espacos; <id> e o id do bridge, ex. sony1):
//   add <id> <modelo> ip <a.b.c.d> [ssh <utilizador> <password>]   cria a camara (nao liga)
//   add <id> <modelo> usb <numero de serie>
//   connect <id> | disconnect <id> | del <id>
//   rec <id> 1|0              comeca / para de gravar video
//   tc <id> HH:MM:SS:FF       escreve o Time Code Preset (com a camara parada)
//   read <id>                 manda ja o estado
//   enum                      procura camaras (USB e rede local): evento "found"
//   models                    os modelos que este SDK conhece: evento "models"
//   quit
// Eventos: {"ev":"ready"}, connected, disconnected, reconnecting, error, fingerprint, state, result, found, models.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "CameraRemote_SDK.h"
#include "CrDeviceProperty.h"
#include "CrCommandData.h"
#include "IDeviceCallback.h"

using namespace SCRSDK;

// {"ILCE-7M4", CrCameraDeviceModel_ILCE_7M4}, ...: gerado do CrDefines.h do SDK instalado (gerar_modelos.py), para um
// SDK mais novo trazer os modelos novos sem mexer aqui.
struct Modelo { const char* nome; CrCameraDeviceModelList codigo; };
static const Modelo MODELOS[] = {
#include "modelos.inc"
};

// Nestes o botao de REC do SDK alterna (Down comeca e tambem para) e pede um Up logo a seguir ao Down
// (API Reference, CrCommandId_MovieRecord). Nos outros Down comeca e Up para.
static const std::set<std::string> REC_ALTERNA = {"ILCE-1", "ILCE-9M2", "ILCE-7RM4A", "ILCE-7RM4", "ILCE-7SM3", "ILCE-7C",
                                                  "DSC-RX0M2"};

static std::mutex g_out;

static std::string esc(const std::string& s) {
    std::string o;
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
        else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    return o;
}

static void emitir(const std::string& json) {
    std::lock_guard<std::mutex> l(g_out);
    std::cout << json << "\n" << std::flush;
}

static std::string hex(CrInt32u v) { char b[16]; snprintf(b, sizeof b, "0x%04X", v); return b; }

// --- uma camara ---------------------------------------------------------------------------------------------------
class Camara : public IDeviceCallback {
public:
    std::string id, modelo;
    ICrCameraObjectInfo* info = nullptr;
    CrDeviceHandle handle = 0;
    std::atomic<bool> ligada{false};
    bool ssh = false;
    std::string user, pass;
    std::mutex m;

    ~Camara() { desligar(); if (info) { info->Release(); info = nullptr; } }

    // Larga a ligacao (o info fica: um connect a seguir volta a tentar com ele).
    void desligar() {
        if (handle) {
            if (ligada) Disconnect(handle);
            ReleaseDevice(handle);
            handle = 0;
        }
        ligada = false;
    }

    void OnConnected(DeviceConnectionVersioin) override {
        ligada = true;
        emitir("{\"ev\":\"connected\",\"id\":\"" + esc(id) + "\"}");
        estado();
    }
    void OnDisconnected(CrInt32u erro) override {
        ligada = false;
        emitir("{\"ev\":\"disconnected\",\"id\":\"" + esc(id) + "\",\"code\":\"" + hex(erro) + "\"}");
    }
    void OnError(CrInt32u erro) override {
        emitir("{\"ev\":\"error\",\"id\":\"" + esc(id) + "\",\"code\":\"" + hex(erro) + "\"}");
    }
    void OnWarning(CrInt32u aviso) override {
        if (aviso == CrWarning_Connect_Reconnecting) {
            ligada = false;
            emitir("{\"ev\":\"reconnecting\",\"id\":\"" + esc(id) + "\"}");
        } else if (aviso == CrWarning_Connect_Reconnected) {
            ligada = true;
            emitir("{\"ev\":\"connected\",\"id\":\"" + esc(id) + "\"}");
        }
    }
    void OnPropertyChanged() override { if (ligada) estado(); }
    void OnPropertyChangedCodes(CrInt32u, CrInt32u*) override { if (ligada) estado(); }

    // O estado que o bridge mostra. Valores em bruto do SDK, so com as conversoes simples (o resto faz o sony.py).
    void estado() {
        static const CrInt32u codigos[] = {
            CrDeviceProperty_RecordingState, CrDeviceProperty_BatteryRemain, CrDeviceProperty_BatteryRemainingInMinutes,
            CrDeviceProperty_BatteryRemainingInVoltage, CrDeviceProperty_BatteryRemainDisplayUnit,
            CrDeviceProperty_MediaSLOT1_RemainingTime, CrDeviceProperty_MediaSLOT2_RemainingTime,
            CrDeviceProperty_IsoSensitivity, CrDeviceProperty_FNumber, CrDeviceProperty_ShutterSpeed,
            CrDeviceProperty_TimeCodePreset};
        static const char* nomes[] = {"rec", "battery_pct", "battery_min", "battery_mv", "battery_unit", "slot1_s", "slot2_s",
                                      "iso", "fnumber", "shutter", "tc_preset"};
        const int n = sizeof(codigos) / sizeof(codigos[0]);
        std::lock_guard<std::mutex> l(m);
        if (!handle || !ligada) return;
        CrDeviceProperty* lista = nullptr;
        CrInt32 nl = 0;
        CrError e = GetSelectDeviceProperties(handle, n, const_cast<CrInt32u*>(codigos), &lista, &nl);
        if (e != CrError_None || !lista) return;
        std::ostringstream o;
        o << "{\"ev\":\"state\",\"id\":\"" << esc(id) << "\"";
        for (CrInt32 i = 0; i < nl; i++) {
            const CrDeviceProperty& p = lista[i];
            if (!p.IsGetEnableCurrentValue()) continue;
            for (int k = 0; k < n; k++)
                if (p.GetCode() == codigos[k]) o << ",\"" << nomes[k] << "\":" << (unsigned long long)p.GetCurrentValue();
        }
        o << "}";
        ReleaseDeviceProperties(handle, lista);
        emitir(o.str());
    }

    CrError rec(bool on) {
        std::lock_guard<std::mutex> l(m);
        if (!handle || !ligada) return CrError_Generic;
        if (REC_ALTERNA.count(modelo)) {
            // Alterna: so se carrega se o estado for o contrario do pedido (o sony.py tambem confirma depois).
            CrDeviceProperty* lista = nullptr;
            CrInt32 nl = 0;
            CrInt32u c = CrDeviceProperty_RecordingState;
            bool grava = false;
            if (GetSelectDeviceProperties(handle, 1, &c, &lista, &nl) == CrError_None && lista && nl > 0) {
                grava = lista[0].GetCurrentValue() == CrMovie_Recording_State_Recording;
                ReleaseDeviceProperties(handle, lista);
            }
            if (grava == on) return CrError_None;
            CrError e = SendCommand(handle, CrCommandId_MovieRecord, CrCommandParam_Down);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            SendCommand(handle, CrCommandId_MovieRecord, CrCommandParam_Up);
            return e;
        }
        return SendCommand(handle, CrCommandId_MovieRecord, on ? CrCommandParam_Down : CrCommandParam_Up);
    }

    CrError timecode(CrInt64u valor) {
        std::lock_guard<std::mutex> l(m);
        if (!handle || !ligada) return CrError_Generic;
        CrDeviceProperty p;
        p.SetCode(CrDeviceProperty_TimeCodePreset);
        p.SetCurrentValue(valor);
        p.SetValueType(CrDataType_UInt32);
        return SetDeviceProperty(handle, &p);
    }
};

static std::map<std::string, std::unique_ptr<Camara>> g_cams;
static std::mutex g_cams_m;

static bool modelo_codigo(const std::string& nome, CrCameraDeviceModelList* cod) {
    for (const Modelo& m : MODELOS)
        if (nome == m.nome) { *cod = m.codigo; return true; }
    return false;
}

static Camara* achar(const std::string& id) {
    std::lock_guard<std::mutex> l(g_cams_m);
    auto it = g_cams.find(id);
    return it == g_cams.end() ? nullptr : it->second.get();
}

static void resultado(const std::string& id, const std::string& cmd, CrError e, const std::string& extra = "") {
    emitir("{\"ev\":\"result\",\"id\":\"" + esc(id) + "\",\"cmd\":\"" + esc(cmd) + "\",\"ok\":" +
           (e == CrError_None ? "true" : "false") + (e == CrError_None ? "" : ",\"code\":\"" + hex(e) + "\"") + extra + "}");
}

static void cmd_add(std::vector<std::string>& a) {
    // add <id> <modelo> ip <a.b.c.d> [ssh <user> <pass>]  |  add <id> <modelo> usb <serie>
    if (a.size() < 5) { resultado(a.size() > 1 ? a[1] : "", "add", CrError_Generic, ",\"error\":\"usage\""); return; }
    const std::string id = a[1], modelo = a[2], via = a[3];
    CrCameraDeviceModelList cod;
    if (!modelo_codigo(modelo, &cod)) {
        resultado(id, "add", CrError_Generic, ",\"error\":\"model not in this SDK: " + esc(modelo) + "\"");
        return;
    }
    auto c = std::make_unique<Camara>();
    c->id = id; c->modelo = modelo;
    CrError e = CrError_Generic;
    if (via == "ip") {
        CrInt32u ip = 0;
        int partes[4];
        if (sscanf(a[4].c_str(), "%d.%d.%d.%d", &partes[0], &partes[1], &partes[2], &partes[3]) != 4) {
            resultado(id, "add", CrError_Generic, ",\"error\":\"invalid ip\""); return;
        }
        for (int i = 0; i < 4; i++) ip |= (CrInt32u)(partes[i] & 0xFF) << (i * 8);
        CrInt8u mac[6] = {0};
        c->ssh = a.size() >= 8 && a[5] == "ssh";
        if (c->ssh) { c->user = a[6]; c->pass = a[7]; }
        e = CreateCameraObjectInfoEthernetConnection(&c->info, cod, ip, mac, c->ssh ? CrSSHsupport_ON : CrSSHsupport_OFF);
    } else if (via == "usb") {
        std::string serie = a[4];
        serie.resize(USB_SERIAL_LENGTH, '\0');
        e = CreateCameraObjectInfoUSBConnection(&c->info, cod, (CrInt8u*)serie.data());
    } else {
        resultado(id, "add", CrError_Generic, ",\"error\":\"via: ip | usb\""); return;
    }
    if (e != CrError_None || !c->info) { resultado(id, "add", e ? e : CrError_Generic); return; }
    {
        std::lock_guard<std::mutex> l(g_cams_m);
        g_cams[id] = std::move(c);
    }
    resultado(id, "add", CrError_None);
}

static void cmd_connect(const std::string& id) {
    Camara* c = achar(id);
    if (!c) { resultado(id, "connect", CrError_Generic, ",\"error\":\"unknown id\""); return; }
    std::lock_guard<std::mutex> l(c->m);
    if (c->handle) { resultado(id, "connect", CrError_None); return; }
    char fp[256] = {0};
    CrInt32u fplen = 0;
    if (c->ssh) {
        // O SDK pede a impressao digital da chave SSH da camara; aceita-se a que ela da (o bridge regista-a).
        CrError e = GetFingerprint(c->info, fp, &fplen);
        if (e != CrError_None) { resultado(id, "connect", e, ",\"error\":\"fingerprint\""); return; }
        emitir("{\"ev\":\"fingerprint\",\"id\":\"" + esc(id) + "\",\"fp\":\"" + esc(std::string(fp, fplen)) + "\"}");
    }
    static const CrInt16u nome[] = {'A', 't', 'o', 'm', ' ', 'B', 'r', 'i', 'd', 'g', 'e', 0};   // mostrado no emparelhamento
    CrError e = Connect(c->info, c, &c->handle, CrSdkControlMode_Remote, CrReconnecting_ON,
                        c->ssh ? c->user.c_str() : nullptr, c->ssh ? c->pass.c_str() : nullptr,
                        c->ssh ? fp : nullptr, fplen, nome);
    if (e != CrError_None) c->handle = 0;
    resultado(id, "connect", e);
}

static void cmd_enum() {
    ICrEnumCameraObjectInfo* lista = nullptr;
    CrError e = EnumCameraObjects(&lista, 3);
    std::ostringstream o;
    o << "{\"ev\":\"found\",\"cameras\":[";
    if (e == CrError_None && lista) {
        for (CrInt32u i = 0; i < lista->GetCount(); i++) {
            const ICrCameraObjectInfo* c = lista->GetCameraObjectInfo(i);
            if (!c) continue;
            if (i) o << ",";
            o << "{\"model\":\"" << esc(c->GetModel() ? c->GetModel() : "") << "\",\"connection\":\""
              << esc(c->GetConnectionTypeName() ? c->GetConnectionTypeName() : "") << "\",\"ip\":\""
              << esc(c->GetIPAddressChar() ? std::string(c->GetIPAddressChar(), c->GetIPAddressCharSize()).c_str() : "")
              << "\",\"ssh\":" << (c->GetSSHsupport() == CrSSHsupport_ON ? "true" : "false");
            // Por USB o id e o numero de serie (texto); por rede e o MAC.
            std::string idtxt;
            if (c->GetId() && c->GetIdSize()) {
                if (std::string(c->GetConnectionTypeName() ? c->GetConnectionTypeName() : "") == "USB")
                    idtxt = std::string((const char*)c->GetId(), strnlen((const char*)c->GetId(), c->GetIdSize()));
                else
                    for (CrInt32u k = 0; k < c->GetIdSize(); k++) { char b[4]; snprintf(b, sizeof b, "%02x", c->GetId()[k]); idtxt += b; }
            }
            o << ",\"serial\":\"" << esc(idtxt) << "\"}";
        }
        lista->Release();
    }
    o << "]}";
    emitir(o.str());
}

static std::vector<std::string> partir(const std::string& s) {
    std::vector<std::string> v;
    std::istringstream is(s);
    std::string t;
    while (is >> t) v.push_back(t);
    return v;
}

int main() {
    std::ios::sync_with_stdio(false);
    if (!Init()) { emitir("{\"ev\":\"fatal\",\"error\":\"SDK Init failed\"}"); return 1; }
    emitir("{\"ev\":\"ready\",\"sdk\":\"" + hex(GetSDKVersion()) + "\"}");

    // O estado de 2 em 2 s mesmo sem avisos do SDK (bateria e tempo de cartao descem sem "mudou").
    std::atomic<bool> fim{false};
    std::thread vigia([&] {
        while (!fim) {
            std::this_thread::sleep_for(std::chrono::seconds(2));
            std::vector<Camara*> v;
            {
                std::lock_guard<std::mutex> l(g_cams_m);
                for (auto& kv : g_cams) v.push_back(kv.second.get());
            }
            for (Camara* c : v) if (c->ligada) c->estado();
        }
    });

    std::string linha;
    while (std::getline(std::cin, linha)) {
        std::vector<std::string> a = partir(linha);
        if (a.empty()) continue;
        const std::string& cmd = a[0];
        const std::string id = a.size() > 1 ? a[1] : "";
        if (cmd == "quit") break;
        else if (cmd == "add") cmd_add(a);
        else if (cmd == "connect") cmd_connect(id);
        else if (cmd == "disconnect" || cmd == "del") {
            Camara* c = achar(id);
            if (c) { std::lock_guard<std::mutex> l(c->m); c->desligar(); }
            if (cmd == "del") { std::lock_guard<std::mutex> l(g_cams_m); g_cams.erase(id); }
            resultado(id, cmd, CrError_None);
        } else if (cmd == "rec" && a.size() >= 3) {
            Camara* c = achar(id);
            resultado(id, a[2] == "1" ? "rec_on" : "rec_off", c ? c->rec(a[2] == "1") : CrError_Generic);
        } else if (cmd == "tc" && a.size() >= 3) {
            Camara* c = achar(id);
            int h, mi, s, f;
            if (!c || sscanf(a[2].c_str(), "%d:%d:%d:%d", &h, &mi, &s, &f) != 4) { resultado(id, "tc", CrError_Generic); continue; }
            CrInt64u v = ((CrInt64u)(h & 0xFF) << 24) | ((mi & 0xFF) << 16) | ((s & 0xFF) << 8) | (f & 0xFF);
            resultado(id, "tc", c->timecode(v));
        } else if (cmd == "read") {
            Camara* c = achar(id);
            if (c) c->estado();
        } else if (cmd == "enum") cmd_enum();
        else if (cmd == "models") {
            std::ostringstream o;
            o << "{\"ev\":\"models\",\"models\":[";
            for (size_t i = 0; i < sizeof(MODELOS) / sizeof(MODELOS[0]); i++) o << (i ? "," : "") << "\"" << MODELOS[i].nome << "\"";
            o << "]}";
            emitir(o.str());
        } else emitir("{\"ev\":\"error\",\"error\":\"unknown command: " + esc(cmd) + "\"}");
    }
    fim = true;
    vigia.join();
    {
        std::lock_guard<std::mutex> l(g_cams_m);
        g_cams.clear();
    }
    Release();
    return 0;
}

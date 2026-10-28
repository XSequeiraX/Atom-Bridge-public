"""Leitura de LTC (SMPTE timecode) por entrada de audio USB.

O Deity TC-1 entrega LTC na saida A-out. Um Raspberry Pi nao tem entrada de
audio -- o jack de 3.5mm e so saida -- por isso isto exige um ADC USB.

Parametros afinados por medicao no proprio equipamento: media movel de 5
amostras e histerese de 0.15 do pico deram 49 frames validos em 50 com
continuidade total, mesmo com o sinal a apenas 13% de fundo de escala.
A leitura repete a 0.06s; o resto do desvio e latencia fixa do buffer ALSA.
"""
import array
import collections
import os
import re
import subprocess
import threading
import time

SYNC = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1]
# Posicoes dos oito grupos de user bits dentro do frame de 80 bits.
USER_BIT_FIELDS = (4, 12, 20, 28, 36, 44, 52, 60)
RATE = 48000
LP_WIDTH = 5
HYST = 0.15
CAPTURE_SECONDS = 3
# Custo real da captura, medido: o arecord demora 3.02s para 3.00s de audio,
# ou seja 20ms entre a ultima amostra e a saida do processo. Nao ha mais nada
# para compensar.
#
# Cuidado com a tentacao de meter aqui um valor grande: uma primeira medicao
# dava -1.00s e parecia latencia de buffer, mas com o buffer a 100ms e a 2000ms
# o valor nao mexia. Nao era latencia -- era o gerador a andar 1s atras do NTP.
# Compensa-lo punha o contador 1s a frente do proprio gerador, que e o oposto
# do que jam sync quer: o objectivo e bater certo com o gerador, nao com o NTP.
# Calibravel por placa: LTC_LATENCY no ambiente do
# servico. 0.02 e o valor medido no UCA202.
LATENCY = float(os.environ.get("LTC_LATENCY", "0.02"))
# Abaixo disto a leitura nao merece confianca e devolvemos None.
MIN_FRAMES = 25
MIN_CONTINUITY = 0.8
# Quantos frames finais tem de estar mesmo seguidos para a leitura servir de
# ancora. Dez frames sao 0.4s a 25fps: curto para nao ser exigente de mais,
# longo para nao passar por sorte.
TAIL_FRAMES = 10

_lock = threading.Lock()


def find_device():
    """Dispositivo de captura do LTC, por nome e nao por indice.

    # LTC_INTERNAL: no Atom le-se pela entrada de
    microfone interna (HDA), com o TC-1 em L-OUT -- testado a 2026-10-06, 74/75
    frames e 100% de continuidade. O indice das placas muda quando um USB
    re-enumera, por isso escolhe-se pelo nome a cada leitura.
    """
    forced = os.environ.get("LTC_DEVICE")
    if forced:
        return forced
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    cards = re.findall(r"^card (\d+): .*?\[(.*?)\], device (\d+): (.*)$", out, re.M)
    # 1) A entrada analogica do audio interno (o jack de microfone).
    for n, name, dev, rest in cards:
        if re.search(r"HDA", name, re.I) and re.search(r"analog", rest, re.I):
            return f"plughw:{n},{dev}"
    # 2) Sem audio interno: um ADC USB (o UCA202 do rig antigo).
    for n, name, dev, rest in cards:
        if "usb" in (name + " " + rest).lower():
            return f"plughw:{n},{dev}"
    return None


def _lowpass(a, w=LP_WIDTH):
    out = array.array('h', bytes(len(a) * 2))
    s = 0
    for i in range(len(a)):
        s += a[i]
        if i >= w:
            s -= a[i - w]
        out[i] = int(s / (w if i >= w else i + 1))
    return out


def _transitions(a, hyst=HYST):
    """Passagens por zero com histerese, para ignorar ruido perto do zero."""
    pk = max(max(a), -min(a)) or 1
    hi, lo = pk * hyst, -pk * hyst
    tr, st = [], 0
    for i, v in enumerate(a):
        if st <= 0 and v > hi:
            st = 1
            tr.append(i)
        elif st >= 0 and v < lo:
            st = -1
            tr.append(i)
    return tr


def decode(raw):
    """(frames, fps, user_bits). frames = [(h, m, s, f), ...] pela ordem lida;
    user_bits e a lista paralela de oito digitos hex por frame."""
    a = array.array('h')
    a.frombytes(raw)
    if len(a) < RATE // 10:
        return [], 0, []
    a = _lowpass(a)
    tr = _transitions(a)
    iv = [tr[i] - tr[i - 1] for i in range(1, len(tr))]
    if len(iv) < 100:
        return [], 0, []
    # O intervalo mais comum tanto pode ser o bit inteiro como o meio bit.
    # Havendo um cluster a metade, o modo e o bit inteiro.
    mode = collections.Counter(iv).most_common(1)[0][0]
    half = sum(1 for x in iv if mode * 0.35 <= x <= mode * 0.65)
    bitp = mode if half > len(iv) * 0.1 else mode * 2
    thr = bitp * 0.75
    bits, k = [], 0
    while k < len(iv):
        if iv[k] >= thr:
            bits.append(0)
            k += 1
        elif k + 1 < len(iv) and iv[k + 1] < thr:
            bits.append(1)
            k += 2
        else:
            k += 1                      # transicao orfa: ressincroniza
    out, ub = [], []
    for i in range(64, len(bits) - 16):
        if bits[i:i + 16] == SYNC:
            f = bits[i - 64:i + 16]
            # O frame vai como argumento por omissao para o lambda nao
            # depender da variavel de ciclo: aqui e chamado de imediato, mas
            # a ligacao solta e uma armadilha classica se alguem o guardar.
            g = lambda s, n, f=f: sum(f[s + j] << j for j in range(n))
            t = (g(48, 4) + g(56, 2) * 10, g(32, 4) + g(40, 3) * 10,
                 g(16, 4) + g(24, 3) * 10, g(0, 4) + g(8, 2) * 10)
            if t[0] < 24 and t[1] < 60 and t[2] < 60 and t[3] < 60:
                out.append(t)
                # User bits (SMPTE 309M): oito nibbles entrelacados com os
                # campos de tempo. E por aqui que os geradores mandam a data,
                # que o LTC nao transporta de outra forma.
                ub.append("".join("%X" % g(b, 4) for b in USER_BIT_FIELDS))
    if not out:
        return [], 0, []
    # O fps sai do periodo de bit, nao da contagem de frames: um frame perdido
    # baixava a contagem e fazia passar 25 por 24. Um frame LTC tem sempre 80
    # bits, por isso fps = (amostras/s / amostras por bit) / 80 -- exacto.
    raw_fps = RATE / bitp / 80
    fps = min((24, 25, 30), key=lambda f: abs(f - raw_fps))
    return out, fps, ub


def decode_anchor(raw):
    """Como decode(), mais a amostra onde acaba o ultimo frame lido (ou None).

    E o que permite ancorar a leitura no fim real desse frame em vez de no fim
    da captura -- ver read(). Mesma logica de decode(); so guarda, para cada
    bit, a amostra da transicao em que ele acaba.
    """
    a = array.array('h')
    a.frombytes(raw)
    if len(a) < RATE // 10:
        return [], 0, [], None
    a = _lowpass(a)
    tr = _transitions(a)
    iv = [tr[i] - tr[i - 1] for i in range(1, len(tr))]
    if len(iv) < 100:
        return [], 0, [], None
    mode = collections.Counter(iv).most_common(1)[0][0]
    half = sum(1 for x in iv if mode * 0.35 <= x <= mode * 0.65)
    bitp = mode if half > len(iv) * 0.1 else mode * 2
    thr = bitp * 0.75
    # iv[k] = tr[k+1] - tr[k]: um 0 ocupa um intervalo e acaba em tr[k+1]; um 1
    # ocupa dois e acaba em tr[k+2].
    bits, ends, k = [], [], 0
    while k < len(iv):
        if iv[k] >= thr:
            bits.append(0)
            ends.append(tr[k + 1])
            k += 1
        elif k + 1 < len(iv) and iv[k + 1] < thr:
            bits.append(1)
            ends.append(tr[k + 2])
            k += 2
        else:
            k += 1
    out, ub, fim = [], [], None
    for i in range(64, len(bits) - 16):
        if bits[i:i + 16] == SYNC:
            f = bits[i - 64:i + 16]
            g = lambda s, n, f=f: sum(f[s + j] << j for j in range(n))
            t = (g(48, 4) + g(56, 2) * 10, g(32, 4) + g(40, 3) * 10,
                 g(16, 4) + g(24, 3) * 10, g(0, 4) + g(8, 2) * 10)
            if t[0] < 24 and t[1] < 60 and t[2] < 60 and t[3] < 60:
                out.append(t)
                ub.append("".join("%X" % g(b, 4) for b in USER_BIT_FIELDS))
                fim = ends[i + 15]      # ultimo bit da palavra de sincronismo
    if not out:
        return [], 0, [], None
    raw_fps = RATE / bitp / 80
    fps = min((24, 25, 30), key=lambda f: abs(f - raw_fps))
    return out, fps, ub, fim


def date_from_user_bits(ub):
    """Data (aaaa, mm, dd) a partir dos user bits, ou None se nao for uma data.

    SMPTE 309M: os oito nibbles formam quatro bytes BCD, e cada byte guarda as
    unidades antes das dezenas porque o LTC transmite LSB primeiro. A ordem dos
    bytes e fuso, ano, mes, dia -- confirmado contra o Deity TC-1, que a
    2026-09-07 debitava "00629070".
    """
    if not ub or len(ub) != 8:
        return None
    try:
        n = [int(c, 16) for c in ub]
    except ValueError:
        return None
    if any(d > 9 for d in n):
        return None                       # BCD nao tem digitos acima de 9
    byte = lambda i: n[i * 2 + 1] * 10 + n[i * 2]
    yy, mm, dd = byte(1), byte(2), byte(3)
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    return (2000 + yy, mm, dd)


def read(seconds=CAPTURE_SECONDS, device=None):
    """Le o LTC actual.

    Devolve dict com secs (segundos do dia, ja compensados da latencia), fps,
    frames e continuity. Levanta RuntimeError com a causa concreta quando nao
    da -- ocupado, mudo, ou sinal que nao e LTC sao problemas diferentes e
    exigem accoes diferentes de quem esta a montar. Bloqueia cerca de
    seconds+1, por isso nunca deve ser chamado no fio do Flask.
    """
    dev = device or find_device()
    if not dev:
        raise RuntimeError("no audio input found — is the TC-1 cable in the microphone jack?")
    # O ADC e exclusivo. Esperar um pouco e melhor que falhar de imediato, mas
    # o erro tem de dizer que estava ocupado: dizer 'sem sinal' mandava alguem
    # verificar cabos por nada.
    if not _lock.acquire(timeout=seconds + 6):
        raise RuntimeError("audio input busy with another read")
    try:
        try:
            p = subprocess.Popen(
                ["arecord", "-D", dev, "-f", "S16_LE", "-r", str(RATE),
                 "-c", "1", "-d", str(seconds), "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise RuntimeError("arecord not available: %s" % e) from None
        try:
            raw, err = p.communicate(timeout=seconds + 5)
        except subprocess.TimeoutExpired:
            # Um arecord preso (placa a re-enumerar) ficava vivo a segurar o ADC
            p.kill()
            p.communicate()
            raise RuntimeError("arecord did not finish — audio input stuck?") from None
        t_end = time.time()
        if p.returncode != 0 or len(raw) < RATE:
            msg = (err or b"").decode(errors="replace").strip().splitlines()
            raise RuntimeError("arecord failed: " + (msg[-1] if msg else "no audio"))
    finally:
        _lock.release()

    # Descodificar PRIMEIRO e so depois julgar. O contrario -- exigir uma
    # amplitude minima antes de tentar -- rejeitava sinal perfeitamente bom:
    # uma placa de linha entrega o LTC a 0.4% de fundo de escala e descodifica
    # a 100%, porque a histerese e relativa ao pico e nao a um valor absoluto.
    # A amplitude so serve para EXPLICAR uma falha, nunca para a causar.
    frames, fps, ubs, fim = decode_anchor(raw)
    a = array.array('h'); a.frombytes(raw)
    pk = max(max(a), -min(a)) if a else 0
    if len(frames) < MIN_FRAMES or not fps:
        if pk < 30:
            raise RuntimeError(f"input silent (peak {pk}) — is the generator on and sending LTC?")
        raise RuntimeError(f"signal present (peak {pk*100/32768:.1f}%) but not readable LTC "
                           f"— only {len(frames)} frames")
    cont = sum(1 for i in range(1, len(frames))
               if (frames[i][2] - frames[i - 1][2]) % 60 <= 1)
    ratio = cont / max(len(frames) - 1, 1)
    if ratio < MIN_CONTINUITY:
        raise RuntimeError(f"LTC unstable: only {int(ratio*100)}% of frames follow the previous "
                           f"one — check the level")

    # A continuidade e uma media, e o que nos interessa e UM frame: o ultimo,
    # que serve de ancora ao instante em que a captura acabou. Um frame final
    # corrompido quase nao mexe na media e acertaria as camaras pelo valor
    # errado. Por isso exige-se que a cauda seja mesmo consecutiva, frame a
    # frame -- e uma verificacao local, sobre o que vai ser usado.
    nums = [((h * 3600 + m * 60 + sec) * fps + fr) for h, m, sec, fr in frames]
    tail = nums[-TAIL_FRAMES:]
    if len(tail) < TAIL_FRAMES or any(tail[i] - tail[i - 1] != 1
                                      for i in range(1, len(tail))):
        raise RuntimeError(f"the last {TAIL_FRAMES} frames are not consecutive — reading is not "
                           f"reliable as an anchor")
    h, m, s, f = frames[-1]
    # O timecode do frame vale no INICIO dele; o frame acaba na amostra `fim`,
    # e tudo o que veio depois tambem ja passou quando a captura acabou.
    resto = (len(raw) // 2 - fim) / RATE if fim is not None else 0.0
    secs = h * 3600 + m * 60 + s + (f + 1) / fps + resto + LATENCY
    # Os user bits sao normalmente constantes ao longo dos frames (data fixa),
    # por isso guarda-se o valor mais repetido em vez do ultimo.
    ub = collections.Counter(ubs).most_common(1)[0][0] if ubs else None
    return {"user_bits": ub, "user_bits_stable": bool(ubs) and ubs.count(ub) > len(ubs) * 0.8,
            "date": date_from_user_bits(ub),
            "secs": secs % 86400, "fps": fps, "frames": len(frames),
            "continuity": round(ratio, 3), "at": t_end, "device": dev,
            "tc": "%02d:%02d:%02d:%02d" % (h, m, s, f)}

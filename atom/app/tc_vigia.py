"""Desvio do timecode de cada camara para o LTC do Atom (2026-10-14).

A BMPCC avisa o timecode por Bluetooth (directo ou pelo C3). Quando o segundo muda, a camara acabou de passar ao frame
0 desse segundo: o desvio e (segundo da camara) - (LTC do Atom no instante em que o aviso chegou). O aviso chega sempre
atrasado (Bluetooth, Wi-Fi do C3, intervalo de leitura), o que so faz o desvio parecer MAIS NEGATIVO: das amostras do
ultimo minuto vale a maior, e o erro e o atraso minimo que sobra (~0,1 s pelo C3).
Os iPhones medem o desvio de outra forma (bmcam._ler_tc, com o erro proprio).
"""
import collections
import threading

JANELA_S = 60.0
MIN_AMOSTRAS = 5
ERRO_BLE = 0.10           # s: o que sobra do atraso do aviso (Bluetooth / C3) depois de escolher a melhor amostra


def tc_segundos(tc):
    """'HH:MM:SS' ou 'HH:MM:SS:FF' -> segundos do dia (sem os frames). None se nao se le."""
    try:
        p = [int(x) for x in str(tc).split(":")[:3]]
        if len(p) != 3 or not (0 <= p[0] < 24 and 0 <= p[1] < 60 and 0 <= p[2] < 60):
            return None
        return p[0] * 3600 + p[1] * 60 + p[2]
    except (TypeError, ValueError):
        return None


class Desvio:
    """As amostras de uma camara. tc_mudou() a cada segundo novo; estimativa() da (desvio, erro) ou None."""

    def __init__(self, erro=ERRO_BLE):
        self.erro = erro
        self._a = collections.deque()
        self._ultimo = None
        self._lock = threading.Lock()

    def tc_mudou(self, tc, chegada, ref):
        """tc: o timecode lido (so conta quando o segundo muda); chegada: time.time() do aviso; ref: LTC do Atom
        (segundos do dia, com fraccao) nesse instante, ou None sem jam."""
        s = tc_segundos(tc)
        with self._lock:
            if s is None or s == self._ultimo:
                return
            novo_segundo = self._ultimo is not None
            self._ultimo = s
            if not novo_segundo or ref is None:       # o primeiro valor nao e uma mudanca: nao se sabe quando comecou
                return
            d = ((s - ref) + 43200) % 86400 - 43200
            self._a.append((chegada, d))
            while self._a and chegada - self._a[0][0] > JANELA_S:
                self._a.popleft()

    def estimativa(self, agora):
        with self._lock:
            while self._a and agora - self._a[0][0] > JANELA_S:
                self._a.popleft()
            if len(self._a) < MIN_AMOSTRAS:
                return None
            return round(max(d for _, d in self._a), 2), self.erro

    def limpar(self):
        with self._lock:
            self._a.clear()
            self._ultimo = None


def desvio_a_mais(off, err, fps):
    """True se o desvio passa de um frame, descontado o erro da medida."""
    if off is None:
        return False
    return abs(off) - (err or 0) > 1.0 / float(fps or 25)

"""Fios de fundo que nao morrem em silencio.

Um ciclo de fundo (consulta de um gateway, vigia...) que levanta uma excepcao deixava de correr sem
aviso: o aparelho ficava com o ultimo estado para sempre. `vigiado(fn, nome, log)` devolve uma funcao que corre `fn` e,
se ela falhar, regista o erro e volta a corre-la passado um intervalo (que cresce se falhar logo a seguir)."""
import time


def vigiado(fn, nome, log=print, pausa=5.0):
    def corre(*a, **k):
        seguidas = 0
        while True:
            t0 = time.time()
            try:
                return fn(*a, **k)
            except Exception as e:                      # nunca deixa o fio morrer
                seguidas = 1 if time.time() - t0 > 60 else seguidas + 1
                espera = min(60.0, pausa * seguidas)
                try:
                    log("%s: %s: %r (restarting in %ds)" % (nome, type(e).__name__, e, espera))
                except Exception:
                    pass
                time.sleep(espera)
    return corre

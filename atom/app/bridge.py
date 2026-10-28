#!/usr/bin/env python3
"""Camera Bridge: o servidor do Atom (camera-bridge.service corre este ficheiro).

O codigo esta partido por area: nucleo.py (estado partilhado, camaras, relogio, ciclos de fundo) e rotas_*.py (a API
de cada area). Este ficheiro so arranca o nucleo: assim o nucleo e importado uma so vez, com o nome `nucleo`, e os
modulos das rotas partilham o mesmo estado.
"""
import nucleo

if __name__ == "__main__":
    nucleo.main()

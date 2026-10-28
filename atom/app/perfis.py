"""Perfis do rig (2026-10-14): conjuntos com nome ("Entrevista 2 cameras", "Exterior com GoPro") do que o operador
costuma mudar de uma filmagem para outra, para os trocar num passo.

Um perfil guarda: as camaras armadas para o REC (rec_targets), o modo estrito, a cena e o reel da claquete e os
gateways ligados no bridge (C3 principal e cada S3). Fica em profiles.json ao lado do bridge.py.
"""
import json
import os
import re
import time

import gravar

CAMINHO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
MAX = 20


def _ler(caminho=None):
    try:
        d = gravar.ler_json(caminho or CAMINHO)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _escrever(perfis, caminho=None):
    caminho = caminho or CAMINHO
    tmp = caminho + ".tmp"
    with open(tmp, "w") as f:
        json.dump(perfis, f, indent=1)
    gravar.trocar(tmp, caminho)


def nome_valido(nome):
    nome = re.sub(r"\s+", " ", str(nome or "")).strip()
    if not nome or len(nome) > 40 or not re.fullmatch(r"[\w .,+\-()/]+", nome):
        raise ValueError("profile name: 1 to 40 letters, digits, spaces or . , + - ( ) /")
    return nome


def listar(caminho=None):
    return _ler(caminho)


def guardar(nome, dados, caminho=None):
    nome = nome_valido(nome)
    perfis = _ler(caminho)
    if nome not in perfis and len(perfis) >= MAX:
        raise ValueError("at most %d profiles: delete one first" % MAX)
    perfis[nome] = dict(dados, saved=time.strftime("%Y-%m-%d %H:%M"))
    _escrever(perfis, caminho)
    return nome


def apagar(nome, caminho=None):
    perfis = _ler(caminho)
    if nome not in perfis:
        raise KeyError(nome)
    del perfis[nome]
    _escrever(perfis, caminho)


def obter(nome, caminho=None):
    return _ler(caminho).get(nome)

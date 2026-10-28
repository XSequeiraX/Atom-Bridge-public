"""Os avisos de licenca que o operador aceita UMA vez, antes de usar o Atom Bridge (consola e pagina).

Mostram-se no arranque da consola e na primeira visita da pagina, todos juntos (agnosticos: valem para qualquer camara ou aparelho, actual ou futuro), em vez
de um pedido a cada camara. O aceite fica em termos.json (ao lado do bridge) com a versao do texto: se o texto mudar
(VERSAO sobe), volta a pedir-se. O texto completo, com as licencas, esta em AVISOS-LICENCAS.md e atom/app/sony/AVISO-SONY.md.
"""
import json
import os
import time

import gravar

VERSAO = 1
FICHEIRO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "termos.json")

# Em ingles, como o resto da interface. Linhas curtas: a consola mostra-as tal e qual.
TEXTO = [
    "ATOM BRIDGE: LICENSES AND NOTICES",
    "",
    "Read this once. It covers every camera and device you use with",
    "this software, now and in the future.",
    "",
    "INDEPENDENT SOFTWARE",
    "  Atom Bridge is open source (MIT License). It is an independent",
    "  project. It is NOT made, approved, endorsed or supported by any",
    "  camera or equipment maker. Support comes only from its authors.",
    "  Brand and product names are used only to say what the software",
    "  works with; they belong to their owners.",
    "",
    "CAMERAS AND DEVICES",
    "  1. Controlling a camera or device with this software may affect",
    "     its manufacturer warranty. Check the maker's terms first.",
    "     Your legal rights as a consumer are not affected.",
    "  2. You are responsible for using each camera and device in line",
    "     with its maker's terms and the law where you are.",
    "  3. Do not use this software or any device it controls for arms,",
    "     for transporting explosives, chemical or biological weapons",
    "     or other harmful items, or for military intelligence,",
    "     surveillance or reconnaissance.",
    "",
    "MAKERS' SOFTWARE AND INFORMATION",
    "  Some makers' libraries (SDKs) or interface documentation are",
    "  used or may be used with this software. They belong to their",
    "  makers and keep their own license terms, which apply to you:",
    "  - they are used only as part of this software, never on their",
    "    own, and are not to be extracted, copied out, modified,",
    "    reverse engineered or redistributed apart from it;",
    "  - confidential documentation is not part of this software;",
    "  - they are provided by their makers as is, without warranty.",
    "",
    "OPEN SOURCE COMPONENTS",
    "  Other components (libraries, fonts, firmware libraries) keep",
    "  their own open source licenses. Full details and texts:",
    "  AVISOS-LICENCAS.md and atom/app/sony/OSS-NOTICES.md, in",
    "  github.com/XSequeiraX/Atom-Bridge-public.",
    "",
    "NO WARRANTY",
    "  This software is provided 'as is', without warranty of any",
    "  kind, and is not for high-risk use. You use it at your own risk.",
]


def estado():
    """{"accepted": bool, "version": int, "at": texto|None}"""
    try:
        d = gravar.ler_json(FICHEIRO)
    except (OSError, ValueError):
        d = None
    ok = isinstance(d, dict) and d.get("version") == VERSAO
    return {"accepted": bool(ok), "version": VERSAO, "at": d.get("at") if ok else None}


def aceitar():
    d = {"version": VERSAO, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = gravar.tmp_unico(FICHEIRO)
    with open(tmp, "w") as f:
        json.dump(d, f)
    gravar.trocar(tmp, FICHEIRO)
    return d

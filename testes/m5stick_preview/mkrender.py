# Extrai o codigo de desenho do .ino para render_src.inc (usado por render.cpp). Ver LEIA-ME.txt.
import os
import re
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '_esp32_tools', 'm5stick_f6_remote', 'm5stick_f6_remote.ino'), encoding='utf-8').read()
defs = "\n".join(l for l in src.splitlines() if l.startswith('#define'))
a = src.index('struct Cfg {'); b = src.index('static bool cfgLoad')
part_cfg = src[a:b]
a = src.index('struct F6State {'); b = src.index('static int jBool')
part_state = src[a:b]
a = src.index('static M5Canvas cv(&M5.Display);'); b = src.index('static void screenMsg')
part_draw = src[a:b].replace('static M5Canvas cv(&M5.Display);', 'static M5Canvas cv;')
# tira o present() verdadeiro (usa o painel) e troca-o pelo da bancada
i = part_draw.index('static void present() {'); j = part_draw.index('\n}\n', i) + 3
part_draw = part_draw[:i] + part_draw[j:]
part_draw = part_draw.replace('  present();\n}', '  host_present();\n}')
# a bateria: na bancada nao ha filtro nem tensao, a % e a do cenario (g_bat, g_chg do render.cpp)
part_draw = part_draw.replace('  batUpdate(now);\n', '  batPct = g_bat; batChg = g_chg;\n')
open('render_src.inc', 'w', encoding='utf-8').write(defs + "\n" + part_cfg + "\n" + part_state + "\n" + "static void host_present();\n" + part_draw)
print("ok", len(part_draw))

#!/bin/bash
# Gera as paginas do comando de bolso em PNG, no PC, com o codigo de desenho do .ino e a biblioteca M5GFX verdadeira.
#   bash build.sh /caminho/para/M5GFX      (git clone https://github.com/m5stack/M5GFX)
set -e
M="${1:?indicar a pasta do M5GFX (a que tem a pasta src)}/src"
cd "$(dirname "$0")"
AQUI="$(pwd)"
OUT="${OUT:-/tmp/m5stick_preview}"; mkdir -p "$OUT"
python3 mkrender.py
for f in miniz pngle qoi qrcode tjpgd; do gcc -c -O0 -I"$M" "$M/lgfx/utility/lgfx_$f.c" -o "$OUT/$f.o"; done
g++ -std=c++17 -O0 -DLGFX_LINUX_FB -I"$M" -I. render.cpp "$M"/lgfx/v1/{lgfx_v1,LGFXBase,lgfx_fonts,lgfx_v1_panel,lgfx_v1_touch,lgfx_v1_platforms}.cpp "$OUT"/*.o \
    -Wl,--unresolved-symbols=ignore-all -o "$OUT/render"      # os tipos de letra japoneses nao fazem falta
(cd "$OUT" && rm -f shot_*.ppm && ./render && ls shot_*.ppm)
python3 ppm2png.py "$OUT" "$AQUI/paginas.png"
rm -f render_src.inc

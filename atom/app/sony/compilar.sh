#!/bin/bash
# Compila o sony_ctl contra o Camera Remote SDK da Sony e instala-o em <destino> com as bibliotecas do SDK ao lado.
#   compilar.sh <pasta ou .zip do SDK> <destino>
# <pasta do SDK>: onde se extraiu o SDK descarregado da Sony, ou o proprio .zip (procura-se la dentro o CameraRemote_SDK.h
# e a libCr_Core.so da arquitectura desta maquina). O SDK nao vem no repositorio: tem licenca propria e cada um
# descarrega-o da Sony (ver LEIA-ME.txt).
set -euo pipefail
SDK="${1:?pasta do SDK}"
DEST="${2:?destino}"
AQUI="$(cd "$(dirname "$0")" && pwd)"
case "$(uname -m)" in
    x86_64) ARQ="Linux64PC" ;;
    aarch64) ARQ="Linux64ARMv8" ;;
    *) echo "no Sony SDK for this architecture: $(uname -m)"; exit 2 ;;
esac
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -f "$SDK" ]; then                       # o .zip tal como vem da Sony
    python3 -m zipfile -e "$SDK" "$TMP/sdk" || { echo "could not extract $SDK"; exit 2; }
    SDK="$TMP/sdk"
fi
# Com as duas variantes extraidas lado a lado, fica-se com a desta maquina.
cands=$(find "$SDK" -name CameraRemote_SDK.h -path "*app/CRSDK/*" 2>/dev/null | sort)
HDR=$(echo "$cands" | grep "$ARQ" | head -1 || true)
[ -n "$HDR" ] || HDR=$(echo "$cands" | head -1)
[ -n "$HDR" ] || { echo "CameraRemote_SDK.h not found under $SDK"; exit 2; }
INC="$(dirname "$HDR")"
LIB="$(dirname "$(dirname "$INC")")/external/crsdk"
[ -f "$LIB/libCr_Core.so" ] || { echo "libCr_Core.so not found in $LIB"; exit 2; }
if command -v file >/dev/null 2>&1; then
    file -L "$LIB/libCr_Core.so" | grep -q "$( [ "$ARQ" = Linux64PC ] && echo x86-64 || echo aarch64)" \
        || { echo "the SDK in $LIB is not for $(uname -m)"; exit 2; }
fi
python3 "$AQUI/gerar_modelos.py" "$INC/CrDefines.h" "$TMP/modelos.inc"
# shellcheck disable=SC2016  # o $ORIGIN e para o ld (as bibliotecas ao lado do programa), nao para o shell
g++ -std=c++17 -O2 -Wall -isystem "$INC" -I"$TMP" "$AQUI/sony_ctl.cpp" -L"$LIB" -lCr_Core -lpthread \
    -Wl,-rpath,'$ORIGIN' -o "$TMP/sony_ctl"
mkdir -p "$DEST/CrAdapter"
cp -f "$TMP/sony_ctl" "$DEST/"
cp -f "$LIB/libCr_Core.so" "$DEST/"
cp -f "$LIB"/CrAdapter/*.so "$DEST/CrAdapter/"
strip "$DEST/sony_ctl" 2>/dev/null || true
# De que codigo saiu este sony_ctl: o CI avisa quando o sony_ctl.cpp mudou e o programa no repositorio ficou para tras.
echo "$(sha256sum "$AQUI/sony_ctl.cpp" | cut -d' ' -f1)  sony_ctl.cpp" > "$DEST/sony_ctl.fonte"
echo "sony_ctl installed in $DEST (SDK from $1)"

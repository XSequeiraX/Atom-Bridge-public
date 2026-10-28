#!/bin/bash
# Passa o estado actual do main do repositorio privado para o publico (XSequeiraX/Atom-Bridge-public), sem o historico.
#   bash ci/publicar.sh            mostra o que mudaria
#   bash ci/publicar.sh --push     faz o commit no publico e envia
# O publico tem a sua propria historia, so a avancar (um commit por sincronizacao): os Atom actualizam-se sempre em
# fast-forward. Corre-se no clone do repositorio privado, com o main em dia (git pull). PUBLICO_URL muda o destino;
# PUBLICO_REF muda o que se publica (por omissao origin/main; por exemplo origin/o-meu-branch).
set -euo pipefail
cd "$(dirname "$0")/.."
REF="${PUBLICO_REF:-origin/main}"
PUBLICO_URL="${PUBLICO_URL:-https://github.com/XSequeiraX/Atom-Bridge-public}"
ORIGEM="$(git rev-parse --short "$REF")"
ASSUNTO="$(git log -1 --format=%s "$REF")"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
if git clone -q "$PUBLICO_URL" "$T/pub" 2>/dev/null && [ -n "$(git -C "$T/pub" rev-parse --verify -q HEAD || true)" ]; then
    :
else
    rm -rf "$T/pub"; git init -q -b main "$T/pub"; git -C "$T/pub" remote add origin "$PUBLICO_URL"
fi
git -C "$T/pub" rm -rq --ignore-unmatch . >/dev/null 2>&1 || true
git archive "$REF" | tar -x -C "$T/pub"
git -C "$T/pub" add -A -f            # -f: ficheiros versionados que o .gitignore tambem apanharia (*.bin, *.csv)
if git -C "$T/pub" diff --cached --quiet; then echo "o publico ja esta em dia (origem $ORIGEM)"; exit 0; fi
git -C "$T/pub" diff --cached --stat | tail -5
[ "${1:-}" = "--push" ] || { echo "(so a mostrar: --push para enviar)"; exit 0; }
git -C "$T/pub" -c user.name="${GIT_AUTHOR_NAME:-$(git config user.name)}" -c user.email="${GIT_AUTHOR_EMAIL:-$(git config user.email)}" \
    commit -q -m "Sincronizado com o privado $ORIGEM: $ASSUNTO"
git -C "$T/pub" push -q origin HEAD:main
echo "publicado: $(git -C "$T/pub" rev-parse --short HEAD) (origem $ORIGEM)"

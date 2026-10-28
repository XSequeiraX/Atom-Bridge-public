#!/bin/bash
# No Atom: puxa a ultima versao do repositorio e reinstala (o instalador e idempotente).
#   bash atualizar_atom.sh              puxa e instala
#   bash atualizar_atom.sh --so-puxar   so o git pull, sem instalar
# A instalacao corre como servico (systemd-run): uma ligacao SSH que cai a meio nao a interrompe. O registo:
#   journalctl -u bridge-atualizar -f
set -e
cd "$(dirname "$0")"
# Um git que morreu a meio (corte de energia) deixa o index.lock e todos os pull seguintes falham: so se apaga com
# nenhum git a correr.
if [ -f .git/index.lock ] && ! pgrep -x git >/dev/null; then
    rm -f .git/index.lock
fi
# Repositorio de onde se actualiza: o publico (sem autenticacao). BRIDGE_REPO_URL muda-o: o clone pode ter vindo de outro.
URL="${BRIDGE_REPO_URL:-https://github.com/XSequeiraX/Atom-Bridge-public}"
BRANCH="main"
export GIT_TERMINAL_PROMPT=0            # um repositorio que pede login falha logo, em vez de ficar a espera
git remote set-url origin "$URL" 2>/dev/null || git remote add origin "$URL"
git fetch origin "$BRANCH"
if git merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    git merge --ff-only "origin/$BRANCH"
else
    echo "A historia e diferente do repositorio novo: o clone passa a segui-lo."
    # Nada do que esta no clone se perde: os commits locais ficam num ramo de reserva e o que se mexeu a mao (ficheiros
    # alterados e novos) numa stash, antes de qualquer reset. Diz-se onde ficaram.
    SALVA="$(date +%Y%m%d-%H%M%S)"
    if [ -n "$(git rev-list --max-count=1 "origin/$BRANCH..HEAD" 2>/dev/null)" ]; then
        git branch "backup-$SALVA" HEAD || { echo "ERRO: nao consegui guardar os commits locais: nada foi alterado."; exit 1; }
        echo "Os commits locais ficaram no ramo backup-$SALVA (git log backup-$SALVA)."
    fi
    if [ -n "$(git status --porcelain)" ]; then
        git stash push -q -u -m "atualizar_atom: alteracoes locais antes de seguir o repositorio novo ($SALVA)" \
            || { echo "ERRO: nao consegui guardar as alteracoes locais (git stash): nada foi alterado."; exit 1; }
        echo "As alteracoes locais ficaram na stash (git stash list)."
    fi
    git checkout -q -f -B "$BRANCH" "origin/$BRANCH"
    git reset --hard "origin/$BRANCH"
fi
git branch --set-upstream-to="origin/$BRANCH" "$BRANCH" || true
[ "${1:-}" = "--so-puxar" ] && exit 0
# Sem "sudo -E": o ambiente do utilizador (PYTHONPATH...) nao vai para o instalador que corre como root.
sudo systemd-run --unit bridge-atualizar --collect --quiet --wait --pipe \
    bash "$(pwd)/atom-install/install_atom.sh" </dev/null

#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# install.sh — instala o backup-runner via pipx
#
# Variáveis:
#   REPO_DIR   caminho do clone local (padrão: a pasta deste script)
#   FORCE      "1" para reinstalar por cima
#
# Depois daqui, rode `backup-runner install` para o cron e o supervisord.
# -----------------------------------------------------------------------------
set -euo pipefail

LAVENDER='\033[38;5;147m'
DIM='\033[2m'
RED='\033[38;5;203m'
GREEN='\033[38;5;114m'
RESET='\033[0m'

die() { printf "${RED}✗${RESET} %s\n" "$1" >&2; exit 1; }
ok()  { printf "${GREEN}✓${RESET} %s\n" "$1"; }
hint(){ printf "${DIM}› %s${RESET}\n" "$1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
FORCE_FLAG=""
[ "${FORCE:-}" = "1" ] && FORCE_FLAG="--force"

command -v python3 >/dev/null 2>&1 || die "python3 não encontrado"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYMAJOR=${PYVER%%.*}; PYMINOR=${PYVER##*.}
if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 10 ]; }; then
    die "python >= 3.10 necessário (tem $PYVER)"
fi
ok "python $PYVER"

command -v mysql   >/dev/null 2>&1 || hint "cliente mysql não encontrado no PATH"
command -v mysqldump >/dev/null 2>&1 || hint "mysqldump não encontrado no PATH"

if ! command -v pipx >/dev/null 2>&1; then
    hint "instalando o pipx via pip --user"
    python3 -m pip install --user pipx
    python3 -m pipx ensurepath
    export PATH="$HOME/.local/bin:$PATH"
fi
ok "pipx pronto"

hint "instalando de $REPO_DIR"
pipx install $FORCE_FLAG --python python3 "$REPO_DIR"

if command -v backup-runner >/dev/null 2>&1; then
    ok "$(backup-runner --version)"
    hint "abra com: backup-runner"
    hint "depois: backup-runner install   (cron e supervisord)"
else
    die "backup-runner fora do PATH, talvez seja preciso reabrir o shell"
fi

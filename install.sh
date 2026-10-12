#!/usr/bin/env bash
# Instalador do Programador por Voz
# Uso: curl -fsSL https://raw.githubusercontent.com/victor-germano/jarvis-assistente-de-voz/main/install.sh | bash

set -e

REPO="https://github.com/victor-germano/jarvis-assistente-de-voz"
BRANCH="claude/voice-command-system-w3g3wa"
DEST="$HOME/programador_por_voz"

GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; BOLD="\033[1m"; RESET="\033[0m"
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $1"; }
err()  { echo -e "  ${RED}✗${RESET} $1"; exit 1; }
step() { echo -e "\n${BOLD}$1${RESET}"; }

echo -e "\n${BOLD}============================================================"
echo   "  Programador por Voz — Instalador"
echo -e "============================================================${RESET}"

# ── Pré-requisitos ─────────────────────────────────────────────────────────────
step "Verificando pré-requisitos..."

command -v python3 >/dev/null || err "Python 3 não encontrado. Instale em: https://python.org"
command -v git     >/dev/null || err "Git não encontrado. Instale em: https://git-scm.com"
ok "Python $(python3 --version | cut -d' ' -f2)"
ok "Git $(git --version | cut -d' ' -f3)"

command -v node >/dev/null && ok "Node.js $(node --version)" || warn "Node.js não encontrado — MCP servers não serão instalados"
command -v claude >/dev/null && ok "Claude Code $(claude --version 2>/dev/null | head -1)" || warn "Claude Code não encontrado — instale após o setup"

# ── Clone ──────────────────────────────────────────────────────────────────────
step "Baixando o projeto..."

if [ -d "$DEST" ]; then
    warn "Pasta $DEST já existe. Atualizando..."
    git -C "$DEST" fetch origin "$BRANCH" --quiet
    git -C "$DEST" checkout "$BRANCH" --quiet
    git -C "$DEST" pull origin "$BRANCH" --quiet
else
    git clone --branch "$BRANCH" "$REPO" "$DEST" --quiet
fi
ok "Projeto em $DEST"
cd "$DEST"

# ── Setup automático ───────────────────────────────────────────────────────────
step "Rodando setup automático..."
python3 setup.py --whisper-model small

# ── Claude Code ────────────────────────────────────────────────────────────────
if ! command -v claude >/dev/null; then
    step "Instalando Claude Code..."
    if command -v npm >/dev/null; then
        npm install -g @anthropic-ai/claude-code
        ok "Claude Code instalado."
        echo ""
        warn "Execute agora: claude auth login"
    else
        warn "npm não disponível — instale Claude Code manualmente:"
        echo "    npm install -g @anthropic-ai/claude-code"
        echo "    claude auth login"
    fi
fi

# ── Finalização ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}Instalação concluída!${RESET}"
echo ""
echo -e "Para iniciar: ${BOLD}cd $DEST && python3 main.py${RESET}"
echo ""

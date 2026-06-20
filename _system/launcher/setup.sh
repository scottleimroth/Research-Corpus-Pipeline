#!/usr/bin/env bash
# ResearchPapersPipeline setup.sh
# Linux (Debian/Ubuntu) and macOS bootstrap. Run once per machine.
#
# Usage:
#   cd /path/to/ResearchPapersPipeline
#   ./setup.sh
#
# What it does:
#   1. Reports OS + Python version
#   2. Checks for the four required system binaries; installs missing via apt/brew
#   3. Creates a Python venv inside the folder if none exists
#   4. Installs Python packages from requirements.txt into the venv
#   5. Prompts for ANTHROPIC_API_KEY if not set (writes to ~/.bashrc)
#   6. Runs config.py self-check

set -uo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
NC="\033[0m"

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[..]${NC} $1"; }
bad()  { echo -e "  ${RED}[!!]${NC} $1"; }
hdr()  { echo -e "${CYAN}$1${NC}"; }

echo ""
hdr "========================================================"
hdr " ResearchPapersPipeline setup"
hdr "========================================================"
echo ""
echo "Folder: $SCRIPT_DIR"
echo ""

# ---------- detect OS ----------
OS="unknown"
PKG=""
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if command -v apt-get &>/dev/null; then
        OS="debian"; PKG="apt"
    elif command -v dnf &>/dev/null; then
        OS="fedora"; PKG="dnf"
    elif command -v pacman &>/dev/null; then
        OS="arch"; PKG="pacman"
    else
        OS="linux"; PKG=""
    fi
elif [[ "$OSTYPE" == "darwin"* ]]; then
    OS="macos"
    if command -v brew &>/dev/null; then PKG="brew"; fi
fi

# ---------- 1. environment ----------
hdr "[1/6] Environment"
ok "OS: $OS  ($OSTYPE)"
if command -v python3 &>/dev/null; then
    ok "Python: $(python3 --version)"
    PY=python3
else
    bad "python3 not found. Install Python 3.10+ first."
    exit 1
fi

# ---------- 2. system binaries ----------
echo ""
hdr "[2/6] System binaries (Tesseract, Poppler, Ghostscript, ocrmypdf)"

install_pkg() {
    local pkgs="$1"
    case "$PKG" in
        apt)    sudo apt-get install -y $pkgs ;;
        dnf)    sudo dnf install -y $pkgs ;;
        pacman) sudo pacman -S --noconfirm $pkgs ;;
        brew)   brew install $pkgs ;;
        *)      bad "No supported package manager found. Install manually: $pkgs"; return 1 ;;
    esac
}

if command -v tesseract &>/dev/null; then
    ok "tesseract found: $(command -v tesseract)"
else
    warn "tesseract missing - installing"
    if [[ "$PKG" == "apt" ]]; then
        install_pkg "tesseract-ocr"
    else
        install_pkg "tesseract"
    fi
    command -v tesseract &>/dev/null && ok "tesseract installed" || bad "tesseract install failed"
fi

if command -v pdftotext &>/dev/null; then
    ok "pdftotext found: $(command -v pdftotext)"
else
    warn "pdftotext missing - installing"
    if [[ "$PKG" == "apt" ]]; then
        install_pkg "poppler-utils"
    else
        install_pkg "poppler"
    fi
    command -v pdftotext &>/dev/null && ok "pdftotext installed" || bad "poppler install failed"
fi

if command -v gs &>/dev/null; then
    ok "ghostscript found: $(command -v gs)"
else
    warn "ghostscript missing - installing"
    install_pkg "ghostscript"
    command -v gs &>/dev/null && ok "ghostscript installed" || bad "ghostscript install failed"
fi

# ---------- 3. Python venv ----------
echo ""
hdr "[3/6] Python virtual environment"
VENV="$SCRIPT_DIR/venv"
if [[ -x "$VENV/bin/python" ]]; then
    ok "venv exists at $VENV"
else
    warn "Creating venv at $VENV"
    $PY -m venv "$VENV"
    ok "venv created"
fi
VENV_PY="$VENV/bin/python"

# ---------- 4. Python packages ----------
echo ""
hdr "[4/6] Python packages (requirements.txt)"
REQ="$SCRIPT_DIR/requirements.txt"
if [[ ! -f "$REQ" ]]; then
    bad "requirements.txt not found at $REQ"
    exit 1
fi
"$VENV_PY" -m pip install --upgrade pip --quiet
if "$VENV_PY" -m pip install -r "$REQ"; then
    ok "Python packages installed into venv"
else
    bad "pip install failed. See output above."
fi

if [[ -x "$VENV/bin/ocrmypdf" ]]; then
    ok "ocrmypdf installed at $VENV/bin/ocrmypdf"
else
    bad "ocrmypdf not found in venv after install"
fi

# ---------- 5. ANTHROPIC_API_KEY ----------
echo ""
hdr "[5/6] Anthropic API key (for Claude metadata fallback tier)"
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    ok "ANTHROPIC_API_KEY is set (length: ${#ANTHROPIC_API_KEY})"
else
    warn "ANTHROPIC_API_KEY not set."
    read -p "  Paste your sk-ant-... key now (or press Enter to skip): " RESP
    if [[ "$RESP" == sk-ant-* ]]; then
        echo "" >> ~/.bashrc
        echo "# Added by ResearchPapersPipeline setup.sh" >> ~/.bashrc
        echo "export ANTHROPIC_API_KEY=\"$RESP\"" >> ~/.bashrc
        export ANTHROPIC_API_KEY="$RESP"
        ok "API key appended to ~/.bashrc and exported for this session"
    elif [[ -n "$RESP" ]]; then
        bad "Doesn't look like a valid sk-ant- key. Skipped."
    else
        warn "Skipped. Claude tier disabled until key is set."
    fi
fi

# ---------- 6. config.py self-check ----------
echo ""
hdr "[6/6] Pipeline config self-check"
"$VENV_PY" "$SCRIPT_DIR/config.py"

echo ""
hdr "========================================================"
hdr " Setup complete."
hdr "========================================================"
echo ""
echo "To use the pipeline, activate the venv:"
echo "  source $VENV/bin/activate"
echo ""
echo "Then run e.g.:"
echo "  python pipeline/add_paper.py papers-staging/*.pdf --extract-metadata"
echo ""
echo "If a binary install failed above, see README.md for manual steps."

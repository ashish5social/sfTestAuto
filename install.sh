#!/usr/bin/env bash
# ============================================================
#  sfauto – Installer
#
#  Auto-detects environment:
#    Developer (has .git)  → editable install, localhost instructions
#    VPS/Server (no .git)  → stops old app, installs fresh, server instructions
#
#  Usage:  chmod +x install.sh && ./install.sh
# ============================================================

set -e

# Suppress interactive service-restart prompts on Ubuntu (needrestart)
export NEEDRESTART_MODE=a
export DEBIAN_FRONTEND=noninteractive

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()      { echo -e "  ${GREEN}✔${NC} $1"; }
warn()    { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail()    { echo -e "  ${RED}✖${NC} $1"; }
header()  { echo -e "\n${CYAN}${BOLD}── $1${NC}"; }

REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=11

SFAUTO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SFAUTO_DIR"

# --------------------------------------------------
# Detect mode
# --------------------------------------------------
if [ -d ".git" ]; then
    MODE="developer"
else
    MODE="server"
fi

echo ""
echo "=============================================="
echo "  sfauto – Installer"
echo "=============================================="
if [ "$MODE" = "developer" ]; then
    echo "  Mode:     Developer  (git repo detected)"
else
    echo "  Mode:     Server/VPS (package deployment)"
fi
echo "  Location: ${SFAUTO_DIR}"
echo "=============================================="

# ==================================================================
# SERVER MODE: Stop running Salesforce processes and preserve .env
# ==================================================================
if [ "$MODE" = "server" ]; then
    header "Stopping previous Salesforce processes"

    # Kill uvicorn processes running the sfauto web app
    CCI_PIDS=$(ps aux 2>/dev/null | grep -E "uvicorn.*src\.web\.app" | grep -v grep | awk '{print $2}' || true)
    if [ -n "$CCI_PIDS" ]; then
        echo "$CCI_PIDS" | xargs kill -TERM 2>/dev/null || true
        sleep 2
        REMAINING=$(ps aux 2>/dev/null | grep -E "uvicorn.*src\.web\.app" | grep -v grep | awk '{print $2}' || true)
        if [ -n "$REMAINING" ]; then
            echo "$REMAINING" | xargs kill -9 2>/dev/null || true
        fi
        ok "Stopped running Salesforce web dashboard"
    else
        ok "No running Salesforce web dashboard found"
    fi

    # Kill stuck pytest
    PYTEST_PIDS=$(ps aux 2>/dev/null | grep -E "pytest.*tests/(generated|definitions)" | grep -v grep | awk '{print $2}' || true)
    if [ -n "$PYTEST_PIDS" ]; then
        echo "$PYTEST_PIDS" | xargs kill -TERM 2>/dev/null || true
        ok "Stopped stuck pytest processes"
    fi

    # Preserve .env from previous install
    if [ ! -f "${SFAUTO_DIR}/.env" ]; then
        header "Looking for previous .env"
        PREV_ENV=""
        for candidate in "${HOME}/sfauto/.env" $(ls -dt ${HOME}/sfauto_*/.env 2>/dev/null | head -1); do
            if [ -f "$candidate" ] 2>/dev/null; then
                PREV_ENV="$candidate"
                break
            fi
        done

        if [ -n "$PREV_ENV" ]; then
            cp "$PREV_ENV" "${SFAUTO_DIR}/.env"
            ok "Copied .env from previous installation (${PREV_ENV})"
        fi
    fi
fi

# ==================================================================
# COMMON: System packages (Linux only)
# ==================================================================
if [[ "$OSTYPE" == "linux"* ]] && command -v apt-get &>/dev/null; then
    header "Checking system packages"

    PKGS_NEEDED=""
    dpkg -s python3-venv &>/dev/null 2>&1 || PKGS_NEEDED="$PKGS_NEEDED python3-venv"
    dpkg -s python3-pip  &>/dev/null 2>&1 || PKGS_NEEDED="$PKGS_NEEDED python3-pip"

    if [ -n "$PKGS_NEEDED" ]; then
        echo "  Installing:$PKGS_NEEDED"
        if [ "$(id -u)" -eq 0 ]; then
            apt-get update -qq && apt-get install -y -qq $PKGS_NEEDED
        elif command -v sudo &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y -qq $PKGS_NEEDED
        else
            fail "Cannot install$PKGS_NEEDED — run as root or install manually"
            exit 1
        fi
        ok "System packages installed"
    else
        ok "System packages already present"
    fi
fi

# ==================================================================
# COMMON: Locate Python 3.11+
# ==================================================================
header "Locating Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+"

PYTHON_CMD=""
for candidate in python3 python python3.13 python3.12 python3.11; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge "$REQUIRED_PYTHON_MAJOR" ] && [ "$minor" -ge "$REQUIRED_PYTHON_MINOR" ]; then
            PYTHON_CMD="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    if [[ "$OSTYPE" == "linux"* ]] && command -v apt-get &>/dev/null; then
        warn "Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ not found — installing Python 3.11..."
        if [ "$(id -u)" -eq 0 ]; then
            apt-get install -y -qq software-properties-common 2>/dev/null
            add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
            apt-get update -qq
            apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
        elif command -v sudo &>/dev/null; then
            sudo apt-get install -y -qq software-properties-common 2>/dev/null
            sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
            sudo apt-get update -qq
            sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
        fi
        if command -v python3.11 &>/dev/null; then
            PYTHON_CMD="python3.11"
            ok "Python 3.11 installed"
        fi
    fi

    if [ -z "$PYTHON_CMD" ]; then
        fail "Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}+ is required but not found."
        echo "    Ubuntu:  sudo apt install software-properties-common && sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11 python3.11-venv"
        echo "    macOS:   https://www.python.org/downloads/"
        exit 1
    fi
fi

PYTHON_VER=$("$PYTHON_CMD" --version 2>&1)
ok "Found $PYTHON_VER ($PYTHON_CMD)"

# ==================================================================
# COMMON: Virtual environment
# ==================================================================
header "Setting up virtual environment"

VENV_DIR="venv"
if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
    ok "Virtual environment already exists"
else
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

source "$VENV_DIR/bin/activate"
ok "Virtual environment activated"

# ==================================================================
# COMMON: Upgrade pip + install dependencies
# ==================================================================
header "Installing dependencies"

pip install --upgrade pip --quiet
ok "pip upgraded ($(pip --version | awk '{print $2}'))"

pip install -e . --quiet
ok "Project dependencies installed (editable mode)"

if command -v pytest &>/dev/null; then
    ok "pytest $(pytest --version 2>&1 | head -1 | awk '{print $2}') available"
else
    fail "pytest was not installed correctly"
    exit 1
fi

# ==================================================================
# COMMON: Playwright browsers
# ==================================================================
header "Installing Playwright browsers"

# The dashboard's browser selector + the GitHub Actions workflow both
# offer Chrome / Edge / Firefox / Safari(WebKit), so install all four
# now to avoid an "Executable doesn't exist" surprise when someone
# switches the browser dropdown later. Total download is ~600 MB and
# takes 2-3 minutes on a decent connection. Set CCI_INSTALL_ONLY=chromium
# in the env to install just Chromium and skip the rest (faster, lighter).
BROWSERS="${CCI_INSTALL_BROWSERS:-chromium chrome firefox webkit msedge}"

if [[ "$OSTYPE" == "linux"* ]]; then
    if [ "$(id -u)" -eq 0 ]; then
        playwright install-deps $BROWSERS 2>/dev/null || warn "Some system deps may need manual install"
    elif command -v sudo &>/dev/null; then
        sudo playwright install-deps $BROWSERS 2>/dev/null || warn "Some system deps may need manual install"
    else
        warn "Cannot install system deps — you may need: sudo playwright install-deps $BROWSERS"
    fi
fi

playwright install $BROWSERS
ok "Playwright browsers installed ($BROWSERS)"

# ==================================================================
# COMMON: .env configuration
# ==================================================================
header "Configuration"

IS_VPS=false
if [[ "$OSTYPE" == "linux"* ]] && [ ! -d "/Applications" ] && [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    IS_VPS=true
fi

if [ -f ".env" ]; then
    ok ".env already exists"
else
    if [ -f ".env.example" ]; then
        cp .env.example .env
        warn ".env created from .env.example — edit with your Salesforce credentials"
    else
        if [ "$IS_VPS" = true ]; then
            HEADLESS_DEFAULT="true"
        else
            HEADLESS_DEFAULT="false"
        fi
        cat > .env <<ENVEOF
# Salesforce Credentials
SF_LOGIN_URL=https://login.salesforce.com
SF_USERNAME=your_username@example.com
SF_PASSWORD=your_password
SF_ORG_ID=

# Browser (set to true to run tests without opening Chrome)
BROWSER_HEADLESS=${HEADLESS_DEFAULT}

# Dashboard
DASHBOARD_PORT=8091
DASHBOARD_HOST=0.0.0.0
ENVEOF
        warn ".env created — edit with your Salesforce credentials"
        if [ "$IS_VPS" = true ]; then
            ok "Detected VPS — BROWSER_HEADLESS set to true"
        fi
    fi
fi

# ==================================================================
# COMMON: Required directories
# ==================================================================
mkdir -p tests/ui/data tests/api/data reports screenshots videos_tmp
ok "Required directories verified"

# ==================================================================
# SERVER MODE: Create ~/sfauto symlink
# ==================================================================
if [ "$MODE" = "server" ]; then
    header "Server setup"
    SYMLINK="${HOME}/sfauto"

    if [ -L "$SYMLINK" ]; then
        rm "$SYMLINK"
        ln -s "$SFAUTO_DIR" "$SYMLINK"
        ok "Updated symlink: ~/sfauto -> ${SFAUTO_DIR}"
    elif [ -d "$SYMLINK" ] && [ "$(readlink -f "$SYMLINK" 2>/dev/null || echo "$SYMLINK")" != "$SFAUTO_DIR" ]; then
        warn "~/sfauto is an existing directory (old installation)"
        warn "Consider: rm -rf ~/sfauto && re-run this installer"
    elif [ ! -e "$SYMLINK" ]; then
        ln -s "$SFAUTO_DIR" "$SYMLINK"
        ok "Created symlink: ~/sfauto -> ${SFAUTO_DIR}"
    fi
fi

# ==================================================================
# DONE — Print usage instructions
# ==================================================================
echo ""
echo "=============================================="
echo -e "  ${GREEN}${BOLD}Installation complete!${NC}"
echo "=============================================="

if [ "$MODE" = "developer" ]; then
    echo ""
    echo -e "  ${BOLD}Editable install${NC} — code changes reflect immediately."
    echo "  No need to re-run install after editing Python files."
    echo ""
    echo -e "  ${CYAN}${BOLD}Next steps:${NC}"
    echo ""
    echo "  1. Configure credentials:"
    echo "       nano .env"
    echo ""
    echo "  2. Activate the virtual environment:"
    echo "       source venv/bin/activate"
    echo ""
    echo -e "  ${CYAN}${BOLD}Start the web dashboard (localhost):${NC}"
    echo "       sfauto server"
    echo "       # Open http://localhost:8091"
    echo ""
    echo -e "  ${CYAN}${BOLD}Run a test:${NC}"
    echo "       sfauto test tests/"
    echo "       sfauto test tests/ --headless"
    echo ""
else
    echo ""
    echo "  Installed at: ${SFAUTO_DIR}"
    if [ -L "${HOME}/sfauto" ]; then
        echo "  Symlink:      ~/sfauto"
    fi
    echo ""
    echo -e "  ${CYAN}${BOLD}Next steps:${NC}"
    echo ""
    echo "  1. Configure credentials (if not already done):"
    echo "       nano ${SFAUTO_DIR}/.env"
    echo ""
    echo "  2. Activate the virtual environment:"
    echo "       cd ${SFAUTO_DIR} && source venv/bin/activate"
    echo ""
    echo -e "  ${CYAN}${BOLD}Start the server (foreground):${NC}"
    echo "       sfauto server"
    echo "       # Access from browser: http://<your-vps-ip>:8091"
    echo ""
    echo -e "  ${CYAN}${BOLD}Start the server (background, survives SSH disconnect):${NC}"
    echo "       nohup sfauto server > server.log 2>&1 &"
    echo "       # Logs:  tail -f server.log"
    echo "       # Access: http://<your-vps-ip>:8091"
    echo ""
    echo -e "  ${CYAN}${BOLD}Stop the server:${NC}"
    echo "       pkill -f 'uvicorn.*src.web.app'"
    echo ""
    echo -e "  ${CYAN}${BOLD}Run a test:${NC}"
    echo "       sfauto test tests/ --headless"
    echo ""
fi

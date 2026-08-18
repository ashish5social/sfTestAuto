#!/usr/bin/env bash
# ============================================================
#  sfauto – Create Installable Package
#
#  Run on developer laptop to create a distributable zip file.
#  The zip can be scp'd to a VPS and installed with ./install.sh
#
#  Usage:  chmod +x create_installable_package.sh
#          ./create_installable_package.sh
#
#  Output: sfauto_DDMMYY_HHMM.zip  (in the current directory)
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✔ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✖ $1${NC}"; exit 1; }

# --------------------------------------------------
# 1. Determine package name with timestamp
# --------------------------------------------------
TIMESTAMP=$(date +"%d%m%y_%H%M")
PKG_NAME="sfauto_${TIMESTAMP}"
PKG_DIR="${PKG_NAME}"
ZIP_FILE="${PKG_NAME}.zip"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "=============================================="
echo "  Salesforce – Creating Installable Package"
echo "  Package: ${ZIP_FILE}"
echo "=============================================="
echo ""

# --------------------------------------------------
# 2. Safety checks
# --------------------------------------------------
if [ -d "$PKG_DIR" ]; then
    fail "Directory ${PKG_DIR} already exists. Remove it first or wait a minute."
fi

if [ -f "$ZIP_FILE" ]; then
    fail "File ${ZIP_FILE} already exists. Remove it first or wait a minute."
fi

# Verify we're in the sfauto project root
if [ ! -f "${SCRIPT_DIR}/pyproject.toml" ]; then
    fail "pyproject.toml not found. Run this script from the sfauto project root."
fi

# --------------------------------------------------
# 3. Create temporary package directory
# --------------------------------------------------
echo "Creating package directory: ${PKG_DIR}/"
mkdir -p "${PKG_DIR}"

# --------------------------------------------------
# 4. Copy project files (excluding build artifacts,
#    reports, screenshots, videos, venv, .git, etc.)
# --------------------------------------------------
echo "Copying project files..."

# Core project files
cp "${SCRIPT_DIR}/pyproject.toml"      "${PKG_DIR}/"
cp "${SCRIPT_DIR}/README.md"           "${PKG_DIR}/"
cp "${SCRIPT_DIR}/DESIGNDOCUMENT.md"   "${PKG_DIR}/"
cp "${SCRIPT_DIR}/CLAUDE.md"           "${PKG_DIR}/" 2>/dev/null || true
cp "${SCRIPT_DIR}/.gitignore"          "${PKG_DIR}/" 2>/dev/null || true
cp "${SCRIPT_DIR}/.env.example"        "${PKG_DIR}/" 2>/dev/null || true
ok "Project metadata copied"

# Install scripts
cp "${SCRIPT_DIR}/install.sh"          "${PKG_DIR}/"
cp "${SCRIPT_DIR}/install.ps1"         "${PKG_DIR}/" 2>/dev/null || true
chmod +x "${PKG_DIR}/install.sh"
ok "Install scripts copied"

# Source code (src/ — includes install scripts and all source)
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' --exclude='.DS_Store' \
    "${SCRIPT_DIR}/src/" "${PKG_DIR}/src/"
ok "src/ copied"

# Tests (all existing tests, conftest, data, definitions)
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
    "${SCRIPT_DIR}/tests/" "${PKG_DIR}/tests/"
ok "tests/ copied"

# Scripts (CI helper scripts)
if [ -d "${SCRIPT_DIR}/scripts" ]; then
    rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
        "${SCRIPT_DIR}/scripts/" "${PKG_DIR}/scripts/"
    ok "scripts/ copied"
fi

# GitHub Actions workflow
if [ -d "${SCRIPT_DIR}/.github" ]; then
    rsync -a "${SCRIPT_DIR}/.github/" "${PKG_DIR}/.github/"
    ok ".github/ copied"
fi

# Skills (Cowork/Claude Code skills)
if [ -d "${SCRIPT_DIR}/skills" ]; then
    rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
        "${SCRIPT_DIR}/skills/" "${PKG_DIR}/skills/"
    ok "skills/ copied"
fi

# --------------------------------------------------
# 5. Create empty directories that the tool expects
# --------------------------------------------------
mkdir -p "${PKG_DIR}/reports"
mkdir -p "${PKG_DIR}/screenshots"
mkdir -p "${PKG_DIR}/videos_tmp"
touch "${PKG_DIR}/reports/.gitkeep"
touch "${PKG_DIR}/screenshots/.gitkeep"
touch "${PKG_DIR}/videos_tmp/.gitkeep"
ok "Empty output directories created"

# Initialize a clean test_run_history.json
echo "[]" > "${PKG_DIR}/test_run_history.json"
ok "Clean test_run_history.json created"

# --------------------------------------------------
# 6. Verify critical files are present
# --------------------------------------------------
echo ""
echo "Verifying package contents..."
MISSING=0

for required in \
    "pyproject.toml" \
    "install.sh" \
    "src/cli.py" \
    "src/core/config.py" \
    "src/core/step_tracker.py" \
    "src/core/html_reporter.py" \
    "src/core/playwright_helpers.py" \
    "src/web/app.py" \
    "src/web/routes/generated_tests.py" \
    "src/web/frontend/runner.html" \
    "tests/conftest.py" \
    "README.md" \
; do
    if [ ! -f "${PKG_DIR}/${required}" ]; then
        fail "MISSING: ${required}"
        MISSING=$((MISSING + 1))
    fi
done

if [ "$MISSING" -gt 0 ]; then
    fail "${MISSING} required file(s) missing. Package not created."
fi
ok "All critical files present"

# --------------------------------------------------
# 7. Show what's NOT included (sanity check)
# --------------------------------------------------
echo ""
echo "Excluded from package (as expected):"
for excluded in venv .git __pycache__ reports/*.html screenshots/*.png videos_tmp/*.webm .env temp node_modules; do
    echo "  ✗ ${excluded}"
done

# --------------------------------------------------
# 8. Create the zip file
# --------------------------------------------------
echo ""
echo "Creating ${ZIP_FILE}..."
zip -r -q "${ZIP_FILE}" "${PKG_DIR}/" -x "*/.DS_Store"
ok "Package created: ${ZIP_FILE}"

# --------------------------------------------------
# 9. Show package size and file count
# --------------------------------------------------
FILE_COUNT=$(find "${PKG_DIR}" -type f | wc -l | tr -d ' ')
ZIP_SIZE=$(du -h "${ZIP_FILE}" | cut -f1)
echo "  Files: ${FILE_COUNT}"
echo "  Size:  ${ZIP_SIZE}"

# --------------------------------------------------
# 10. Clean up the temporary directory
# --------------------------------------------------
echo ""
echo "Cleaning up temporary directory..."
rm -rf "${PKG_DIR}"
ok "Temporary directory ${PKG_DIR}/ removed"

# --------------------------------------------------
# Done
# --------------------------------------------------
echo ""
echo "=============================================="
echo -e "  ${GREEN}Package ready: ${ZIP_FILE}${NC}"
echo "=============================================="
echo ""
echo "Deploy to VPS:"
echo "  scp ${ZIP_FILE} root@<your-vps-ip>:~/"
echo "  ssh root@<your-vps-ip>"
echo "  unzip ${ZIP_FILE}"
echo "  cd ${PKG_NAME}"
echo "  chmod +x install.sh && ./install.sh"
echo ""

# ============================================================
#  sfauto – Installer for Windows
#  Run:  powershell -ExecutionPolicy Bypass -File install.ps1
# ============================================================

$ErrorActionPreference = "Stop"

$RequiredMajor = 3
$RequiredMinor = 11

function Write-Ok($msg)   { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg)  { Write-Host "[FAIL] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  sfauto - Installer (Windows)"    -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------
# 1. Locate Python 3.11+
# --------------------------------------------------
$pythonCmd = $null

foreach ($candidate in @("python", "python3", "py")) {
    try {
        $verOutput = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($verOutput -match '^(\d+)\.(\d+)$') {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge $RequiredMajor -and $minor -ge $RequiredMinor) {
                $pythonCmd = $candidate
                break
            }
        }
    } catch {
        continue
    }
}

# Also try the py launcher with version flag
if (-not $pythonCmd) {
    try {
        $verOutput = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($verOutput -match '^(\d+)\.(\d+)$') {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge $RequiredMajor -and $minor -ge $RequiredMinor) {
                $pythonCmd = "py -3"
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Fail "Python ${RequiredMajor}.${RequiredMinor}+ is required but not found."
    Write-Host "    Please install Python from https://www.python.org/downloads/"
    Write-Host "    Make sure to check 'Add Python to PATH' during installation."
    exit 1
}

$pythonVer = & $pythonCmd --version 2>&1
Write-Ok "Found $pythonVer ($pythonCmd)"

# --------------------------------------------------
# 2. Create / reuse virtual environment
# --------------------------------------------------
$venvDir = "venv"
$venvActivate = Join-Path $venvDir "Scripts\Activate.ps1"

if ((Test-Path $venvDir) -and (Test-Path $venvActivate)) {
    Write-Ok "Virtual environment already exists ($venvDir\)"
} else {
    Write-Host "Creating virtual environment..."
    if ($pythonCmd -eq "py -3") {
        & py -3 -m venv $venvDir
    } else {
        & $pythonCmd -m venv $venvDir
    }
    Write-Ok "Virtual environment created ($venvDir\)"
}

# Activate
& $venvActivate
Write-Ok "Virtual environment activated"

# --------------------------------------------------
# 3. Upgrade pip
# --------------------------------------------------
Write-Host "Upgrading pip..."
pip install --upgrade pip --quiet
Write-Ok "pip is up to date"

# --------------------------------------------------
# 4. Install project dependencies
# --------------------------------------------------
Write-Host "Installing project dependencies..."
pip install -e . --quiet
Write-Ok "Project dependencies installed"

# --------------------------------------------------
# 5. Verify pytest
# --------------------------------------------------
try {
    $pytestVer = pytest --version 2>&1
    Write-Ok "pytest is available"
} catch {
    Write-Fail "pytest was not installed correctly"
    exit 1
}

# --------------------------------------------------
# 6. Install Playwright browsers
# --------------------------------------------------
# Install all four so the dashboard's browser dropdown (Chrome / Edge /
# Firefox / Safari-WebKit) works without "Executable doesn't exist"
# surprises. Total download is ~600 MB. Set $env:CCI_INSTALL_BROWSERS
# to "chromium" to install just Chromium.
$Browsers = if ($env:CCI_INSTALL_BROWSERS) { $env:CCI_INSTALL_BROWSERS } else { "chromium chrome firefox webkit msedge" }
Write-Host "Installing Playwright browsers: $Browsers ..."
$BrowsersArr = $Browsers.Split(" ")
playwright install @BrowsersArr
Write-Ok "Playwright browsers installed ($Browsers)"

# --------------------------------------------------
# 7. Ensure .env file exists
# --------------------------------------------------
if (Test-Path ".env") {
    Write-Ok ".env file already exists"
} elseif (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    Write-Warn ".env file created from .env.example - please edit it with your Salesforce credentials"
} else {
    @"
# Salesforce Credentials
SF_LOGIN_URL=https://login.salesforce.com
SF_USERNAME=your_username@example.com
SF_PASSWORD=your_password
SF_ORG_ID=

# Browser (set to true to run tests without opening Chrome)
BROWSER_HEADLESS=false

# Dashboard
DASHBOARD_PORT=8091
DASHBOARD_HOST=0.0.0.0
"@ | Out-File -FilePath ".env" -Encoding UTF8
    Write-Warn ".env file created - please edit it with your Salesforce credentials"
}

# --------------------------------------------------
# 8. Create required directories
# --------------------------------------------------
foreach ($dir in @("tests\ui\data", "tests\api\data", "reports", "screenshots", "videos_tmp")) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}
Write-Ok "Required directories verified"

# --------------------------------------------------
# Done
# --------------------------------------------------
Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "  Installation complete!"                       -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit .env with your Salesforce credentials"
Write-Host "  2. Activate the virtual environment:"
Write-Host "       .\venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Usage:"
Write-Host "  Run all tests:                 sfauto test tests\"
Write-Host "  Run one test (headed):         sfauto test tests\ui\test_create_account.py"
Write-Host "  Run headless:                  sfauto test tests\ --headless"
Write-Host "  Check your setup:              sfauto doctor"
Write-Host "  Start web dashboard:           sfauto server start   (http://localhost:8091)"
Write-Host ""

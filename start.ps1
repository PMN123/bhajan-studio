<#
.SYNOPSIS
    Starts Bhajan Studio (the local FastAPI app) and opens it in your browser.

.DESCRIPTION
    Starts the app and opens it in your browser. Pick a port, control the browser,
    and decide how uv (the small Python runner it uses) is handled:

      .\start.ps1                start the app and open it in your browser
      .\start.ps1 -Port 9000     use another port (the next free one is used if it is busy)
      .\start.ps1 -NoOpen        don't open the browser
      .\start.ps1 -Yes           don't ask before installing uv
      .\start.ps1 -NoInstall     never install uv; only use a uv that is already on your PATH

    The only thing it may install is uv (https://docs.astral.sh/uv/), and it asks first.
    uv then fetches Python 3.12 and the Python packages into this folder's .venv, once.

.PARAMETER Port
    TCP port to run on. If it is busy, the next free one is used.

.PARAMETER NoOpen
    Don't open the browser.

.PARAMETER Yes
    Don't ask before installing uv.

.PARAMETER NoInstall
    Never install uv; only use a uv that is already on your PATH.

.PARAMETER Help
    Show the usage lines and exit.

.EXAMPLE
    .\start.ps1

    Start the app and open it in your browser.

.EXAMPLE
    .\start.ps1 -Port 9000 -NoOpen

    Start on port 9000 without opening the browser.
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoOpen,
    [switch]$Yes,
    [switch]$NoInstall,
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if ($Help) {
    $usage = @"
  .\start.ps1                start the app and open it in your browser
  .\start.ps1 -Port 9000     use another port (the next free one is used if it is busy)
  .\start.ps1 -NoOpen        don't open the browser
  .\start.ps1 -Yes           don't ask before installing uv
  .\start.ps1 -NoInstall     never install uv; only use a uv that is already on your PATH
"@
    Write-Host $usage
    exit 0
}

function Say([string]$Message) {
    Write-Host $Message
}

function Die([string]$Message) {
    Write-Host "Error: $Message" -ForegroundColor Red
    exit 1
}

if ($PSBoundParameters.ContainsKey('Port')) {
    if ($Port -lt 1 -or $Port -gt 65535) {
        Die "-Port must be between 1 and 65535, e.g. -Port 9000"
    }
}

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        return $cmd.Source
    }
    # Installed earlier but this window hasn't picked up the new PATH yet?
    if ($env:USERPROFILE) {
        foreach ($relative in @('.local\bin\uv.exe', '.cargo\bin\uv.exe')) {
            $candidate = Join-Path $env:USERPROFILE $relative
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
    }
    return $null
}

$UV_INSTALL_CMD = 'powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"'

function Show-UvInstructions {
    Say "Bhajan Studio needs uv, a small free tool that runs Python apps without touching the rest"
    Say "of your computer. To install it, open PowerShell and run:"
    Say ""
    Say "    $UV_INSTALL_CMD"
    Say ""
    Say "then run start.bat (double-click it) or .\start.ps1 again."
    Say "More about uv: https://docs.astral.sh/uv/getting-started/installation/"
}

$uv = Find-Uv
if ($null -eq $uv -and $NoInstall) {
    Say "uv was not found on your PATH."
    Show-UvInstructions
    exit 1
}
if ($null -eq $uv) {
    Say "Bhajan Studio runs with uv, a small free tool that downloads and runs Python apps."
    Say "It isn't installed yet. The official installer (from astral.sh, the makers of uv) puts it"
    Say "in %USERPROFILE%\.local\bin and needs no administrator rights:"
    Say ""
    Say "    $UV_INSTALL_CMD"
    Say ""
    if ($Yes) {
        $answer = 'y'
    } else {
        try {
            if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
                Say "No terminal to ask in; run .\start.ps1 -Yes to install without asking."
                exit 1
            }
            Write-Host "Install uv now? [Y/n] " -NoNewline
            $answer = Read-Host
        } catch {
            Say "No terminal to ask in; run .\start.ps1 -Yes to install without asking."
            exit 1
        }
    }
    if ($null -ne $answer) { $ans = $answer.Trim().ToLower() } else { $ans = '' }
    if (-not ($ans -eq '' -or $ans -eq 'y' -or $ans -eq 'yes')) {
        Say "OK, not installing. To install uv later, run the command above in PowerShell, then start Bhajan Studio again."
        exit 1
    }
    Say ""
    Say "Installing uv now..."
    $ok = $false
    try {
        & powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE -eq 0) {
            $ok = $true
        }
    } catch {
        $ok = $false
    }
    if (-not $ok) {
        Say ""
        Say "Installing uv failed (are you online?)."
        Show-UvInstructions
        exit 1
    }
    # Add the new install dir to PATH for this process only, then look for uv again.
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if ($env:Path) {
        $env:Path = $uvBin + ';' + $env:Path
    } else {
        $env:Path = $uvBin
    }
    $uv = Find-Uv
    if ($null -eq $uv) {
        Say "uv was installed but can't be found; open a new PowerShell window and try again."
        exit 1
    }
    Say ""
}

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Say "First run: uv will now download Python 3.12 and the app's packages, including ffmpeg and the"
    Say "Rubber Band pitch shifter (about 100 MB to download, about 350 MB on disk). This happens once:"
    Say "under a minute on a fast connection, a few minutes on a slow one. Later starts take a few seconds."
    Say ""
    Say "Reading lyrics from sheet music (OCR) on Windows needs tesseract. It is optional (for example:"
    Say "winget install UB-Mannheim.TesseractOCR); everything else works without it."
    Say ""
}

# Only use uv's own Python builds: never a random system python. Pin 3.12, the version this app is tested with.
if (-not $env:UV_PYTHON_PREFERENCE) { $env:UV_PYTHON_PREFERENCE = 'only-managed' }
# Slow connections: give package downloads more time before uv gives up.
if (-not $env:UV_HTTP_TIMEOUT) { $env:UV_HTTP_TIMEOUT = '120' }

# --frozen: install exactly the committed uv.lock and never rewrite it.
$uvArgs = @('run', '--frozen', '--python', '3.12', 'server.py')
if (-not $NoOpen) { $uvArgs += '--open' }
if ($PSBoundParameters.ContainsKey('Port')) { $uvArgs += '--port', "$Port" }

& $uv @uvArgs
exit $LASTEXITCODE

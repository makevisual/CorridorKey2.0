<#
.SYNOPSIS
    Install (or switch to) a CorridorKey 2.0 release on one GPU workstation.

.DESCRIPTION
    Run elevated on each machine that should be able to press "Key Shot"
    (render nodes and workstations that only RENDER scripts do not need it).

        C:\MAKE\CorridorKey2\
            releases\20261118_abc1234\    local copy of the release + its venv
            releases\20261201_def5678\
            current  ->  releases\20261201_def5678     (directory junction)

    MAKE_CK2_ROOT (machine scope) always points at "current", so switching
    releases or rolling back is only a junction swap. Nuke and the node never
    need to change.

    Code and models are copied locally: torch imports thousands of files and
    the model is ~5 GB, both of which are slow from a network share.

.EXAMPLE
    .\Install-CK2.ps1                         # newest release on the share
    .\Install-CK2.ps1 -Release 20261118_abc1234
    .\Install-CK2.ps1 -Release 20261118_abc1234 -Force   # rebuild its venv
#>
[CmdletBinding()]
param(
    [string]$Release = "latest",
    [string]$ShareRoot = "U:\Nuke\CorridorKey2",
    [string]$LocalRoot = "C:\MAKE\CorridorKey2",
    [string]$PythonVersion = "3.12",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    Write-Host ">> $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Exe" }
}

# --- Resolve the release ---------------------------------------------------------
$shareReleases = Join-Path $ShareRoot "releases"
if ($Release -eq "latest") {
    $Release = (Get-ChildItem $shareReleases -Directory | Sort-Object Name | Select-Object -Last 1).Name
    if (-not $Release) { throw "No releases found in $shareReleases" }
}
$src = Join-Path $shareReleases $Release
$dst = Join-Path $LocalRoot ("releases\" + $Release)
if (-not (Test-Path (Join-Path $src "infer.py"))) { throw "Not a release folder: $src" }
Write-Host "Installing CorridorKey2 release $Release" -ForegroundColor Cyan

# --- Copy code + models (wheelhouse stays on the share) ---------------------------
& robocopy $src $dst /MIR /NFL /NDL /NJH /NP /XD wheelhouse venv | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }

# --- Venv, built offline from the release wheelhouse -----------------------------
$venvPy = Join-Path $dst "venv\Scripts\python.exe"
if ($Force -or -not (Test-Path $venvPy)) {
    if (Test-Path (Join-Path $dst "venv")) { Remove-Item (Join-Path $dst "venv") -Recurse -Force }
    Invoke-Checked py @("-$PythonVersion", "-m", "venv", (Join-Path $dst "venv"))
    Invoke-Checked $venvPy @("-m", "pip", "install", "--quiet", "--no-index",
        "--find-links", (Join-Path $src "wheelhouse"),
        "-r", (Join-Path $dst "requirements.lock.txt"))
} else {
    Write-Host "venv already present (use -Force to rebuild)"
}

# --- Smoke test: imports and sees a CUDA GPU --------------------------------------
Invoke-Checked $venvPy @("-c",
    "import torch, cv2; assert torch.cuda.is_available(), 'no CUDA GPU'; print('GPU:', torch.cuda.get_device_name(0))")

# --- Swap the junction --------------------------------------------------------------
# Remove the OLD junction with "cmd /c rmdir". Remove-Item on a junction in
# PowerShell 5 can recurse into the TARGET and delete the release it points to.
$current = Join-Path $LocalRoot "current"
if (Test-Path $current) { cmd /c rmdir "$current" | Out-Null }
cmd /c mklink /J "$current" "$dst" | Out-Null
if (-not (Test-Path (Join-Path $current "infer.py"))) { throw "Junction swap failed: $current" }

# --- Machine environment (set once; value never changes) ---------------------------
if ([Environment]::GetEnvironmentVariable("MAKE_CK2_ROOT", "Machine") -ne $current) {
    [Environment]::SetEnvironmentVariable("MAKE_CK2_ROOT", $current, "Machine")
    Write-Host "Set MAKE_CK2_ROOT=$current (machine). Restart Nuke to pick it up."
}

Write-Host ""
Write-Host "CorridorKey2 $Release is active: $current" -ForegroundColor Green

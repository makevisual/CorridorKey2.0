<#
.SYNOPSIS
    Build a self-contained CorridorKey 2.0 release folder on the studio share.

.DESCRIPTION
    Run ONCE per release, on a machine with internet, git and Python 3.12
    (MAKE52). Produces:

        <ShareRoot>\releases\<yyyyMMdd>_<sha>\
            infer.py, corridorkey\, integrations\nuke\, ...   (code)
            weights\, vfms\                                    (~5 GB models)
            requirements.lock.txt                              (exact versions)
            wheelhouse\                                        (every wheel, offline install)
            VERSION.json                                       (what this is)

    No venv is copied to the share: Windows venvs hard-code their own path and
    do not survive being moved. Workstations build their own venv from the
    wheelhouse with Install-CK2.ps1, fully offline and reproducible.

.EXAMPLE
    .\Stage-CK2Release.ps1 -Ref make/nuke
#>
[CmdletBinding()]
param(
    [string]$Ref = "make/nuke",
    [string]$RepoUrl = "https://github.com/makevisual/CorridorKey2.0",
    [string]$ShareRoot = "U:\Nuke\CorridorKey2",
    [string]$StageRoot = "C:\temp\ck2_stage",
    [string]$PythonVersion = "3.12",
    [string]$TorchIndex = "https://download.pytorch.org/whl/cu128",
    [string]$TorchPins = "torch==2.9.1 torchvision==0.24.1"
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    # Run a native command and stop on a non-zero exit code. PowerShell 5
    # does not do this on its own for external programs.
    param([string]$Exe, [string[]]$Arguments)
    Write-Host ">> $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Exe" }
}

# --- 1. Fresh checkout of the requested ref -----------------------------------
$work = Join-Path $StageRoot ("work_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Path $work -Force | Out-Null
Invoke-Checked git @("clone", "--quiet", $RepoUrl, $work)
Invoke-Checked git @("-C", $work, "checkout", "--quiet", $Ref)
$sha = (& git -C $work rev-parse --short HEAD).Trim()
$release = (Get-Date -Format "yyyyMMdd") + "_" + $sha
Write-Host "Release name: $release" -ForegroundColor Cyan

$dest = Join-Path $ShareRoot ("releases\" + $release)
if (Test-Path $dest) { throw "Release already exists on the share: $dest" }

# --- 2. Temporary venv, used only to resolve versions and fetch models ---------
$venvPy = Join-Path $work "venv\Scripts\python.exe"
Invoke-Checked py @("-$PythonVersion", "-m", "venv", (Join-Path $work "venv"))
Invoke-Checked $venvPy @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Invoke-Checked $venvPy (@("-m", "pip", "install", "--quiet") + $TorchPins.Split(" ") + @("--index-url", $TorchIndex))
Invoke-Checked $venvPy @("-m", "pip", "install", "--quiet", "-r", (Join-Path $work "requirements.txt"))

Push-Location $work
try { Invoke-Checked $venvPy @("scripts\download_weights.py") } finally { Pop-Location }

# --- 3. Lock file: exact name==version for everything installed ---------------
# "pip list --format=freeze" writes git dependencies (utils3d) as plain
# name==version, which an offline install can satisfy from the wheelhouse.
$lock = Join-Path $work "requirements.lock.txt"
$lines = & $venvPy -m pip list --format=freeze |
    Where-Object { $_ -notmatch '^(pip|setuptools|wheel)==' }
[System.IO.File]::WriteAllLines($lock, [string[]]$lines)   # ASCII-safe, no BOM

# --- 4. Wheelhouse: every wheel needed, so workstations never touch the internet
# Git dependencies have no wheel on any index, so build those first...
$wheels = Join-Path $work "wheelhouse"
$gitReqs = Get-Content (Join-Path $work "requirements.txt") | Where-Object { $_ -match '@\s*git\+' }
foreach ($req in $gitReqs) {
    Invoke-Checked $venvPy @("-m", "pip", "wheel", "--quiet", "--no-deps", "-w", $wheels, $req.Trim())
}
# ...then fetch exactly the locked versions (the lock is complete, so --no-deps).
Invoke-Checked $venvPy @("-m", "pip", "download", "--quiet", "--no-deps", "-r", $lock,
    "-d", $wheels, "--find-links", $wheels, "--extra-index-url", $TorchIndex)

# --- 5. Release metadata --------------------------------------------------------
$info = [ordered]@{
    release = $release
    ref     = $Ref
    commit  = (& git -C $work rev-parse HEAD).Trim()
    staged  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    by      = "$env:USERNAME@$env:COMPUTERNAME"
    python  = $PythonVersion
}
$info | ConvertTo-Json | Set-Content -Path (Join-Path $work "VERSION.json") -Encoding ASCII

# --- 6. Copy to the share (no venv, no .git) ------------------------------------
& robocopy $work $dest /E /NFL /NDL /NJH /NP /XD venv .git __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }   # 0-7 = success

Write-Host ""
Write-Host "Staged: $dest" -ForegroundColor Green
Write-Host "Install on a workstation (elevated):"
Write-Host "    .\Install-CK2.ps1 -Release $release"
Write-Host "Staging folder can be deleted: $work"

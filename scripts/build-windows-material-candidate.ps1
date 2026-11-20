param(
    [switch]$SkipDependencies,
    [switch]$PackageOnly,
    [string]$PythonExe = 'py.exe',
    [string]$ElectronCacheRoot = (Join-Path $env:LOCALAPPDATA 'electron\Cache'),
    [string]$ElectronBuilderCacheRoot = (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache')
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
if ($root -notmatch '\\candidates\\') { throw 'Only an isolated candidates directory is allowed.' }
# macOS archive sidecars are not Python package metadata or application assets.
foreach ($sourceDirectory in @('services', 'src', 'scripts', 'build')) {
    Get-ChildItem -LiteralPath $sourceDirectory -Filter '._*' -File -Recurse |
        Remove-Item -Force
}
Get-ChildItem Env:HSC_* | ForEach-Object { Remove-Item "Env:$($_.Name)" }
$env:HSC_DATA_DIR = Join-Path $root '.build-isolation'
$env:HSC_DATABASE_PATH = Join-Path $env:HSC_DATA_DIR 'build-only.db'
$env:HSC_MAINTENANCE_ENABLED = '0'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# Candidate archives may reuse a dependency-only virtual environment. Put the
# candidate source first so editable metadata from an older checkout can never
# be frozen into the release executable.
$env:PYTHONPATH = [IO.Path]::GetFullPath((Join-Path $root 'services\local_agent\src'))
if (Test-Path -LiteralPath $ElectronBuilderCacheRoot) {
    $env:ELECTRON_BUILDER_CACHE = [IO.Path]::GetFullPath($ElectronBuilderCacheRoot)
}
$log = Join-Path $root 'candidate-build.log'
$status = Join-Path $root 'candidate-status.json'
function State([string]$stage, [string]$result) {
    @{ stage=$stage; status=$result; at=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -Encoding UTF8 $status
}
function Checked([string]$stage, [string]$program, [string[]]$arguments) {
    State $stage 'running'
    $command = Get-Command $program -ErrorAction Stop
    $priorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell wraps native stderr (including INFO) as ErrorRecord.
        $ErrorActionPreference = 'Continue'
        & $command @arguments >> $log 2>&1
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $priorPreference }
    if ($exitCode -ne 0) { throw "$stage failed with exit code $exitCode" }
}
try {
    Checked 'private-resources' 'node.exe' @('scripts/check-private-resources.mjs','all')
    if (-not $SkipDependencies -and -not $PackageOnly) {
        if (-not (Test-Path '.venv\Scripts\python.exe')) {
            if ((Split-Path $PythonExe -Leaf) -ieq 'py.exe') {
                Checked 'create-venv' $PythonExe @('-3.11','-m','venv','.venv')
            } else {
                Checked 'create-venv' $PythonExe @('-m','venv','.venv')
            }
        }
        Checked 'python-version' '.venv\Scripts\python.exe' @('-c','import sys; assert sys.version_info[:2] == (3, 11), sys.version')
        Checked 'python-dependencies' '.venv\Scripts\python.exe' @('-m','pip','install','-e','services/local_agent[dev]')
        Checked 'frontend-dependencies' 'npm.cmd' @('ci','--no-audit','--no-fund')
    }
    Checked 'python-source-binding' '.venv\Scripts\python.exe' @('-c',
        'from pathlib import Path; import home_study_agent; root=Path.cwd().resolve(); source=Path(home_study_agent.__file__).resolve(); assert source.is_relative_to(root), (''Python package resolves outside candidate: '' + str(source))')
    if (-not $PackageOnly) {
        Checked 'typecheck' 'npm.cmd' @('run','typecheck')
        Checked 'python-build' 'npm.cmd' @('run','build:agent:win')
        Checked 'smtp-worker-build' 'npm.cmd' @('run','build:smtp-worker:win')
        Checked 'frontend-build' 'npm.cmd' @('run','build')
    }
    Checked 'release-inputs' 'node.exe' @('scripts/windows-release-readiness.mjs')
    # Reuse the downloaded runtime only after checking the npm package's checksum.
    $electronVersion = (Get-Content 'node_modules/electron/package.json' -Raw | ConvertFrom-Json).version
    $zipName = "electron-v$electronVersion-win32-x64.zip"
    $checksums = Get-Content 'node_modules/electron/checksums.json' -Raw | ConvertFrom-Json
    $expectedChecksum = $checksums.PSObject.Properties[$zipName].Value
    if (-not $expectedChecksum) { throw 'Electron package has no checksum for the Windows runtime.' }
    $electronZip = if (Test-Path -LiteralPath $ElectronCacheRoot) {
        Get-ChildItem -LiteralPath $ElectronCacheRoot -Recurse -File -Filter $zipName |
            Where-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash -eq $expectedChecksum } |
            Select-Object -First 1
    }
    if (-not $electronZip) { throw 'No checksum-verified cached Windows Electron runtime; run the dependency download first.' }
    Checked 'installer-build' '.\node_modules\.bin\electron-builder.cmd' @(
        '--win','nsis','--publish','never',("--config.electronDist=" + $electronZip.FullName))
    $files = @(Get-ChildItem -LiteralPath 'release' -Filter '*.exe' -File | ForEach-Object {
        @{ name=$_.Name; bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower() }
    })
    if ($files.Count -eq 0) { throw 'No installer produced.' }
    @{ status='candidate_built_not_installed'; installers=$files; family_data_touched=$false;
       target_runtime_verified=$false; created_at=[DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 'candidate-artifacts.json'
    State 'installer-build' 'completed'
} catch {
    State 'build' 'failed'
    $_.Exception.Message | Add-Content -Encoding UTF8 $log
    exit 1
}

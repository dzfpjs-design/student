param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if ($root -notmatch '\\candidates\\') { throw 'Only an isolated candidates directory is allowed.' }
Set-Location -LiteralPath $root
Get-ChildItem Env:HSC_* | ForEach-Object { Remove-Item "Env:$($_.Name)" }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$evidence = Join-Path $root ('verification-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $evidence | Out-Null
$env:RUNNER_TEMP = $evidence
$checks = @()
function Checked([string]$name, [string]$program, [string[]]$arguments) {
    @{status='running'; check=$name; evidence=$evidence} | ConvertTo-Json |
        Set-Content -Encoding UTF8 'candidate-verification-status.json'
    $command = Get-Command $program -ErrorAction Stop
    $prior = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $command @arguments > (Join-Path $evidence ($name + '.log')) 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prior }
    if ($code -ne 0) { throw "$name failed with exit code $code" }
    $script:checks += $name
}
try {
    if (-not $SkipFrontend) {
        Checked 'frontend-tests' 'npm.cmd' @('run', 'test', '--', '--reporter=dot')
    }
    Checked 'service-smoke' 'powershell.exe' @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', 'scripts/smoke-windows-agent.ps1')
    Checked 'memory' '.venv\Scripts\python.exe' @('scripts/verify-companion-memory-runtime.py', '--exe', 'dist/home-study-local-agent.exe', '--output', (Join-Path $evidence 'memory.json'))
    Checked 'continuous-study' '.venv\Scripts\python.exe' @('scripts/verify-companion-memory-runtime.py', '--exe', 'dist/home-study-local-agent.exe', '--document', '--output', (Join-Path $evidence 'continuous-study.json'))
    Checked 'daily-learning' '.venv\Scripts\python.exe' @('scripts/verify-daily-learning-runtime.py', '--exe', 'dist/home-study-local-agent.exe', '--output', (Join-Path $evidence 'daily-learning.json'))
    Checked 'curriculum' '.venv\Scripts\python.exe' @('scripts/verify-curriculum-packaged-runtime.py', '--exe', 'dist/home-study-local-agent.exe', '--bundle', 'hsc-curriculum-20260905.zip', '--sha256', '21cb2232e1f73754b8fadffda25c7ce159ef53a86339e027b2c36d2406e2f49c', '--output', (Join-Path $evidence 'curriculum.json'))
    @{status='passed'; checks=$checks; evidence=$evidence; actual_camera_or_child_verified=$false; installed=$false} |
        ConvertTo-Json | Set-Content -Encoding UTF8 'candidate-verification-status.json'
} catch {
    @{status='failed'; checks=$checks; evidence=$evidence; failure=$_.Exception.Message} |
        ConvertTo-Json | Set-Content -Encoding UTF8 'candidate-verification-status.json'
    exit 1
}

# Synthetic-only electron-builder 26.15.3 template experiment.
# Runs only on a disposable GitHub-hosted Windows runner; never reads product data.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$testGuid = [Guid]::NewGuid().ToString('D')
$testId = $testGuid.Replace('-', '')
if ($testId -cnotmatch '^[a-f0-9]{32}$') { throw 'SYNTHETIC_GUID_INVALID' }
$name = 'synthetic-nsis-' + $testId
$appId = 'test.synthetic.nsis.x' + $testId
$temp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
$root = Join-Path $temp ('synthetic-template-' + $testId)
$syntheticDataDir = Join-Path $env:APPDATA $name
$installKey = 'HKCU:\Software\' + $testGuid
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\' + $testGuid
$ownedRoot = $false
$ownedProfile = $false
$ownedRegistry = $false
$ownedProcesses = [Collections.Generic.List[Diagnostics.Process]]::new()
if ($root -match '\s' -or -not $root.StartsWith($temp + '\synthetic-template-', [StringComparison]::OrdinalIgnoreCase)) { throw 'SYNTHETIC_PATH_INVALID' }

function Wait-Owned([string]$file, [string[]]$arguments, [int]$seconds = 90) {
    $process = Start-Process -FilePath $file -ArgumentList $arguments -PassThru
    $ownedProcesses.Add($process)
    $deadline = [DateTime]::UtcNow.AddSeconds($seconds)
    while (-not $process.HasExited -and [DateTime]::UtcNow -lt $deadline) {
        # The real template copies the child to NSIS's temporary plug-in dir.
        # Touch only direct children with its exact name under the runner temp.
        foreach ($child in @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $process.Id) -ErrorAction SilentlyContinue)) {
            if ($child.Name -ieq 'old-uninstaller.exe' -and $child.ExecutablePath -and
                $child.ExecutablePath.StartsWith($env:TEMP.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
                $window = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
                if ($window -and $window.MainWindowHandle -ne 0) { [void]$window.CloseMainWindow() }
            }
        }
        Start-Sleep -Milliseconds 200
        $process.Refresh()
    }
    if (-not $process.HasExited) {
        foreach ($child in @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $process.Id) -ErrorAction SilentlyContinue)) {
            if ($child.Name -ieq 'old-uninstaller.exe' -and $child.ExecutablePath -and
                $child.ExecutablePath.StartsWith($env:TEMP.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
        try { $process.Kill() } catch {}
        throw 'SYNTHETIC_TIMEOUT_INCONCLUSIVE'
    }
    $process.WaitForExit()
    return $process.ExitCode
}
function Read-SyntheticRegistration([string]$program) {
    $loc = (Get-ItemProperty -LiteralPath $installKey -Name InstallLocation).InstallLocation
    $entry = Get-ItemProperty -LiteralPath $uninstallKey -Name UninstallString, DisplayVersion
    if ([IO.Path]::GetFullPath($loc).TrimEnd('\') -ine [IO.Path]::GetFullPath($program).TrimEnd('\')) { throw 'SYNTHETIC_REGISTERED_PATH_MISMATCH' }
    if (-not ($entry.UninstallString -match '^"([^"]+)"')) { throw 'SYNTHETIC_UNINSTALL_STRING_INVALID' }
    $uninstaller = [IO.Path]::GetFullPath($Matches[1])
    if (-not $uninstaller.StartsWith($program.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'SYNTHETIC_UNINSTALL_PATH_ESCAPE' }
    return @{uninstaller=$uninstaller; version=$entry.DisplayVersion}
}
function Build-Synthetic([string]$version, [string]$output) {
    $pkg = Get-Content -Raw -LiteralPath 'package.json' | ConvertFrom-Json
    $pkg.name = $name
    $pkg.version = $version
    $pkg.build.appId = $appId
    $pkg.build | Add-Member -Force NoteProperty productName $name
    $pkg.build | Add-Member -Force NoteProperty directories @{output=$output}
    $pkg.build.nsis | Add-Member -Force NoteProperty guid $testGuid
    [IO.File]::WriteAllText((Join-Path (Get-Location) 'package.json'), ($pkg | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    & '.\node_modules\.bin\electron-builder.cmd' --win nsis --x64 --publish never | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'SYNTHETIC_ELECTRON_BUILDER_FAILED' }
    $out = @(Get-ChildItem -LiteralPath $output -Filter '*.exe' -File)
    if ($out.Count -ne 1) { throw 'SYNTHETIC_INSTALLER_COUNT' }
    return @{installer=$out[0].FullName; asar=(Get-FileHash -LiteralPath (Join-Path $output 'win-unpacked\resources\app.asar') -Algorithm SHA256).Hash}
}
function Reset-Synthetic([string]$program) {
    foreach ($key in @($installKey,$uninstallKey)) { if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key -Recurse -Force } }
    if (Test-Path -LiteralPath $program) {
        if (-not $program.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'SYNTHETIC_DELETE_SCOPE' }
        Remove-Item -LiteralPath $program -Recurse -Force
    }
}
function Assert-SyntheticData([string]$db, [string]$expected) {
    if ((Get-FileHash -LiteralPath $db -Algorithm SHA256).Hash -ne $expected) { throw 'SYNTHETIC_DATA_CHANGED' }
    & python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); assert c.execute("pragma quick_check").fetchone()[0]=="ok"; assert c.execute("select value from marker").fetchone()[0]=="synthetic only"; c.close()' $db
    if ($LASTEXITCODE -ne 0) { throw 'SYNTHETIC_SQLITE_INVALID' }
}
Add-Type -TypeDefinition @'
public static class TemplateSyntheticCrc32 {
  public static uint Compute(byte[] bytes, int from, int to) {
    uint crc = 0xffffffffu;
    for (int n = from; n < to; n++) {
      crc ^= bytes[n];
      for (int k = 0; k < 8; k++) crc = (crc & 1u) != 0 ? 0xedb88320u ^ (crc >> 1) : crc >> 1;
    }
    return crc ^ 0xffffffffu;
  }
}
'@
function Damage-SyntheticCrc([string]$file) {
    $bytes = [IO.File]::ReadAllBytes($file)
    $signature = [byte[]](0xef,0xbe,0xad,0xde) + [Text.Encoding]::ASCII.GetBytes('NullsoftInst')
    $offset = -1
    for ($i=512; $i+28 -lt $bytes.Length; $i+=512) {
        $found = $true
        for ($j=0; $j -lt $signature.Length; $j++) { if ($bytes[$i+4+$j] -ne $signature[$j]) { $found=$false; break } }
        if ($found) { $offset=$i; break }
    }
    if ($offset -lt 0) { throw 'SYNTHETIC_NSIS_HEADER_MISSING' }
    $end = [long]$offset + [BitConverter]::ToUInt32($bytes,$offset+24) - 4
    if ($end -le ($offset+28) -or $end+4 -gt $bytes.Length) { throw 'SYNTHETIC_NSIS_CRC_BOUNDS' }
    $stored = [BitConverter]::ToUInt32($bytes,[int]$end)
    if ([TemplateSyntheticCrc32]::Compute($bytes,512,[int]$end) -ne $stored) { throw 'SYNTHETIC_PREEXISTING_CRC_ERROR' }
    $at = $offset+28+[int][Math]::Floor(($end-$offset-28)/2)
    $bytes[$at] = $bytes[$at] -bxor 1
    if ([TemplateSyntheticCrc32]::Compute($bytes,512,[int]$end) -eq $stored) { throw 'SYNTHETIC_CORRUPTION_FAILED' }
    [IO.File]::WriteAllBytes($file,$bytes)
}
try {
    if ((Test-Path -LiteralPath $root) -or (Test-Path -LiteralPath $syntheticDataDir) -or
        (Test-Path -LiteralPath $installKey) -or (Test-Path -LiteralPath $uninstallKey)) { throw 'SYNTHETIC_SCOPE_EXISTS' }
    New-Item -ItemType Directory -Path $root -ErrorAction Stop | Out-Null
    $ownedRoot=$true
    New-Item -ItemType Directory -Path $syntheticDataDir -ErrorAction Stop | Out-Null
    $ownedProfile=$true
    $ownedRegistry=$true
    $db=Join-Path $syntheticDataDir 'synthetic.db'
    & python -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("create table marker(value text)"); c.execute("insert into marker values (?)",("synthetic only",)); c.commit(); c.close()' $db
    if ($LASTEXITCODE -ne 0) { throw 'SYNTHETIC_SQLITE_CREATE_FAILED' }
    $dataHash=(Get-FileHash -LiteralPath $db -Algorithm SHA256).Hash
    $v1=Build-Synthetic '1.0.0' 'dist-v1'
    $v2=Build-Synthetic '2.0.0' 'dist-v2'
    if ($v1.asar -eq $v2.asar) { throw 'SYNTHETIC_BUILD_IDENTITIES_EQUAL' }
    $nsisCommand=Get-Command makensis.exe -ErrorAction SilentlyContinue
    $nsis=if ($nsisCommand) { $nsisCommand.Source } else { Join-Path ${env:ProgramFiles(x86)} 'NSIS\makensis.exe' }
    if (-not (Test-Path -LiteralPath $nsis -PathType Leaf)) { throw 'RUNNER_NSIS_UNAVAILABLE' }
    $stub=Join-Path $root 'return-seven.exe'
    $stubSource=Join-Path $root 'return-seven.nsi'
    $stubNsi=@'
Unicode true
Name "Synthetic return-seven"
OutFile "${OUTPUT}"
RequestExecutionLevel user
SilentInstall silent
Section
  SetErrorLevel 7
  Quit
SectionEnd
'@
    [IO.File]::WriteAllText($stubSource,$stubNsi,[Text.Encoding]::ASCII)
    & $nsis "/DOUTPUT=$stub" $stubSource
    if ($LASTEXITCODE -ne 0) { throw 'SYNTHETIC_STUB_BUILD_FAILED' }
    if ((Wait-Owned $stub @('/S')) -ne 7) { throw 'SYNTHETIC_STUB_EXIT_MISMATCH' }
    foreach ($scenario in @('normal','crc','exit7','missing')) {
        $program=Join-Path $root ('program-' + $scenario)
        Reset-Synthetic $program
        if ((Wait-Owned $v1.installer @('/S',"/D=$program")) -ne 0) { throw 'SYNTHETIC_V1_INSTALL_FAILED' }
        $before=Read-SyntheticRegistration $program
        if ($before.version -ne '1.0.0' -or -not (Test-Path -LiteralPath $before.uninstaller -PathType Leaf)) { throw 'SYNTHETIC_V1_STATE_INVALID' }
        $beforeAsar=(Get-FileHash -LiteralPath (Join-Path $program 'resources\app.asar') -Algorithm SHA256).Hash
        if ($beforeAsar -ne $v1.asar) { throw 'SYNTHETIC_V1_ASAR_MISMATCH' }
        if ($scenario -eq 'crc') { Damage-SyntheticCrc $before.uninstaller }
        if ($scenario -eq 'exit7') {
            [IO.File]::Copy($stub,$before.uninstaller,$true)
            if ((Get-FileHash -LiteralPath $before.uninstaller -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $stub -Algorithm SHA256).Hash) { throw 'SYNTHETIC_STUB_COPY_MISMATCH' }
        }
        if ($scenario -eq 'missing') { Remove-Item -LiteralPath $before.uninstaller -Force }
        $exit=Wait-Owned $v2.installer @('/S',"/D=$program")
        $after=Read-SyntheticRegistration $program
        $afterAsar=(Get-FileHash -LiteralPath (Join-Path $program 'resources\app.asar') -Algorithm SHA256).Hash
        Assert-SyntheticData $db $dataHash
        $state=if ($afterAsar -eq $v1.asar) {'v1'} elseif ($afterAsar -eq $v2.asar) {'v2'} else {'unknown'}
        Write-Output "SYNTHETIC_TEMPLATE_RESULT case=$scenario installer_exit=$exit program=$state registry_version=$($after.version) data_unchanged=true"
        if ($scenario -eq 'normal' -and ($exit -ne 0 -or $state -ne 'v2' -or $after.version -ne '2.0.0')) { throw 'SYNTHETIC_NORMAL_FAILED' }
        if ($scenario -in @('crc','exit7') -and ($exit -ne 2 -or $state -ne 'v1' -or $after.version -ne '1.0.0')) { throw 'SYNTHETIC_REJECTION_MISMATCH' }
        if ($scenario -eq 'missing' -and $state -eq 'unknown') { throw 'SYNTHETIC_MISSING_UNEXPECTED_PARTIAL' }
    }
} finally {
    foreach ($p in $ownedProcesses) {
        try { if (-not $p.HasExited) { $p.Kill() } } catch {}
    }
    if ($ownedRegistry) { foreach ($key in @($installKey,$uninstallKey)) { if (Test-Path -LiteralPath $key) { Remove-Item -LiteralPath $key -Recurse -Force } } }
    if ($ownedProfile -and $name -cmatch '^synthetic-nsis-[a-f0-9]{32}$' -and (Test-Path -LiteralPath $syntheticDataDir)) { Remove-Item -LiteralPath $syntheticDataDir -Recurse -Force }
    if ($ownedRoot -and $root.StartsWith($temp + '\synthetic-template-', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $root)) { Remove-Item -LiteralPath $root -Recurse -Force }
}

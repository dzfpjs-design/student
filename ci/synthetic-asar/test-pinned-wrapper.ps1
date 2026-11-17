param([Parameter(Mandatory=$true)][string]$FixtureDir)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or -not $env:RUNNER_TEMP){throw 'RUNNER_CONTEXT_INVALID'}
$stage=[IO.Path]::GetFullPath($FixtureDir).TrimEnd('\')
$temp=[IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
if([IO.Path]::GetFileName($stage) -cnotmatch '^synthetic-asar-[a-f0-9]{32}$' -or [IO.Directory]::GetParent($stage).FullName.TrimEnd('\') -ine $temp){throw 'FIXTURE_SCOPE_INVALID'}
$dir=Join-Path $stage ([string][char]0x4e2d+[char]0x6587)
$archive=Join-Path $dir 'fixture.asar'
$wrapper=Join-Path $stage 'synthetic-pinned-wrapper.ps1'
$good='370f33344da9de9cfd8546ce2a432943876b9ad898347ea06843aa6c3f6fd51b'
$template=[IO.File]::ReadAllText((Join-Path $stage 'pinned-wrapper-template.txt'))
$core=[IO.File]::ReadAllText((Join-Path $stage 'readonly-asar.ps1'))
$shell=[Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
function Render([string]$HostName,[string]$ExpectedHash){
 return $template.Replace('__PINNED_HOST__',$HostName.Replace("'","''")).Replace('__PINNED_PATH__',$archive.Replace("'","''")).Replace('__PINNED_SHA__',$ExpectedHash).Replace('__REVIEWED_STREAMING_CORE__',$core)
}
function Invoke-Wrapper([string]$Code,[bool]$WithRun){
 [IO.File]::WriteAllText($wrapper,$Code,(New-Object Text.UTF8Encoding($true)))
 if($WithRun){$output=@(& $shell -NoLogo -NoProfile -NonInteractive -File $wrapper -RunPinned)}
 else{$output=@(& $shell -NoLogo -NoProfile -NonInteractive -File $wrapper)}
 return @{code=$LASTEXITCODE;lines=$output}
}
try {
 if([IO.Directory]::Exists($dir) -or [IO.File]::Exists($wrapper)){throw 'FIXTURE_COLLISION'}
 [IO.Directory]::CreateDirectory($dir) | Out-Null
 [IO.File]::Copy((Join-Path $stage 'fixture.asar'),$archive,$false)
 $code=Render $env:COMPUTERNAME $good
 $ok=Invoke-Wrapper $code $true
 if($ok.code -ne 0 -or $ok.lines.Count -ne 1 -or $ok.lines[0] -cne 'ASAR_PROBE_OK module_sha256=f76832182e106bd11aa2157ca4d37150b9e71f460acff877c9bceda99917c40b marker_api=true marker_smtp=true'){throw 'VALID_WRAPPER_FAILED'}
 $notOpted=Invoke-Wrapper $code $false
 if($notOpted.code -eq 0 -or $notOpted.lines.Count -ne 1 -or $notOpted.lines[0] -cne 'ASAR_PROBE_SCOPE_FAILED'){throw 'RUN_FLAG_GATE_FAILED'}
 $wrongHost=Invoke-Wrapper (Render 'invalid-fixture-host' $good) $true
 if($wrongHost.code -eq 0 -or $wrongHost.lines.Count -ne 1 -or $wrongHost.lines[0] -cne 'ASAR_PROBE_SCOPE_FAILED'){throw 'HOST_GATE_FAILED'}
 $wrongHash=Invoke-Wrapper (Render $env:COMPUTERNAME ('0'*64)) $true
 if($wrongHash.code -eq 0 -or $wrongHash.lines.Count -ne 1 -or $wrongHash.lines[0] -cne 'ASAR_PROBE_FAILED'){throw 'HASH_GATE_FAILED'}
 Write-Output 'SYNTHETIC_PINNED_TEST chinese_path=true fixed_hash=true host_gate=true run_flag_gate=true output_bounded=true'
} finally {
 if([IO.File]::Exists($wrapper)){[IO.File]::Delete($wrapper)}
 if([IO.File]::Exists($archive)){[IO.File]::Delete($archive)}
 if([IO.Directory]::Exists($dir)){[IO.Directory]::Delete($dir,$false)}
}

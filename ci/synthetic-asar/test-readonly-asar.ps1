param([Parameter(Mandatory=$true)][string]$FixtureDir)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or -not $env:RUNNER_TEMP){throw 'RUNNER_CONTEXT_INVALID'}
$stage=[IO.Path]::GetFullPath($FixtureDir).TrimEnd('\')
$temp=[IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
if([IO.Path]::GetFileName($stage) -cnotmatch '^synthetic-asar-[a-f0-9]{32}$' -or [IO.Directory]::GetParent($stage).FullName.TrimEnd('\') -ine $temp){throw 'FIXTURE_SCOPE_INVALID'}
. (Join-Path $stage 'readonly-asar.ps1')
function Expect-Failure([scriptblock]$Action,[string]$Expected) {
 try {& $Action | Out-Null}catch{if($_.Exception.Message -match [regex]::Escape($Expected)){return};throw 'WRONG_FAILURE'}
 throw 'FAILURE_NOT_DETECTED'
}
$archive=Join-Path $stage 'fixture.asar'
$want='370f33344da9de9cfd8546ce2a432943876b9ad898347ea06843aa6c3f6fd51b'
$result=Read-HscAsarEvidence $archive $want
if($result.module_sha256 -cne 'f76832182e106bd11aa2157ca4d37150b9e71f460acff877c9bceda99917c40b' -or -not $result.marker_api -or -not $result.marker_smtp){throw 'FIXTURE_RESULT_MISMATCH'}
Expect-Failure {Read-HscAsarEvidence $archive ('0'*64)} 'ARCHIVE_HASH_MISMATCH'
Expect-Failure {Read-HscAsarEvidence ($archive+':unexpected') $want} 'PATH_UNSUPPORTED'
$corrupt=Join-Path $stage 'synthetic-corrupt.asar'
function Expect-ArchiveFailure([byte[]]$Bytes,[string]$Expected) {
 try {
  [IO.File]::WriteAllBytes($corrupt,$Bytes)
  $stream=[IO.File]::OpenRead($corrupt);try{$corruptHash=Get-StreamHash $stream}finally{$stream.Dispose()}
  Expect-Failure {Read-HscAsarEvidence $corrupt $corruptHash} $Expected
 } finally {if(Test-Path -LiteralPath $corrupt){Remove-Item -LiteralPath $corrupt -Force}}
}
[byte[]]$truncated=New-Object byte[] 8
Expect-ArchiveFailure $truncated 'ARCHIVE_SIZE_INVALID'
[byte[]]$bytes=[IO.File]::ReadAllBytes($archive)
[Array]::Copy([BitConverter]::GetBytes([uint32]2147483647),0,$bytes,4,4)
Expect-ArchiveFailure $bytes 'HEADER_INVALID'
[byte[]]$bytes=[IO.File]::ReadAllBytes($archive)
$offsetNeedle='"offset":"0"'
$offsetPosition=[Text.Encoding]::ASCII.GetString($bytes).IndexOf($offsetNeedle,[StringComparison]::Ordinal)
if($offsetPosition -lt 0){throw 'FIXTURE_OFFSET_NOT_FOUND'}
$bytes[$offsetPosition+10]=[byte][char]'9'
Expect-ArchiveFailure $bytes 'ENTRY_BOUNDS_INVALID'
# Synthetic oversized module with a valid in-file range; only the size cap rejects it.
$json='{"files":{"out":{"files":{"main":{"files":{"index.js":{"offset":"0","size":20971521}}}}}}}'
$j=[Text.Encoding]::UTF8.GetBytes($json);$padded=[int]([Math]::Ceiling($j.Length/4.0)*4);$headerLen=8+$padded
[byte[]]$large=New-Object byte[] (8+$headerLen)
[Array]::Copy([BitConverter]::GetBytes([uint32]4),0,$large,0,4)
[Array]::Copy([BitConverter]::GetBytes([uint32]$headerLen),0,$large,4,4)
[Array]::Copy([BitConverter]::GetBytes([uint32]($headerLen-4)),0,$large,8,4)
[Array]::Copy([BitConverter]::GetBytes([uint32]$j.Length),0,$large,12,4)
[Array]::Copy($j,0,$large,16,$j.Length)
try {
 $big=[IO.File]::Open($corrupt,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
 try {
  $big.Write($large,0,$large.Length)
  $big.SetLength([long]$large.Length+20971521)
  $big.Flush($true)
  $largeHash=Get-StreamHash $big
 } finally {$big.Dispose()}
 Expect-Failure {Read-HscAsarEvidence $corrupt $largeHash} 'ENTRY_BOUNDS_INVALID'
} finally {if(Test-Path -LiteralPath $corrupt){Remove-Item -LiteralPath $corrupt -Force}}
Expect-Failure {Assert-UniqueJsonKeys '{"a":1,"a":2}'} 'JSON_DUPLICATE_KEY'
Expect-Failure {Assert-UniqueJsonKeys '{"a":1,"\u0061":2}'} 'JSON_DUPLICATE_KEY'
$unpacked=Join-Path $stage 'unpacked.asar'
Expect-Failure {Read-HscAsarEvidence $unpacked '6369ab4c01e49cf53f196c115c05bd9a9da333fa300432af93b7df6792a1161e'} 'ENTRY_UNSUPPORTED'
$lock=[HscAsarNative]::HoldFile($archive)
try {
 $denied=$false
 try{$write=[IO.File]::Open($archive,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::Read);$write.Dispose()}catch [IO.IOException]{$denied=$true}
 if(-not $denied){throw 'WRITE_LOCK_NOT_ENFORCED'}
}finally{$lock.Dispose()}
$hardlink=Join-Path $stage 'synthetic-hardlink.asar'
try {
 New-Item -ItemType HardLink -Path $hardlink -Target $archive -ErrorAction Stop | Out-Null
 Expect-Failure {Read-HscAsarEvidence $hardlink $want} 'FILE_IDENTITY_INVALID'
}finally{if(Test-Path -LiteralPath $hardlink){Remove-Item -LiteralPath $hardlink -Force}}
$junction=Join-Path $stage 'synthetic-junction'
try {
 New-Item -ItemType Junction -Path $junction -Target $stage -ErrorAction Stop | Out-Null
 Expect-Failure {Read-HscAsarEvidence (Join-Path $junction 'fixture.asar') $want} 'DIRECTORY_REPARSE_OR_INVALID'
}finally{if(Test-Path -LiteralPath $junction){[IO.Directory]::Delete($junction,$false)}}
Write-Output 'SYNTHETIC_STREAM_TEST valid=true hash_mismatch_rejected=true ads_rejected=true truncated_rejected=true invalid_header_rejected=true offset_rejected=true oversized_module_rejected=true duplicate_keys_rejected=true readonly_lock=true hardlink_rejected=true junction_rejected=true'

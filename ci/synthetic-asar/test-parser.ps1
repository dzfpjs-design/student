param([Parameter(Mandatory=$true)][string]$FixtureDir)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -cne 'true' -or $env:RUNNER_OS -cne 'Windows' -or -not $env:RUNNER_TEMP) {throw 'RUNNER_CONTEXT_INVALID'}
$stage=[IO.Path]::GetFullPath($FixtureDir).TrimEnd('\')
$temp=[IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
if ([IO.Path]::GetFileName($stage) -cnotmatch '^synthetic-asar-[a-f0-9]{32}$' -or
    [IO.Path]::GetFullPath([IO.Directory]::GetParent($stage).FullName).TrimEnd('\') -ine $temp) {throw 'FIXTURE_SCOPE_INVALID'}
$ancestor=$stage
while($ancestor){
  if (([IO.File]::GetAttributes($ancestor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {throw 'REPARSE_REJECTED'}
  $ancestor=[IO.Path]::GetDirectoryName($ancestor)
}

function Sha([byte[]]$Bytes) {
  $h=[Security.Cryptography.SHA256]::Create()
  try { return [BitConverter]::ToString($h.ComputeHash($Bytes)).Replace('-','').ToLowerInvariant() }
  finally { $h.Dispose() }
}
function Parse-SyntheticAsar([byte[]]$Bytes,[string]$ExpectedSha) {
  if(-not [BitConverter]::IsLittleEndian){throw 'UNSUPPORTED_ENDIAN'}
  if((Sha $Bytes) -cne $ExpectedSha){throw 'ARCHIVE_HASH_MISMATCH'}
  if($Bytes.Length -lt 16){throw 'SHORT_HEADER'}
  $sizePayload=[BitConverter]::ToUInt32($Bytes,0)
  $headerLen=[BitConverter]::ToUInt32($Bytes,4)
  $pickleLen=[BitConverter]::ToUInt32($Bytes,8)
  $jsonLen=[BitConverter]::ToUInt32($Bytes,12)
  if($sizePayload -ne 4 -or $headerLen -lt 8 -or $headerLen -gt 8388608 -or $headerLen % 4 -ne 0 -or
     $headerLen -gt ($Bytes.Length-8) -or $pickleLen -ne ($headerLen-4) -or $jsonLen -gt ($pickleLen-4)) {throw 'HEADER_INVALID'}
  $utf8=New-Object System.Text.UTF8Encoding($false,$true)
  $json=$utf8.GetString($Bytes,16,[int]$jsonLen)
  $header=ConvertFrom-Json -InputObject $json -ErrorAction Stop
  $entry=$header.files.out.files.main.files.'index.js'
  if(-not $entry -or $entry.PSObject.Properties['unpacked'] -or $entry.PSObject.Properties['link']) {throw 'ENTRY_UNSUPPORTED'}
  [uint64]$offset=0; [long]$size=0
  if(-not [uint64]::TryParse([string]$entry.offset,[ref]$offset) -or -not [long]::TryParse([string]$entry.size,[ref]$size)) {throw 'ENTRY_SIZE_INVALID'}
  $dataBase=[uint64](8+$headerLen)
  if($size -lt 0 -or $size -gt 20971520 -or $offset -gt [uint64]$Bytes.Length -or
     $dataBase -gt [uint64]$Bytes.Length -or $offset -gt ([uint64]$Bytes.Length-$dataBase)) {throw 'ENTRY_BOUNDS_INVALID'}
  $start=$dataBase+$offset
  if([uint64]$size -gt ([uint64]$Bytes.Length-$start)){throw 'ENTRY_BOUNDS_INVALID'}
  $module=New-Object byte[] ([int]$size)
  [Array]::Copy($Bytes,[long]$start,$module,0,[long]$size)
  $source=$utf8.GetString($module)
  return @{module_sha256=(Sha $module); marker_api=$source.Contains('encryptedApiKey'); marker_smtp=$source.Contains('encryptedSmtpPassword')}
}
function Expect-Failure([scriptblock]$Action,[string]$Expected) {
  try {& $Action | Out-Null} catch {if($_.Exception.Message -eq $Expected){return};throw 'WRONG_FAILURE'}
  throw 'FAILURE_NOT_DETECTED'
}
$goodPath=Join-Path $stage 'fixture.asar'
$unpackedPath=Join-Path $stage 'unpacked.asar'
foreach($p in @($goodPath,$unpackedPath)) {
  $item=Get-Item -LiteralPath $p -Force
  if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.Length -gt 1048576){throw 'FIXTURE_FILE_INVALID'}
}
$good=[IO.File]::ReadAllBytes($goodPath)
$goodSha='370f33344da9de9cfd8546ce2a432943876b9ad898347ea06843aa6c3f6fd51b'
$result=Parse-SyntheticAsar $good $goodSha
if($result.module_sha256 -cne 'f76832182e106bd11aa2157ca4d37150b9e71f460acff877c9bceda99917c40b' -or -not $result.marker_api -or -not $result.marker_smtp){throw 'FIXTURE_RESULT_MISMATCH'}
Expect-Failure {Parse-SyntheticAsar $good ('0'*64)} 'ARCHIVE_HASH_MISMATCH'
[byte[]]$truncated=$good[0..7]
Expect-Failure {Parse-SyntheticAsar $truncated (Sha $truncated)} 'SHORT_HEADER'
[byte[]]$broken=$good.Clone();[Array]::Copy([BitConverter]::GetBytes([uint32]2147483647),0,$broken,4,4)
Expect-Failure {Parse-SyntheticAsar $broken (Sha $broken)} 'HEADER_INVALID'
[byte[]]$badOffset=$good.Clone()
$needle='"offset":"0"'
$position=[Text.Encoding]::ASCII.GetString($badOffset).IndexOf($needle,[StringComparison]::Ordinal)
if($position -lt 0){throw 'FIXTURE_OFFSET_NOT_FOUND'}
$badOffset[$position+10]=[byte][char]'9'
Expect-Failure {Parse-SyntheticAsar $badOffset (Sha $badOffset)} 'ENTRY_BOUNDS_INVALID'
[byte[]]$unpacked=[IO.File]::ReadAllBytes($unpackedPath)
Expect-Failure {Parse-SyntheticAsar $unpacked '6369ab4c01e49cf53f196c115c05bd9a9da333fa300432af93b7df6792a1161e'} 'ENTRY_UNSUPPORTED'
Write-Output 'SYNTHETIC_ASAR_TEST valid=true hash_mismatch_rejected=true truncated_rejected=true bad_header_rejected=true bad_offset_rejected=true unpacked_rejected=true markers=true'

# Synthetic-only tests: no registry reads, installer launches or filesystem writes.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'windows-installation-guard.ps1')
$roots=@('CurrentUser/Registry64','CurrentUser/Registry32','LocalMachine/Registry64','LocalMachine/Registry32')
$guid='287b65e9-fe12-5f0b-bf34-169a48b4025f'
function New-Record([string]$key,[string]$name,[string]$location) {
  return [pscustomobject]@{KeyName=$key;DisplayName=$name;InstallLocation=$location;UninstallString='';QuietUninstallString='';DisplayIcon=''}
}
function Run-Case([object]$Snapshot,[string[]]$Existing,[bool]$Allowed,[switch]$ReadFailure,[string]$AppId='cn.family.homestudycompanion',[string]$NsisGuid='') {
  $read={ if ($ReadFailure) { throw 'synthetic access denied' }; return $Snapshot }.GetNewClosure()
  $exists={ param($path) return $Existing -contains $path }.GetNewClosure()
  $passed=$false
  try {
    Assert-HscIsolatedInstallSafe -ReadRegistry $read -PathExists $exists -AppId $AppId -NsisGuid $NsisGuid -FormalDir 'C:\Synthetic\Formal' -InstallDir 'C:\Synthetic\Smoke' -DataDir 'C:\Synthetic\Data'
    $passed=$true
  } catch {
    if ($_.Exception.Message -notmatch '^SMOKE_INSTALL_(EXISTING_OR_UNCONFIRMED|IDENTITY_UNCONFIRMED)$') { throw }
  }
  if ($passed -ne $Allowed) { throw 'SYNTHETIC_GUARD_CASE_FAILED' }
}
$clean=[pscustomobject]@{Roots=$roots;Records=@()}
Run-Case $clean @() $true
# A product GUID blocks even if the actual install path is a custom D: path.
Run-Case ([pscustomobject]@{Roots=$roots;Records=@((New-Record $guid '' 'D:\AI家教'))}) @() $false
Run-Case ([pscustomobject]@{Roots=$roots;Records=@((New-Record "{$guid}" '' ''))}) @() $false
# A legacy/custom product key is still identified by its displayed name/path.
Run-Case ([pscustomobject]@{Roots=$roots;Records=@((New-Record 'legacy-custom' '家庭学习伙伴 0.1.2' 'D:\AI家教'))}) @() $false
Run-Case ([pscustomobject]@{Roots=$roots;Records=@((New-Record 'random' 'Other app' 'D:\AI家教'))}) @() $false
Run-Case ([pscustomobject]@{Roots=$roots;Records=@((New-Record 'unrelated' 'Other app' 'D:\Other'))}) @() $true
Run-Case $clean @() $false -ReadFailure
Run-Case ([pscustomobject]@{Roots=$roots[0..2];Records=@()}) @() $false
Run-Case $clean @('C:\Synthetic\Formal') $false
Run-Case $clean @('C:\Synthetic\Smoke') $false
Run-Case $clean @('C:\Synthetic\Data') $false
Run-Case $clean @() $false -AppId 'unexpected.app'
Run-Case $clean @() $false -NsisGuid 'different-guid'
$script:syntheticReadCount=0
$changingReader={
  $script:syntheticReadCount++
  if ($script:syntheticReadCount -eq 1) { return $clean }
  return [pscustomobject]@{Roots=$roots;Records=@((New-Record $guid '' 'D:\AI家教'))}
}
$noPaths={param($path) return $false}
Assert-HscIsolatedInstallSafe -ReadRegistry $changingReader -PathExists $noPaths -AppId 'cn.family.homestudycompanion' -NsisGuid '' -FormalDir 'C:\Synthetic\Formal' -InstallDir 'C:\Synthetic\Smoke' -DataDir 'C:\Synthetic\Data'
$blocked=$false
try { Assert-HscTargetRegistryAbsent $changingReader } catch { $blocked=$_.Exception.Message -eq 'SMOKE_INSTALL_EXISTING_OR_UNCONFIRMED' }
if (-not $blocked -or $script:syntheticReadCount -ne 2) { throw 'SYNTHETIC_RECHECK_FAILED' }
$denied=[pscustomobject]@{}
$denied | Add-Member ScriptMethod GetSubKeyNames { throw 'synthetic access denied' }
$blocked=$false
try { Open-HscConfirmedOptionalKey $denied 'synthetic' | Out-Null } catch { $blocked=$true }
if (-not $blocked) { throw 'SYNTHETIC_ENUM_DENIAL_NOT_BLOCKED' }
$raced=[pscustomobject]@{}
$raced | Add-Member ScriptMethod GetSubKeyNames { return @('present') }
$raced | Add-Member ScriptMethod OpenSubKey { param($name,$writable) return $null }
$blocked=$false
try { Open-HscConfirmedOptionalKey $raced 'present' | Out-Null } catch { $blocked=$true }
if (-not $blocked) { throw 'SYNTHETIC_PRESENT_NULL_NOT_BLOCKED' }
Write-Host 'SYNTHETIC_INSTALL_GUARD_PASSED'

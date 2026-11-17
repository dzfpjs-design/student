# Read-only admission guard for the isolated NSIS smoke. Never prints registry values.
$script:HscExpectedAppId = 'cn.family.homestudycompanion'
$script:HscExpectedGuid = '287b65e9-fe12-5f0b-bf34-169a48b4025f'

function Test-HscTargetRecord([object]$record) {
  if ($null -eq $record) { throw 'SMOKE_INSTALL_REGISTRY_UNCONFIRMED' }
  foreach ($key in @('KeyName','DisplayName','InstallLocation','UninstallString','QuietUninstallString','DisplayIcon')) {
    if (-not $record.PSObject.Properties[$key]) { throw 'SMOKE_INSTALL_REGISTRY_UNCONFIRMED' }
    $value = $record.$key
    if ($null -ne $value -and ($value -isnot [string] -or $value.Length -gt 32768)) {
      throw 'SMOKE_INSTALL_REGISTRY_UNCONFIRMED'
    }
  }
  $name = [string]$record.KeyName
  if ($name.Trim('{}') -ieq $script:HscExpectedGuid -or $name -ieq $script:HscExpectedAppId -or $name -ieq 'home-study-companion') { return $true }
  foreach ($value in @($record.DisplayName,$record.InstallLocation,$record.UninstallString,$record.QuietUninstallString,$record.DisplayIcon)) {
    if ($null -ne $value -and ($value -match '(?i)cn\.family\.homestudycompanion|home-study-companion|家庭学习伙伴|(^|[\\/])AI家教([\\/]|$)')) { return $true }
  }
  return $false
}

function Open-HscConfirmedOptionalKey([object]$Base,[string]$Path) {
  # OpenSubKey can return null on failure; only treat an enumerated absence as
  # missing. An inaccessible parent, race, or null open after presence blocks.
  $current=$Base; $owned=$false
  try {
    foreach ($part in $Path.Split('\')) {
      if ([string]::IsNullOrWhiteSpace($part)) { throw 'REGISTRY_PATH_INVALID' }
      $children=@($current.GetSubKeyNames())
      if ($part -inotIn $children) { return $null }
      $next=$current.OpenSubKey($part,$false)
      if ($null -eq $next) { throw 'REGISTRY_PRESENT_BUT_UNREADABLE' }
      if ($owned) { $current.Dispose() }
      $current=$next;$owned=$true
    }
    $owned=$false
    return $current
  } finally { if ($owned) { $current.Dispose() } }
}

function Get-HscInstallRegistrySnapshot {
  $records = New-Object System.Collections.ArrayList
  $roots = New-Object System.Collections.ArrayList
  foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser,[Microsoft.Win32.RegistryHive]::LocalMachine)) {
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64,[Microsoft.Win32.RegistryView]::Registry32)) {
      $base = $null; $uninstall = $null
      try {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
        if ($null -eq $base) { throw 'REGISTRY_ROOT_UNAVAILABLE' }
        foreach ($id in @($script:HscExpectedGuid,"{$script:HscExpectedGuid}",$script:HscExpectedAppId,'home-study-companion')) {
          $known = Open-HscConfirmedOptionalKey $base "Software\$id"
          if ($null -ne $known) {
            try { [void]$records.Add([pscustomobject]@{KeyName=$id;DisplayName=$null;InstallLocation=$null;UninstallString=$null;QuietUninstallString=$null;DisplayIcon=$null}) }
            finally { $known.Dispose() }
          }
        }
        $uninstall=Open-HscConfirmedOptionalKey $base 'Software\Microsoft\Windows\CurrentVersion\Uninstall'
        if ($null -eq $uninstall) { [void]$roots.Add("$hive/$view"); continue }
        $names=@($uninstall.GetSubKeyNames())
        if ($names.Count -gt 50000) { throw 'REGISTRY_ENUMERATION_TOO_LARGE' }
        foreach ($name in $names) {
          $entry=$uninstall.OpenSubKey($name,$false)
          if ($null -eq $entry) { throw 'REGISTRY_ENTRY_UNAVAILABLE' }
          try {
            $options=[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
            [void]$records.Add([pscustomobject]@{
              KeyName=$name;DisplayName=$entry.GetValue('DisplayName',$null,$options)
              InstallLocation=$entry.GetValue('InstallLocation',$null,$options)
              UninstallString=$entry.GetValue('UninstallString',$null,$options)
              QuietUninstallString=$entry.GetValue('QuietUninstallString',$null,$options)
              DisplayIcon=$entry.GetValue('DisplayIcon',$null,$options)
            })
          } finally { $entry.Dispose() }
        }
        [void]$roots.Add("$hive/$view")
      } finally {
        if ($null -ne $uninstall) { $uninstall.Dispose() }
        if ($null -ne $base) { $base.Dispose() }
      }
    }
  }
  return [pscustomobject]@{Roots=@($roots.ToArray());Records=@($records.ToArray())}
}

function Assert-HscTargetRegistryAbsent([scriptblock]$ReadRegistry) {
  try {
    $snapshot=& $ReadRegistry
    $required=@('CurrentUser/Registry64','CurrentUser/Registry32','LocalMachine/Registry64','LocalMachine/Registry32')
    if ($null -eq $snapshot -or $null -eq $snapshot.Roots -or $null -eq $snapshot.Records -or
        @($snapshot.Roots).Count -ne 4 -or @($snapshot.Roots | Select-Object -Unique).Count -ne 4) { throw 'ROOTS_INVALID' }
    foreach ($root in $required) { if ($root -cnotin @($snapshot.Roots)) { throw 'ROOTS_INVALID' } }
    foreach ($record in @($snapshot.Records)) {
      if (Test-HscTargetRecord $record) { throw 'TARGET_REGISTERED' }
    }
  } catch {
    throw 'SMOKE_INSTALL_EXISTING_OR_UNCONFIRMED'
  }
}

function Assert-HscIsolatedInstallSafe {
  param([scriptblock]$ReadRegistry,[scriptblock]$PathExists,[string]$AppId,[string]$NsisGuid,
        [string]$FormalDir,[string]$InstallDir,[string]$DataDir)
  if ($AppId -cne $script:HscExpectedAppId -or
      (-not [string]::IsNullOrWhiteSpace($NsisGuid) -and $NsisGuid.Trim('{}') -ine $script:HscExpectedGuid)) {
    throw 'SMOKE_INSTALL_IDENTITY_UNCONFIRMED'
  }
  Assert-HscTargetRegistryAbsent $ReadRegistry
  try {
    foreach ($path in @($FormalDir,$InstallDir,$DataDir)) {
      if ([string]::IsNullOrWhiteSpace($path) -or (& $PathExists $path)) { throw 'PATH_NOT_EMPTY_OR_UNCONFIRMED' }
    }
  } catch { throw 'SMOKE_INSTALL_EXISTING_OR_UNCONFIRMED' }
}

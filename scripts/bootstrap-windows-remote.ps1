param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^ssh-ed25519\s+[A-Za-z0-9+/=]+(?:\s+.*)?$')]
  [string]$AuthorizedKey,
  [string]$DeployUser = "hscdeploy"
)

$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this script in Windows PowerShell as Administrator."
}

$capability = Get-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
if ($capability.State -ne "Installed") {
  Add-WindowsCapability -Online -Name $capability.Name | Out-Null
}

$deployAccount = Get-LocalUser -Name $DeployUser -ErrorAction SilentlyContinue
if (-not $deployAccount) {
  $alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%"
  $randomPassword = -join (1..32 | ForEach-Object { $alphabet[(Get-Random -Maximum $alphabet.Length)] })
  $securePassword = ConvertTo-SecureString $randomPassword -AsPlainText -Force
  New-LocalUser -Name $DeployUser -Password $securePassword -AccountNeverExpires -PasswordNeverExpires -Description "Home Study Companion key-only deployment account" | Out-Null
} elseif (-not $deployAccount.Enabled) {
  Enable-LocalUser -Name $DeployUser
}

$administrators = Get-LocalGroup -SID "S-1-5-32-544"
$isAdministrator = Get-LocalGroupMember -Group $administrators -ErrorAction SilentlyContinue |
  Where-Object { $_.Name.Split("\")[-1] -eq $DeployUser }
if (-not $isAdministrator) {
  Add-LocalGroupMember -Group $administrators -Member $DeployUser
}

$sshRoot = Join-Path $env:ProgramData "ssh"
New-Item -ItemType Directory -Path $sshRoot -Force | Out-Null
$authorizedKeys = Join-Path $sshRoot "administrators_authorized_keys"
$existingKeys = if (Test-Path $authorizedKeys) { @(Get-Content $authorizedKeys) } else { @() }
if ($existingKeys -notcontains $AuthorizedKey) {
  Add-Content -Path $authorizedKeys -Value $AuthorizedKey -Encoding ASCII
}
& icacls.exe $authorizedKeys /inheritance:r | Out-Null
& icacls.exe $authorizedKeys /grant "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null

Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
if (-not (Get-NetFirewallRule -Name "HSC-OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name "HSC-OpenSSH-Server-In-TCP" -DisplayName "Home Study Companion SSH deployment" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any -RemoteAddress LocalSubnet | Out-Null
}

[pscustomobject]@{
  ready = $true
  computer_name = $env:COMPUTERNAME
  deploy_user = $DeployUser
  port = 22
  authentication = "ed25519_public_key"
} | ConvertTo-Json

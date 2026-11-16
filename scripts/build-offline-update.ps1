param(
  [Parameter(Mandatory=$true)][string]$ManifestPath,
  [Parameter(Mandatory=$true)][string]$AsarPath,
  [Parameter(Mandatory=$true)][string]$AgentPath,
  [Parameter(Mandatory=$true)][string]$PythonExe,
  [Parameter(Mandatory=$true)][string]$PythonLicensePath,
  [Parameter(Mandatory=$true)][string]$MakensisExe,
  [Parameter(Mandatory=$true)][string]$OutputDir,
  [string]$BaseFingerprintPath
)
$ErrorActionPreference='Stop'
throw 'SINGLE_FILE_AGENT_UPDATE_DISABLED_USE_FULL_INSTALLER'
$hasBaseFingerprint=$PSBoundParameters.ContainsKey('BaseFingerprintPath')
$baseArgument='-'
if($hasBaseFingerprint -and [string]::IsNullOrWhiteSpace($BaseFingerprintPath)){throw 'EXPLICIT_BASE_FINGERPRINT_PATH_REQUIRED'}
function Invoke-RootNativeBuild(
  [string]$Tool,[string[]]$NativeArguments,[string]$LogFile
){
  $savedPreference=$ErrorActionPreference
  try{
    # Windows PowerShell 5.1 otherwise turns PyInstaller INFO stderr into a
    # terminating NativeCommandError even when the real process exits zero.
    $ErrorActionPreference='Continue'
    & $Tool @NativeArguments *> $LogFile
    $nativeExit=$LASTEXITCODE
  }finally{$ErrorActionPreference=$savedPreference}
  return $nativeExit
}
$sourceRoot=Split-Path -Parent $PSScriptRoot
$author=Join-Path $sourceRoot 'tools\create_payload_manifest.py'
$helper=Join-Path $sourceRoot 'tools\offline_update.py'
$nsis=Join-Path $sourceRoot 'build\offline-update.nsi'
if($hasBaseFingerprint){
  $baseItem=Get-Item -LiteralPath $BaseFingerprintPath
  if($baseItem.PSIsContainer -or ($baseItem.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'BASE_FINGERPRINT_MUST_BE_REGULAR_FILE'}
  $baseArgument=$baseItem.FullName
}
foreach($p in @($ManifestPath,$AsarPath,$AgentPath,$PythonExe,$PythonLicensePath,$author,$MakensisExe,$helper,$nsis)){
  $item=Get-Item -LiteralPath $p
  if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'BUILD_INPUT_MUST_BE_REGULAR_FILE'}
}
if((Get-FileHash -LiteralPath $helper -Algorithm SHA256).Hash.ToLower() -ne '2443b11fe0b2c865f85acd2441a5b9950970e7ed6aa6aa399d9f83244f21dabe'){throw 'REVIEWED_R4_CORE_PIN_MISMATCH'}
if(Test-Path -LiteralPath $OutputDir){throw 'FRESH_OUTPUT_DIRECTORY_REQUIRED'}
New-Item -ItemType Directory -Path $OutputDir|Out-Null
$OutputDir=(Get-Item -LiteralPath $OutputDir).FullName
$payload=Join-Path $OutputDir 'payload'
New-Item -ItemType Directory -Path $payload|Out-Null
Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $payload 'manifest.json')
if($hasBaseFingerprint){(Get-Item -LiteralPath (Join-Path $payload 'manifest.json')).IsReadOnly=$false}
Copy-Item -LiteralPath $AsarPath -Destination (Join-Path $payload 'app.asar')
Copy-Item -LiteralPath $AgentPath -Destination (Join-Path $payload 'home-study-local-agent.exe')
$env:PYTHONDONTWRITEBYTECODE='1'
$check=@'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("checked_update_source",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
sys.path.insert(0,str(Path(sys.argv[1]).parent))
import create_payload_manifest as author
payload=Path(sys.argv[2]);manifest=author.bind_fresh_payload_manifest(payload/"manifest.json",None if sys.argv[3]=='-' else sys.argv[3])
for key,name in m.PAYLOAD_NAMES.items():
    assert m.matches(payload/name,m.artifact(manifest["files"][key],name)), "PAYLOAD_SHA_MISMATCH"
print("CLOSED_PAYLOAD_VERIFIED")
'@
$checkPath=Join-Path $OutputDir 'check-payload.py'
[IO.File]::WriteAllText($checkPath,$check,(New-Object Text.UTF8Encoding($false)))
if((Invoke-RootNativeBuild -Tool $PythonExe -NativeArguments @($checkPath,$helper,$payload,$baseArgument) -LogFile (Join-Path $OutputDir 'payload-check.log')) -ne 0){throw 'CLOSED_PAYLOAD_CHECK_FAILED'}
$sourcePins=@{}
foreach($p in @($helper,$author,$nsis,$PSCommandPath,$ManifestPath,$PythonLicensePath)){
  $sourcePins[$p]=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower()
}
if($hasBaseFingerprint){$sourcePins[$BaseFingerprintPath]=(Get-FileHash -LiteralPath $BaseFingerprintPath -Algorithm SHA256).Hash.ToLower()}
$dist=Join-Path $OutputDir 'helper-dist'
$pyArgs=@(
  '-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
  '--disable-windowed-traceback','--name','hsc-offline-update-helper',
  '--distpath',$dist,'--workpath',(Join-Path $OutputDir 'helper-work'),
  '--specpath',(Join-Path $OutputDir 'helper-spec'),
  '--add-data',($PythonLicensePath+';licenses'),$helper
)
# Publisher-only build. Never starts either the application or the update helper.
if((Invoke-RootNativeBuild -Tool $PythonExe -NativeArguments $pyArgs -LogFile (Join-Path $OutputDir 'helper-build.log')) -ne 0){throw 'PYINSTALLER_BUILD_FAILED'}
$helperExe=Join-Path $dist 'hsc-offline-update-helper.exe'
if(!(Test-Path -LiteralPath $helperExe)){throw 'HELPER_EXE_MISSING'}
$installer=Join-Path $OutputDir '家庭学习伙伴-离线更新.exe'
$nsisArgs=@(
  '/INPUTCHARSET','UTF8',
  ('/DHELPER_EXE='+$helperExe),
  ('/DMANIFEST_FILE='+(Join-Path $payload 'manifest.json')),
  ('/DASAR_FILE='+(Join-Path $payload 'app.asar')),
  ('/DAGENT_FILE='+(Join-Path $payload 'home-study-local-agent.exe')),
  ('/DOUTPUT_EXE='+$installer),
  $nsis
)
if((Invoke-RootNativeBuild -Tool $MakensisExe -NativeArguments $nsisArgs -LogFile (Join-Path $OutputDir 'nsis-build.log')) -ne 0 -or !(Test-Path -LiteralPath $installer)){throw 'NSIS_BUILD_FAILED'}
foreach($p in $sourcePins.Keys){
  if((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower() -ne $sourcePins[$p]){throw 'SOURCE_OR_MANIFEST_CHANGED_DURING_BUILD'}
}
$sha=(Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText((Join-Path $OutputDir 'SHA256SUMS.txt'),
  ($sha+'  '+[IO.Path]::GetFileName($installer)+[Environment]::NewLine),
  (New-Object Text.UTF8Encoding($false)))
[ordered]@{
  schema=1;status='BUILT_NOT_INSTALLED';installer_sha256=$sha
  installer_bytes=(Get-Item -LiteralPath $installer).Length
  input_manifest_sha256=(Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLower()
  release_manifest_sha256=(Get-FileHash -LiteralPath (Join-Path $payload 'manifest.json') -Algorithm SHA256).Hash.ToLower()
  helper_sha256=(Get-FileHash -LiteralPath $helperExe -Algorithm SHA256).Hash.ToLower()
  python_sha256=(Get-FileHash -LiteralPath $PythonExe -Algorithm SHA256).Hash.ToLower()
  makensis_sha256=(Get-FileHash -LiteralPath $MakensisExe -Algorithm SHA256).Hash.ToLower()
  source_pins=$sourcePins
  application_started=$false;update_helper_executed=$false;Windows_installation_verified=$false
}|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $OutputDir 'bundle-build.json') -Encoding UTF8
Write-Output ('BUILT_NOT_INSTALLED sha256='+$sha)

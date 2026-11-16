param([switch]$DirectShow)
$ErrorActionPreference = 'Stop'
$repo = 'C:\HSC\candidates\material-20260906'
$profile = 'C:\HSC\voice-controls-20260907-profile'
Get-ChildItem Env:HSC_* | Remove-Item
$env:HSC_ELECTRON_USER_DATA_DIR = $profile
$env:HSC_LOCAL_AGENT_PYTHON = "$repo\.venv\Scripts\python.exe"
$env:HSC_MAINTENANCE_ENABLED = 'false'
$env:PYTHONUTF8 = '1'
$env:PATH = 'C:\Program Files\nodejs;' + $env:PATH
Set-Location $repo
$arguments = @($repo, '--remote-debugging-port=9231')
if ($DirectShow) { $arguments += '--disable-features=MediaFoundationVideoCapture' }
$child = Start-Process -FilePath 'C:\HSC\tooling\electron-44.0.0\electron.exe' -ArgumentList $arguments -WorkingDirectory $repo -RedirectStandardOutput 'C:\HSC\voice-controls-desktop.stdout.log' -RedirectStandardError 'C:\HSC\voice-controls-desktop.stderr.log' -PassThru
Wait-Process -Id $child.Id

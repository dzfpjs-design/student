param(
  [string]$ProjectRoot = 'C:\HSC\candidates\full-product-20260925',
  [string]$OutputDirectory = 'C:\HSC\test-sessions\camera-mode-probe'
)

$ErrorActionPreference = 'Stop'
$electron = Join-Path $ProjectRoot '.electron-runtime\electron.exe'
$probe = Join-Path $ProjectRoot 'scripts\probe-document-camera-mode.mjs'
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$modes = @(
  @{ Width = 4160; Height = 3120 },
  @{ Width = 3840; Height = 2160 },
  @{ Width = 3264; Height = 2448 },
  @{ Width = 2560; Height = 1440 },
  @{ Width = 1920; Height = 1080 },
  @{ Width = 1280; Height = 720 }
)

$results = @()
foreach ($mode in $modes) {
  $name = "$($mode.Width)x$($mode.Height)"
  $stdout = Join-Path $OutputDirectory "$name.stdout.json"
  $stderr = Join-Path $OutputDirectory "$name.stderr.json"
  Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
  $process = Start-Process -FilePath $electron -ArgumentList @(
    $probe,
    '--width', $mode.Width,
    '--height', $mode.Height,
    '--device-pattern', '^Usb HDCamera',
    '--open-timeout-ms', '15000'
  ) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
  $completed = $process.WaitForExit(35000)
  if (-not $completed) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Get-Process -Name electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $results += [ordered]@{ requested = $name; status = 'failed'; error = 'EXTERNAL_PROCESS_TIMEOUT' }
  } else {
    $raw = if (Test-Path $stdout) { (Get-Content $stdout -Raw).Trim() } else { '' }
    $errorText = if (Test-Path $stderr) { (Get-Content $stderr -Raw).Trim() } else { '' }
    if ($raw) {
      try { $results += ($raw | ConvertFrom-Json) }
      catch { $results += [ordered]@{ requested = $name; status = 'failed'; error = 'INVALID_PROBE_OUTPUT'; detail = $raw.Substring(0, [Math]::Min(240, $raw.Length)) } }
    } else {
      $results += [ordered]@{ requested = $name; status = 'failed'; error = if ($errorText) { $errorText } else { "EXIT_$($process.ExitCode)" } }
    }
  }
  Start-Sleep -Milliseconds 1200
}

$report = [ordered]@{
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  raw_frame_saved = $false
  raw_frame_uploaded = $false
  results = $results
}
$reportPath = Join-Path $OutputDirectory 'summary.json'
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $reportPath -Encoding UTF8
$report | ConvertTo-Json -Depth 8

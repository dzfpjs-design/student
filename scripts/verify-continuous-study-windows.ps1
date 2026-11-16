param([string]$Repo = "C:\HSC\source-20260904-1250")
$ErrorActionPreference = "Stop"
Set-Location $Repo
$evidence = "C:\HSC\device-evidence\continuous-study-20260905"
New-Item -ItemType Directory -Force $evidence | Out-Null
$ErrorActionPreference = "Continue"
$tests = @(
  "test_document_study_integration.py", "test_document_learning.py", "test_document_vision.py",
  "test_document_monitoring.py", "test_document_evidence.py", "test_document_companion_memory.py",
  "test_agent_document_observation_context.py", "test_companion_memory.py",
  "test_companion_memory_integration.py", "test_companion_memory_study_review.py",
  "test_memory_asr_privacy.py", "test_behavior_ocr_app_integration.py", "test_local_ocr_pipeline.py",
  "test_app.py"
) | ForEach-Object { Join-Path "services/local_agent/tests" $_ }
& .venv\Scripts\python.exe -m pytest @tests -q *> (Join-Path $evidence "backend-tests.log")
if ($LASTEXITCODE -ne 0) { throw "Backend regression failed; see backend-tests.log" }
Write-Output "Backend regression passed"
& npm.cmd run typecheck *> (Join-Path $evidence "typecheck.log")
if ($LASTEXITCODE -ne 0) { throw "Typecheck failed" }
& npm.cmd run test -- --reporter=dot *> (Join-Path $evidence "frontend-tests.log")
if ($LASTEXITCODE -ne 0) { throw "Frontend regression failed; see frontend-tests.log" }
Write-Output "Frontend regression passed"
& npm.cmd run build:agent:win *> (Join-Path $evidence "build-agent.log")
if ($LASTEXITCODE -ne 0) { throw "Agent build failed" }
& npm.cmd run build *> (Join-Path $evidence "build-ui.log")
if ($LASTEXITCODE -ne 0) { throw "UI build failed" }
& npm.cmd run verify:package:win *> (Join-Path $evidence "package-inputs.log")
if ($LASTEXITCODE -ne 0) { throw "Package inputs failed" }
& node_modules\.bin\electron-builder.cmd --win nsis --config.electronDist=node_modules/electron/dist *> (Join-Path $evidence "installer.log")
if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
Write-Output "Windows installer verified and built; not installed yet"

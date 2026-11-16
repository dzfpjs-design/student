from pathlib import Path

from home_study_agent.database import LATEST_SCHEMA_VERSION


def test_windows_package_smokes_pin_the_current_local_service_schema() -> None:
    root = Path(__file__).resolve().parents[3]
    expected = f'"{LATEST_SCHEMA_VERSION}"'
    agent_smoke = (root / "scripts" / "smoke-windows-agent.ps1").read_text(encoding="utf-8")
    installer_smoke = (root / "scripts" / "smoke-windows-installer.ps1").read_text(encoding="utf-8")
    assert f'$schemaVersion.Trim() -ne {expected}' in agent_smoke
    assert f'$runtimeLines[1] -ne {expected}' in installer_smoke
    assert f'$upgradeLines[0] -ne {expected}' in installer_smoke
    assert f'$upgradeMetadata.target_schema_version -ne {LATEST_SCHEMA_VERSION}' in installer_smoke
    runbook = (root / 'docs/development/windows-release-runbook.md').read_text(encoding='utf-8')
    assert f'SQLite v{LATEST_SCHEMA_VERSION}' in runbook
    assert f'数据库版本不是{LATEST_SCHEMA_VERSION}' in runbook


def test_windows_installer_smoke_allows_fresh_startup_backup_to_finish() -> None:
    root = Path(__file__).resolve().parents[3]
    installer_smoke = (root / "scripts" / "smoke-windows-installer.ps1").read_text(encoding="utf-8")
    assert "$automaticBackupWaitSeconds = 60" in installer_smoke
    assert "$automaticBackupAttempts" in installer_smoke
    assert "within $automaticBackupWaitSeconds seconds" in installer_smoke


def test_windows_installer_smoke_cleans_only_its_installed_process_tree() -> None:
    root = Path(__file__).resolve().parents[3]
    installer_smoke = (root / "scripts" / "smoke-windows-installer.ps1").read_text(encoding="utf-8")
    assert "function Stop-OwnedInstallProcesses" in installer_smoke
    assert "ExecutablePath.StartsWith" in installer_smoke
    assert "taskkill.exe /PID $process.ProcessId /T /F" in installer_smoke
    assert "Stop-Process -Name" not in installer_smoke

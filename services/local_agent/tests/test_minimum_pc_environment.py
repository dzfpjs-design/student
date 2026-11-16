from __future__ import annotations

import pytest

from home_study_agent.evaluation.minimum_pc_environment import collect_environment, has_collected_provenance


def test_windows_environment_collector_uses_measured_fields_and_explicit_avx2_evidence() -> None:
    evidence = collect_environment(
        system_name="Windows", machine="AMD64", logical_cpus=8,
        memory_bytes=16 * 1024 ** 3, nvme_bytes=512 * 1024 ** 3,
        cpu_flags={"sse2", "avx2"}, gpu_devices=["Integrated GPU"],
    )
    assert evidence["architecture"] == "x64"
    assert evidence["avx2"] is True and evidence["avx2_evidence"] == "cpu_flags"
    assert evidence["memory_gb"] == 16 and evidence["nvme_gb"] == 512
    assert evidence["discrete_gpu_required"] is False
    assert has_collected_provenance(evidence) is True


def test_windows_environment_collector_never_assumes_avx2_and_rejects_non_windows() -> None:
    evidence = collect_environment(system_name="Windows", machine="AMD64", logical_cpus=8, memory_bytes=16 * 1024 ** 3, nvme_bytes=512 * 1024 ** 3, cpu_flags=set(), gpu_devices=[])
    assert evidence["avx2"] is False and evidence["avx2_evidence"] == "not_verified"
    with pytest.raises(RuntimeError, match="Windows"):
        collect_environment(system_name="Darwin")

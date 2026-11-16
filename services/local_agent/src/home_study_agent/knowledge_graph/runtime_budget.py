from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass(frozen=True)
class MinimumRuntimeBudget:
    """CPU-only deployment envelope; GPU/NPU is optional acceleration only."""
    cpu_cores: int = 4
    cpu_threads: int = 8
    memory_gb: int = 16
    nvme_gb: int = 512
    vector_dimensions: int = 64
    batch_resources: int = 25
    throttle_ms: int = 50
    minimum_free_gb: int = 20

    def storage_check(self, index_root: Path) -> dict:
        usage = shutil.disk_usage(index_root)
        return {"index_root": str(index_root), "free_gb": round(usage.free / 1024**3, 2),
                "minimum_free_gb": self.minimum_free_gb, "sufficient": usage.free >= self.minimum_free_gb * 1024**3}

    @staticmethod
    def estimate_vector_bytes(segment_count: int, dimensions: int = 64) -> int:
        # Raw float32 payload only; SQLite/FTS text overhead must be provisioned separately.
        return segment_count * dimensions * 4

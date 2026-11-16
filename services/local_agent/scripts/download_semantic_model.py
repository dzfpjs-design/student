"""Explicit installation only; normal application inference never uses network."""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from home_study_agent.retrieval.local_semantic import FILES, MODEL_DIR, MODEL_REPO, REVISION, file_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'models')
    parser.add_argument('--mirror', action='store_true', help='Use hf-mirror.com with the same pinned checksums')
    args = parser.parse_args()
    root = args.output.resolve() / MODEL_DIR
    if root.is_symlink():
        raise RuntimeError('Model directory must not be a symbolic link')
    root.mkdir(parents=True, exist_ok=True)
    host = 'hf-mirror.com' if args.mirror else 'huggingface.co'
    for name, (size, expected) in FILES.items():
        target = root / name
        if target.is_file() and not target.is_symlink() and target.stat().st_size == size and file_hash(target) == expected:
            continue
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=root)
        try:
            with os.fdopen(fd, 'wb') as output:
                with httpx.stream('GET', f'https://{host}/{MODEL_REPO}/resolve/{REVISION}/{name}',
                                  timeout=180, follow_redirects=True) as response:
                    response.raise_for_status()
                    written = 0
                    for block in response.iter_bytes(1024 * 1024):
                        written += len(block)
                        if written > size:
                            raise RuntimeError('Model file exceeds pinned size')
                        output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if Path(temporary).stat().st_size != size or file_hash(temporary) != expected:
                raise RuntimeError('Model integrity check failed; previous file retained')
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f'Pinned semantic model verified: {root}')


if __name__ == '__main__':
    main()

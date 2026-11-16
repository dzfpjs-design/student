"""Pinned, CPU-only BGE retrieval. Never downloads or grants scoring authority."""
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import monotonic

MODEL_DIR = 'bge-small-zh-v1.5'
MODEL_REPO = 'Qdrant/bge-small-zh-v1.5'
REVISION = '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
FILES = {
    'model_optimized.onnx': (94781076, '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38'),
    'tokenizer.json': (439125, '48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26'),
}


def file_hash(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class LocalSemanticMatcher:
    def __init__(self, model_root):
        self.root = Path(model_root) / MODEL_DIR
        self._lock = RLock()
        self._loaded_stamp = None
        self._session = self._tokenizer = None
        self._last_status = 'not_run'

    def status(self):
        return {'engine': 'bge_small_zh_onnx_v1', 'model_revision': REVISION,
                'assets_present': all((self.root / name).is_file() for name in FILES),
                'last_inference_status': self._last_status, 'network_allowed': False}

    def _load(self):
        paths = [self.root / name for name in FILES]
        if self.root.is_symlink():
            return 'invalid_assets'
        if any(not p.is_file() or p.is_symlink() for p in paths):
            return 'missing_assets'
        stamp = tuple((p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ino) for p in paths)
        if stamp == self._loaded_stamp:
            return 'ready'
        self._session = self._tokenizer = None
        self._loaded_stamp = None
        for path, (size, digest) in zip(paths, FILES.values()):
            if path.stat().st_size != size or file_hash(path) != digest:
                return 'invalid_assets'
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(paths[0]), sess_options=options, providers=['CPUExecutionProvider'])
        self._tokenizer = Tokenizer.from_file(str(paths[1]))
        self._tokenizer.no_truncation()
        self._tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
        self._loaded_stamp = stamp
        return 'ready'

    def scores(self, query, documents):
        result = self._scores(query, documents)
        self._last_status = result['status']
        return result

    def _scores(self, query, documents):
        result = {'engine': 'bge_small_zh_onnx_v1', 'model_revision': REVISION, 'status': 'unavailable'}
        if (not isinstance(query, str) or not query.strip() or not isinstance(documents, list)
                or not documents or any(not isinstance(s, str) or not s.strip()
                or len(s) > 4000 for s in [query, *documents])):
            return {**result, 'status': 'input_limit'}
        with self._lock:
            try:
                status = self._load()
                if status != 'ready':
                    return {**result, 'status': status}
                import numpy as np
                scores = []
                query_vector = None
                start = monotonic()
                texts = [query, *documents]
                for offset in range(0, len(texts), 8):
                    if monotonic() - start > 5:
                        return {**result, 'status': 'time_budget'}
                    encoded = self._tokenizer.encode_batch(texts[offset:offset + 8])
                    if any(len(e.ids) > 512 for e in encoded):
                        return {**result, 'status': 'token_limit'}
                    inputs = {name: np.asarray([getattr(e, attr) for e in encoded], dtype=np.int64)
                              for name, attr in [('input_ids', 'ids'), ('attention_mask', 'attention_mask'),
                                                 ('token_type_ids', 'type_ids')]}
                    hidden = self._session.run(['last_hidden_state'], inputs)[0]
                    cls = hidden[:, 0, :]
                    norms = np.linalg.norm(cls, axis=1, keepdims=True)
                    if cls.shape != (len(encoded), 512) or not np.isfinite(cls).all() or (norms <= 0).any():
                        return {**result, 'status': 'invalid_output'}
                    normalized = cls / norms
                    if query_vector is None:
                        query_vector = normalized[0]
                        normalized = normalized[1:]
                    # Retain only scalar scores, not all source embeddings.
                    values = normalized @ query_vector
                    scores.extend(round(float(v), 6) for v in values)
                if monotonic() - start > 5:
                    return {**result, 'status': 'time_budget'}
                return {**result, 'status': 'ready', 'scores': scores}
            except ImportError:
                return {**result, 'status': 'missing_dependency'}
            except Exception:
                # No inputs, vectors, provider exception text or file paths enter logs.
                return {**result, 'status': 'inference_failed'}

"""Short-lived receipts for actual local household ASR; no audio/text retention.

Only the ASR service issues receipts. A browser's transcript is never evidence
of having run ASR. Once canonically admitted, the durable witness replaces this
RAM receipt; restarting before admission deliberately invalidates the candidate.
"""
import hashlib
import re
import threading
import time
from uuid import uuid4

SOURCE = 'household_local_transcript'


def text_hash(text):
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()


class HouseholdEntryConflict(ValueError):
    pass


class HouseholdEntryReceipts:
    def __init__(self, *, clock=None, monotonic=None, generation_reader=None):
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.generation_reader = generation_reader or (lambda: None)
        self._lock = threading.Lock()
        self._items = {}

    def issue(self, result, *, generation_before=None):
        if (result.get('status') != 'completed' or result.get('cloud_used') is not False
                or result.get('persisted') is not False or type(result.get('transcript')) is not str
                or not result['transcript'].strip() or len(result['transcript']) > 16000
                or type(result.get('client_action_id')) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', result['client_action_id'])
                or type(result.get('model_version')) is not str or not 1 <= len(result['model_version']) <= 300):
            raise HouseholdEntryConflict('HOUSEHOLD_ASR_RECEIPT_INVALID')
        storage_generation = self.generation_reader()
        if storage_generation is None or generation_before is not None and storage_generation != generation_before:
            raise HouseholdEntryConflict('HOUSEHOLD_ENTRY_GENERATION_CHANGED')
        receipt = {'version': 1, 'origin_scope': 'unassigned_household', 'storage_generation': storage_generation, 'receipt_id': str(uuid4()),
            'client_action_id': result['client_action_id'],
            'issued_at_ms': int(self.clock() * 1000),
            'transcript_sha256': text_hash(result['transcript']),
            'model_version': result['model_version'], 'source_kind': SOURCE}
        with self._lock:
            now = self.monotonic()
            self._items = {k: v for k, v in self._items.items() if now < v[0]}
            while len(self._items) >= 128:
                self._items.pop(next(iter(self._items)))
            self._items[receipt['receipt_id']] = (now + 120, dict(receipt))
        return receipt

    def verify(self, receipt, transcript):
        if (type(receipt) is not dict or type(transcript) is not str
                or type(receipt.get('receipt_id')) is not str):
            raise HouseholdEntryConflict('HOUSEHOLD_ASR_RECEIPT_INVALID')
        with self._lock:
            item = self._items.get(receipt.get('receipt_id'))
            if (not item or self.monotonic() >= item[0] or receipt != item[1]
                    or any(type(receipt[k]) is not type(v) for k, v in item[1].items())
                    or text_hash(transcript) != item[1]['transcript_sha256']):
                raise HouseholdEntryConflict('HOUSEHOLD_ASR_RECEIPT_UNCONFIRMED')
            return dict(item[1])

"""Ephemeral, single-transaction calibration read. Never a score authority cache."""
from copy import deepcopy
import json

from .rubric_calibration_evidence import digest, require


def _changes(c):
    value = getattr(c, 'total_changes', None)
    return value if value is not None else c.execute('SELECT total_changes()').fetchone()[0]


class ReadScope:
    def __init__(self, c, child_id, resolver):
        from .rubric_calibration_authority import SignatureChecks
        require(c.in_transaction, 'READ_TRANSACTION_REQUIRED')
        self.c, self.child_id, self.resolver = c, child_id, resolver
        self.changes = _changes(c)
        self.sources = {}
        self.index = None
        self.used = {}
        self.verified = {}
        self.policy = None
        self.signatures = SignatureChecks()

    def resolve(self, c, *, child_id, kind, reference_id):
        from . import rubric_calibration_authority as authority
        require(c is self.c and child_id == self.child_id and c.in_transaction
                and _changes(c) == self.changes, 'READ_SCOPE_CHANGED')
        if self.resolver is not None:
            return self.resolver(c, child_id=child_id, kind=kind, reference_id=reference_id)
        if kind == 'assessment_scorer':
            return authority.resolve(c, child_id=child_id, kind=kind, reference_id=reference_id)
        if self.index is None:
            self.policy = digest(authority.trusted_issuers())
            self.index, invalid = {}, set()
            self.malformed = False
            for row in authority.rows(c, child_id):
                try:
                    value = json.loads(row['summary_json'])
                    if row['action'] == authority.IMPORT:
                        envelope = value['envelope']
                        self.index.setdefault((envelope['kind'], envelope['receipt_id']), []).append((row, value))
                    else:
                        invalid.add(value['import_id'])
                except (ValueError, TypeError, KeyError):
                    self.malformed = True
            self.invalid = invalid
        matches = self.index.get((kind, reference_id), [])
        if self.malformed or len(matches) != 1 or matches[0][0]['audit_id'] in self.invalid:
            return None
        row, value = matches[0]
        if row['audit_id'] in self.verified:
            return deepcopy(self.verified[row['audit_id']])
        try:
            proof = authority.verify(child_id, deepcopy(value['envelope']), value['signature'],
                                     _signatures=self.signatures)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        self.used[row['audit_id']] = value
        self.verified[row['audit_id']] = deepcopy(proof)
        return proof

    def finish(self):
        try:
            self._finish()
            from .rubric_calibration_transaction import scope_finished
            scope_finished(self)
        finally:
            self.signatures.clear()

    def _finish(self):
        from . import rubric_calibration_authority as authority
        require(self.c.in_transaction and _changes(self.c) == self.changes, 'READ_SCOPE_CHANGED')
        if self.policy is not None:
            require(digest(authority.trusted_issuers()) == self.policy, 'TRUST_CONFIGURATION_CHANGED')
            # Recheck expiration and trust at acceptance, including receipts read early.
            for value in self.used.values():
                authority.verify(self.child_id, value['envelope'], value['signature'],
                                 _signatures=self.signatures)
            require(digest(authority.trusted_issuers()) == self.policy, 'TRUST_CONFIGURATION_CHANGED')

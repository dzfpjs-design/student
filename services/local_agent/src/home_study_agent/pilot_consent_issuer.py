"""Verify installer-supplied family diagnostic consent without a built-in recipient."""
from __future__ import annotations
import base64
import binascii

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ISSUER_PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA2lNvpp59ZjMepjDgy0tRCp5xLYlpVvsorhXaGXO7G4U=
-----END PUBLIC KEY-----'''


def verify_issuer_signature(bootstrap_id: str, recipient: str, signature: str | None) -> bool:
    if not signature:
        return False
    try:
        encoded = base64.b64decode(signature, validate=True)
        key = serialization.load_pem_public_key(ISSUER_PUBLIC_KEY)
        if len(encoded) != 64 or not isinstance(key, Ed25519PublicKey):
            return False
        message = f'hsc-pilot-diagnostic-consent-v1\n{bootstrap_id}\n{recipient}'.encode()
        key.verify(encoded, message)
        return True
    except (ValueError, TypeError, binascii.Error, InvalidSignature):
        return False

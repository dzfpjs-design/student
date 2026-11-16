from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import math
import secrets

from .database import Database
from .repository import utc_now


class ParentAuth:
    def __init__(self, database: Database):
        self.database = database
        self._tokens: dict[str, datetime] = {}

    def status(self) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT failed_attempts, locked_until FROM parent_security WHERE singleton_id = 1"
            ).fetchone()
        locked_seconds = 0
        if row and row["locked_until"]:
            locked_until = datetime.fromisoformat(row["locked_until"])
            locked_seconds = self._remaining_lock_seconds(locked_until, datetime.now(timezone.utc))
        return {"configured": row is not None, "locked_seconds": locked_seconds}

    def setup(self, pin: str) -> str:
        self._validate_pin(pin)
        salt = secrets.token_bytes(16)
        digest = self._derive(pin, salt)
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM parent_security WHERE singleton_id = 1"
            ).fetchone()
            if existing:
                raise ParentAlreadyConfiguredError()
            connection.execute(
                """
                INSERT INTO parent_security(
                  singleton_id, pin_salt, pin_hash, failed_attempts, locked_until, created_at, updated_at
                ) VALUES (1, ?, ?, 0, NULL, ?, ?)
                """,
                (salt, digest, now, now),
            )
            connection.commit()
        return self._issue_token()

    def unlock(self, pin: str) -> str:
        self._validate_pin(pin)
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT pin_salt, pin_hash, failed_attempts, locked_until FROM parent_security WHERE singleton_id = 1"
            ).fetchone()
            if not row:
                raise ParentNotConfiguredError()
            failed_attempts = row["failed_attempts"]
            if row["locked_until"]:
                locked_until = datetime.fromisoformat(row["locked_until"])
                locked_seconds = self._remaining_lock_seconds(locked_until, now)
                if locked_seconds > 0:
                    raise ParentLockedError(locked_seconds)
                failed_attempts = 0
            if not hmac.compare_digest(self._derive(pin, row["pin_salt"]), row["pin_hash"]):
                attempts = failed_attempts + 1
                locked_until = (now + timedelta(minutes=5)).isoformat() if attempts >= 5 else None
                connection.execute(
                    "UPDATE parent_security SET failed_attempts = ?, locked_until = ?, updated_at = ? WHERE singleton_id = 1",
                    (attempts, locked_until, utc_now()),
                )
                connection.commit()
                if locked_until:
                    raise ParentLockedError(300)
                raise InvalidParentPinError(5 - attempts)
            connection.execute(
                "UPDATE parent_security SET failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE singleton_id = 1",
                (utc_now(),),
            )
            connection.commit()
        return self._issue_token()

    def require(self, token: str | None) -> None:
        if not token:
            raise ParentSessionRequiredError()
        expiry = self._tokens.get(token)
        if not expiry or expiry <= datetime.now(timezone.utc):
            self._tokens.pop(token, None)
            raise ParentSessionRequiredError()

    def expires_at(self, token: str | None) -> datetime:
        """Read the existing expiry without extending the parent session."""
        self.require(token)
        return self._tokens[token]

    def _issue_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = datetime.now(timezone.utc) + timedelta(minutes=30)
        return token

    @staticmethod
    def _validate_pin(pin: str) -> None:
        if not pin.isdigit() or not 4 <= len(pin) <= 8:
            raise InvalidParentPinFormatError()

    @staticmethod
    def _derive(pin: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 310_000, dklen=32)

    @staticmethod
    def _remaining_lock_seconds(locked_until: datetime, now: datetime) -> int:
        return max(0, math.ceil((locked_until - now).total_seconds()))


class ParentAlreadyConfiguredError(Exception):
    pass


class ParentNotConfiguredError(Exception):
    pass


class InvalidParentPinFormatError(Exception):
    pass


class InvalidParentPinError(Exception):
    def __init__(self, attempts_remaining: int):
        super().__init__(str(attempts_remaining))
        self.attempts_remaining = attempts_remaining


class ParentLockedError(Exception):
    def __init__(self, locked_seconds: int):
        super().__init__(str(locked_seconds))
        self.locked_seconds = locked_seconds


class ParentSessionRequiredError(Exception):
    pass

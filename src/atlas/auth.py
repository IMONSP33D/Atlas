"""Bearer-key auth against argon2id hashes, plus per-key rate limiting."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from .config import ApiKey, AuthConfig
from .errors import AtlasError

_ph = PasswordHasher()


@dataclass
class _Bucket:
    capacity: int
    tokens: float
    updated: float


@dataclass
class Authenticator:
    config: AuthConfig
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict, init=False)
    # Verified plaintext -> key name; argon2 is deliberately slow, don't re-run it per request.
    _cache: dict[str, str] = field(default_factory=dict, init=False)

    def authenticate(self, header: str | None) -> ApiKey:
        if not header:
            raise AtlasError("AUTH_MISSING", "Authorization header required")
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AtlasError("AUTH_MISSING", "expected 'Authorization: Bearer <key>'")

        key = self._lookup(token)
        if key is None:
            raise AtlasError("AUTH_INVALID", "key does not match any configured key")
        if key.revoked:
            raise AtlasError("AUTH_REVOKED", f"key '{key.name}' has been revoked", key=key.name)
        self._take_token(key)
        return key

    def _lookup(self, token: str) -> ApiKey | None:
        by_name = {k.name: k for k in self.config.keys}
        name = self._cache.get(token)
        if name in by_name:
            return by_name[name]
        for k in self.config.keys:
            try:
                if _ph.verify(k.hash, token):
                    self._cache[token] = k.name
                    return k
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                continue
        return None

    def _take_token(self, key: ApiKey) -> None:
        now = self.clock()
        b = self._buckets.get(key.name)
        if b is None or b.capacity != key.rate_limit_per_min:
            b = _Bucket(key.rate_limit_per_min, float(key.rate_limit_per_min), now)
            self._buckets[key.name] = b
        refill_per_sec = b.capacity / 60.0
        b.tokens = min(b.capacity, b.tokens + (now - b.updated) * refill_per_sec)
        b.updated = now
        if b.tokens < 1.0:
            retry_after = int((1.0 - b.tokens) / refill_per_sec) + 1
            raise AtlasError(
                "RATE_LIMITED",
                f"key '{key.name}' exceeded {key.rate_limit_per_min} requests/min",
                retry_after_sec=retry_after,
            )
        b.tokens -= 1.0


def hash_key(plaintext: str) -> str:
    """Same parameters as scripts/atlas-keygen.py."""
    from argon2.low_level import Type
    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=Type.ID).hash(plaintext)

"""sandbox/auth.py

Authentication and authorisation layer for the autonomous AI healing engine.

Contains:
  * four credential dataclasses: ``JWTPayload``, ``UserContext``,
    ``RBACPolicy`` and ``APICredential``
  * ``TokenValidator``: HS256 JWT validation, API-key validation, rate
    limiting and RBAC checks, instrumented with the telemetry decorators
  * helpers that raise the custom exceptions defined in ``sandbox.config``
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from sandbox.config import (
    AppConfig,
    AuthFailedError,
    AuthScheme,
    ConfigValidationError,
    LogLevel,
    ModuleInitializationError,
    PolicyViolationError,
    RateLimitExceededError,
    SecurityPolicy,
    SystemDegradedError,
    TelemetryError,
    create_app_config,
)
from sandbox.telemetry import (
    MetricsAggregator,
    audit_log,
    circuit_breaker,
    track_latency,
)

MIN_SECRET_BYTES = 16
SUPPORTED_ALGORITHMS: FrozenSet[str] = frozenset({"HS256"})


# ---------------------------------------------------------------------------
# Credential dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JWTPayload:
    """Validated claim set of a JSON Web Token."""

    sub: str
    iss: str
    aud: str
    exp: float
    iat: float
    jti: str = field(default_factory=lambda: uuid.uuid4().hex)
    roles: Tuple[str, ...] = ()
    scopes: Tuple[str, ...] = ()

    @classmethod
    def from_claims(cls, claims: Dict[str, Any]) -> "JWTPayload":
        required = ("sub", "iss", "aud", "exp", "iat")
        missing = [name for name in required if name not in claims]
        if missing:
            raise AuthFailedError(f"Token missing required claims: {', '.join(missing)}")
        try:
            return cls(
                sub=str(claims["sub"]),
                iss=str(claims["iss"]),
                aud=str(claims["aud"]),
                exp=float(claims["exp"]),
                iat=float(claims["iat"]),
                jti=str(claims.get("jti") or uuid.uuid4().hex),
                roles=tuple(claims.get("roles", ())),
                scopes=tuple(claims.get("scopes", ())),
            )
        except (TypeError, ValueError) as exc:
            raise AuthFailedError("Token contains malformed claims") from exc

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.exp

    def to_claims(self) -> Dict[str, Any]:
        return {
            "sub": self.sub,
            "iss": self.iss,
            "aud": self.aud,
            "exp": self.exp,
            "iat": self.iat,
            "jti": self.jti,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
        }


@dataclass
class UserContext:
    """Authenticated caller identity passed through the engine."""

    user_id: str
    roles: Tuple[str, ...] = ()
    scopes: Tuple[str, ...] = ()
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    authenticated_at: float = field(default_factory=time.time)
    client_ip: str = "127.0.0.1"
    metadata: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: JWTPayload, client_ip: str = "127.0.0.1") -> "UserContext":
        return cls(
            user_id=payload.sub,
            roles=payload.roles,
            scopes=payload.scopes,
            client_ip=client_ip,
            metadata={"jti": payload.jti, "issuer": payload.iss},
        )

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass
class RBACPolicy:
    """Role-based access control matrix with simple role inheritance."""

    role_permissions: Dict[str, Set[str]] = field(
        default_factory=lambda: {
            "viewer": {"metrics:read", "status:read"},
            "operator": {"healing:trigger", "patch:review"},
            "admin": {"patch:apply", "config:write", "rollback:execute"},
        }
    )
    role_hierarchy: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "operator": ["viewer"],
            "admin": ["operator"],
        }
    )

    def permissions_for(self, role: str, _seen: Optional[Set[str]] = None) -> Set[str]:
        seen = _seen if _seen is not None else set()
        if role in seen:
            raise ConfigValidationError("role_hierarchy", f"cycle detected at role '{role}'")
        seen.add(role)
        granted: Set[str] = set(self.role_permissions.get(role, set()))
        for parent in self.role_hierarchy.get(role, []):
            granted |= self.permissions_for(parent, seen)
        return granted

    def is_allowed(self, roles: Tuple[str, ...], permission: str) -> bool:
        return any(permission in self.permissions_for(role) for role in roles)


@dataclass
class APICredential:
    """Stored API key record (only a hash of the secret is kept)."""

    key_id: str
    secret_hash: str
    owner: str
    scopes: Tuple[str, ...] = ()
    expires_at: Optional[float] = None
    revoked: bool = False

    @staticmethod
    def hash_secret(secret: str) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def is_active(self, now: Optional[float] = None) -> bool:
        if self.revoked:
            return False
        if self.expires_at is None:
            return True
        return (now if now is not None else time.time()) < self.expires_at


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError) as exc:
        raise AuthFailedError("Token segment is not valid base64url") from exc


# ---------------------------------------------------------------------------
# Token validator
# ---------------------------------------------------------------------------
class TokenValidator:
    """Validates JWTs and API keys, enforces rate limits and RBAC."""

    def __init__(
        self,
        secret: str,
        app_config: Optional[AppConfig] = None,
        rbac: Optional[RBACPolicy] = None,
        aggregator: Optional[MetricsAggregator] = None,
        issuer: str = "ai-healing-engine",
        audience: str = "healing-api",
        clock_skew_s: float = 5.0,
    ) -> None:
        self.config: AppConfig = app_config or create_app_config()
        self.policy: SecurityPolicy = self.config.security

        if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
            raise ModuleInitializationError(
                "TokenValidator",
                ConfigValidationError("secret", f"must be at least {MIN_SECRET_BYTES} bytes"),
            )
        if self.policy.auth_scheme not in (AuthScheme.JWT, AuthScheme.API_KEY):
            raise ModuleInitializationError(
                "TokenValidator",
                ConfigValidationError("auth_scheme", "TokenValidator requires JWT or API_KEY"),
            )

        self._secret = secret.encode("utf-8")
        self.rbac: RBACPolicy = rbac or RBACPolicy()
        self.aggregator = aggregator
        self.issuer = issuer
        self.audience = audience
        self.clock_skew_s = clock_skew_s

        self._revoked_jti: Set[str] = set()
        self._api_keys: Dict[str, APICredential] = {}
        self._buckets: Dict[str, Tuple[float, float]] = {}
        self._backend_healthy = True

    # -- internal helpers ---------------------------------------------------
    def _emit(self, name: str, value: float = 1.0, **tags: str) -> None:
        if self.aggregator is None:
            return
        try:
            self.aggregator.record(name, value, **tags)
        except TelemetryError:
            pass

    @circuit_breaker("auth.signing_key")
    def _fetch_signing_key(self) -> bytes:
        if not self._backend_healthy:
            raise SystemDegradedError("Signing key backend unavailable", ["auth"])
        return self._secret

    def set_backend_health(self, healthy: bool) -> None:
        self._backend_healthy = healthy

    def _sign(self, signing_input: bytes) -> bytes:
        return hmac.new(self._fetch_signing_key(), signing_input, hashlib.sha256).digest()

    # -- rate limiting ------------------------------------------------------
    def check_rate_limit(self, client_id: str) -> None:
        rl = self.config.rate_limit
        if client_id in rl.exempt_clients:
            return
        now = time.monotonic()
        tokens, last = self._buckets.get(client_id, (float(rl.burst_size), now))
        tokens = min(float(rl.burst_size), tokens + (now - last) * rl.requests_per_second)
        if tokens < 1.0:
            retry_after = (1.0 - tokens) / rl.requests_per_second + rl.penalty_seconds
            self._buckets[client_id] = (tokens, now)
            self._emit("auth.rate_limited", client=client_id)
            raise RateLimitExceededError(retry_after)
        self._buckets[client_id] = (tokens - 1.0, now)

    # -- JWT issue / decode -------------------------------------------------
    def issue_token(
        self,
        subject: str,
        roles: Tuple[str, ...] = ("viewer",),
        scopes: Tuple[str, ...] = (),
        ttl_s: Optional[int] = None,
    ) -> str:
        now = time.time()
        payload = JWTPayload(
            sub=subject,
            iss=self.issuer,
            aud=self.audience,
            exp=now + (ttl_s if ttl_s is not None else self.policy.token_ttl_seconds),
            iat=now,
            roles=roles,
            scopes=scopes,
        )
        header = {"alg": "HS256", "typ": "JWT"}
        head_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        body_b64 = _b64url_encode(json.dumps(payload.to_claims(), separators=(",", ":")).encode())
        signing_input = f"{head_b64}.{body_b64}".encode("ascii")
        return f"{head_b64}.{body_b64}.{_b64url_encode(self._sign(signing_input))}"

    def _split_token(self, token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]:
        if not isinstance(token, str) or token.count(".") != 2:
            raise AuthFailedError("Token is not a well-formed JWT")
        head_b64, body_b64, sig_b64 = token.split(".")
        try:
            header = json.loads(_b64url_decode(head_b64))
            claims = json.loads(_b64url_decode(body_b64))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthFailedError("Token segments are not valid JSON") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthFailedError("Token segments must be JSON objects")
        return header, claims, f"{head_b64}.{body_b64}".encode("ascii"), _b64url_decode(sig_b64)

    def verify_signature(self, header: Dict[str, Any], signing_input: bytes, signature: bytes) -> None:
        alg = header.get("alg")
        if alg not in SUPPORTED_ALGORITHMS:
            raise AuthFailedError(f"Unsupported or insecure algorithm: {alg!r}")
        expected = self._sign(signing_input)
        if not hmac.compare_digest(expected, signature):
            raise AuthFailedError("Token signature mismatch")

    def validate_claims(self, payload: JWTPayload, now: Optional[float] = None) -> None:
        current = now if now is not None else time.time()
        if payload.iss != self.issuer:
            raise AuthFailedError(f"Unexpected issuer '{payload.iss}'")
        if payload.aud != self.audience:
            raise AuthFailedError(f"Unexpected audience '{payload.aud}'")
        if payload.is_expired(current - self.clock_skew_s):
            raise AuthFailedError("Token has expired")
        if payload.iat > current + self.clock_skew_s:
            raise AuthFailedError("Token issued in the future")
        if payload.exp - payload.iat > self.policy.token_ttl_seconds * 4:
            raise PolicyViolationError("token_lifetime", "token lifetime exceeds policy maximum")
        if payload.jti in self._revoked_jti:
            raise AuthFailedError("Token has been revoked")

    # -- public validation entry points ------------------------------------
    @track_latency("auth.validate_token")
    @audit_log("validate_token", LogLevel.INFO)
    def validate_token(self, token: str, client_ip: str = "127.0.0.1") -> UserContext:
        self.check_rate_limit(client_ip)
        header, claims, signing_input, signature = self._split_token(token)
        self.verify_signature(header, signing_input, signature)
        payload = JWTPayload.from_claims(claims)
        self.validate_claims(payload)
        if self.policy.allowed_origins and client_ip not in (
            *self.policy.allowed_origins,
            "127.0.0.1",
        ):
            raise PolicyViolationError("allowed_origins", f"origin '{client_ip}' not permitted")
        self._emit("auth.token_valid", subject=payload.sub)
        return UserContext.from_payload(payload, client_ip=client_ip)

    def register_api_key(self, key_id: str, secret: str, owner: str,
                         scopes: Tuple[str, ...] = (), ttl_s: Optional[float] = None) -> APICredential:
        if not key_id or not secret:
            raise ConfigValidationError("api_key", "key_id and secret are required")
        credential = APICredential(
            key_id=key_id,
            secret_hash=APICredential.hash_secret(secret),
            owner=owner,
            scopes=scopes,
            expires_at=time.time() + ttl_s if ttl_s is not None else None,
        )
        self._api_keys[key_id] = credential
        return credential

    @track_latency("auth.validate_api_key")
    @audit_log("validate_api_key", LogLevel.INFO)
    def validate_api_key(self, key_id: str, secret: str, client_ip: str = "127.0.0.1") -> UserContext:
        self.check_rate_limit(client_ip)
        credential = self._api_keys.get(key_id)
        if credential is None:
            raise AuthFailedError("Unknown API key")
        if not credential.is_active():
            raise AuthFailedError("API key is revoked or expired")
        if not hmac.compare_digest(credential.secret_hash, APICredential.hash_secret(secret)):
            raise AuthFailedError("API key secret mismatch")
        self._emit("auth.api_key_valid", owner=credential.owner)
        return UserContext(user_id=credential.owner, scopes=credential.scopes,
                           client_ip=client_ip, metadata={"key_id": key_id})

    @audit_log("authorize", LogLevel.DEBUG)
    def authorize(self, context: UserContext, permission: str) -> bool:
        if not self.rbac.is_allowed(context.roles, permission):
            self._emit("auth.denied", permission=permission)
            raise PolicyViolationError("rbac", permission)
        return True

    def require_scope(self, context: UserContext, scope: str) -> None:
        if not context.has_scope(scope):
            raise PolicyViolationError("scope_check", f"missing scope '{scope}'")

    def revoke_token(self, token: str) -> str:
        _, claims, _, _ = self._split_token(token)
        jti = JWTPayload.from_claims(claims).jti
        self._revoked_jti.add(jti)
        return jti

    def revoke_api_key(self, key_id: str) -> None:
        credential = self._api_keys.get(key_id)
        if credential is None:
            raise AuthFailedError("Unknown API key")
        credential.revoked = True

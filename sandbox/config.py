"""sandbox/config.py

Configuration layer for the autonomous AI healing engine.

Contains:
  * a hierarchy of custom exceptions
  * enums describing modes / levels
  * dataclass configuration schemas
  * decorators used for validation and factory bookkeeping
  * type-hinted factory functions that build and return the schemas
"""
from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class HealingEngineError(Exception):
    """Base class for every error raised by the healing engine."""

    def __init__(self, message: str, *, code: int = 1000) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class AuthFailedError(HealingEngineError):
    """Raised when authentication or token validation fails."""

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message, code=1001)


class SystemDegradedError(HealingEngineError):
    """Raised when the system is running in a degraded state."""

    def __init__(self, message: str = "System degraded", degraded_modules: Optional[List[str]] = None) -> None:
        super().__init__(message, code=1002)
        self.degraded_modules: List[str] = degraded_modules or []


class ConfigValidationError(HealingEngineError):
    """Raised when a configuration object fails validation."""

    def __init__(self, field_name: str, reason: str) -> None:
        super().__init__(f"Invalid '{field_name}': {reason}", code=1003)
        self.field_name = field_name
        self.reason = reason


class RateLimitExceededError(HealingEngineError):
    """Raised when a caller exceeds the configured rate limit."""

    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__(f"Rate limit exceeded; retry after {retry_after:.2f}s", code=1004)
        self.retry_after = retry_after


class PolicyViolationError(HealingEngineError):
    """Raised when an action violates the active security policy."""

    def __init__(self, policy: str, action: str) -> None:
        super().__init__(f"Action '{action}' violates policy '{policy}'", code=1005)
        self.policy = policy
        self.action = action


class ModuleInitializationError(HealingEngineError):
    """Raised when an engine module cannot be initialised."""

    def __init__(self, module_name: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(f"Module '{module_name}' failed to initialise", code=1006)
        self.module_name = module_name
        self.__cause__ = cause


class HealingTimeoutError(HealingEngineError):
    """Raised when a healing attempt exceeds its time budget."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"Healing attempt timed out after {timeout_s:.1f}s", code=1007)
        self.timeout_s = timeout_s


class RollbackFailedError(HealingEngineError):
    """Raised when a failed patch cannot be rolled back."""

    def __init__(self, patch_id: str) -> None:
        super().__init__(f"Rollback failed for patch '{patch_id}'", code=1008)
        self.patch_id = patch_id


class ResourceExhaustedError(HealingEngineError):
    """Raised when memory, CPU or worker resources are exhausted."""

    def __init__(self, resource: str, limit: float) -> None:
        super().__init__(f"Resource '{resource}' exhausted (limit={limit})", code=1009)
        self.resource = resource
        self.limit = limit


class CircuitOpenError(HealingEngineError):
    """Raised when a circuit breaker is open and calls are rejected."""

    def __init__(self, circuit: str) -> None:
        super().__init__(f"Circuit '{circuit}' is open", code=1010)
        self.circuit = circuit


class TelemetryError(HealingEngineError):
    """Raised when telemetry collection or export fails."""

    def __init__(self, message: str = "Telemetry failure") -> None:
        super().__init__(message, code=1011)


class SandboxEscapeError(PolicyViolationError):
    """Raised when generated code attempts to leave the sandbox."""

    def __init__(self, action: str) -> None:
        super().__init__("sandbox_isolation", action)
        self.code = 1012


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class HealingStrategy(str, Enum):
    RESTART = "restart"
    PATCH = "patch"
    ROLLBACK = "rollback"
    ISOLATE = "isolate"
    ESCALATE = "escalate"


class AuthScheme(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    JWT = "jwt"
    MTLS = "mtls"


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def validated(func: F) -> F:
    """Run ``validate()`` on the object returned by a factory function."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = func(*args, **kwargs)
        validator = getattr(result, "validate", None)
        if callable(validator):
            validator()
        return result

    return wrapper  # type: ignore[return-value]


def timed_factory(func: F) -> F:
    """Record how long a factory takes to build a config object."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - start
            FACTORY_TIMINGS[func.__name__] = elapsed

    return wrapper  # type: ignore[return-value]


def require_positive(*field_names: str) -> Callable[[F], F]:
    """Decorator factory: ensure the named attributes on ``self`` are > 0."""

    def decorator(method: F) -> F:
        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            for name in field_names:
                value = getattr(self, name)
                if value <= 0:
                    raise ConfigValidationError(name, f"must be positive, got {value}")
            return method(self, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


FACTORY_TIMINGS: Dict[str, float] = {}


# ---------------------------------------------------------------------------
# Dataclass configuration schemas
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RateLimitConfig:
    """Token-bucket rate limiting parameters."""

    requests_per_second: float = 50.0
    burst_size: int = 100
    window_seconds: float = 60.0
    penalty_seconds: float = 5.0
    exempt_clients: Tuple[str, ...] = ()

    @require_positive("requests_per_second", "burst_size", "window_seconds")
    def validate(self) -> None:
        if self.penalty_seconds < 0:
            raise ConfigValidationError("penalty_seconds", "cannot be negative")

    def tokens_per_window(self) -> float:
        return self.requests_per_second * self.window_seconds


@dataclass
class SecurityPolicy:
    """Security posture for the engine and the sandbox."""

    auth_scheme: AuthScheme = AuthScheme.JWT
    token_ttl_seconds: int = 900
    allowed_origins: List[str] = field(default_factory=lambda: ["localhost"])
    forbidden_imports: List[str] = field(
        default_factory=lambda: ["os.system", "subprocess", "ctypes", "socket"]
    )
    max_patch_size_bytes: int = 64 * 1024
    require_signed_patches: bool = True
    sandbox_network_access: bool = False

    @require_positive("token_ttl_seconds", "max_patch_size_bytes")
    def validate(self) -> None:
        if self.auth_scheme is AuthScheme.NONE and self.require_signed_patches:
            raise ConfigValidationError(
                "auth_scheme", "signed patches require an authentication scheme"
            )

    def is_import_allowed(self, name: str) -> bool:
        return not any(name.startswith(bad) for bad in self.forbidden_imports)


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker thresholds guarding unstable modules."""

    failure_threshold: int = 5
    recovery_timeout_s: float = 30.0
    half_open_max_calls: int = 3
    tracked_exceptions: Tuple[type, ...] = (SystemDegradedError, HealingTimeoutError)

    @require_positive("failure_threshold", "recovery_timeout_s", "half_open_max_calls")
    def validate(self) -> None:
        return None


@dataclass
class HealingConfig:
    """Behaviour of the autonomous healing loop."""

    strategies: List[HealingStrategy] = field(
        default_factory=lambda: [
            HealingStrategy.RESTART,
            HealingStrategy.PATCH,
            HealingStrategy.ROLLBACK,
        ]
    )
    max_attempts: int = 3
    attempt_timeout_s: float = 45.0
    backoff_base_s: float = 1.5
    backoff_cap_s: float = 60.0
    auto_rollback: bool = True
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    @require_positive("max_attempts", "attempt_timeout_s", "backoff_base_s")
    def validate(self) -> None:
        if not self.strategies:
            raise ConfigValidationError("strategies", "at least one strategy is required")
        if self.backoff_cap_s < self.backoff_base_s:
            raise ConfigValidationError("backoff_cap_s", "must be >= backoff_base_s")
        self.circuit_breaker.validate()

    def backoff_for(self, attempt: int) -> float:
        return min(self.backoff_cap_s, self.backoff_base_s ** max(attempt, 1))


@dataclass
class TelemetryConfig:
    """Metrics, tracing and log export settings."""

    enabled: bool = True
    log_level: LogLevel = LogLevel.INFO
    export_interval_s: float = 10.0
    endpoint: Optional[str] = None
    sample_rate: float = 1.0
    labels: Dict[str, str] = field(default_factory=dict)

    @require_positive("export_interval_s")
    def validate(self) -> None:
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ConfigValidationError("sample_rate", "must be between 0 and 1")
        if self.enabled and self.endpoint is not None and not self.endpoint.startswith(
            ("http://", "https://")
        ):
            raise ConfigValidationError("endpoint", "must be an http(s) URL")


@dataclass
class AppConfig:
    """Top-level application configuration aggregating all sub-schemas."""

    app_name: str = "ai-healing-engine"
    version: str = "0.1.0"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    worker_count: int = 4
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    healing: HealingConfig = field(default_factory=HealingConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    @require_positive("worker_count")
    def validate(self) -> None:
        if self.environment is Environment.PRODUCTION and self.debug:
            raise ConfigValidationError("debug", "must be disabled in production")
        self.security.validate()
        self.rate_limit.validate()
        self.healing.validate()
        self.telemetry.validate()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Type-hinted factory functions
# ---------------------------------------------------------------------------
@validated
@timed_factory
def create_rate_limit_config(
    requests_per_second: float = 50.0,
    burst_size: int = 100,
    exempt_clients: Optional[List[str]] = None,
) -> RateLimitConfig:
    return RateLimitConfig(
        requests_per_second=requests_per_second,
        burst_size=burst_size,
        exempt_clients=tuple(exempt_clients or ()),
    )


@validated
@timed_factory
def create_security_policy(
    auth_scheme: AuthScheme = AuthScheme.JWT,
    strict: bool = True,
) -> SecurityPolicy:
    return SecurityPolicy(
        auth_scheme=auth_scheme,
        token_ttl_seconds=300 if strict else 3600,
        require_signed_patches=strict,
        sandbox_network_access=not strict,
    )


@validated
@timed_factory
def create_circuit_breaker_config(
    failure_threshold: int = 5,
    recovery_timeout_s: float = 30.0,
) -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        failure_threshold=failure_threshold,
        recovery_timeout_s=recovery_timeout_s,
    )


@validated
@timed_factory
def create_healing_config(
    max_attempts: int = 3,
    strategies: Optional[List[HealingStrategy]] = None,
    breaker: Optional[CircuitBreakerConfig] = None,
) -> HealingConfig:
    return HealingConfig(
        max_attempts=max_attempts,
        strategies=strategies or [HealingStrategy.RESTART, HealingStrategy.PATCH],
        circuit_breaker=breaker or create_circuit_breaker_config(),
    )


@validated
@timed_factory
def create_telemetry_config(
    log_level: LogLevel = LogLevel.INFO,
    endpoint: Optional[str] = None,
) -> TelemetryConfig:
    return TelemetryConfig(
        enabled=endpoint is not None or log_level is not LogLevel.CRITICAL,
        log_level=log_level,
        endpoint=endpoint,
        labels={"service": "ai-healing-engine"},
    )


@validated
@timed_factory
def create_app_config(environment: Environment = Environment.DEVELOPMENT) -> AppConfig:
    is_prod = environment is Environment.PRODUCTION
    return AppConfig(
        environment=environment,
        debug=not is_prod,
        worker_count=max(1, (os.cpu_count() or 2) // 2),
        security=create_security_policy(strict=is_prod),
        rate_limit=create_rate_limit_config(),
        healing=create_healing_config(),
        telemetry=create_telemetry_config(
            log_level=LogLevel.WARNING if is_prod else LogLevel.DEBUG
        ),
    )


def load_config_from_env() -> AppConfig:
    """Build an :class:`AppConfig` using the ``HEALER_ENV`` environment variable."""
    raw = os.environ.get("HEALER_ENV", Environment.DEVELOPMENT.value)
    try:
        environment = Environment(raw.lower())
    except ValueError as exc:
        raise ConfigValidationError("HEALER_ENV", f"unknown environment '{raw}'") from exc
    return create_app_config(environment)

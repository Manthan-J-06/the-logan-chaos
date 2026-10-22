"""sandbox/core.py

Central orchestration layer ("God Node") of the autonomous AI healing engine.

Depends on every other module in the sandbox:
  * sandbox.config     - schemas, enums, exceptions, factories
  * sandbox.telemetry  - decorators, sinks, aggregator, shared registries
  * sandbox.auth       - credential models and the token validator
  * sandbox.router     - payload models and the request dispatcher

Contains ``GatewayEngine`` and ``execute_system_pipeline()``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from sandbox.config import (
    AppConfig,
    AuthFailedError,
    AuthScheme,
    CircuitBreakerConfig,
    CircuitOpenError,
    ConfigValidationError,
    Environment,
    HealingConfig,
    HealingEngineError,
    HealingStrategy,
    HealingTimeoutError,
    LogLevel,
    ModuleInitializationError,
    PolicyViolationError,
    RateLimitConfig,
    RateLimitExceededError,
    ResourceExhaustedError,
    RollbackFailedError,
    SandboxEscapeError,
    SecurityPolicy,
    SystemDegradedError,
    TelemetryConfig,
    TelemetryError,
    create_app_config,
    create_circuit_breaker_config,
    create_rate_limit_config,
    create_telemetry_config,
)
from sandbox.telemetry import (
    AUDIT_TRAIL,
    LATENCY_REGISTRY,
    AWSTimestream,
    BreakerState,
    DatadogSink,
    ELKSink,
    MetricPoint,
    MetricSink,
    MetricsAggregator,
    PrometheusSink,
    SplunkForwarder,
    _BREAKER_STATE,
    audit_log,
    circuit_breaker,
    track_latency,
)
from sandbox.auth import (
    APICredential,
    JWTPayload,
    RBACPolicy,
    TokenValidator,
    UserContext,
)
from sandbox.router import (
    InboundRequest,
    OutboundResponse,
    RequestDispatcher,
    RouteContext,
    RouteDefinition,
    RouteNotFoundError,
)

SIGNING_KEY_BREAKER = "auth.signing_key"


# ---------------------------------------------------------------------------
# Pipeline models
# ---------------------------------------------------------------------------
class PipelineStage(str, Enum):
    BOOTSTRAP = "bootstrap"
    PREFLIGHT_AUTH = "preflight_auth"
    DISPATCH = "dispatch"
    HEALING = "healing"
    TEARDOWN = "teardown"
    COMPLETE = "complete"


@dataclass
class HealingRecord:
    """One attempt by the engine to recover from a degraded state."""

    attempt: int
    strategy: HealingStrategy
    succeeded: bool
    delay_s: float
    reason: str = ""


@dataclass
class PipelineResult:
    """Outcome of a full gateway pipeline execution."""

    request_id: str
    stage: PipelineStage
    status: int
    body: Dict[str, Any] = field(default_factory=dict)
    user_id: Optional[str] = None
    error_type: Optional[str] = None
    error_code: Optional[int] = None
    healed: bool = False
    healing_log: List[HealingRecord] = field(default_factory=list)
    latency_ms: float = 0.0
    sink_report: Dict[str, Tuple[bool, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error_type is None


# Ordered from most specific to least specific; first isinstance match wins.
ERROR_CLASSIFICATION: List[Tuple[type, int]] = [
    (SandboxEscapeError, 403),
    (PolicyViolationError, 403),
    (AuthFailedError, 401),
    (RateLimitExceededError, 429),
    (RouteNotFoundError, 404),
    (ConfigValidationError, 400),
    (ResourceExhaustedError, 503),
    (CircuitOpenError, 503),
    (SystemDegradedError, 503),
    (HealingTimeoutError, 504),
    (RollbackFailedError, 500),
    (ModuleInitializationError, 500),
    (TelemetryError, 502),
    (HealingEngineError, 500),
]

DEGRADED_ERROR_NAMES = frozenset({"SystemDegradedError", "CircuitOpenError"})


def classify_error(exc: HealingEngineError) -> int:
    for exc_type, status in ERROR_CLASSIFICATION:
        if isinstance(exc, exc_type):
            return status
    return 500


# ---------------------------------------------------------------------------
# Gateway engine
# ---------------------------------------------------------------------------
class GatewayEngine:
    """Owns the sinks, aggregator, validator and dispatcher, and heals them."""

    def __init__(
        self,
        secret: str,
        app_config: Optional[AppConfig] = None,
        sinks: Optional[List[MetricSink]] = None,
        rbac: Optional[RBACPolicy] = None,
        backoff_scale: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config: AppConfig = app_config or create_app_config()
        self.config.validate()
        self.backoff_scale = backoff_scale
        self._sleep = sleeper

        telemetry_cfg: TelemetryConfig = self.config.telemetry
        self.aggregator = MetricsAggregator(config=telemetry_cfg, sinks=sinks)
        self.validator = TokenValidator(
            secret,
            app_config=self.config,
            rbac=rbac,
            aggregator=self.aggregator,
        )
        self.dispatcher = RequestDispatcher(
            self.validator,
            app_config=self.config,
            aggregator=self.aggregator,
        )
        self.healing_history: List[HealingRecord] = []
        self._started = False

        self._strategy_handlers: Dict[HealingStrategy, Callable[[], None]] = {
            HealingStrategy.RESTART: self._heal_restart,
            HealingStrategy.PATCH: self._heal_patch,
            HealingStrategy.ROLLBACK: self._heal_rollback,
            HealingStrategy.ISOLATE: self._heal_isolate,
            HealingStrategy.ESCALATE: self._heal_escalate,
        }

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self.aggregator.start()
        self._started = True

    def stop(self) -> Dict[str, Tuple[bool, int]]:
        report: Dict[str, Tuple[bool, int]] = {}
        if self._started:
            try:
                self.aggregator.flush()
            except TelemetryError:
                pass
            report = self.aggregator.sink_report()
            self.aggregator.stop()
            self._started = False
        return report

    def emit(self, name: str, value: float = 1.0, **tags: str) -> None:
        try:
            self.aggregator.record(name, value, **tags)
        except (TelemetryError, ResourceExhaustedError):
            pass

    def register_service_account(self, key_id: str, secret: str, owner: str,
                                 scopes: Tuple[str, ...] = (), ttl_s: Optional[float] = None) -> APICredential:
        credential = self.validator.register_api_key(key_id, secret, owner, scopes, ttl_s)
        RequestDispatcher.verify_service_credential(credential)
        return credential

    # -- healing strategies -------------------------------------------------
    def _heal_restart(self) -> None:
        self.validator.set_backend_health(True)
        _BREAKER_STATE[SIGNING_KEY_BREAKER] = BreakerState()

    def _heal_patch(self) -> None:
        if not self.validator._backend_healthy:
            raise SystemDegradedError("patch cannot repair an unhealthy backend", ["auth"])

    def _heal_rollback(self) -> None:
        for name in list(_BREAKER_STATE):
            _BREAKER_STATE[name] = BreakerState()
        self.validator.set_backend_health(True)

    def _heal_isolate(self) -> None:
        raise RollbackFailedError("isolate-" + uuid_fragment())

    def _heal_escalate(self) -> None:
        raise SystemDegradedError("escalation required", ["core"])

    @track_latency("core.heal")
    @audit_log("heal_system", LogLevel.WARNING)
    def heal(self, trigger: HealingEngineError) -> List[HealingRecord]:
        healing: HealingConfig = self.config.healing
        started = time.monotonic()
        records: List[HealingRecord] = []

        for attempt in range(1, healing.max_attempts + 1):
            if time.monotonic() - started > healing.attempt_timeout_s:
                raise HealingTimeoutError(healing.attempt_timeout_s)

            strategy = healing.strategies[(attempt - 1) % len(healing.strategies)]
            delay = healing.backoff_for(attempt) * self.backoff_scale
            if delay > 0:
                self._sleep(delay)

            handler = self._strategy_handlers[strategy]
            try:
                handler()
            except HealingEngineError as exc:
                records.append(HealingRecord(attempt, strategy, False, delay, type(exc).__name__))
                self.emit("core.heal_failed", strategy=strategy.value)
                if strategy is HealingStrategy.ROLLBACK and healing.auto_rollback:
                    raise RollbackFailedError(f"auto-rollback-{attempt}") from exc
                continue

            records.append(HealingRecord(attempt, strategy, True, delay, type(trigger).__name__))
            self.emit("core.heal_succeeded", strategy=strategy.value)
            break
        else:
            self.healing_history.extend(records)
            raise SystemDegradedError(
                "all healing attempts exhausted", degraded_modules=["auth", "router"]
            ) from trigger

        self.healing_history.extend(records)
        return records

    # -- pre-flight authentication -----------------------------------------
    @track_latency("core.preflight_auth")
    def preflight_auth(self, request: InboundRequest) -> Optional[UserContext]:
        """Validate credentials without consuming rate-limit budget."""
        authorization = request.header("authorization")
        if not authorization:
            return None

        scheme, _, credential = authorization.partition(" ")
        scheme = scheme.lower()
        policy: SecurityPolicy = self.config.security

        if scheme == "bearer":
            if policy.auth_scheme is not AuthScheme.JWT:
                raise PolicyViolationError("auth_scheme", "bearer tokens are disabled")
            header, claims, signing_input, signature = self.validator._split_token(credential)
            self.validator.verify_signature(header, signing_input, signature)
            payload = JWTPayload.from_claims(claims)
            self.validator.validate_claims(payload)
            context = UserContext.from_payload(payload, client_ip=request.client_ip)
            unknown = [r for r in context.roles if r not in self.validator.rbac.role_permissions]
            if unknown:
                raise PolicyViolationError("rbac", f"unknown roles: {', '.join(unknown)}")
            return context

        if scheme == "apikey":
            key_id, sep, secret = credential.partition(":")
            if not sep:
                raise AuthFailedError("ApiKey credentials must be 'key_id:secret'")
            stored = self.validator._api_keys.get(key_id)
            if stored is None:
                raise AuthFailedError("Unknown API key")
            RequestDispatcher.verify_service_credential(stored)
            if stored.secret_hash != APICredential.hash_secret(secret):
                raise AuthFailedError("API key secret mismatch")
            return UserContext(user_id=stored.owner, scopes=stored.scopes,
                               client_ip=request.client_ip, metadata={"key_id": key_id})

        raise AuthFailedError(f"Unsupported authorization scheme '{scheme}'")

    # -- engine entry point -------------------------------------------------
    @track_latency("core.execute")
    @audit_log("gateway_execute", LogLevel.INFO)
    def execute(self, request: InboundRequest) -> PipelineResult:
        started = time.perf_counter()
        result = PipelineResult(request_id=request.request_id, stage=PipelineStage.PREFLIGHT_AUTH, status=500)
        healed_once = False

        for _ in range(2):
            try:
                result.stage = PipelineStage.PREFLIGHT_AUTH
                user = self.preflight_auth(request)
                result.user_id = user.user_id if user else None

                result.stage = PipelineStage.DISPATCH
                response: OutboundResponse = self.dispatcher.dispatch(request)
                result.status = response.status
                result.body = response.body

                degraded = response.body.get("error") in DEGRADED_ERROR_NAMES
                if degraded and not healed_once:
                    raise SystemDegradedError(response.body.get("message", "degraded"), ["router"])
                if not response.is_success():
                    result.error_type = response.body.get("error")
                    result.error_code = response.body.get("code")
                break

            except (SystemDegradedError, CircuitOpenError) as exc:
                result.stage = PipelineStage.HEALING
                if healed_once:
                    self._apply_error(result, exc)
                    break
                try:
                    result.healing_log = self.heal(exc)
                    result.healed = True
                    healed_once = True
                except (HealingTimeoutError, RollbackFailedError, SystemDegradedError) as heal_exc:
                    self._apply_error(result, heal_exc)
                    break
            except RateLimitExceededError as exc:
                self._apply_error(result, exc)
                result.body["retry_after"] = exc.retry_after
                break
            except HealingEngineError as exc:
                self._apply_error(result, exc)
                break

        if result.error_type is None and result.ok:
            result.stage = PipelineStage.COMPLETE
        result.latency_ms = (time.perf_counter() - started) * 1000.0
        self.emit("core.pipeline", result.latency_ms, status=str(result.status))
        return result

    @staticmethod
    def _apply_error(result: PipelineResult, exc: HealingEngineError) -> None:
        result.status = classify_error(exc)
        result.error_type = type(exc).__name__
        result.error_code = exc.code
        result.body = {"error": result.error_type, "code": exc.code, "message": exc.message}

    # -- introspection ------------------------------------------------------
    def health_report(self) -> Dict[str, Any]:
        latencies = {
            name: round(sum(values) / len(values), 3)
            for name, values in LATENCY_REGISTRY.items()
            if values
        }
        breakers = {
            name: {"failures": state.failures, "open": state.opened_at is not None}
            for name, state in _BREAKER_STATE.items()
        }
        return {
            "environment": self.config.environment.value,
            "sinks": self.aggregator.sink_report(),
            "breakers": breakers,
            "avg_latency_ms": latencies,
            "audit_entries": len(AUDIT_TRAIL),
            "healing_attempts": len(self.healing_history),
            "status_counts": dict(self.dispatcher.status_counts),
        }


def uuid_fragment() -> str:
    return format(int(time.time() * 1000) & 0xFFFFFF, "06x")


# ---------------------------------------------------------------------------
# Central pipeline function
# ---------------------------------------------------------------------------
def execute_system_pipeline(
    request: InboundRequest,
    secret: str,
    environment: Environment = Environment.DEVELOPMENT,
    service_accounts: Optional[List[Tuple[str, str, str, Tuple[str, ...]]]] = None,
    requests_per_second: float = 50.0,
    burst_size: int = 100,
    simulate_backend_failure: bool = False,
) -> PipelineResult:
    """Build the whole stack, authenticate, dispatch and heal in one call."""
    stage = PipelineStage.BOOTSTRAP
    engine: Optional[GatewayEngine] = None
    result = PipelineResult(request_id=request.request_id, stage=stage, status=500)
    started = time.perf_counter()

    try:
        config = create_app_config(environment)
        config.rate_limit = create_rate_limit_config(requests_per_second, burst_size)
        config.telemetry = create_telemetry_config(
            log_level=LogLevel.WARNING if environment is Environment.PRODUCTION else LogLevel.DEBUG
        )
        config.healing.circuit_breaker = create_circuit_breaker_config(failure_threshold=5)
        config.validate()

        sinks: List[MetricSink] = [
            DatadogSink(api_key="dd-core-key"),
            PrometheusSink(port=9100),
            AWSTimestream(database="healer", table="core"),
            SplunkForwarder(index="core"),
            ELKSink(index_prefix="core-metrics"),
        ]

        engine = GatewayEngine(secret, app_config=config, sinks=sinks)
        engine.start()
        for key_id, key_secret, owner, scopes in service_accounts or []:
            engine.register_service_account(key_id, key_secret, owner, scopes)

        if simulate_backend_failure:
            engine.validator.set_backend_health(False)

        result = engine.execute(request)

    except ModuleInitializationError as exc:
        result.stage = PipelineStage.BOOTSTRAP
        GatewayEngine._apply_error(result, exc)
    except ConfigValidationError as exc:
        result.stage = PipelineStage.BOOTSTRAP
        GatewayEngine._apply_error(result, exc)
    except (AuthFailedError, PolicyViolationError, RateLimitExceededError) as exc:
        result.stage = PipelineStage.PREFLIGHT_AUTH
        GatewayEngine._apply_error(result, exc)
    except (SystemDegradedError, CircuitOpenError, HealingTimeoutError, RollbackFailedError) as exc:
        result.stage = PipelineStage.HEALING
        GatewayEngine._apply_error(result, exc)
    except (ResourceExhaustedError, TelemetryError, RouteNotFoundError) as exc:
        result.stage = PipelineStage.DISPATCH
        GatewayEngine._apply_error(result, exc)
    except HealingEngineError as exc:
        GatewayEngine._apply_error(result, exc)
    finally:
        if engine is not None:
            result.stage = result.stage if result.error_type else PipelineStage.TEARDOWN
            result.sink_report = engine.stop()
            if result.error_type is None:
                result.stage = PipelineStage.COMPLETE
        result.latency_ms = (time.perf_counter() - started) * 1000.0

    return result

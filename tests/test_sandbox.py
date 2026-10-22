"""tests/test_sandbox.py

Contract tests for the autonomous AI healing engine sandbox:

    sandbox/config.py     schemas, enums, exceptions, factories
    sandbox/telemetry.py  decorators, mock sinks, MetricsAggregator
    sandbox/auth.py       credential models, TokenValidator
    sandbox/router.py     payload models, RequestDispatcher
    sandbox/core.py       GatewayEngine, execute_system_pipeline

Every assertion below encodes the *current* behaviour of the modules. If a test
fails, the code under test has drifted from its contract.
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox import config as config_module  # noqa: E402
from sandbox.auth import (  # noqa: E402
    APICredential,
    JWTPayload,
    RBACPolicy,
    TokenValidator,
    UserContext,
    _b64url_encode,
)
from sandbox.config import (  # noqa: E402
    FACTORY_TIMINGS,
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
    create_healing_config,
    create_rate_limit_config,
    create_security_policy,
    create_telemetry_config,
    load_config_from_env,
)
from sandbox.core import (  # noqa: E402
    GatewayEngine,
    HealingRecord,
    PipelineResult,
    PipelineStage,
    classify_error,
    execute_system_pipeline,
)
from sandbox.router import (  # noqa: E402
    InboundRequest,
    OutboundResponse,
    RequestDispatcher,
    RouteContext,
    RouteDefinition,
    RouteNotFoundError,
    build_dispatcher,
)
from sandbox.telemetry import (  # noqa: E402
    AUDIT_TRAIL,
    LATENCY_REGISTRY,
    AWSTimestream,
    BreakerState,
    DatadogSink,
    ELKSink,
    MetricPoint,
    MetricsAggregator,
    PrometheusSink,
    SplunkForwarder,
    _BREAKER_STATE,
    audit_log,
    build_aggregator,
    circuit_breaker,
    track_latency,
)

SECRET = "a-very-long-secret-key-123"
SIGNING_KEY_BREAKER = "auth.signing_key"
SINK_NAMES = ["datadog", "prometheus", "aws_timestream", "splunk", "elk"]
SINK_CLASSES = [
    (DatadogSink, "datadog"),
    (PrometheusSink, "prometheus"),
    (AWSTimestream, "aws_timestream"),
    (SplunkForwarder, "splunk"),
    (ELKSink, "elk"),
]


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_shared_state():
    """Telemetry registries and breaker state are module-level globals."""
    for store in (_BREAKER_STATE, LATENCY_REGISTRY, AUDIT_TRAIL, FACTORY_TIMINGS):
        store.clear()
    yield
    for store in (_BREAKER_STATE, LATENCY_REGISTRY, AUDIT_TRAIL, FACTORY_TIMINGS):
        store.clear()


@pytest.fixture
def app_config() -> AppConfig:
    return create_app_config()


@pytest.fixture
def validator(app_config: AppConfig) -> TokenValidator:
    return TokenValidator(SECRET, app_config=app_config)


@pytest.fixture
def dispatcher() -> RequestDispatcher:
    return build_dispatcher(SECRET, aggregator=build_aggregator())


def issue(role: str, scopes=(), sub=None, ttl_s=None) -> str:
    return TokenValidator(SECRET).issue_token(
        sub or role, roles=(role,), scopes=tuple(scopes), ttl_s=ttl_s
    )


def make_request(method="GET", path="/health", token=None, body=None,
                 headers=None, client_ip="127.0.0.1") -> InboundRequest:
    hdrs = dict(headers or {})
    if token is not None:
        hdrs.setdefault("Authorization", token if token.startswith("ApiKey ") else f"Bearer {token}")
    raw = b""
    if body is not None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    return InboundRequest(method=method, path=path, headers=hdrs, body=raw, client_ip=client_ip)


def five_sinks():
    return [DatadogSink(), PrometheusSink(), AWSTimestream(), SplunkForwarder(), ELKSink()]


def make_engine(cfg=None, sinks=None, **kwargs) -> GatewayEngine:
    engine = GatewayEngine(SECRET, app_config=cfg or create_app_config(), sinks=sinks, **kwargs)
    engine.start()
    return engine


# ===========================================================================
# config.py
# ===========================================================================
EXCEPTION_CASES = [
    (HealingEngineError("x"), 1000),
    (AuthFailedError(), 1001),
    (SystemDegradedError(), 1002),
    (ConfigValidationError("f", "r"), 1003),
    (RateLimitExceededError(2.0), 1004),
    (PolicyViolationError("p", "a"), 1005),
    (ModuleInitializationError("m"), 1006),
    (HealingTimeoutError(1.0), 1007),
    (RollbackFailedError("patch-1"), 1008),
    (ResourceExhaustedError("cpu", 1.0), 1009),
    (CircuitOpenError("c"), 1010),
    (TelemetryError(), 1011),
    (SandboxEscapeError("exec"), 1012),
    (RouteNotFoundError("/x", "GET"), 1013),
]


@pytest.mark.parametrize("exc,code", EXCEPTION_CASES, ids=[type(e).__name__ for e, _ in EXCEPTION_CASES])
def test_exception_codes_and_string_format(exc, code):
    assert isinstance(exc, HealingEngineError)
    assert exc.code == code
    assert str(exc) == f"[{code}] {exc.message}"


def test_exception_specific_attributes_and_messages():
    degraded = SystemDegradedError("down", ["auth", "router"])
    assert degraded.degraded_modules == ["auth", "router"]
    assert SystemDegradedError().degraded_modules == []

    invalid = ConfigValidationError("port", "too low")
    assert (invalid.field_name, invalid.reason) == ("port", "too low")
    assert invalid.message == "Invalid 'port': too low"

    assert RateLimitExceededError(2.5).message == "Rate limit exceeded; retry after 2.50s"
    assert HealingTimeoutError(3.0).message == "Healing attempt timed out after 3.0s"
    assert PolicyViolationError("rbac", "x").message == "Action 'x' violates policy 'rbac'"
    assert ResourceExhaustedError("cpu", 4).limit == 4

    cause = ValueError("boom")
    init_error = ModuleInitializationError("m", cause)
    assert init_error.module_name == "m"
    assert init_error.__cause__ is cause

    escape = SandboxEscapeError("open('/etc/passwd')")
    assert isinstance(escape, PolicyViolationError)
    assert escape.policy == "sandbox_isolation"


def test_rate_limit_config_defaults_frozen_and_validation():
    cfg = RateLimitConfig()
    assert cfg.tokens_per_window() == pytest.approx(50.0 * 60.0)
    with pytest.raises(FrozenInstanceError):
        cfg.burst_size = 1  # type: ignore[misc]

    with pytest.raises(ConfigValidationError) as info:
        RateLimitConfig(requests_per_second=0).validate()
    assert info.value.field_name == "requests_per_second"
    assert "positive" in info.value.reason

    with pytest.raises(ConfigValidationError) as info:
        RateLimitConfig(penalty_seconds=-1.0).validate()
    assert info.value.field_name == "penalty_seconds"


def test_security_policy_import_filter_and_validation():
    policy = SecurityPolicy()
    assert policy.is_import_allowed("json") is True
    assert policy.is_import_allowed("os") is True
    assert policy.is_import_allowed("os.system") is False
    assert policy.is_import_allowed("subprocess.run") is False
    assert policy.is_import_allowed("socket") is False

    with pytest.raises(ConfigValidationError) as info:
        SecurityPolicy(auth_scheme=AuthScheme.NONE).validate()
    assert info.value.field_name == "auth_scheme"

    SecurityPolicy(auth_scheme=AuthScheme.NONE, require_signed_patches=False).validate()


def test_healing_config_backoff_and_validation():
    cfg = HealingConfig()
    assert cfg.backoff_for(1) == pytest.approx(1.5)
    assert cfg.backoff_for(2) == pytest.approx(2.25)
    assert cfg.backoff_for(3) == pytest.approx(3.375)
    assert cfg.backoff_for(0) == pytest.approx(1.5)
    assert cfg.backoff_for(100) == pytest.approx(60.0)

    with pytest.raises(ConfigValidationError) as info:
        HealingConfig(strategies=[]).validate()
    assert info.value.field_name == "strategies"

    with pytest.raises(ConfigValidationError) as info:
        HealingConfig(backoff_base_s=10.0, backoff_cap_s=1.0).validate()
    assert info.value.field_name == "backoff_cap_s"


def test_telemetry_and_app_config_validation():
    with pytest.raises(ConfigValidationError) as info:
        TelemetryConfig(sample_rate=1.5).validate()
    assert info.value.field_name == "sample_rate"

    with pytest.raises(ConfigValidationError) as info:
        TelemetryConfig(endpoint="ftp://metrics.example").validate()
    assert info.value.field_name == "endpoint"

    TelemetryConfig(endpoint="https://metrics.example").validate()

    with pytest.raises(ConfigValidationError) as info:
        AppConfig(environment=Environment.PRODUCTION, debug=True).validate()
    assert info.value.field_name == "debug"

    with pytest.raises(ConfigValidationError) as info:
        AppConfig(worker_count=0).validate()
    assert info.value.field_name == "worker_count"


def test_rbac_policy_inheritance_and_cycle_detection():
    rbac = RBACPolicy()
    assert rbac.permissions_for("viewer") == {"metrics:read", "status:read"}
    assert "metrics:read" in rbac.permissions_for("admin")
    assert "healing:trigger" in rbac.permissions_for("admin")
    assert "config:write" not in rbac.permissions_for("operator")
    assert rbac.permissions_for("ghost") == set()
    assert rbac.is_allowed(("viewer", "admin"), "rollback:execute") is True
    assert rbac.is_allowed(("viewer",), "rollback:execute") is False
    assert rbac.is_allowed((), "metrics:read") is False

    cyclic = RBACPolicy(role_hierarchy={"a": ["b"], "b": ["a"]})
    with pytest.raises(ConfigValidationError) as info:
        cyclic.permissions_for("a")
    assert info.value.field_name == "role_hierarchy"


def test_factories_build_expected_configs_and_record_timings():
    app = create_app_config()
    assert app.environment is Environment.DEVELOPMENT
    assert app.debug is True
    assert app.worker_count >= 1
    assert app.security.require_signed_patches is False
    assert app.security.token_ttl_seconds == 3600
    assert app.telemetry.log_level is LogLevel.DEBUG
    assert app.healing.strategies == [HealingStrategy.RESTART, HealingStrategy.PATCH]

    prod = create_app_config(Environment.PRODUCTION)
    assert prod.debug is False
    assert prod.security.require_signed_patches is True
    assert prod.security.token_ttl_seconds == 300
    assert prod.security.sandbox_network_access is False
    assert prod.telemetry.log_level is LogLevel.WARNING

    assert {"create_app_config", "create_security_policy", "create_healing_config"} <= set(FACTORY_TIMINGS)
    assert all(value >= 0.0 for value in FACTORY_TIMINGS.values())


def test_validated_factories_reject_invalid_inputs():
    assert create_rate_limit_config(exempt_clients=["vip"]).exempt_clients == ("vip",)

    with pytest.raises(ConfigValidationError) as info:
        create_circuit_breaker_config(failure_threshold=0)
    assert info.value.field_name == "failure_threshold"

    with pytest.raises(ConfigValidationError) as info:
        create_healing_config(max_attempts=0)
    assert info.value.field_name == "max_attempts"

    with pytest.raises(ConfigValidationError) as info:
        create_security_policy(auth_scheme=AuthScheme.NONE, strict=True)
    assert info.value.field_name == "auth_scheme"

    relaxed = create_security_policy(auth_scheme=AuthScheme.NONE, strict=False)
    assert relaxed.require_signed_patches is False

    assert create_telemetry_config(log_level=LogLevel.CRITICAL).enabled is False
    assert create_telemetry_config(log_level=LogLevel.CRITICAL, endpoint="https://x").enabled is True
    with pytest.raises(ConfigValidationError):
        create_telemetry_config(endpoint="ftp://x")


def test_load_config_from_env(monkeypatch):
    monkeypatch.delenv("HEALER_ENV", raising=False)
    assert load_config_from_env().environment is Environment.DEVELOPMENT

    monkeypatch.setenv("HEALER_ENV", "PRODUCTION")
    assert load_config_from_env().environment is Environment.PRODUCTION

    monkeypatch.setenv("HEALER_ENV", "bogus")
    with pytest.raises(ConfigValidationError) as info:
        load_config_from_env()
    assert info.value.field_name == "HEALER_ENV"
    assert isinstance(info.value.__cause__, ValueError)


def test_app_config_to_dict_is_nested(app_config):
    data = app_config.to_dict()
    assert data["app_name"] == "ai-healing-engine"
    assert data["environment"] == "development"
    assert data["security"]["token_ttl_seconds"] == 3600
    assert data["rate_limit"]["burst_size"] == 100
    assert config_module.Environment is Environment


# ===========================================================================
# telemetry.py
# ===========================================================================
def test_track_latency_records_success_and_failure():
    @track_latency("unit.latency")
    def work(value):
        return value * 2

    @track_latency()
    def broken():
        raise ValueError("nope")

    assert work(4) == 8
    assert work.__name__ == "work"
    assert len(LATENCY_REGISTRY["unit.latency"]) == 1
    assert LATENCY_REGISTRY["unit.latency"][0] >= 0.0

    with pytest.raises(ValueError):
        broken()
    default_key = f"latency.{broken.__module__}.{broken.__qualname__}"
    assert len(LATENCY_REGISTRY[default_key]) == 1


def test_audit_log_records_ok_and_error_entries():
    @audit_log("do_work", LogLevel.WARNING)
    def good():
        return "done"

    @audit_log("fail_work")
    def bad():
        raise KeyError("missing")

    assert good() == "done"
    with pytest.raises(KeyError):
        bad()

    ok_entry, error_entry = list(AUDIT_TRAIL)
    assert ok_entry["action"] == "do_work"
    assert ok_entry["level"] == "WARNING"
    assert ok_entry["status"] == "ok"
    assert ok_entry["function"].endswith("good")
    assert ok_entry["finished_at"] >= ok_entry["started_at"]
    assert error_entry["status"] == "error"
    assert error_entry["error"] == "KeyError"
    assert error_entry["level"] == "INFO"


def test_circuit_breaker_opens_after_threshold_and_ignores_untracked_errors():
    cfg = CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0)

    @circuit_breaker("unit.breaker", cfg)
    def failing():
        raise SystemDegradedError("down")

    for _ in range(2):
        with pytest.raises(SystemDegradedError):
            failing()
    with pytest.raises(CircuitOpenError) as info:
        failing()
    assert info.value.circuit == "unit.breaker"
    assert _BREAKER_STATE["unit.breaker"].failures == 2
    assert _BREAKER_STATE["unit.breaker"].opened_at is not None

    @circuit_breaker("unit.untracked", cfg)
    def untracked():
        raise ValueError("not tracked")

    for _ in range(5):
        with pytest.raises(ValueError):
            untracked()
    assert _BREAKER_STATE["unit.untracked"].failures == 0


def test_circuit_breaker_half_open_recovery_resets_state():
    cfg = CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=30.0)
    healthy = {"value": False}

    @circuit_breaker("unit.recover", cfg)
    def call():
        if not healthy["value"]:
            raise HealingTimeoutError(1.0)
        return "recovered"

    with pytest.raises(HealingTimeoutError):
        call()
    with pytest.raises(CircuitOpenError):
        call()

    _BREAKER_STATE["unit.recover"].opened_at = time.monotonic() - 100.0
    healthy["value"] = True
    assert call() == "recovered"
    assert _BREAKER_STATE["unit.recover"] == BreakerState()


def test_breaker_state_is_open_window():
    cfg = CircuitBreakerConfig(recovery_timeout_s=30.0)
    assert BreakerState().is_open(cfg, now=100.0) is False
    assert BreakerState(opened_at=90.0).is_open(cfg, now=100.0) is True
    assert BreakerState(opened_at=90.0).is_open(cfg, now=120.0) is False


@pytest.mark.parametrize("cls,name", SINK_CLASSES, ids=[n for _, n in SINK_CLASSES])
def test_sink_lifecycle_requires_connection(cls, name):
    sink = cls()
    point = MetricPoint("m.cpu", 1.5, tags={"k": "v"})
    assert sink.sink_name == name
    assert sink.connected is False

    with pytest.raises(TelemetryError) as info:
        sink.emit(point)
    assert name in info.value.message

    sink.connect()
    assert sink.connected is True
    sink.emit(point)
    assert sink.sent == [point]
    assert sink.flush() == 1
    assert sink.sent == []
    sink.close()
    assert sink.connected is False


def test_sink_specific_methods():
    datadog = DatadogSink()
    assert datadog.submit_event("t", "body") == {"title": "t", "text": "body", "site": "datadoghq.com"}

    prom = PrometheusSink()
    prom.connect()
    prom.emit(MetricPoint("b.metric", 2.0))
    prom.emit(MetricPoint("a.metric", 1.0))
    assert prom.gauges == {"b.metric": 2.0, "a.metric": 1.0}
    assert prom.render_exposition() == "a.metric 1.0\nb.metric 2.0"

    points = [MetricPoint("x", 1.0), MetricPoint("y", 2.0)]
    aws = AWSTimestream()
    with pytest.raises(TelemetryError):
        aws.write_records(points)
    aws.connect()
    assert aws.write_records(points) == 2
    assert len(aws.sent) == 2

    splunk = SplunkForwarder()
    splunk.connect()
    assert splunk.forward_event({"a": 1}) is True
    assert splunk.forward_event({}) is False

    elk = ELKSink()
    assert elk.hosts == ["http://localhost:9200"]
    with pytest.raises(TelemetryError):
        elk.bulk_index(points)
    elk.connect()
    assert elk.bulk_index(points) == 2


def test_metrics_aggregator_flush_invokes_each_sink_per_point():
    sinks = five_sinks()
    aggregator = MetricsAggregator(config=create_app_config().telemetry, sinks=sinks)
    aggregator.start()

    with ExitStack() as stack:
        spies = [stack.enter_context(patch.object(s, "emit", wraps=s.emit)) for s in sinks]
        aggregator.record("m.a", 1.0, env="test")
        aggregator.record("m.b", 2.5)
        assert aggregator.flush() == 10

    for spy in spies:
        assert spy.call_count == 2
        assert [call.args[0].name for call in spy.call_args_list] == ["m.a", "m.b"]

    first = sinks[0].sent[0]
    assert first.tags == {"service": "ai-healing-engine", "env": "test"}
    assert aggregator.totals() == {"m.a": 1.0, "m.b": 2.5}
    assert aggregator.sink_report() == {name: (True, 2) for name in SINK_NAMES}
    assert "aggregator.flush" in LATENCY_REGISTRY
    assert any(entry["action"] == "record_metric" and entry["level"] == "DEBUG" for entry in AUDIT_TRAIL)


def test_metrics_aggregator_isolates_failing_sink():
    sinks = five_sinks()
    aggregator = MetricsAggregator(config=create_app_config().telemetry, sinks=sinks)
    aggregator.start()
    aggregator.record("m.a", 1.0)

    with patch.object(sinks[1], "emit", side_effect=TelemetryError("prometheus down")):
        assert aggregator.flush() == 4

    assert len(sinks[1].sent) == 0
    assert all(len(s.sent) == 1 for i, s in enumerate(sinks) if i != 1)


def test_metrics_aggregator_guards():
    aggregator = MetricsAggregator(config=create_app_config().telemetry, sinks=five_sinks(), buffer_limit=1)
    with pytest.raises(TelemetryError) as info:
        aggregator.flush()
    assert info.value.message == "aggregator has not been started"

    aggregator.record("m.a", 1.0)
    with pytest.raises(ResourceExhaustedError) as exhausted:
        aggregator.record("m.b", 1.0)
    assert exhausted.value.resource == "metrics_buffer"
    assert exhausted.value.limit == 1

    disabled_cfg = create_telemetry_config()
    disabled_cfg.enabled = False
    disabled = MetricsAggregator(config=disabled_cfg, sinks=five_sinks())
    disabled.record("m.a", 1.0)
    assert disabled.totals() == {}

    with pytest.raises(ConfigValidationError):
        MetricsAggregator(config=TelemetryConfig(sample_rate=2.0), sinks=[])


def test_metrics_aggregator_stop_flushes_and_disconnects():
    sinks = five_sinks()
    aggregator = MetricsAggregator(config=create_app_config().telemetry, sinks=sinks)
    aggregator.start()
    aggregator.record("m.a", 1.0)
    aggregator.stop()
    assert all(s.connected is False for s in sinks)
    assert all(len(s.sent) == 1 for s in sinks)


def test_build_aggregator_starts_default_sinks():
    aggregator = build_aggregator()
    assert [type(s) for s in aggregator.sinks] == [c for c, _ in SINK_CLASSES]
    assert all(s.connected for s in aggregator.sinks)
    with pytest.raises(ConfigValidationError) as info:
        build_aggregator("not-a-config")  # type: ignore[arg-type]
    assert info.value.field_name == "app_config"


# ===========================================================================
# auth.py
# ===========================================================================
def test_jwt_payload_from_claims_and_roundtrip():
    claims = {"sub": "u", "iss": "i", "aud": "a", "exp": 200.0, "iat": 100.0,
              "jti": "j1", "roles": ["admin"], "scopes": ["s1"]}
    payload = JWTPayload.from_claims(claims)
    assert payload.roles == ("admin",)
    assert payload.scopes == ("s1",)
    assert payload.to_claims() == claims
    assert payload.is_expired(now=200.0) is True
    assert payload.is_expired(now=199.9) is False

    with pytest.raises(AuthFailedError) as missing:
        JWTPayload.from_claims({"iss": "i", "aud": "a", "exp": 1, "iat": 1})
    assert missing.value.message == "Token missing required claims: sub"

    with pytest.raises(AuthFailedError) as malformed:
        JWTPayload.from_claims({**claims, "exp": "soon"})
    assert malformed.value.message == "Token contains malformed claims"
    assert isinstance(malformed.value.__cause__, ValueError)


def test_user_context_and_api_credential_models():
    payload = JWTPayload(sub="u1", iss="issuer", aud="aud", exp=2.0, iat=1.0, jti="j",
                         roles=("operator",), scopes=("heal",))
    ctx = UserContext.from_payload(payload, client_ip="10.0.0.1")
    assert (ctx.user_id, ctx.roles, ctx.scopes, ctx.client_ip) == ("u1", ("operator",), ("heal",), "10.0.0.1")
    assert ctx.metadata == {"jti": "j", "issuer": "issuer"}
    assert ctx.has_role("operator") and not ctx.has_role("admin")
    assert ctx.has_scope("heal") and not ctx.has_scope("other")
    assert len(ctx.session_id) == 32

    secret_hash = APICredential.hash_secret("pw")
    assert len(secret_hash) == 64
    assert APICredential.hash_secret("pw") == secret_hash
    cred = APICredential("k", secret_hash, "owner", scopes=("s",), expires_at=100.0)
    assert cred.is_active(now=99.0) is True
    assert cred.is_active(now=100.0) is False
    cred.revoked = True
    assert cred.is_active(now=1.0) is False
    assert APICredential("k2", secret_hash, "o").is_active() is True


def test_token_validator_rejects_weak_configuration(app_config):
    with pytest.raises(ModuleInitializationError) as short:
        TokenValidator("short")
    assert short.value.module_name == "TokenValidator"
    assert isinstance(short.value.__cause__, ConfigValidationError)
    assert short.value.__cause__.field_name == "secret"

    app_config.security.auth_scheme = AuthScheme.MTLS
    with pytest.raises(ModuleInitializationError) as scheme:
        TokenValidator(SECRET, app_config=app_config)
    assert scheme.value.__cause__.field_name == "auth_scheme"


def test_validate_token_success_records_telemetry(validator):
    token = validator.issue_token("alice", roles=("admin",), scopes=("heal", "patch"))
    ctx = validator.validate_token(token, client_ip="127.0.0.1")

    assert ctx.user_id == "alice"
    assert ctx.roles == ("admin",)
    assert ctx.scopes == ("heal", "patch")
    assert ctx.client_ip == "127.0.0.1"
    assert ctx.metadata["issuer"] == "ai-healing-engine"
    assert len(LATENCY_REGISTRY["auth.validate_token"]) == 1
    assert [e["action"] for e in AUDIT_TRAIL if e["action"] == "validate_token"] == ["validate_token"]


def test_validate_token_rejects_tampered_and_malformed_tokens(validator):
    token = validator.issue_token("alice")
    head, body, _ = token.split(".")

    forged = f"{head}.{body}.{_b64url_encode(b'x' * 32)}"
    with pytest.raises(AuthFailedError) as info:
        validator.validate_token(forged)
    assert info.value.message == "Token signature mismatch"

    none_header = _b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    with pytest.raises(AuthFailedError) as alg:
        validator.validate_token(f"{none_header}.{body}.{_b64url_encode(b'sig')}")
    assert "Unsupported or insecure algorithm" in alg.value.message

    with pytest.raises(AuthFailedError) as shape:
        validator.validate_token("only.two")
    assert shape.value.message == "Token is not a well-formed JWT"

    with pytest.raises(AuthFailedError):
        validator.validate_token(12345)  # type: ignore[arg-type]
    with pytest.raises(AuthFailedError):
        validator.validate_token("a.b.c")

    not_json = _b64url_encode(b"not json")
    with pytest.raises(AuthFailedError) as not_json_error:
        validator.validate_token(f"{not_json}.{not_json}.{not_json}")
    assert not_json_error.value.message == "Token segments are not valid JSON"


def test_validate_token_claim_checks(validator):
    other_issuer = TokenValidator(SECRET, issuer="someone-else")
    with pytest.raises(AuthFailedError) as issuer:
        validator.validate_token(other_issuer.issue_token("u"))
    assert issuer.value.message == "Unexpected issuer 'someone-else'"

    other_audience = TokenValidator(SECRET, audience="other-api")
    with pytest.raises(AuthFailedError) as audience:
        validator.validate_token(other_audience.issue_token("u"))
    assert audience.value.message == "Unexpected audience 'other-api'"

    with pytest.raises(AuthFailedError) as expired:
        validator.validate_token(validator.issue_token("u", ttl_s=-100))
    assert expired.value.message == "Token has expired"

    long_lived = validator.issue_token("u", ttl_s=validator.policy.token_ttl_seconds * 4 + 1)
    with pytest.raises(PolicyViolationError) as lifetime:
        validator.validate_token(long_lived)
    assert lifetime.value.policy == "token_lifetime"


def test_validate_token_origin_and_revocation(validator):
    token = validator.issue_token("u")
    with pytest.raises(PolicyViolationError) as origin:
        validator.validate_token(token, client_ip="10.9.9.9")
    assert origin.value.policy == "allowed_origins"

    jti = validator.revoke_token(token)
    assert isinstance(jti, str) and len(jti) == 32
    with pytest.raises(AuthFailedError) as revoked:
        validator.validate_token(token)
    assert revoked.value.message == "Token has been revoked"


def test_rate_limiting_buckets_and_exemptions(app_config):
    app_config.rate_limit = RateLimitConfig(
        requests_per_second=0.001, burst_size=2, penalty_seconds=5.0, exempt_clients=("vip",)
    )
    limited = TokenValidator(SECRET, app_config=app_config)

    limited.check_rate_limit("client-a")
    limited.check_rate_limit("client-a")
    with pytest.raises(RateLimitExceededError) as info:
        limited.check_rate_limit("client-a")
    assert info.value.retry_after >= 5.0

    limited.check_rate_limit("client-b")
    for _ in range(10):
        limited.check_rate_limit("vip")


def test_api_key_lifecycle(validator):
    credential = validator.register_api_key("svc-1", "s3cret", "service-owner", scopes=("heal",))
    assert credential.secret_hash == APICredential.hash_secret("s3cret")

    ctx = validator.validate_api_key("svc-1", "s3cret")
    assert ctx.user_id == "service-owner"
    assert ctx.scopes == ("heal",)
    assert ctx.roles == ()
    assert ctx.metadata == {"key_id": "svc-1"}

    with pytest.raises(AuthFailedError) as unknown:
        validator.validate_api_key("nope", "s3cret")
    assert unknown.value.message == "Unknown API key"
    with pytest.raises(AuthFailedError) as mismatch:
        validator.validate_api_key("svc-1", "wrong")
    assert mismatch.value.message == "API key secret mismatch"

    validator.register_api_key("expired", "pw", "o", ttl_s=-1.0)
    with pytest.raises(AuthFailedError) as expired:
        validator.validate_api_key("expired", "pw")
    assert expired.value.message == "API key is revoked or expired"

    validator.revoke_api_key("svc-1")
    with pytest.raises(AuthFailedError):
        validator.validate_api_key("svc-1", "s3cret")
    with pytest.raises(AuthFailedError):
        validator.revoke_api_key("missing")
    with pytest.raises(ConfigValidationError) as empty:
        validator.register_api_key("", "pw", "o")
    assert empty.value.field_name == "api_key"


def test_authorize_and_scope_enforcement(validator):
    admin = UserContext(user_id="a", roles=("admin",), scopes=("heal",))
    viewer = UserContext(user_id="v", roles=("viewer",))

    assert validator.authorize(admin, "config:write") is True
    assert validator.authorize(viewer, "metrics:read") is True
    with pytest.raises(PolicyViolationError) as denied:
        validator.authorize(viewer, "config:write")
    assert (denied.value.policy, denied.value.action) == ("rbac", "config:write")

    validator.require_scope(admin, "heal")
    with pytest.raises(PolicyViolationError) as scope:
        validator.require_scope(viewer, "heal")
    assert scope.value.policy == "scope_check"


def test_signing_backend_failure_trips_circuit_breaker(validator):
    token = validator.issue_token("alice")
    validator.set_backend_health(False)

    for _ in range(5):
        with pytest.raises(SystemDegradedError) as degraded:
            validator.validate_token(token)
        assert degraded.value.degraded_modules == ["auth"]
    with pytest.raises(CircuitOpenError) as opened:
        validator.validate_token(token)
    assert opened.value.circuit == SIGNING_KEY_BREAKER

    validator.set_backend_health(True)
    _BREAKER_STATE.clear()
    assert validator.validate_token(token).user_id == "alice"


# ===========================================================================
# router.py
# ===========================================================================
def test_inbound_request_helpers():
    request = InboundRequest("POST", "/x", {"Content-Type": "application/json"}, b'{"a": 1}')
    assert request.header("content-type") == "application/json"
    assert request.header("missing", "dflt") == "dflt"
    assert request.json_body() == {"a": 1}
    assert len(request.request_id) == 32
    assert InboundRequest("GET", "/x").json_body() == {}

    with pytest.raises(ConfigValidationError) as bad_json:
        InboundRequest("POST", "/x", body=b"{nope").json_body()
    assert bad_json.value.field_name == "body"
    with pytest.raises(ConfigValidationError) as not_object:
        InboundRequest("POST", "/x", body=b"[1, 2]").json_body()
    assert "object" in not_object.value.reason


def test_outbound_response_and_route_models():
    ok = OutboundResponse.ok({"a": 1}, request_id="r1")
    assert (ok.status, ok.body, ok.request_id, ok.is_success()) == (200, {"a": 1}, "r1", True)

    error = RouteNotFoundError("/x", "GET")
    failure = OutboundResponse.from_error(404, error, "r2", {"H": "v"})
    assert failure.body == {"error": "RouteNotFoundError", "code": 1013, "message": "no such route: GET /x"}
    assert failure.headers == {"H": "v"}
    assert failure.is_success() is False
    assert RouteNotFoundError("/x", "GET", method_mismatch=True).message == "method not allowed: GET /x"

    route = RouteDefinition(path="/x", method="GET", handler=lambda ctx: {})
    with pytest.raises(FrozenInstanceError):
        route.path = "/y"  # type: ignore[misc]
    ctx = RouteContext(request=InboundRequest("GET", "/x"), user=UserContext("u"), route=route)
    assert ctx.payload == {} and ctx.attributes == {}
    assert len(ctx.trace_id) == 32
    assert ctx.elapsed_ms() >= 0.0


def test_dispatcher_default_routes_and_registration_rules(dispatcher):
    assert dispatcher.list_routes() == [
        ("/heal", "POST"), ("/health", "GET"), ("/metrics", "GET"), ("/patch", "POST"), ("/rollback", "POST"),
    ]
    handler = lambda ctx: {}  # noqa: E731
    with pytest.raises(ConfigValidationError) as dup:
        dispatcher.register_route("/health", "GET", handler)
    assert dup.value.field_name == "route"
    with pytest.raises(ConfigValidationError) as method:
        dispatcher.register_route("/new", "PATCH", handler)
    assert method.value.field_name == "method"
    with pytest.raises(ConfigValidationError) as path:
        dispatcher.register_route("no-slash", "GET", handler)
    assert path.value.field_name == "path"

    empty = RequestDispatcher(dispatcher.validator, register_defaults=False)
    assert empty.list_routes() == []


def test_dispatch_health_success_populates_telemetry(dispatcher):
    request = make_request("get", "/health")
    response = dispatcher.dispatch(request)

    assert response.status == 200
    assert response.body == {"status": "ok", "environment": "development"}
    assert response.request_id == request.request_id
    assert len(response.headers["X-Trace-Id"]) == 32
    assert response.latency_ms >= 0.0
    assert dispatcher.status_counts == {200: 1}
    assert {"router.dispatch", "router.validate_request", "router.resolve_route",
            "router.authenticate", "router.authorize", "handler.health"} <= set(LATENCY_REGISTRY)
    assert {"process_request", "authenticate", "authorize_route"} <= {e["action"] for e in AUDIT_TRAIL}
    assert dispatcher.aggregator.totals()["router.requests"] == 1.0


def test_dispatch_authentication_failures(dispatcher):
    anonymous = dispatcher.dispatch(make_request("GET", "/metrics"))
    assert anonymous.status == 401
    assert anonymous.body["error"] == "AuthFailedError"
    assert anonymous.body["code"] == 1001
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"

    for header in ("Bearer ", "Basic abc", "ApiKey nocolon"):
        response = dispatcher.dispatch(make_request("GET", "/metrics", headers={"Authorization": header}))
        assert response.status == 401, header
        assert response.body["error"] == "AuthFailedError"

    bad_token = dispatcher.dispatch(make_request("GET", "/metrics", token="a.b.c"))
    assert bad_token.status == 401


def test_dispatch_authorization_failures_and_success(dispatcher):
    viewer, operator, admin = issue("viewer"), issue("operator"), issue("admin", scopes=("heal",))

    assert dispatcher.dispatch(make_request("GET", "/metrics", viewer)).status == 200
    denied = dispatcher.dispatch(make_request("POST", "/heal", viewer, {"target": "db"}))
    assert denied.status == 403
    assert denied.body["error"] == "PolicyViolationError"

    no_scope = dispatcher.dispatch(make_request("POST", "/heal", operator, {"target": "db"}))
    assert no_scope.status == 403
    assert "scope_check" in no_scope.body["message"]

    allowed = dispatcher.dispatch(make_request("POST", "/heal", admin, {"target": "db", "strategy": "patch"}))
    assert allowed.status == 200
    assert allowed.body == {"accepted": True, "target": "db", "strategy": "patch", "by": "admin"}


def test_dispatch_request_validation_errors(dispatcher):
    admin = issue("admin", scopes=("heal",))

    unknown = dispatcher.dispatch(make_request("GET", "/nope", admin))
    assert (unknown.status, unknown.body["code"]) == (404, 1013)
    wrong_method = dispatcher.dispatch(make_request("GET", "/heal", admin))
    assert wrong_method.status == 405

    assert dispatcher.dispatch(make_request("PATCH", "/health")).status == 400
    assert dispatcher.dispatch(InboundRequest("GET", "no-slash")).status == 400

    no_content_type = dispatcher.dispatch(InboundRequest("POST", "/heal", {}, b"{}"))
    assert no_content_type.status == 400
    assert dispatcher.dispatch(make_request("POST", "/heal", admin, b"{broken")).status == 400

    evil_origin = dispatcher.dispatch(make_request("GET", "/health", headers={"Origin": "http://evil"}))
    assert evil_origin.status == 403
    assert "cors" in evil_origin.body["message"]
    assert dispatcher.dispatch(make_request("GET", "/health", headers={"Origin": "localhost"})).status == 200

    too_big = dispatcher.dispatch(make_request("POST", "/heal", admin, b"x" * (65 * 1024)))
    assert (too_big.status, too_big.body["code"]) == (503, 1009)

    many_headers = {f"h{i}": "v" for i in range(65)}
    assert dispatcher.dispatch(InboundRequest("GET", "/health", many_headers)).status == 503


def test_validate_request_raises_specific_exceptions(dispatcher):
    with pytest.raises(ConfigValidationError) as method:
        dispatcher.validate_request(InboundRequest("TRACE", "/x"))
    assert method.value.field_name == "method"
    with pytest.raises(ConfigValidationError) as path:
        dispatcher.validate_request(InboundRequest("GET", "x"))
    assert path.value.field_name == "path"
    with pytest.raises(ResourceExhaustedError) as headers:
        dispatcher.validate_request(InboundRequest("GET", "/x", {f"h{i}": "v" for i in range(65)}))
    assert (headers.value.resource, headers.value.limit) == ("headers", 64)
    with pytest.raises(ConfigValidationError) as content_type:
        dispatcher.validate_request(InboundRequest("POST", "/x", {}, b"{}"))
    assert content_type.value.field_name == "content-type"


def test_heal_handler_payload_validation(dispatcher):
    admin = issue("admin", scopes=("heal",))
    missing_target = dispatcher.dispatch(make_request("POST", "/heal", admin, {"strategy": "patch"}))
    assert (missing_target.status, missing_target.body["error"]) == (400, "ConfigValidationError")

    unknown_strategy = dispatcher.dispatch(make_request("POST", "/heal", admin, {"target": "db", "strategy": "magic"}))
    assert unknown_strategy.status == 400

    disallowed = dispatcher.dispatch(make_request("POST", "/heal", admin, {"target": "db", "strategy": "rollback"}))
    assert disallowed.status == 403
    assert "healing_strategy" in disallowed.body["message"]

    default_strategy = dispatcher.dispatch(make_request("POST", "/heal", admin, {"target": "db"}))
    assert default_strategy.body["strategy"] == "restart"


def test_patch_and_rollback_handlers_respect_signing_policy():
    admin_token = issue("admin")

    dev = build_dispatcher(SECRET)
    assert dev.dispatch(make_request("POST", "/patch", admin_token, {"patch_id": "p1"})).body == {
        "applied": True, "patch_id": "p1"}
    assert dev.dispatch(make_request("POST", "/patch", admin_token, {})).status == 400
    assert dev.dispatch(make_request("POST", "/rollback", admin_token, {"patch_id": "p1"})).body == {
        "rolled_back": True, "patch_id": "p1"}
    assert dev.dispatch(make_request("POST", "/rollback", admin_token, {})).status == 400

    prod = build_dispatcher(SECRET, app_config=create_app_config(Environment.PRODUCTION))
    # Production tokens must be issued under the production TTL (300s); a
    # development token (3600s) exceeds the policy maximum of 4x the TTL.
    prod_token = prod.validator.issue_token("admin", roles=("admin",))
    unsigned = prod.dispatch(make_request("POST", "/patch", prod_token, {"patch_id": "p1"}))
    assert unsigned.status == 403
    assert "signed_patches" in unsigned.body["message"]
    signed = prod.dispatch(make_request("POST", "/patch", prod_token, {"patch_id": "p1", "signature": "sig"}))
    assert signed.status == 200
    assert prod.dispatch(make_request("POST", "/patch", prod_token, {"signature": "sig"})).status == 400

    dev_token_in_prod = prod.dispatch(make_request("POST", "/patch", admin_token, {"patch_id": "p1"}))
    assert dev_token_in_prod.status == 403
    assert "token_lifetime" in dev_token_in_prod.body["message"]


def test_dispatch_rate_limit_response_has_retry_after():
    cfg = create_app_config()
    cfg.rate_limit = RateLimitConfig(requests_per_second=0.001, burst_size=1, penalty_seconds=5.0)
    limited = build_dispatcher(SECRET, app_config=cfg)

    assert limited.dispatch(make_request("GET", "/health")).status == 200
    second = limited.dispatch(make_request("GET", "/health"))
    assert second.status == 429
    assert second.body["code"] == 1004
    assert int(second.headers["Retry-After"]) >= 6
    assert limited.status_counts == {200: 1, 429: 1}


def test_decorator_route_context_and_custom_limits(dispatcher):
    captured = {}

    @dispatcher.route("/echo", "POST", auth_required=False)
    def echo(ctx: RouteContext):
        captured["ctx"] = ctx
        return {"echo": ctx.payload}

    response = dispatcher.dispatch(make_request("POST", "/echo", body={"a": 1}))
    assert response.status == 200
    assert response.body == {"echo": {"a": 1}}
    ctx = captured["ctx"]
    assert ctx.user.user_id == "anonymous"
    assert ctx.user.metadata == {"anonymous": "true"}
    hint = ctx.attributes["claims_hint"]
    assert isinstance(hint, JWTPayload)
    assert (hint.sub, hint.iss, hint.aud) == ("anonymous", "router", "handler")
    assert hint.exp - hint.iat == pytest.approx(30.0)

    dispatcher.register_route("/tiny", "POST", lambda c: {}, auth_required=False, max_body_bytes=10)
    oversized = dispatcher.dispatch(make_request("POST", "/tiny", body={"key": "value-that-is-long"}))
    assert (oversized.status, oversized.body["code"]) == (503, 1009)


def test_dispatch_wraps_unexpected_errors_without_leaking(dispatcher):
    def explode(ctx):
        raise RuntimeError("secret internals")

    dispatcher.register_route("/boom", "GET", explode, auth_required=False)
    response = dispatcher.dispatch(make_request("GET", "/boom"))
    assert response.status == 500
    assert response.body == {"error": "HealingEngineError", "code": 1999, "message": "Unhandled RuntimeError"}
    assert "secret internals" not in json.dumps(response.body)


def test_dispatch_maps_degraded_handler_to_503_and_breaker_opens(dispatcher):
    def degraded(ctx):
        raise SystemDegradedError("backend down")

    dispatcher.register_route("/flaky", "GET", degraded, auth_required=False)
    statuses = [dispatcher.dispatch(make_request("GET", "/flaky")).status for _ in range(6)]
    assert statuses == [503] * 6
    assert dispatcher.dispatch(make_request("GET", "/flaky")).body["error"] == "CircuitOpenError"
    assert _BREAKER_STATE["route.GET./flaky"].failures == 5


def test_metrics_handler_reports_totals_and_status_counts(dispatcher):
    dispatcher.dispatch(make_request("GET", "/health"))
    dispatcher.dispatch(make_request("GET", "/health"))
    response = dispatcher.dispatch(make_request("GET", "/metrics", issue("viewer")))
    assert response.status == 200
    assert response.body["status_counts"] == {200: 2}
    assert response.body["totals"]["router.requests"] == 2.0
    assert response.body["totals"]["auth.token_valid"] == 1.0


def test_verify_service_credential_contract():
    active = APICredential("k", APICredential.hash_secret("pw"), "o", scopes=("s",))
    assert RequestDispatcher.verify_service_credential(active) is None

    with pytest.raises(AuthFailedError) as inactive:
        RequestDispatcher.verify_service_credential(
            APICredential("k", "h", "o", scopes=("s",), revoked=True))
    assert "inactive" in inactive.value.message

    with pytest.raises(PolicyViolationError) as no_scopes:
        RequestDispatcher.verify_service_credential(APICredential("k", "h", "o"))
    assert no_scopes.value.policy == "service_credential"


# ===========================================================================
# core.py
# ===========================================================================
CLASSIFICATION_CASES = [
    (SandboxEscapeError("x"), 403),
    (PolicyViolationError("p", "a"), 403),
    (AuthFailedError(), 401),
    (RateLimitExceededError(1.0), 429),
    (RouteNotFoundError("/x", "GET"), 404),
    (ConfigValidationError("f", "r"), 400),
    (ResourceExhaustedError("r", 1.0), 503),
    (CircuitOpenError("c"), 503),
    (SystemDegradedError(), 503),
    (HealingTimeoutError(1.0), 504),
    (RollbackFailedError("p"), 500),
    (ModuleInitializationError("m"), 500),
    (TelemetryError(), 502),
    (HealingEngineError("x"), 500),
]


@pytest.mark.parametrize("exc,status", CLASSIFICATION_CASES,
                         ids=[type(e).__name__ for e, _ in CLASSIFICATION_CASES])
def test_classify_error_status_mapping(exc, status):
    assert classify_error(exc) == status


def test_pipeline_models():
    assert PipelineStage("complete") is PipelineStage.COMPLETE
    assert isinstance(PipelineStage.BOOTSTRAP, str)
    assert PipelineResult("r", PipelineStage.COMPLETE, 200).ok is True
    assert PipelineResult("r", PipelineStage.DISPATCH, 404).ok is False
    assert PipelineResult("r", PipelineStage.DISPATCH, 200, error_type="X").ok is False
    record = HealingRecord(attempt=1, strategy=HealingStrategy.RESTART, succeeded=True, delay_s=0.0)
    assert record.reason == ""


def test_engine_lifecycle_and_construction_guards():
    sinks = five_sinks()
    engine = make_engine(sinks=sinks)
    engine.start()
    assert all(s.connected for s in sinks)

    engine.emit("custom.metric", 3.0)
    report = engine.stop()
    assert report == {name: (True, 1) for name in SINK_NAMES}
    assert all(s.connected is False for s in sinks)
    assert engine.stop() == {}

    bad_cfg = create_app_config(Environment.PRODUCTION)
    bad_cfg.debug = True
    with pytest.raises(ConfigValidationError) as info:
        GatewayEngine(SECRET, app_config=bad_cfg)
    assert info.value.field_name == "debug"

    with pytest.raises(ModuleInitializationError):
        GatewayEngine("short")


def test_engine_emit_swallows_telemetry_failures():
    engine = make_engine()
    engine.aggregator.buffer_limit = 0
    engine.emit("dropped.metric")
    assert engine.aggregator.totals() == {}

    engine.aggregator.buffer_limit = 10
    engine.config.telemetry.enabled = False
    engine.emit("ignored.metric")
    assert engine.aggregator.totals() == {}


def test_engine_execute_success_emits_metrics_to_all_sinks():
    sinks = five_sinks()
    engine = make_engine(sinks=sinks)
    request = make_request("POST", "/heal", issue("admin", scopes=("heal",), sub="alice"),
                           {"target": "db", "strategy": "patch"})
    result = engine.execute(request)

    assert result.ok is True
    assert result.stage is PipelineStage.COMPLETE
    assert (result.status, result.user_id, result.request_id) == (200, "alice", request.request_id)
    assert result.body["accepted"] is True
    assert result.error_type is None and result.healed is False
    assert result.latency_ms > 0.0

    totals = engine.aggregator.totals()
    assert totals["router.requests"] == 1.0
    assert totals["core.pipeline"] > 0.0
    assert totals["auth.token_valid"] == 1.0

    engine.stop()
    for sink in sinks:
        assert {p.name for p in sink.sent} >= {"router.requests", "core.pipeline", "auth.token_valid"}
    pipeline_point = next(p for p in sinks[0].sent if p.name == "core.pipeline")
    assert pipeline_point.tags == {"service": "ai-healing-engine", "status": "200"}


def test_engine_preflight_auth_contract():
    engine = make_engine()
    assert engine.preflight_auth(make_request("GET", "/health")) is None

    ctx = engine.preflight_auth(make_request("GET", "/metrics", issue("viewer", scopes=("s",), sub="bob")))
    assert isinstance(ctx, UserContext)
    assert (ctx.user_id, ctx.roles, ctx.scopes) == ("bob", ("viewer",), ("s",))

    with pytest.raises(PolicyViolationError) as unknown_role:
        engine.preflight_auth(make_request("GET", "/metrics", issue("ghost")))
    assert unknown_role.value.policy == "rbac"
    assert "ghost" in unknown_role.value.action

    with pytest.raises(AuthFailedError) as scheme:
        engine.preflight_auth(make_request("GET", "/metrics", headers={"Authorization": "Basic abc"}))
    assert scheme.value.message == "Unsupported authorization scheme 'basic'"

    with pytest.raises(AuthFailedError):
        engine.preflight_auth(make_request("GET", "/metrics", headers={"Authorization": "ApiKey nocolon"}))
    with pytest.raises(AuthFailedError) as unknown_key:
        engine.preflight_auth(make_request("GET", "/metrics", headers={"Authorization": "ApiKey k:s"}))
    assert unknown_key.value.message == "Unknown API key"


def test_engine_preflight_api_key_and_disabled_bearer():
    engine = make_engine()
    engine.register_service_account("svc", "pw", "svc-owner", scopes=("heal",))

    ctx = engine.preflight_auth(make_request("GET", "/metrics", token="ApiKey svc:pw"))
    assert (ctx.user_id, ctx.scopes, ctx.roles) == ("svc-owner", ("heal",), ())
    with pytest.raises(AuthFailedError) as mismatch:
        engine.preflight_auth(make_request("GET", "/metrics", token="ApiKey svc:bad"))
    assert mismatch.value.message == "API key secret mismatch"

    with pytest.raises(PolicyViolationError) as no_scopes:
        engine.register_service_account("empty", "pw", "owner")
    assert no_scopes.value.policy == "service_credential"

    cfg = create_app_config()
    cfg.security.auth_scheme = AuthScheme.API_KEY
    api_only = make_engine(cfg)
    with pytest.raises(PolicyViolationError) as bearer_disabled:
        api_only.preflight_auth(make_request("GET", "/metrics", issue("viewer")))
    assert bearer_disabled.value.policy == "auth_scheme"


def test_engine_preflight_does_not_consume_rate_limit():
    cfg = create_app_config()
    cfg.rate_limit = RateLimitConfig(requests_per_second=0.001, burst_size=1)
    engine = make_engine(cfg)
    request = make_request("GET", "/metrics", issue("viewer"))
    for _ in range(5):
        assert engine.preflight_auth(request) is not None

    assert engine.execute(make_request("GET", "/health")).status == 200
    limited = engine.execute(make_request("GET", "/health"))
    assert limited.status == 429
    assert limited.error_type == "RateLimitExceededError"
    assert limited.error_code == 1004


def test_engine_heals_degraded_backend_and_retries():
    engine = make_engine()
    token = issue("admin", scopes=("heal",), sub="alice")
    engine.validator.set_backend_health(False)

    result = engine.execute(make_request("POST", "/heal", token, {"target": "db"}))

    assert (result.status, result.healed, result.error_type) == (200, True, None)
    assert result.stage is PipelineStage.COMPLETE
    assert result.healing_log == [
        HealingRecord(attempt=1, strategy=HealingStrategy.RESTART, succeeded=True,
                      delay_s=0.0, reason="SystemDegradedError")
    ]
    assert engine.validator._backend_healthy is True
    assert engine.healing_history == result.healing_log
    totals = engine.aggregator.totals()
    assert totals["core.heal_succeeded"] == 1.0
    assert {"core.heal", "core.preflight_auth", "core.execute"} <= set(LATENCY_REGISTRY)


def test_engine_heals_flaky_downstream_route_once():
    engine = make_engine()
    calls = {"count": 0}

    def flaky(ctx):
        calls["count"] += 1
        if calls["count"] == 1:
            raise SystemDegradedError("transient")
        return {"ok": True}

    engine.dispatcher.register_route("/flaky", "GET", flaky, auth_required=False)
    result = engine.execute(make_request("GET", "/flaky"))
    assert (result.status, result.body, result.healed) == (200, {"ok": True}, True)
    assert calls["count"] == 2
    assert result.healing_log[0].strategy is HealingStrategy.RESTART

    engine.dispatcher.register_route("/dead", "GET",
                                     lambda ctx: (_ for _ in ()).throw(SystemDegradedError("still down")),
                                     auth_required=False)
    persistent = engine.execute(make_request("GET", "/dead"))
    assert persistent.healed is True
    assert persistent.status == 503
    assert persistent.error_type == "SystemDegradedError"
    assert persistent.error_code == 1002
    assert persistent.stage is PipelineStage.DISPATCH


def test_engine_heal_exhaustion_returns_degraded_error():
    cfg = create_app_config()
    cfg.healing.strategies = [HealingStrategy.ISOLATE, HealingStrategy.ESCALATE]
    cfg.healing.max_attempts = 2
    engine = make_engine(cfg)
    engine.validator.set_backend_health(False)

    result = engine.execute(make_request("POST", "/heal", issue("admin", scopes=("heal",)), {"target": "db"}))

    assert result.status == 503
    assert result.error_type == "SystemDegradedError"
    assert result.stage is PipelineStage.HEALING
    assert result.healed is False
    assert result.body["message"] == "all healing attempts exhausted"
    assert [(r.strategy, r.succeeded, r.reason) for r in engine.healing_history] == [
        (HealingStrategy.ISOLATE, False, "RollbackFailedError"),
        (HealingStrategy.ESCALATE, False, "SystemDegradedError"),
    ]
    assert engine.aggregator.totals()["core.heal_failed"] == 2.0


def test_engine_auto_rollback_failure_and_timeout():
    rollback_cfg = create_app_config()
    rollback_cfg.healing.strategies = [HealingStrategy.ROLLBACK]
    engine = make_engine(rollback_cfg)
    engine._strategy_handlers[HealingStrategy.ROLLBACK] = MagicMock(side_effect=SystemDegradedError("x"))
    engine.validator.set_backend_health(False)
    token = issue("admin", scopes=("heal",))

    result = engine.execute(make_request("POST", "/heal", token, {"target": "db"}))
    assert (result.status, result.error_type, result.error_code) == (500, "RollbackFailedError", 1008)
    assert "auto-rollback-1" in result.body["message"]

    # GatewayEngine validates its config on construction (attempt_timeout_s must
    # be positive), so force an immediate timeout after the engine is built.
    slow = make_engine(create_app_config())
    slow.config.healing.attempt_timeout_s = -1.0
    slow.validator.set_backend_health(False)
    timed_out = slow.execute(make_request("POST", "/heal", token, {"target": "db"}))
    assert (timed_out.status, timed_out.error_type, timed_out.error_code) == (504, "HealingTimeoutError", 1007)


def test_engine_heal_applies_backoff_through_injected_sleeper():
    sleeper = MagicMock()
    cfg = create_app_config()
    cfg.healing.strategies = [HealingStrategy.RESTART]
    engine = make_engine(cfg, backoff_scale=1.0, sleeper=sleeper)
    engine.validator.set_backend_health(False)

    result = engine.execute(make_request("POST", "/heal", issue("admin", scopes=("heal",)), {"target": "db"}))

    assert result.healed is True
    sleeper.assert_called_once()
    assert sleeper.call_args.args[0] == pytest.approx(1.5)
    assert result.healing_log[0].delay_s == pytest.approx(1.5)


def test_heal_strategy_handlers_reset_breaker_state():
    engine = make_engine()
    _BREAKER_STATE[SIGNING_KEY_BREAKER] = BreakerState(failures=3, opened_at=1.0)
    engine.validator.set_backend_health(False)
    engine._heal_restart()
    assert _BREAKER_STATE[SIGNING_KEY_BREAKER] == BreakerState()
    assert engine.validator._backend_healthy is True

    _BREAKER_STATE["a"] = BreakerState(failures=1)
    _BREAKER_STATE["b"] = BreakerState(failures=2)
    engine.validator.set_backend_health(False)
    engine._heal_rollback()
    assert _BREAKER_STATE["a"] == BreakerState() and _BREAKER_STATE["b"] == BreakerState()
    assert engine.validator._backend_healthy is True

    engine.validator.set_backend_health(False)
    with pytest.raises(SystemDegradedError):
        engine._heal_patch()
    engine.validator.set_backend_health(True)
    engine._heal_patch()

    with pytest.raises(RollbackFailedError):
        engine._heal_isolate()
    with pytest.raises(SystemDegradedError) as escalate:
        engine._heal_escalate()
    assert escalate.value.degraded_modules == ["core"]


def test_engine_health_report_shape():
    engine = make_engine()
    engine.execute(make_request("GET", "/health"))
    report = engine.health_report()

    assert set(report) == {"environment", "sinks", "breakers", "avg_latency_ms",
                           "audit_entries", "healing_attempts", "status_counts"}
    assert report["environment"] == "development"
    assert report["sinks"] == {name: (True, 0) for name in SINK_NAMES}
    assert report["status_counts"] == {200: 1}
    assert report["healing_attempts"] == 0
    assert report["audit_entries"] >= 1
    assert {"core.execute", "router.dispatch"} <= set(report["avg_latency_ms"])
    assert "route.GET./health" in report["breakers"]
    assert report["breakers"]["route.GET./health"] == {"failures": 0, "open": False}


# ---------------------------------------------------------------------------
# execute_system_pipeline
# ---------------------------------------------------------------------------
def test_pipeline_success_flushes_all_five_sinks():
    result = execute_system_pipeline(make_request("GET", "/health"), SECRET)

    assert result.ok is True
    assert result.stage is PipelineStage.COMPLETE
    assert (result.status, result.body) == (200, {"status": "ok", "environment": "development"})
    assert set(result.sink_report) == set(SINK_NAMES)
    counts = {count for _, count in result.sink_report.values()}
    assert len(counts) == 1 and counts.pop() >= 2
    assert all(connected is True for connected, _ in result.sink_report.values())
    assert result.latency_ms > 0.0


def test_pipeline_production_environment_and_authenticated_route():
    prod = execute_system_pipeline(make_request("GET", "/health"), SECRET, environment=Environment.PRODUCTION)
    assert prod.body["environment"] == "production"

    token = issue("admin", scopes=("heal",), sub="alice")
    result = execute_system_pipeline(
        make_request("POST", "/heal", token, {"target": "db"}), SECRET)
    assert result.status == 200
    assert result.user_id == "alice"
    assert result.body == {"accepted": True, "target": "db", "strategy": "restart", "by": "alice"}


def test_pipeline_invokes_every_sink_emit_with_expected_points():
    originals = {cls: cls.emit for cls, _ in SINK_CLASSES}
    with ExitStack() as stack:
        spies = {
            cls: stack.enter_context(patch.object(cls, "emit", autospec=True, side_effect=originals[cls]))
            for cls, _ in SINK_CLASSES
        }
        result = execute_system_pipeline(make_request("GET", "/health"), SECRET)

    assert result.status == 200
    for cls, spy in spies.items():
        assert spy.call_count == 2, cls.__name__
        assert [call.args[1].name for call in spy.call_args_list] == ["router.requests", "core.pipeline"]


def test_pipeline_survives_failing_sink():
    with patch.object(PrometheusSink, "emit", side_effect=TelemetryError("prometheus down")):
        result = execute_system_pipeline(make_request("GET", "/health"), SECRET)

    assert result.status == 200
    assert result.sink_report["prometheus"] == (True, 0)
    assert result.sink_report["datadog"][1] >= 2


def test_pipeline_auth_failures_report_stage_and_error_body():
    no_auth = execute_system_pipeline(make_request("GET", "/metrics"), SECRET)
    assert (no_auth.status, no_auth.error_type, no_auth.error_code) == (401, "AuthFailedError", 1001)
    assert no_auth.stage is PipelineStage.DISPATCH
    assert no_auth.user_id is None
    assert no_auth.ok is False

    bad_token = execute_system_pipeline(make_request("GET", "/metrics", token="abc.def.ghi"), SECRET)
    assert bad_token.status == 401
    assert bad_token.stage is PipelineStage.PREFLIGHT_AUTH
    assert set(bad_token.body) == {"error", "code", "message"}
    assert set(bad_token.sink_report) == set(SINK_NAMES)

    ghost = execute_system_pipeline(make_request("GET", "/metrics", issue("ghost")), SECRET)
    assert (ghost.status, ghost.error_type, ghost.stage) == (403, "PolicyViolationError", PipelineStage.PREFLIGHT_AUTH)

    forbidden = execute_system_pipeline(
        make_request("POST", "/heal", issue("viewer", sub="bob"), {"target": "db"}), SECRET)
    assert (forbidden.status, forbidden.error_type, forbidden.stage, forbidden.user_id) == (
        403, "PolicyViolationError", PipelineStage.DISPATCH, "bob")

    missing = execute_system_pipeline(make_request("GET", "/nope", issue("viewer")), SECRET)
    assert (missing.status, missing.error_type, missing.error_code) == (404, "RouteNotFoundError", 1013)


def test_pipeline_service_accounts():
    accounts = [("svc", "pw", "svc-owner", ("heal",))]
    api_key = execute_system_pipeline(
        make_request("GET", "/metrics", token="ApiKey svc:pw"), SECRET, service_accounts=accounts)
    assert (api_key.status, api_key.error_type, api_key.user_id) == (403, "PolicyViolationError", "svc-owner")

    wrong_secret = execute_system_pipeline(
        make_request("GET", "/metrics", token="ApiKey svc:bad"), SECRET, service_accounts=accounts)
    assert (wrong_secret.status, wrong_secret.stage) == (401, PipelineStage.PREFLIGHT_AUTH)

    scopeless = execute_system_pipeline(
        make_request("GET", "/health"), SECRET, service_accounts=[("svc", "pw", "owner", ())])
    assert (scopeless.status, scopeless.error_type, scopeless.stage) == (
        403, "PolicyViolationError", PipelineStage.PREFLIGHT_AUTH)
    assert set(scopeless.sink_report) == set(SINK_NAMES)


def test_pipeline_bootstrap_failures_have_no_sink_report():
    short = execute_system_pipeline(make_request("GET", "/health"), "short")
    assert (short.status, short.error_type, short.error_code) == (500, "ModuleInitializationError", 1006)
    assert short.stage is PipelineStage.BOOTSTRAP
    assert short.sink_report == {}

    invalid = execute_system_pipeline(make_request("GET", "/health"), SECRET, burst_size=0)
    assert (invalid.status, invalid.error_type, invalid.error_code) == (400, "ConfigValidationError", 1003)
    assert invalid.stage is PipelineStage.BOOTSTRAP
    assert invalid.sink_report == {}


def test_pipeline_self_heals_simulated_backend_failure():
    token = issue("admin", scopes=("heal",), sub="alice")
    result = execute_system_pipeline(
        make_request("POST", "/heal", token, {"target": "db"}), SECRET, simulate_backend_failure=True)

    assert result.ok is True
    assert result.status == 200
    assert result.healed is True
    assert result.stage is PipelineStage.COMPLETE
    assert [(r.attempt, r.strategy, r.succeeded) for r in result.healing_log] == [
        (1, HealingStrategy.RESTART, True)]
    assert result.user_id == "alice"

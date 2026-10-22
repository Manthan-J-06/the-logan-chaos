"""sandbox/router.py

Request routing layer for the autonomous AI healing engine.

Contains:
  * network payload dataclasses: ``InboundRequest``, ``OutboundResponse``,
    ``RouteContext`` and ``RouteDefinition``
  * ``RequestDispatcher``: authenticates, validates, authorises and routes
    requests, instrumented heavily with the telemetry decorators
  * validation logic that builds auth dataclasses and raises the custom
    exceptions defined in ``sandbox.config``
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from sandbox.auth import (
    APICredential,
    JWTPayload,
    TokenValidator,
    UserContext,
)
from sandbox.config import (
    AppConfig,
    AuthFailedError,
    CircuitBreakerConfig,
    CircuitOpenError,
    ConfigValidationError,
    HealingEngineError,
    HealingStrategy,
    HealingTimeoutError,
    LogLevel,
    PolicyViolationError,
    RateLimitExceededError,
    ResourceExhaustedError,
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

SUPPORTED_METHODS: Tuple[str, ...] = ("GET", "POST", "PUT", "DELETE")
MAX_HEADER_COUNT = 64
MAX_PATH_LENGTH = 512

Handler = Callable[["RouteContext"], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Router-specific exception
# ---------------------------------------------------------------------------
class RouteNotFoundError(HealingEngineError):
    """Raised when no route matches the request path or method."""

    def __init__(self, path: str, method: str, method_mismatch: bool = False) -> None:
        reason = "method not allowed" if method_mismatch else "no such route"
        super().__init__(f"{reason}: {method} {path}", code=1013)
        self.path = path
        self.method = method
        self.method_mismatch = method_mismatch


# ---------------------------------------------------------------------------
# Network payload dataclasses
# ---------------------------------------------------------------------------
@dataclass
class InboundRequest:
    """A raw request as received from the network edge."""

    method: str
    path: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    query: Dict[str, str] = field(default_factory=dict)
    client_ip: str = "127.0.0.1"
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    received_at: float = field(default_factory=time.time)

    def header(self, name: str, default: str = "") -> str:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    def json_body(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigValidationError("body", "request body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ConfigValidationError("body", "JSON body must be an object")
        return parsed


@dataclass
class OutboundResponse:
    """A response ready to be serialised back to the caller."""

    status: int
    body: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    request_id: str = ""
    latency_ms: float = 0.0

    @classmethod
    def ok(cls, body: Dict[str, Any], request_id: str = "") -> "OutboundResponse":
        return cls(status=200, body=body, request_id=request_id)

    @classmethod
    def from_error(cls, status: int, error: HealingEngineError, request_id: str = "",
                   headers: Optional[Dict[str, str]] = None) -> "OutboundResponse":
        return cls(
            status=status,
            body={"error": type(error).__name__, "code": error.code, "message": error.message},
            headers=headers or {},
            request_id=request_id,
        )

    def is_success(self) -> bool:
        return 200 <= self.status < 300


@dataclass(frozen=True)
class RouteDefinition:
    """Declarative description of a single route."""

    path: str
    method: str
    handler: Handler
    required_permission: Optional[str] = None
    required_scopes: Tuple[str, ...] = ()
    auth_required: bool = True
    max_body_bytes: Optional[int] = None


@dataclass
class RouteContext:
    """Everything a handler needs to process an authenticated request."""

    request: InboundRequest
    user: UserContext
    route: RouteDefinition
    payload: Dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.perf_counter)
    attributes: Dict[str, Any] = field(default_factory=dict)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0


# ---------------------------------------------------------------------------
# Request dispatcher
# ---------------------------------------------------------------------------
class RequestDispatcher:
    """Authenticates, validates, authorises and dispatches requests."""

    ERROR_STATUS: Dict[type, int] = {
        AuthFailedError: 401,
        PolicyViolationError: 403,
        RouteNotFoundError: 404,
        ConfigValidationError: 400,
        RateLimitExceededError: 429,
        ResourceExhaustedError: 503,
        CircuitOpenError: 503,
        SystemDegradedError: 503,
        HealingTimeoutError: 504,
    }

    def __init__(
        self,
        validator: TokenValidator,
        app_config: Optional[AppConfig] = None,
        aggregator: Optional[MetricsAggregator] = None,
        register_defaults: bool = True,
    ) -> None:
        self.validator = validator
        self.config: AppConfig = app_config or validator.config
        self.aggregator = aggregator
        self._routes: Dict[Tuple[str, str], RouteDefinition] = {}
        self.status_counts: Dict[int, int] = {}
        if register_defaults:
            self._register_default_routes()

    # -- route registration -------------------------------------------------
    def register_route(
        self,
        path: str,
        method: str,
        handler: Handler,
        required_permission: Optional[str] = None,
        required_scopes: Tuple[str, ...] = (),
        auth_required: bool = True,
        max_body_bytes: Optional[int] = None,
    ) -> RouteDefinition:
        method = method.upper()
        if method not in SUPPORTED_METHODS:
            raise ConfigValidationError("method", f"unsupported method '{method}'")
        if not path.startswith("/") or len(path) > MAX_PATH_LENGTH:
            raise ConfigValidationError("path", "route path must start with '/' and be short")
        if (path, method) in self._routes:
            raise ConfigValidationError("route", f"duplicate route {method} {path}")

        breaker_cfg = CircuitBreakerConfig(failure_threshold=5, recovery_timeout_s=15.0)
        protected = circuit_breaker(f"route.{method}.{path}", breaker_cfg)(handler)
        definition = RouteDefinition(
            path=path,
            method=method,
            handler=protected,
            required_permission=required_permission,
            required_scopes=required_scopes,
            auth_required=auth_required,
            max_body_bytes=max_body_bytes,
        )
        self._routes[(path, method)] = definition
        return definition

    def route(self, path: str, method: str = "GET", **options: Any) -> Callable[[Handler], Handler]:
        """Decorator form of :meth:`register_route`."""

        def decorator(handler: Handler) -> Handler:
            self.register_route(path, method, handler, **options)
            return handler

        return decorator

    def _register_default_routes(self) -> None:
        self.register_route("/health", "GET", self._handle_health, auth_required=False)
        self.register_route("/metrics", "GET", self._handle_metrics, required_permission="metrics:read")
        self.register_route("/heal", "POST", self._handle_heal, required_permission="healing:trigger",
                            required_scopes=("heal",))
        self.register_route("/patch", "POST", self._handle_patch, required_permission="patch:apply",
                            max_body_bytes=self.config.security.max_patch_size_bytes)
        self.register_route("/rollback", "POST", self._handle_rollback, required_permission="rollback:execute")

    # -- internal metric helper --------------------------------------------
    def _emit(self, name: str, value: float = 1.0, **tags: str) -> None:
        if self.aggregator is None:
            return
        try:
            self.aggregator.record(name, value, **tags)
        except TelemetryError:
            pass

    # -- validation ---------------------------------------------------------
    @track_latency("router.validate_request")
    def validate_request(self, request: InboundRequest) -> None:
        policy = self.config.security
        method = request.method.upper()

        if method not in SUPPORTED_METHODS:
            raise ConfigValidationError("method", f"unsupported method '{request.method}'")
        if not request.path.startswith("/") or len(request.path) > MAX_PATH_LENGTH:
            raise ConfigValidationError("path", "malformed request path")
        if len(request.headers) > MAX_HEADER_COUNT:
            raise ResourceExhaustedError("headers", MAX_HEADER_COUNT)
        if len(request.body) > policy.max_patch_size_bytes:
            raise ResourceExhaustedError("request_body", policy.max_patch_size_bytes)

        if request.body and "json" not in request.header("content-type").lower():
            raise ConfigValidationError("content-type", "request bodies must be application/json")

        origin = request.header("origin")
        if origin and policy.allowed_origins and origin not in policy.allowed_origins:
            raise PolicyViolationError("cors", f"origin '{origin}' is not allowed")

    @track_latency("router.resolve_route")
    def resolve_route(self, request: InboundRequest) -> RouteDefinition:
        method = request.method.upper()
        found = self._routes.get((request.path, method))
        if found is not None:
            return found
        path_known = any(path == request.path for path, _ in self._routes)
        raise RouteNotFoundError(request.path, method, method_mismatch=path_known)

    @track_latency("router.authenticate")
    @audit_log("authenticate", LogLevel.INFO)
    def authenticate(self, request: InboundRequest, route: RouteDefinition) -> UserContext:
        authorization = request.header("authorization")

        if not authorization:
            if route.auth_required:
                raise AuthFailedError("Authorization header is required")
            self.validator.check_rate_limit(request.client_ip)
            return UserContext(user_id="anonymous", roles=(), client_ip=request.client_ip,
                               metadata={"anonymous": "true"})

        scheme, _, credential = authorization.partition(" ")
        scheme = scheme.lower()

        if scheme == "bearer":
            if not credential:
                raise AuthFailedError("Bearer token is empty")
            return self.validator.validate_token(credential, client_ip=request.client_ip)

        if scheme == "apikey":
            key_id, sep, secret = credential.partition(":")
            if not sep or not key_id or not secret:
                raise AuthFailedError("ApiKey credentials must be 'key_id:secret'")
            return self.validator.validate_api_key(key_id, secret, client_ip=request.client_ip)

        raise AuthFailedError(f"Unsupported authorization scheme '{scheme}'")

    @track_latency("router.authorize")
    @audit_log("authorize_route", LogLevel.DEBUG)
    def authorize_route(self, user: UserContext, route: RouteDefinition) -> None:
        if route.required_permission is not None:
            self.validator.authorize(user, route.required_permission)
        for scope in route.required_scopes:
            self.validator.require_scope(user, scope)

    def build_context(self, request: InboundRequest, user: UserContext,
                      route: RouteDefinition) -> RouteContext:
        if route.max_body_bytes is not None and len(request.body) > route.max_body_bytes:
            raise ResourceExhaustedError(f"body:{route.path}", route.max_body_bytes)
        payload = request.json_body()
        context = RouteContext(request=request, user=user, route=route, payload=payload)
        context.attributes["claims_hint"] = self._claims_hint(user)
        return context

    @staticmethod
    def _claims_hint(user: UserContext) -> JWTPayload:
        """Summarise an authenticated user as a short-lived JWTPayload."""
        now = time.time()
        return JWTPayload(
            sub=user.user_id,
            iss="router",
            aud="handler",
            exp=now + 30.0,
            iat=now,
            roles=user.roles,
            scopes=user.scopes,
        )

    @staticmethod
    def verify_service_credential(credential: APICredential) -> None:
        """Reject inactive service credentials before they are trusted."""
        if not credential.is_active():
            raise AuthFailedError(f"Service credential '{credential.key_id}' is inactive")
        if not credential.scopes:
            raise PolicyViolationError("service_credential", "credential has no scopes")

    # -- dispatch -----------------------------------------------------------
    @audit_log("process_request", LogLevel.INFO)
    def _process(self, request: InboundRequest) -> OutboundResponse:
        self.validate_request(request)
        route = self.resolve_route(request)
        user = self.authenticate(request, route)
        self.authorize_route(user, route)
        context = self.build_context(request, user, route)
        result = route.handler(context)
        response = OutboundResponse.ok(result, request_id=request.request_id)
        response.headers["X-Trace-Id"] = context.trace_id
        return response

    @track_latency("router.dispatch")
    def dispatch(self, request: InboundRequest) -> OutboundResponse:
        start = time.perf_counter()
        try:
            response = self._process(request)
        except HealingEngineError as exc:
            response = self._error_response(exc, request.request_id)
        except Exception as exc:  # unexpected failure: wrap without leaking internals
            wrapped = HealingEngineError(f"Unhandled {type(exc).__name__}", code=1999)
            response = OutboundResponse.from_error(500, wrapped, request.request_id)

        response.latency_ms = (time.perf_counter() - start) * 1000.0
        self.status_counts[response.status] = self.status_counts.get(response.status, 0) + 1
        self._emit("router.requests", status=str(response.status), path=request.path)
        return response

    def _error_response(self, exc: HealingEngineError, request_id: str) -> OutboundResponse:
        status = 500
        for exc_type, code in self.ERROR_STATUS.items():
            if isinstance(exc, exc_type):
                status = code
                break
        headers: Dict[str, str] = {}
        if isinstance(exc, RateLimitExceededError):
            headers["Retry-After"] = str(int(exc.retry_after) + 1)
        if isinstance(exc, RouteNotFoundError) and exc.method_mismatch:
            status = 405
        if isinstance(exc, AuthFailedError):
            headers["WWW-Authenticate"] = "Bearer"
        return OutboundResponse.from_error(status, exc, request_id, headers)

    # -- default handlers ---------------------------------------------------
    @track_latency("handler.health")
    def _handle_health(self, ctx: RouteContext) -> Dict[str, Any]:
        return {"status": "ok", "environment": self.config.environment.value}

    @track_latency("handler.metrics")
    def _handle_metrics(self, ctx: RouteContext) -> Dict[str, Any]:
        totals = self.aggregator.totals() if self.aggregator else {}
        return {"totals": totals, "status_counts": dict(self.status_counts)}

    @track_latency("handler.heal")
    @audit_log("trigger_healing", LogLevel.WARNING)
    def _handle_heal(self, ctx: RouteContext) -> Dict[str, Any]:
        target = ctx.payload.get("target")
        if not isinstance(target, str) or not target:
            raise ConfigValidationError("target", "a non-empty string is required")
        raw_strategy = ctx.payload.get("strategy", HealingStrategy.RESTART.value)
        try:
            strategy = HealingStrategy(raw_strategy)
        except ValueError as exc:
            raise ConfigValidationError("strategy", f"unknown strategy '{raw_strategy}'") from exc
        if strategy not in self.config.healing.strategies:
            raise PolicyViolationError("healing_strategy", strategy.value)
        return {"accepted": True, "target": target, "strategy": strategy.value, "by": ctx.user.user_id}

    @track_latency("handler.patch")
    @audit_log("apply_patch", LogLevel.WARNING)
    def _handle_patch(self, ctx: RouteContext) -> Dict[str, Any]:
        patch_id = ctx.payload.get("patch_id")
        if not patch_id:
            raise ConfigValidationError("patch_id", "is required")
        if self.config.security.require_signed_patches and not ctx.payload.get("signature"):
            raise PolicyViolationError("signed_patches", "unsigned patch rejected")
        return {"applied": True, "patch_id": patch_id}

    @track_latency("handler.rollback")
    @audit_log("rollback", LogLevel.WARNING)
    def _handle_rollback(self, ctx: RouteContext) -> Dict[str, Any]:
        patch_id = ctx.payload.get("patch_id")
        if not patch_id:
            raise ConfigValidationError("patch_id", "is required")
        return {"rolled_back": True, "patch_id": patch_id}

    # -- introspection ------------------------------------------------------
    def list_routes(self) -> List[Tuple[str, str]]:
        return sorted(self._routes.keys())


def build_dispatcher(secret: str, app_config: Optional[AppConfig] = None,
                     aggregator: Optional[MetricsAggregator] = None) -> RequestDispatcher:
    """Construct a ``RequestDispatcher`` with its own ``TokenValidator``."""
    cfg = app_config or create_app_config()
    validator = TokenValidator(secret, app_config=cfg, aggregator=aggregator)
    return RequestDispatcher(validator, app_config=cfg, aggregator=aggregator)

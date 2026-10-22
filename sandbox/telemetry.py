"""sandbox/telemetry.py

Telemetry layer for the autonomous AI healing engine.

Contains:
  * three decorators: ``track_latency``, ``audit_log`` and ``circuit_breaker``
  * five mock sink integrations (Datadog, Prometheus, AWS Timestream,
    Splunk, ELK) that simulate external metric backends
  * ``MetricsAggregator``, which fans metrics out to the configured sinks
"""
from __future__ import annotations

import functools
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, TypeVar

from sandbox.config import (
    AppConfig,
    CircuitBreakerConfig,
    CircuitOpenError,
    ConfigValidationError,
    LogLevel,
    ResourceExhaustedError,
    TelemetryConfig,
    TelemetryError,
    create_app_config,
)

F = TypeVar("F", bound=Callable[..., Any])

# Shared module-level state used by the decorators.
LATENCY_REGISTRY: Dict[str, List[float]] = defaultdict(list)
AUDIT_TRAIL: Deque[Dict[str, Any]] = deque(maxlen=1000)
_BREAKER_STATE: Dict[str, "BreakerState"] = {}


# ---------------------------------------------------------------------------
# Supporting data structures
# ---------------------------------------------------------------------------
@dataclass
class MetricPoint:
    """A single metric sample."""

    name: str
    value: float
    timestamp: float = field(default_factory=time.time)
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class BreakerState:
    """Mutable runtime state backing one circuit breaker."""

    failures: int = 0
    opened_at: Optional[float] = None
    half_open_calls: int = 0

    def is_open(self, cfg: CircuitBreakerConfig, now: float) -> bool:
        if self.opened_at is None:
            return False
        if now - self.opened_at >= cfg.recovery_timeout_s:
            return False
        return True


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def track_latency(metric_name: Optional[str] = None) -> Callable[[F], F]:
    """Record wall-clock latency (ms) of the wrapped callable."""

    def decorator(func: F) -> F:
        name = metric_name or f"latency.{func.__module__}.{func.__qualname__}"

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                LATENCY_REGISTRY[name].append(elapsed_ms)

        return wrapper  # type: ignore[return-value]

    return decorator


def audit_log(action: str, level: LogLevel = LogLevel.INFO) -> Callable[[F], F]:
    """Append an audit record for every call, including failures."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            record: Dict[str, Any] = {
                "action": action,
                "function": func.__qualname__,
                "level": level.value,
                "started_at": time.time(),
                "status": "ok",
            }
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                record["status"] = "error"
                record["error"] = type(exc).__name__
                raise
            finally:
                record["finished_at"] = time.time()
                AUDIT_TRAIL.append(record)

        return wrapper  # type: ignore[return-value]

    return decorator


def circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
) -> Callable[[F], F]:
    """Reject calls with ``CircuitOpenError`` after repeated failures."""

    cfg = config or CircuitBreakerConfig()
    cfg.validate()

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _BREAKER_STATE.setdefault(name, BreakerState())
            now = time.monotonic()

            if state.is_open(cfg, now):
                raise CircuitOpenError(name)

            if state.opened_at is not None:
                # Recovery window elapsed: enter half-open mode.
                if state.half_open_calls >= cfg.half_open_max_calls:
                    raise CircuitOpenError(name)
                state.half_open_calls += 1

            try:
                result = func(*args, **kwargs)
            except cfg.tracked_exceptions:
                state.failures += 1
                if state.failures >= cfg.failure_threshold:
                    state.opened_at = time.monotonic()
                    state.half_open_calls = 0
                raise
            else:
                state.failures = 0
                state.opened_at = None
                state.half_open_calls = 0
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Mock sink integrations
# ---------------------------------------------------------------------------
class MetricSink(ABC):
    """Abstract base for every external metric sink."""

    sink_name: str = "base"

    def __init__(self) -> None:
        self.connected: bool = False
        self.sent: List[MetricPoint] = []

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def emit(self, point: MetricPoint) -> None:
        ...

    def flush(self) -> int:
        count = len(self.sent)
        self.sent.clear()
        return count

    def close(self) -> None:
        self.connected = False

    def _require_connection(self) -> None:
        if not self.connected:
            raise TelemetryError(f"{self.sink_name} sink is not connected")


class DatadogSink(MetricSink):
    sink_name = "datadog"

    def __init__(self, api_key: str = "dd-mock-key", site: str = "datadoghq.com") -> None:
        super().__init__()
        self.api_key = api_key
        self.site = site

    def connect(self) -> None:
        self.connected = True

    def emit(self, point: MetricPoint) -> None:
        self._require_connection()
        self.sent.append(point)

    def submit_event(self, title: str, text: str) -> Dict[str, str]:
        return {"title": title, "text": text, "site": self.site}


class PrometheusSink(MetricSink):
    sink_name = "prometheus"

    def __init__(self, port: int = 9090) -> None:
        super().__init__()
        self.port = port
        self.gauges: Dict[str, float] = {}

    def connect(self) -> None:
        self.connected = True

    def emit(self, point: MetricPoint) -> None:
        self._require_connection()
        self.gauges[point.name] = point.value
        self.sent.append(point)

    def render_exposition(self) -> str:
        return "\n".join(f"{k} {v}" for k, v in sorted(self.gauges.items()))


class AWSTimestream(MetricSink):
    sink_name = "aws_timestream"

    def __init__(self, database: str = "healer", table: str = "metrics", region: str = "ap-south-1") -> None:
        super().__init__()
        self.database = database
        self.table = table
        self.region = region

    def connect(self) -> None:
        self.connected = True

    def emit(self, point: MetricPoint) -> None:
        self._require_connection()
        self.sent.append(point)

    def write_records(self, points: List[MetricPoint]) -> int:
        self._require_connection()
        self.sent.extend(points)
        return len(points)


class SplunkForwarder(MetricSink):
    sink_name = "splunk"

    def __init__(self, hec_token: str = "splunk-mock-token", index: str = "main") -> None:
        super().__init__()
        self.hec_token = hec_token
        self.index = index

    def connect(self) -> None:
        self.connected = True

    def emit(self, point: MetricPoint) -> None:
        self._require_connection()
        self.sent.append(point)

    def forward_event(self, payload: Dict[str, Any]) -> bool:
        self._require_connection()
        return bool(payload)


class ELKSink(MetricSink):
    sink_name = "elk"

    def __init__(self, hosts: Optional[List[str]] = None, index_prefix: str = "healer-metrics") -> None:
        super().__init__()
        self.hosts = hosts or ["http://localhost:9200"]
        self.index_prefix = index_prefix

    def connect(self) -> None:
        self.connected = True

    def emit(self, point: MetricPoint) -> None:
        self._require_connection()
        self.sent.append(point)

    def bulk_index(self, points: List[MetricPoint]) -> int:
        self._require_connection()
        self.sent.extend(points)
        return len(points)


# ---------------------------------------------------------------------------
# Central aggregator
# ---------------------------------------------------------------------------
class MetricsAggregator:
    """Collects metrics and fans them out to every registered sink."""

    def __init__(
        self,
        config: Optional[TelemetryConfig] = None,
        sinks: Optional[List[MetricSink]] = None,
        buffer_limit: int = 5000,
    ) -> None:
        self.config: TelemetryConfig = config or create_app_config().telemetry
        self.config.validate()
        self.sinks: List[MetricSink] = sinks if sinks is not None else self._default_sinks()
        self.buffer_limit = buffer_limit
        self._buffer: List[MetricPoint] = []
        self._counters: Dict[str, float] = defaultdict(float)
        self._started = False

    @staticmethod
    def _default_sinks() -> List[MetricSink]:
        return [DatadogSink(), PrometheusSink(), AWSTimestream(), SplunkForwarder(), ELKSink()]

    def start(self) -> None:
        for sink in self.sinks:
            sink.connect()
        self._started = True

    def stop(self) -> None:
        self.flush()
        for sink in self.sinks:
            sink.close()
        self._started = False

    @audit_log("record_metric", LogLevel.DEBUG)
    def record(self, name: str, value: float, **tags: str) -> None:
        if not self.config.enabled:
            return
        if len(self._buffer) >= self.buffer_limit:
            raise ResourceExhaustedError("metrics_buffer", self.buffer_limit)
        merged = {**self.config.labels, **tags}
        self._buffer.append(MetricPoint(name=name, value=value, tags=merged))
        self._counters[name] += value

    @track_latency("aggregator.flush")
    def flush(self) -> int:
        if not self._started:
            raise TelemetryError("aggregator has not been started")
        delivered = 0
        for point in self._buffer:
            for sink in self.sinks:
                try:
                    sink.emit(point)
                    delivered += 1
                except TelemetryError:
                    continue
        self._buffer.clear()
        return delivered

    def totals(self) -> Dict[str, float]:
        return dict(self._counters)

    def sink_report(self) -> Dict[str, Tuple[bool, int]]:
        return {s.sink_name: (s.connected, len(s.sent)) for s in self.sinks}


def build_aggregator(app_config: Optional[AppConfig] = None) -> MetricsAggregator:
    """Create and start a ``MetricsAggregator`` from an ``AppConfig``."""
    cfg = app_config or create_app_config()
    if not isinstance(cfg, AppConfig):
        raise ConfigValidationError("app_config", "expected an AppConfig instance")
    aggregator = MetricsAggregator(config=cfg.telemetry)
    aggregator.start()
    return aggregator

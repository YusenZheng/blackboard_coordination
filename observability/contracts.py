"""观测端口契约及完全无副作用的默认实现。"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import uuid4


@runtime_checkable
class SpanHandle(Protocol):
    """可作为 context manager 使用的 Span 句柄。"""

    @property
    def span_context(self) -> Mapping[str, str]: ...

    def __enter__(self) -> "SpanHandle": ...

    def __exit__(self, exc_type, exc, traceback) -> bool: ...

    def set_output(self, payload: Any) -> None: ...

    def record_exception(self, exc: BaseException) -> None: ...

    def set_attribute(self, name: str, value: Any) -> None: ...


@runtime_checkable
class ObservabilityPort(Protocol):
    """V2 运行时依赖的最小观测契约。"""

    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        links: list[Any] | None = None,
        input_payload: Any = None,
        **kwargs: Any,
    ) -> AbstractContextManager[SpanHandle]: ...

    def event(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        payload: Any = None,
        level: str = "INFO",
    ) -> str | None: ...

    def counter(
        self,
        name: str,
        value: int | float = 1,
        attributes: Mapping[str, Any] | None = None,
    ) -> None: ...

    def histogram(
        self,
        name: str,
        value: int | float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None: ...

    def flush(self, timeout_s: float = 2.0) -> bool: ...


class _NullSpan(AbstractContextManager["_NullSpan"]):
    @property
    def span_context(self) -> Mapping[str, str]:
        return {}

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def set_output(self, payload: Any) -> None:
        del payload

    def record_exception(self, exc: BaseException) -> None:
        del exc

    def set_attribute(self, name: str, value: Any) -> None:
        del name, value


class NoopObservability:
    """未注入真实实现时使用，确保业务语义不因观测开关而改变。"""

    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        links: list[Any] | None = None,
        input_payload: Any = None,
        **kwargs: Any,
    ) -> _NullSpan:
        del name, attributes, links, input_payload, kwargs
        return _NullSpan()

    def event(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
        payload: Any = None,
        level: str = "INFO",
    ) -> None:
        del name, attributes, payload, level

    def counter(
        self,
        name: str,
        value: int | float = 1,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        del name, value, attributes

    def histogram(
        self,
        name: str,
        value: int | float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        del name, value, attributes

    def flush(self, timeout_s: float = 2.0) -> bool:
        del timeout_s
        return True

    def start_run(
        self,
        run_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        del task_id, kwargs
        return run_id or str(uuid4())

    def end_run(self, run_id: str, status: str = "ok", **kwargs: Any) -> None:
        del run_id, status, kwargs

    def update_run(self, run_id: str, **kwargs: Any) -> None:
        del run_id, kwargs

    def inject_carrier(self, carrier: dict[str, str] | None = None) -> dict[str, str]:
        return carrier if carrier is not None else {}

    def extract_carrier(self, carrier: Mapping[str, str] | None) -> None:
        del carrier
        return None

    def consumer_context(
        self, carrier: Mapping[str, str] | None
    ) -> AbstractContextManager[None]:
        del carrier
        return nullcontext()

    def health(self) -> dict[str, Any]:
        return {
            "status": "disabled",
            "degraded": False,
            "queue_depth": 0,
            "dropped": 0,
        }

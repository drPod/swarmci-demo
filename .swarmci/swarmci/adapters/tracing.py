"""Respan SDK lifecycle and content-free application spans.

The provider instrumentor owns LLM payloads/tokens. Application spans record only
explicit metadata, never serialized browser sessions, cookies, or connector bodies.
"""

import asyncio
import atexit
import inspect
import json
import logging
import os
from contextlib import contextmanager
from functools import wraps
from threading import Lock

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from respan import Respan, get_client, propagate_attributes
from respan_instrumentation_openai import OpenAIInstrumentor

from swarmci.config import settings

log = logging.getLogger(__name__)
_sdk = None
_lock = Lock()
_failed = False


def _dashboard_metadata(span):
    """Bridge SDK propagated JSON metadata to the dashboard's indexed attributes.

    Respan's postprocess callback receives an ended ReadableSpan. Replace its
    attribute mapping rather than mutating the read-only public mapping.
    """
    attrs = dict(span.attributes or {})
    model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")
    if model:
        span._name = f"LLM · {str(model).rsplit('/', 1)[-1]}"
    raw = attrs.get("respan.metadata")
    if not raw:
        return
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(values, dict):
            return
        for key, value in values.items():
            if value is not None:
                attrs.setdefault(
                    f"respan.metadata.{key}", json.dumps(value) if isinstance(value, dict) else value
                )
        span._attributes = attrs
    except (TypeError, ValueError):
        log.warning("Could not normalize Respan metadata")


def initialize():
    global _sdk, _failed
    if not settings.respan_enabled or not settings.respan_api_key or _failed:
        return None
    with _lock:
        if _sdk is None:
            try:
                # Respan 2.20 semantic naming collapses custom tasks to "task".
                # Preserve the descriptive names we supply for the dashboard.
                os.environ["RESPAN_SPAN_NAME_STYLE"] = "legacy"
                _sdk = Respan(
                    api_key=settings.respan_api_key,
                    base_url=settings.respan_endpoint or settings.respan_base_url,
                    app_name="swarmci",
                    instrumentations=[OpenAIInstrumentor()],
                    is_auto_instrument=False,
                    metadata={"service": "swarmci"},
                    log_level="WARNING",
                    auto_flush="off",
                    span_postprocess_callback=_dashboard_metadata,
                )
                atexit.register(shutdown)
            except Exception:
                _failed = True
                log.warning("Respan initialization failed; continuing without telemetry")
    return _sdk


def flush():
    if _sdk is not None:
        try:
            _sdk.flush()
        except Exception:
            log.warning("Respan flush failed")


def shutdown():
    global _sdk
    if _sdk is not None:
        try:
            _sdk.flush()
            _sdk.shutdown()
        except Exception:
            log.warning("Respan shutdown failed")
        finally:
            _sdk = None


def status():
    if not settings.respan_enabled:
        return "disabled"
    if not settings.respan_api_key:
        return "not configured"
    return "initialization failed" if _failed else "initialized" if _sdk else "configured"


def rename(name):
    trace.get_current_span().update_name(name)


def attributes(**values):
    span = trace.get_current_span()
    for key, value in values.items():
        if value is not None:
            span.set_attribute(key, value)
    if span.is_recording() and _sdk is not None:
        get_client().update_current_span(
            respan_params={"metadata": {k: v for k, v in values.items() if v is not None}}
        )


def summary(*, inputs=None, outputs=None):
    active = trace.get_current_span()
    if inputs is not None:
        active.set_attribute("traceloop.entity.input", json.dumps(inputs, default=str))
    if outputs is not None:
        active.set_attribute("traceloop.entity.output", json.dumps(outputs, default=str))


def outcome(value, *, error=False, field="outcome"):
    attributes(**{field: value})
    if error:
        trace.get_current_span().set_status(Status(StatusCode.ERROR, value))


@contextmanager
def span(name, *, kind="task", root=False, **metadata):
    if initialize() is None:
        yield trace.INVALID_SPAN
        return
    context = {"metadata": metadata}
    if metadata.get("run_id"):
        context["thread_identifier"] = metadata["run_id"]
    token = otel_context.attach(otel_context.Context()) if root else None
    try:
        with propagate_attributes(**context):
            with get_client().start_span(name=name, kind=kind) as active:
                rename(name)
                attributes(**metadata)
                if metadata:
                    summary(inputs=metadata)
                try:
                    yield active
                except asyncio.CancelledError:
                    outcome("cancelled")
                    raise
    finally:
        if token is not None:
            otel_context.detach(token)


def traced(name, *, kind="task", metadata=None, root=False):
    """Use Respan's imperative API without decorators serializing function arguments."""

    def decorate(fn):
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def asynchronous(*args, **kwargs):
                values = metadata(*args, **kwargs) if metadata else {}
                with span(name, kind=kind, root=root, **values):
                    return await fn(*args, **kwargs)

            return asynchronous

        @wraps(fn)
        def synchronous(*args, **kwargs):
            values = metadata(*args, **kwargs) if metadata else {}
            with span(name, kind=kind, root=root, **values):
                return fn(*args, **kwargs)

        return synchronous

    return decorate

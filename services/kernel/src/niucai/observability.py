import threading

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

_lock = threading.Lock()
_configured = False


def configure():
    global _configured
    with _lock:
        if _configured:
            return
        trace.set_tracer_provider(TracerProvider(resource=Resource.create({"service.name": "niucai-kernel"})))
        _configured = True

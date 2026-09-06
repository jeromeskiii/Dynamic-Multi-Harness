"""DMH - Dynamic Multi-Harness control plane.

A host owns authoritative session state (append-only log, inbox, approval,
sandbox world) and swaps replaceable harness runtimes behind a frozen ABI.
One source of model-visible truth. Many replaceable drivers.
"""

from .errors import HarnessError
from .provider import HarnessProvider, StreamItem
from .capabilities import effective as effective_capabilities
from .host import Host, RuntimeSpec
from . import ev1h_runtime, cognihak_runtime, qwen_runtime

__version__ = "0.1.0"

__all__ = [
    "HarnessError",
    "HarnessProvider",
    "Host",
    "RuntimeSpec",
    "StreamItem",
    "effective_capabilities",
    "ev1h_runtime",
    "cognihak_runtime",
    "qwen_runtime",
]


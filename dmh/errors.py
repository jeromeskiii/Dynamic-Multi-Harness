"""Stable error surface for the harness ABI (design doc, section 7).

Layer-0 exit codes live in handshake.py; these are the JSON-RPC errorCodes.
"""

RPC_PROTOCOL_BASE = -32001
JSONRPC_METHOD_NOT_FOUND = -32601

ERROR_CODES = (
    "ABI_UNSUPPORTED",
    "ABI_MISMATCH",
    "ABI_TOO_OLD",
    "CAPABILITY_REQUIRED",
    "AUTH_REQUIRED",
    "NOT_INITIALIZED",
    "ALREADY_INITIALIZED",
    "HANDSHAKE_MALFORMED",
    "STDOUT_POLLUTION",
    "BIND_NOT_LOOPBACK",
    "DIGEST_MISMATCH",
    "BROKER_MUX_UNSUPPORTED",
    "HANDSHAKE_TIMEOUT",
    "INITIALIZE_TIMEOUT",
    "CONFIGURE_TIMEOUT",
    "STOP_TIMEOUT",
    "TURN_OPEN",
    "DELEGATION_DEPTH_EXCEEDED",
    "RUNTIME_FAULT",
    "METHOD_NOT_FOUND",
)


class HarnessError(Exception):
    """Deterministic protocol failure carrying a stable errorCode string."""

    def __init__(self, code, message, data=None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown errorCode: {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = dict(data or {})

    def to_rpc_error(self):
        return {
            "code": RPC_PROTOCOL_BASE,
            "message": self.message,
            "data": {"errorCode": self.code, **self.data},
        }

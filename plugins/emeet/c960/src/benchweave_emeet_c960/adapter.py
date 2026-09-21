"""Synthetic OTDP adapter for the EMeet SmartCam C960 4K UVC webcam.

Reads the declared parameter list from the descriptor and models identify/read/
write over a synthetic text control channel. No device I/O; not hardware
qualified. A real integration would replace protocol.py with V4L2 control
(``v4l2-ctl``) and capture (``ffmpeg``) via the working MCP service.
"""
import math
from .protocol import transaction, parse_identity, parse_value


def create_plugin():
    return Plugin()


class Plugin:
    def __init__(self):
        self.services = None
        self.closed = False
        self._params = {}

    async def open(self, descriptor, services, context):
        if self.services is not None or self.closed:
            raise RuntimeError("Use a fresh plugin instance")
        self.services = services
        self._params = {p["name"]: p for p in descriptor.get("parameters", [])}

    async def execute(self, request, context):
        verb = request.get("verb")
        operation_id = request.get("operation_id")
        dispatched = False

        def failure(code, message, uncertain=False):
            return {"operation_id": operation_id, "verb": verb,
                    "status": "unknown" if uncertain else "error",
                    "error": {"code": code, "message": message,
                              "dispatch_state": "unknown" if uncertain else "not_dispatched"}}

        def remaining():
            deadline = context.deadline_monotonic
            if (not math.isfinite(deadline) or context.is_cancelled()
                    or self.services.monotonic() >= deadline):
                raise TimeoutError("Cancelled or expired")

        if self.services is None or self.closed:
            return failure("INTERNAL_ERROR", "Plugin is not open")
        if operation_id != context.operation_id:
            return failure("INVALID_ARGUMENT", "Context identity mismatch")
        if verb not in ("identify", "read", "write"):
            return failure("UNSUPPORTED", "Only identify, read and write are supported")
        if (set(request) != {"operation_id", "verb", "arguments"}
                or not isinstance(operation_id, str) or not operation_id):
            return failure("INVALID_ARGUMENT", "Invalid envelope")

        if verb == "identify":
            if request["arguments"] != {}:
                return failure("INVALID_ARGUMENT", "Identify takes no arguments")
            exchange = transaction("identify")
        elif verb == "read":
            if set(request["arguments"]) != {"parameter"}:
                return failure("INVALID_ARGUMENT", "Read requires a single parameter")
            name = request["arguments"]["parameter"]
            param = self._params.get(name)
            if param is None:
                return failure("INVALID_ARGUMENT", "Unknown parameter %r" % name)
            exchange = transaction("read", name)
        else:  # write
            if set(request["arguments"]) != {"parameter", "value"}:
                return failure("INVALID_ARGUMENT", "Write requires parameter and value")
            name = request["arguments"]["parameter"]
            value = request["arguments"]["value"]
            param = self._params.get(name)
            if param is None:
                return failure("INVALID_ARGUMENT", "Unknown parameter %r" % name)
            if not self._check_value(param, value):
                return failure("INVALID_ARGUMENT", "Value %r invalid for %s" % (value, name))
            exchange = transaction("write", name, value)

        try:
            remaining()
            await context.mark_dispatch_started()
            dispatched = True
            response = await self.services.transfer(exchange, context)
            remaining()
            if verb == "identify":
                data = parse_identity(response["data"])
            elif verb == "read":
                data = {"parameter": name, "value": parse_value(param["type"], response["data"]),
                        "unit": param.get("unit"), "observed_at": self.services.utc_now(),
                        "age_ms": 0, "quality": "valid", "source": "device"}
            else:
                data = {"parameter": name, "requested_value": value,
                        "effective_value": value, "assurance": "acknowledged",
                        "verification": None}
            return {"operation_id": operation_id, "verb": verb, "status": "ok", "data": data}
        except TimeoutError:
            return failure("TIMEOUT", "Deadline or cancellation", dispatched)
        except ConnectionError:
            return failure("TRANSPORT_ERROR", "Connection lost", dispatched)
        except (ValueError, KeyError, TypeError):
            return failure("PROTOCOL_ERROR", "Invalid device response", dispatched)
        except RuntimeError:
            return failure("INTERNAL_ERROR", "Host resource or internal failure", dispatched)

    async def next_event(self, subscription_id, context):
        return None

    async def close(self, context):
        if self.closed:
            return
        if self.services is not None:
            await self.services.close_transport(context)
        self.closed = True

    @staticmethod
    def _check_value(param, value):
        ptype = param["type"]
        if ptype == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            lo, hi = param["range"]
            return lo <= value <= hi
        if ptype == "bool":
            return isinstance(value, bool)
        # enum
        return isinstance(value, str) and value in param["enum_values"]

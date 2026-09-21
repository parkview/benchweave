"""Synthetic V4L2-style control exchanges only; this is not a camera driver.

The EMeet C960 is a plain UVC device with no vendor protocol, so this module
models a hypothetical text control channel purely so the adapter contract can be
exercised without hardware. Every exchange here is synthetic evidence, not a
claimed device response.
"""


def transaction(verb, parameter=None, value=None):
    """Build the synthetic exchange envelope for a verb."""
    if verb == "identify":
        data = b"ID?\n"
    elif verb == "read":
        data = f"GET {parameter}\n".encode("ascii")
    else:  # write
        data = f"SET {parameter}={value}\n".encode("ascii")
    return {"kind": "stream_exchange", "data": data, "max_bytes": 256,
            "termination": "lf", "exact_bytes": None}


def parse_identity(raw):
    """Parse the synthetic identify response into an OTDP identity object.

    The camera has no device-readable identity string, so serial/firmware are
    unknown and the source is commissioned (the descriptor declares
    firmware_policy "commissioned").
    """
    if raw != b"EMeet,SmartCam C960 4K\n":
        raise ValueError("Unexpected identity")
    return {"manufacturer": "EMeet", "model": "SmartCam C960 4K",
            "serial": None, "firmware": None, "source": "commissioned"}


def parse_value(ptype, raw):
    """Parse a synthetic GET response into a Python value of the declared type."""
    if not isinstance(raw, bytes) or len(raw) > 256 or not raw.endswith(b"\n"):
        raise ValueError("Incomplete frame")
    text = raw[:-1].decode("ascii")
    if ptype == "int":
        return int(text)
    if ptype == "bool":
        if text not in ("0", "1"):
            raise ValueError("Bad boolean")
        return text == "1"
    # enum
    return text

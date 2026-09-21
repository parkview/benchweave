import pytest
from benchweave_emeet_c960.protocol import transaction, parse_identity, parse_value


def test_transaction_shapes():
    assert transaction("identify") == {"kind": "stream_exchange", "data": b"ID?\n",
                                       "max_bytes": 256, "termination": "lf", "exact_bytes": None}
    assert transaction("read", "brightness")["data"] == b"GET brightness\n"
    assert transaction("write", "brightness", 32)["data"] == b"SET brightness=32\n"


def test_parse_identity():
    assert parse_identity(b"EMeet,SmartCam C960 4K\n") == {
        "manufacturer": "EMeet", "model": "SmartCam C960 4K",
        "serial": None, "firmware": None, "source": "commissioned"}


def test_parse_value_int():
    assert parse_value("int", b"0\n") == 0
    assert parse_value("int", b"-64\n") == -64


def test_parse_value_bool():
    assert parse_value("bool", b"1\n") is True
    assert parse_value("bool", b"0\n") is False


def test_parse_value_enum():
    assert parse_value("enum", b"50 Hz\n") == "50 Hz"


def test_parse_value_rejects_bad_frames():
    with pytest.raises(ValueError):
        parse_value("int", b"nan\n")
    with pytest.raises(ValueError):
        parse_value("int", b"3.3")  # no terminating newline
    with pytest.raises(ValueError):
        parse_value("bool", b"2\n")

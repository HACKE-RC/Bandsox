import base64
import json

from bandsox.cli import _decode_terminal_frame


def test_decode_terminal_frame_plain_text():
    assert _decode_terminal_frame("root@vm:/# ") == b"root@vm:/# "


def test_decode_terminal_frame_does_not_treat_short_text_as_base64():
    assert _decode_terminal_frame("output") == b"output"


def test_decode_terminal_frame_json_base64():
    payload = {
        "data": base64.b64encode(b"hello\r\n").decode("ascii"),
        "encoding": "base64",
    }
    assert _decode_terminal_frame(json.dumps(payload)) == b"hello\r\n"

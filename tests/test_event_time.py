"""event_time flows through db.post_message and orders reads (uses the app db layer)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "covasant-partyline"))

from app import db


def test_msg_cols_include_three_clocks():
    assert "event_time" in db._MSG_COLS
    assert "received_at" in db._MSG_COLS


def test_post_message_signature_accepts_event_time():
    import inspect
    params = list(inspect.signature(db.post_message).parameters)
    assert params[-1] == "event_time", f"event_time must be the last param, got {params[-1]}"
    assert params[-2] == "summary"

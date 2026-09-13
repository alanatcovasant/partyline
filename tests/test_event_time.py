"""event_time flows through db.post_message and orders reads (uses the app db layer)."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "covasant-partyline"))

from app import db


def test_msg_cols_include_three_clocks():
    assert "event_time" in db._MSG_COLS
    assert "received_at" in db._MSG_COLS


def test_post_message_signature_accepts_event_time():
    """event_time must stay the trailing POSITIONAL arg — the REST layer passes it by position.

    Later additions have to be keyword-only, or they silently steal that slot and the
    bridge's emit timestamp lands in the wrong parameter.
    """
    import inspect
    positional = [
        name
        for name, p in inspect.signature(db.post_message).parameters.items()
        if p.kind is not inspect.Parameter.KEYWORD_ONLY
    ]
    assert positional[-1] == "event_time", f"event_time must be last, got {positional[-1]}"
    assert positional[-2] == "summary"

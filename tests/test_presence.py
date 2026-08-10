"""Pure unit tests for presence classification (no Postgres, no network)."""

import pytest

from app.presence import (
    IDLE,
    LIVE,
    STALE,
    classify,
    delivery_warning,
    describe,
    humanize_age,
)


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (None, "never"),
        (0, "0s"),
        (42, "42s"),
        (59, "59s"),
        (60, "1m"),
        (599, "9m"),
        (3600, "1h"),
        (68400, "19h"),
        (86400, "1d"),
        (540000, "6d"),
    ],
)
def test_humanize_age(seconds, expected):
    assert humanize_age(seconds) == expected


def test_negative_age_from_clock_skew_does_not_render_as_minus():
    assert humanize_age(-5) == "0s"


@pytest.mark.parametrize(
    "heartbeat_age, expected",
    [(0, LIVE), (300, LIVE), (301, IDLE), (3600, IDLE), (3601, STALE), (68400, STALE)],
)
def test_status_comes_from_the_heartbeat_clock(heartbeat_age, expected):
    assert classify(heartbeat_age, read_age_seconds=0)["status"] == expected


def test_reading_comes_from_the_read_clock_independently():
    """The case the roster could never show: alive and posting, but not consuming."""
    busy_but_deaf = classify(heartbeat_age_seconds=10, read_age_seconds=68400)
    assert busy_but_deaf["status"] == LIVE
    assert busy_but_deaf["reading"] is False


def test_never_acked_is_not_reading_and_reports_no_read_age():
    p = classify(heartbeat_age_seconds=10, read_age_seconds=None)
    assert p["read_age_seconds"] is None
    assert p["reading"] is False


def test_thresholds_are_configurable():
    assert classify(400, 0, live_seconds=600, idle_seconds=7200)["status"] == LIVE


def test_missing_heartbeat_degrades_to_stale():
    assert classify(None, None)["status"] == STALE


def test_describe_is_terse_when_everything_is_fine():
    assert describe(classify(1, 1)) == "live"


def test_describe_names_both_clocks_when_something_is_cold():
    text = describe(classify(68400, 68400))
    assert "stale" in text and "19h" in text


def test_no_warning_for_a_live_reading_recipient():
    assert delivery_warning("alan_aura_2", classify(5, 5)) is None


def test_warning_names_the_recipient_and_does_not_claim_message_loss():
    warning = delivery_warning("alan_aura_2", classify(68400, 68400))
    assert "alan_aura_2" in warning
    # The message IS durable — a warning that implied loss would be a lie, and would push
    # senders toward pointless resends.
    assert "stored" in warning
    assert "silence" in warning


def test_warning_fires_for_a_live_agent_that_is_not_reading():
    assert delivery_warning("alan_aura_2", classify(5, None)) is not None

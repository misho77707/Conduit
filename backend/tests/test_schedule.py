"""Pipeline scheduling engine (app/engine/schedule.py): cron validation, next-run math and the due/claim logic.
Pure unit tests against plain objects; the beat-driven end-to-end path (a real overdue pipeline getting a real
job queued) is covered in test_pipelines_api.py, alongside the rest of the pipelines API.
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from app.engine.schedule import STALE_CLAIM_S, ScheduleError, is_due, next_after, validate_cron

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


def _p(**kw) -> SimpleNamespace:
    defaults = dict(enabled=True, schedule="0 2 * * *", output_columns=[{"name": "a"}],
                    schedule_claimed_at=None, next_run_at=None)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_validate_cron_accepts_standard_expressions_and_shortcuts():
    validate_cron("*/5 * * * *")
    validate_cron("0 2 * * *")
    validate_cron("@daily")
    validate_cron("@hourly")


def test_validate_cron_rejects_garbage():
    with pytest.raises(ScheduleError):
        validate_cron("not a cron")
    with pytest.raises(ScheduleError):
        validate_cron("70 * * * *")  # minute out of range
    with pytest.raises(ScheduleError):
        validate_cron("")


def test_next_after_is_deterministic_and_strictly_after_now():
    nxt = next_after("0 2 * * *", NOW)
    assert nxt == dt.datetime(2026, 9, 28, 2, 0, 0, tzinfo=UTC)
    assert nxt > NOW


def test_next_after_handles_shortcuts():
    assert next_after("@hourly", NOW) == dt.datetime(2026, 9, 27, 13, 0, 0, tzinfo=UTC)


def test_next_after_rejects_bad_cron():
    with pytest.raises(ScheduleError):
        next_after("nonsense", NOW)


def test_is_due_false_when_disabled_no_schedule_or_no_cached_preview():
    overdue = NOW - dt.timedelta(hours=1)
    assert not is_due(_p(enabled=False, next_run_at=overdue), NOW)
    assert not is_due(_p(schedule=None, next_run_at=overdue), NOW)
    assert not is_due(_p(output_columns=None, next_run_at=overdue), NOW)  # never previewed - same guard as /run
    assert not is_due(_p(next_run_at=None), NOW)  # nothing scheduled yet


def test_is_due_true_only_once_next_run_at_has_actually_passed():
    assert not is_due(_p(next_run_at=NOW + dt.timedelta(minutes=1)), NOW)
    assert is_due(_p(next_run_at=NOW), NOW)  # exactly due
    assert is_due(_p(next_run_at=NOW - dt.timedelta(minutes=1)), NOW)


def test_is_due_false_while_freshly_claimed_true_once_the_claim_goes_stale():
    fresh = _p(next_run_at=NOW - dt.timedelta(minutes=1), schedule_claimed_at=NOW - dt.timedelta(seconds=STALE_CLAIM_S - 1))
    assert not is_due(fresh, NOW)  # someone (or a killed worker within the grace window) is already running it
    stale = _p(next_run_at=NOW - dt.timedelta(minutes=1), schedule_claimed_at=NOW - dt.timedelta(seconds=STALE_CLAIM_S + 1))
    assert is_due(stale, NOW)  # the claim outlived a worker; another tick may retry it


def test_is_due_treats_a_naive_next_run_at_as_utc():
    """SQLite drops tzinfo on round-trip; a naive timestamp read back must still compare correctly."""
    naive_overdue = dt.datetime(2026, 9, 27, 11, 0, 0)  # one hour before NOW, no tzinfo
    assert is_due(_p(next_run_at=naive_overdue), NOW)

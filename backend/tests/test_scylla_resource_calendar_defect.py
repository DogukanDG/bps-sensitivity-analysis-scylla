"""
A defect in Scylla's resource calendar handling, measured end to end.

When a resource with a narrow, fragmented calendar goes idle, Scylla can wake it
up much later than its next open window -- in the worst case a full week late.
The eligibility plugin made this visible: it restricts each activity to the
resources the model lists for it, and BPIC 2012's busiest activity has only two,
one of which works five hours a week across three windows. That is exactly the
shape that triggers the defect.

Where it comes from, in Scylla's own code:

  SimulationUtils.scheduleNextResourceAvailableEvent picks a timetable window
  with DateTimeUtils.getTimeTableIndexWithinOrNext, then converts it to a
  datetime with DateTimeUtils.getNextZonedDateTime.

  The two disagree. The picker ranks windows by getNextOrSameZonedDateTime,
  which treats a window that opened earlier today as being zero away and so wins
  the ranking. The converter then uses getNextZonedDateTime, which always moves
  strictly forward -- to the same weekday next week.

  Probed over a full week against the three-window calendar below: of 163 idle
  hours, 34 wake the resource later than its next open window, the worst by
  7440 minutes.

The same helper is used by the arrival calendar path, where we worked around it
inside our own plugin. These tests document the resource-calendar half, which we
have not worked around, so the behaviour is attributable rather than mysterious.

The measurement here is behavioural: it drives the engines rather than the Java
helper, so it holds regardless of how the defect is eventually fixed.
"""

import json
from pathlib import Path

import pytest

from src.simulation_pipeline.simulation.scylla import build_sim_config as S
from src.simulation_pipeline.simulation.scylla import run_scylla as R
from test_t1_determinism import has_jar, has_prosimos

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "example_sensitivity_analysis_inputs" / "BPIC_2012"
START_ISO = "2023-01-01T00:00:00+02:00"
CASES = 100
SEED = 100

needs_jar = pytest.mark.skipif(not has_jar(), reason="needs a built scylla.jar")
needs_prosimos = pytest.mark.skipif(not has_prosimos(),
                                    reason="needs prosimos installed")

# The resource with a five-hour week, and the activity that depends on it.
NARROW_RESOURCE = "10188"
DONOR_CALENDAR = "Undifferentiated_calendar"


@pytest.fixture(scope="module")
def model():
    path = MODEL_DIR / "BPIC_2012_train.json"
    if not path.exists():
        pytest.skip("BPIC 2012 model not available")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bpmn():
    return MODEL_DIR / "BPIC_2012_train.bpmn"


def widen(model, resource_id, calendar_id=DONOR_CALENDAR):
    """Move one resource onto a wider calendar, changing nothing else."""
    copy = json.loads(json.dumps(model))
    for profile in copy.get("resource_profiles", []):
        for resource in profile.get("resource_list", []):
            if resource["id"] == resource_id:
                resource["calendar"] = calendar_id
    return copy


def scylla(model, bpmn, **options):
    original = S.build_sim_config
    S.build_sim_config = lambda *a, **k: original(*a, **{**k, **options})
    R.build_sim_config = S.build_sim_config
    try:
        result = R.simulate_sample_scylla(
            sample_id=0, sample_data=model, bpmn_path=bpmn, total_cases=CASES,
            start_iso=START_ISO, jar_path=R.resolve_jar(), seed=SEED, heap="1g",
        )
    finally:
        S.build_sim_config = original
        R.build_sim_config = original
    assert result["error"] is None, result["error"]
    return {r["metric"]: r["avg"] for r in result["process_rows"]}


def prosimos(model, bpmn):
    from src.simulation_pipeline.simulation.simulate_samples import simulate_sample

    result = simulate_sample(
        sample_id=0, sample_data=model, bpmn_path=str(bpmn),
        total_cases=CASES, start_iso=START_ISO,
    )
    assert result["error"] is None, result["error"]
    return {r["metric"]: r["avg"] for r in result["process_rows"]}


def test_the_narrow_calendar_is_still_in_the_model(model):
    """Guards the tests below: they mean nothing if the model changed."""
    calendars = {c["id"]: c for c in model["resource_calendars"]}
    assigned = {
        r["id"]: r.get("calendar")
        for p in model.get("resource_profiles", [])
        for r in p.get("resource_list", [])
    }
    calendar = calendars[assigned[NARROW_RESOURCE]]

    def weekly_hours(cal):
        total = 0.0
        for period in cal["time_periods"]:
            begin = [int(x) for x in period["beginTime"].split(":")]
            end = [int(x) for x in period["endTime"].split(":")]
            total += ((end[0] * 3600 + end[1] * 60 + end[2])
                      - (begin[0] * 3600 + begin[1] * 60 + begin[2])) / 3600.0
        return total

    assert weekly_hours(calendar) < 10, "the narrow calendar is no longer narrow"
    assert len(calendar["time_periods"]) > 1, (
        "the defect needs a fragmented calendar; one window would not trigger it")


@needs_jar
@needs_prosimos
def test_one_narrow_calendar_accounts_for_the_whole_gap(model, bpmn):
    """The measurement.

    With eligibility on, Scylla's cycle time is several times Prosimos's. Moving
    the single five-hour resource onto a wider calendar -- changing nothing else,
    eligibility still on -- brings the two engines back into line. The gap is
    that one calendar, not eligibility.
    """
    narrow_p = prosimos(model, bpmn)
    narrow_s = scylla(model, bpmn, eligibility=True)
    narrow_ratio = narrow_s["cycle_time"] / narrow_p["cycle_time"]

    widened = widen(model, NARROW_RESOURCE)
    wide_p = prosimos(widened, bpmn)
    wide_s = scylla(widened, bpmn, eligibility=True)
    wide_ratio = wide_s["cycle_time"] / wide_p["cycle_time"]

    assert narrow_ratio > 2.0, (
        f"expected Scylla to blow up on the narrow calendar, got "
        f"{narrow_ratio:.2f}x -- the defect may have been fixed")
    assert wide_ratio < 1.5, (
        f"widening the one calendar left the engines {wide_ratio:.2f}x apart, "
        f"so the narrow calendar is not the whole story")
    assert narrow_ratio > wide_ratio * 2, (
        f"{narrow_ratio:.2f}x narrow against {wide_ratio:.2f}x wide is not the "
        f"large separation this test is documenting")


@needs_jar
@needs_prosimos
def test_prosimos_is_untroubled_by_the_same_calendar(model, bpmn):
    """The control.

    Both engines run the same model with the same narrow calendar. If Prosimos
    slowed down too, the calendar would simply be a hard constraint rather than
    something Scylla mishandles.
    """
    narrow = prosimos(model, bpmn)
    widened = prosimos(widen(model, NARROW_RESOURCE), bpmn)

    change = abs(narrow["cycle_time"] - widened["cycle_time"]) / widened["cycle_time"]
    assert change < 1.0, (
        f"Prosimos's cycle time moved {change:.0%} when the calendar was "
        f"widened, so the calendar constrains it too and the comparison above "
        f"attributes too much to Scylla")

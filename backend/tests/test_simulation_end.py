"""
The end date that bounds a Scylla run.

Scylla treats `endDateTime` as optional, and without it
`SimulationUtils.scheduleNextResourceAvailableEvent` has no termination
condition: each resource-availability event schedules the next one, so a run
keeps producing them forever. Measured, one sample went from not finishing in
120 s to 0.9 s once the attribute was present.

The horizon has to clear three separate things, and each was found by a run that
lost cases when it did not:

  - the time the arrivals span
  - the wall-clock stretch imposed by the arrival calendar -- a calendar open
    four hours a week turns a 0.2-day arrival span into ten days
  - the time the resources need to work the cases off, which for one resource on
    a five-hour week is 40 weeks for 200 one-hour cases

Too short truncates the run and Scylla reports fewer cases than were asked for.
Too long costs only unused availability events. The factor is therefore generous
on purpose; its job is to be finite, not tight.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from src.simulation_pipeline.simulation.scylla.build_sim_config import (
    DEFAULT_HORIZON_FACTOR, build_sim_config, simulation_end)
from src.simulation_pipeline.simulation.scylla.distributions import BSIM

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "example_sensitivity_analysis_inputs" / "BPIC_2012"
START = "2023-01-01T00:00:00+02:00"


def q(tag):
    return "{" + BSIM + "}" + tag


def days(end_iso, start_iso=START):
    return (dt.datetime.fromisoformat(end_iso)
            - dt.datetime.fromisoformat(start_iso)).total_seconds() / 86400


@pytest.fixture(scope="module")
def model():
    path = MODEL_DIR / "BPIC_2012_train.json"
    if not path.exists():
        pytest.skip("BPIC 2012 model not available")
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_attribute_is_emitted(model):
    """Without it the run never terminates, so its absence is the bug."""
    root = build_sim_config(model, MODEL_DIR / "BPIC_2012_train.bpmn",
                            total_cases=3000, start_iso=START, seed=1)
    sim = root.find(q("simulationConfiguration"))
    assert sim.get("endDateTime"), "no endDateTime: availability events never stop"
    assert days(sim.get("endDateTime")) > 0


def test_horizon_covers_the_arrival_span(model):
    """The last case has to be able to arrive."""
    copy = json.loads(json.dumps(model))
    copy["arrival_time_distribution"] = {
        "distribution_name": "fix",
        "distribution_params": [{"value": 600.0}],
    }
    copy["arrival_time_calendar"] = [
        {"from": d, "to": d, "beginTime": "00:00:00", "endTime": "23:59:59"}
        for d in ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
                  "SATURDAY", "SUNDAY")
    ]
    span = 600.0 * 1000 / 86400          # 6.9 days of arrivals, calendar always open
    assert days(simulation_end(copy, 1000, START)) >= span


def test_a_narrow_arrival_calendar_stretches_the_horizon(model):
    """Cases arrive only while the calendar is open.

    Four hours a week is 1/42 of the time, so the same arrivals take 42x longer
    in wall-clock. Ignoring this cut smoke runs off mid-arrival: Scylla reported
    71 of 100 cases.
    """
    narrow = json.loads(json.dumps(model))
    narrow["arrival_time_distribution"] = {
        "distribution_name": "fix", "distribution_params": [{"value": 211.5}]}
    narrow["arrival_time_calendar"] = [
        {"from": "MONDAY", "to": "MONDAY",
         "beginTime": "09:00:00", "endTime": "13:00:00"}]     # 4 h/week

    wide = json.loads(json.dumps(narrow))
    wide["arrival_time_calendar"] = [
        {"from": d, "to": d, "beginTime": "00:00:00", "endTime": "23:59:59"}
        for d in ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
                  "SATURDAY", "SUNDAY")
    ]

    narrow_days = days(simulation_end(narrow, 100, START))
    wide_days = days(simulation_end(wide, 100, START))
    assert narrow_days > wide_days * 5, (
        f"narrow calendar gave {narrow_days:.1f} days against {wide_days:.1f} "
        f"for an always-open one; the stretch is not being applied")
    # 100 cases at 211.5 s is 0.24 days of arrivals, 10.3 days of wall clock.
    assert narrow_days >= 10.3


def test_horizon_covers_slow_resources(model):
    """Capacity, not arrivals, can be what a run waits on.

    One resource on a five-hour week serving 200 one-hour cases needs 40 weeks.
    An arrivals-only horizon gave six and the run lost four fifths of its cases
    -- caught by test_t4_calendars.
    """
    slow = json.loads(json.dumps(model))
    slow["arrival_time_distribution"] = {
        "distribution_name": "fix", "distribution_params": [{"value": 1800.0}]}
    slow["resource_calendars"] = [{
        "id": "narrow",
        "time_periods": [{"from": "MONDAY", "to": "MONDAY",
                          "beginTime": "09:00:00", "endTime": "14:00:00"}],
    }]
    slow["resource_profiles"] = [{
        "id": "p", "name": "p",
        "resource_list": [{"id": "r1", "name": "r1", "amount": 1,
                           "cost_per_hour": 1, "calendar": "narrow",
                           "assigned_tasks": []}],
    }]
    for task in slow["task_resource_distribution"]:
        task["resources"] = [{"resource_id": "r1", "distribution_name": "fix",
                              "distribution_params": [{"value": 3600.0}]}]

    # Six activities at an hour each, 200 cases, five hours a week available.
    weeks_needed = 200 * 6 * 3600 / 3600 / 5
    assert days(simulation_end(slow, 200, START)) >= weeks_needed * 7


def test_a_model_without_an_arrival_rate_gets_no_attribute(model):
    """Rather than guess a horizon, leave it off and behave as Scylla did."""
    broken = json.loads(json.dumps(model))
    broken["arrival_time_distribution"] = {"distribution_name": "fix",
                                           "distribution_params": []}
    broken["task_resource_distribution"] = []
    broken["resource_profiles"] = []
    assert simulation_end(broken, 100, START) is None


def test_the_floor_is_a_week(model):
    """Weekly calendars need at least one week to come round."""
    fast = json.loads(json.dumps(model))
    fast["arrival_time_distribution"] = {
        "distribution_name": "fix", "distribution_params": [{"value": 0.5}]}
    fast["task_resource_distribution"] = []
    assert days(simulation_end(fast, 2, START)) >= 7


def test_the_factor_scales_the_horizon(model):
    """The factor is the safety margin; halving it should halve the horizon."""
    full = days(simulation_end(model, 3000, START, DEFAULT_HORIZON_FACTOR))
    half = days(simulation_end(model, 3000, START, DEFAULT_HORIZON_FACTOR / 2))
    assert half == pytest.approx(full / 2, rel=0.01)

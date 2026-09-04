"""
All three plugins active at once, checked end to end.

Each plugin has its own test file, but until this was written nothing exercised
them together on a real model. They interact: eligibility narrows which resources
an activity can use, per-resource durations decide how long each takes, and the
arrival calendar decides when work shows up. A regression in one shows up as a
plausible number rather than a failure, which is how the arrival-stacking bug
survived three rounds of measurement.

One reading caveat this file has to encode, because it caused a false alarm.
Scylla's XES logger converts timestamps through java.util.Date
(`XESLogger.java:119`), and Date carries no zone, so the log is written in the
JVM's system timezone rather than the `zoneOffset` from the global config. The
instants are correct; only their rendering differs. Comparing the local clock in
the log against a calendar defined in the model's zone reports arrivals outside
their window that are in fact inside it -- 53 of 300 on this machine. Always
convert to the declared zone before comparing against a calendar.
"""

import collections
import datetime as dt
import json
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.simulation_pipeline.simulation.scylla import build_sim_config as S
from test_t1_determinism import has_jar

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "example_sensitivity_analysis_inputs" / "BPIC_2012"
START_ISO = "2023-01-01T00:00:00+02:00"
# The zone START_ISO and the global config declare. See the caveat above.
MODEL_TZ = dt.timezone(dt.timedelta(hours=2))
CASES = 300
SEED = 100

WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
            "SATURDAY", "SUNDAY"]

needs_jar = pytest.mark.skipif(not has_jar(), reason="needs a built scylla.jar")


@pytest.fixture(scope="module")
def model():
    path = MODEL_DIR / "BPIC_2012_train.json"
    if not path.exists():
        pytest.skip("BPIC 2012 model not available")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bpmn():
    return MODEL_DIR / "BPIC_2012_train.bpmn"


@pytest.fixture(scope="module")
def run(model, bpmn, tmp_path_factory):
    """One run with all three plugins on, shared by every test here."""
    if not has_jar():
        pytest.skip("needs a built scylla.jar")
    from src.simulation_pipeline.simulation.scylla import run_scylla as R

    out = tmp_path_factory.mktemp("all_plugins")
    original = S.build_sim_config
    S.build_sim_config = lambda *a, **k: original(*a, **{
        **k, "eligibility": True, "arrival_calendar": True,
        "resource_durations": True})
    R.build_sim_config = S.build_sim_config
    try:
        result = R.simulate_sample_scylla(
            sample_id=0, sample_data=model, bpmn_path=bpmn, total_cases=CASES,
            start_iso=START_ISO, jar_path=R.resolve_jar(), seed=SEED, heap="1g",
            keep_output=out,
        )
    finally:
        S.build_sim_config = original
        R.build_sim_config = original

    assert result["error"] is None, result["error"]
    root = ET.parse(out / "sample_00000" / "model.xes").getroot()
    return result, root


def events(root):
    for element in root.iter():
        if element.tag.split("}")[-1] == "event":
            yield {f.get("key"): f.get("value") for f in list(element)}


def case_starts(root):
    starts = []
    for trace in root.findall(".//{*}trace"):
        stamps = [dt.datetime.fromisoformat(f.get("value"))
                  for event in trace.findall("{*}event")
                  for f in list(event) if f.get("key") == "time:timestamp"]
        if stamps:
            starts.append(min(stamps))
    return sorted(starts)


# --------------------------------------------------------------------------
# Plugin 3 — eligibility
# --------------------------------------------------------------------------

@needs_jar
def test_no_activity_uses_an_ineligible_resource(run, model, bpmn):
    _, root = run
    names = S.read_bpmn(bpmn)["task_names"]
    declared = {names.get(t["task_id"]): {r["resource_id"] for r in t["resources"]}
                for t in model["task_resource_distribution"]}

    observed = collections.defaultdict(set)
    for fields in events(root):
        activity, resource = fields.get("concept:name"), fields.get("org:resource")
        if activity in declared and resource:
            observed[activity].add(resource.rsplit("__", 1)[-1])

    assert observed, "no resource assignments logged"
    for activity, used in observed.items():
        assert used <= declared[activity], (
            f"{activity} was performed by "
            f"{sorted(used - declared[activity])}, which the model does not "
            f"list for it")


# --------------------------------------------------------------------------
# Plugin 2 — arrival calendar
# --------------------------------------------------------------------------

@needs_jar
def test_every_case_arrives_inside_the_calendar(run, model):
    calendar = model["arrival_time_calendar"]

    def inside(moment):
        local = moment.astimezone(MODEL_TZ)
        day = WEEKDAYS[local.weekday()]
        for period in calendar:
            if period["from"] != day:
                continue
            begin = dt.time.fromisoformat(period["beginTime"])
            end = dt.time.fromisoformat(period["endTime"])
            if begin <= local.time() <= end:
                return True
        return False

    _, root = run
    starts = case_starts(root)
    assert len(starts) == CASES
    outside = [t for t in starts if not inside(t)]
    assert not outside, (
        f"{len(outside)} of {len(starts)} cases arrived outside the calendar, "
        f"first at {outside[0].astimezone(MODEL_TZ) if outside else None}")


@needs_jar
def test_arrivals_are_spread_not_stacked(run):
    """Guards the bug that distorted every earlier engine comparison: deferred
    arrivals landing together on the instant their window opened."""
    _, root = run
    starts = case_starts(root)

    span = (starts[-1] - starts[0]).total_seconds()
    assert span > 3600, f"{len(starts)} arrivals span only {span:.0f}s"

    first_minute = sum(1 for t in starts if (t - starts[0]).total_seconds() < 60)
    assert first_minute < len(starts) / 10, (
        f"{first_minute} of {len(starts)} arrivals land in the first minute")


# --------------------------------------------------------------------------
# Plugin 1 — per-resource durations
# --------------------------------------------------------------------------

@needs_jar
def test_durations_differ_by_resource(run):
    """Pooling would give every resource on an activity the same distribution,
    so the observed means would cluster instead of spreading."""
    _, root = run

    durations = collections.defaultdict(list)
    for trace in root.findall(".//{*}trace"):
        started = {}
        for event in trace.findall("{*}event"):
            fields = {f.get("key"): f.get("value") for f in list(event)}
            activity = fields.get("concept:name")
            stamp = fields.get("time:timestamp")
            if not activity or not stamp:
                continue
            moment = dt.datetime.fromisoformat(stamp)
            if fields.get("lifecycle:transition") == "start":
                started[activity] = (moment, fields.get("org:resource"))
            elif fields.get("lifecycle:transition") == "complete" and activity in started:
                begin, resource = started.pop(activity)
                if resource:
                    key = (activity, resource.rsplit("__", 1)[-1])
                    durations[key].append((moment - begin).total_seconds())

    activity = "W_Completeren aanvraag"
    per_resource = {resource: statistics.mean(values)
                    for (name, resource), values in durations.items()
                    if name == activity and len(values) >= 3}
    assert len(per_resource) > 5, (
        f"only {len(per_resource)} resources ran {activity} often enough")

    slowest, fastest = max(per_resource.values()), min(per_resource.values())
    assert slowest / fastest > 10, (
        f"observed means span only {slowest / fastest:.1f}x "
        f"({fastest:.0f}..{slowest:.0f} s); durations look pooled")


# --------------------------------------------------------------------------
# The run as a whole
# --------------------------------------------------------------------------

@needs_jar
def test_kpis_are_plausible(run):
    """A cheap tripwire: the three plugins interacting should not produce a
    queue that dwarfs the work, which is what the arrival bug looked like."""
    result, _ = run
    kpis = {row["metric"]: row["avg"] for row in result["process_rows"]}

    assert kpis["cycle_time"] > 0
    assert kpis["waiting_time"] < kpis["processing_time"], (
        f"waiting {kpis['waiting_time']:.0f}s exceeds processing "
        f"{kpis['processing_time']:.0f}s; work is queueing behind something")

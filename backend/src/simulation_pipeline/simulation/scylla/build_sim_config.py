"""
Simod parameters.json + BPMN -> Scylla simulation configuration XML.

Carries activity durations, gateway branching, the arrival rate and the case
count. Resources live in the global config; this file only references the pools
that build_global_config created.

Two Scylla behaviours shape the code:

  - Unknown elements are logged and skipped, never rejected
    (`SimulationConfigurationParser.java:245-252`). A typo produces a
    simulation that runs and reports wrong numbers, so validate_sim_config()
    checks the emitted tree rather than trusting it.
  - At least one startEvent carrying an arrivalRate is mandatory
    (`SimulationConfigurationParser.java:98-110`); without it the parser throws.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence
from xml.etree import ElementTree as ET

from . import distributions as D
from .build_global_config import SHARED_POOL_ID

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Scylla reads branching probabilities for these; parallel gateways carry none.
PROBABILISTIC_GATEWAYS = {"exclusiveGateway", "inclusiveGateway"}


def _q(tag: str) -> str:
    return f"{{{D.BSIM}}}{tag}"


def _b(tag: str) -> str:
    return f"{{{BPMN_NS}}}{tag}"


def read_bpmn(bpmn_path: str | Path) -> Dict[str, Any]:
    """Pull the few things the simulation config needs out of the BPMN.

    Returns the process id, the gateway element types (so each gateway gets the
    right Scylla tag), the start event ids, and the task names -- the model and
    Scylla's event log identify an activity differently, by id and by name, so
    comparing them needs the mapping.
    """
    root = ET.parse(str(bpmn_path)).getroot()
    process = root.find(_b("process"))
    if process is None:
        raise ValueError(f"no <process> element in {bpmn_path}")

    gateway_types: Dict[str, str] = {}
    for tag in ("exclusiveGateway", "inclusiveGateway", "parallelGateway",
                "eventBasedGateway"):
        for el in process.findall(_b(tag)):
            gateway_types[el.get("id")] = tag

    start_events = [el.get("id") for el in process.findall(_b("startEvent"))]
    if not start_events:
        raise ValueError(f"no <startEvent> in {bpmn_path}")

    task_names: Dict[str, str] = {}
    for tag in ("task", "userTask", "serviceTask", "manualTask", "scriptTask",
                "sendTask", "receiveTask", "businessRuleTask"):
        for el in process.findall(_b(tag)):
            task_names[el.get("id")] = el.get("name")

    return {
        "process_id": process.get("id"),
        "gateway_types": gateway_types,
        "start_events": start_events,
        "task_names": task_names,
    }


# Retained only so `weighted=True` still means something specific if anyone
# re-enables it; the default is off. See resource_weights().
MAX_WEIGHT_RATIO = 20.0


def resource_weights(
    task: Dict[str, Any],
    rng: random.Random,
    max_ratio: float = MAX_WEIGHT_RATIO,
) -> List[float]:
    """Throughput-proportional weights: 1 / mean duration, ratio-capped.

    **Off by default: the assumption behind it is false.** A faster resource
    was expected to take on more work, but Prosimos allocates by availability,
    not speed -- on a 42-resource BPIC 2012 activity the fastest (6.7 s) took
    4.8% of executions and the slowest (1060.7 s) took 2.7%. Weighting cut
    pooled durations by 11-65% and made agreement worse. Kept so the comparison
    can be reproduced with `weighted=True`.
    """
    means = [D.values_of(res)[0] for res in task["resources"]]
    positive = [m for m in means if m and m > 0]
    if not positive:
        return [1.0] * len(means)

    fastest = min(positive)
    raw = [1.0 / (m if m and m > 0 else fastest) for m in means]

    ceiling = min(raw) * max_ratio
    return [min(w, ceiling) for w in raw]



# How much slack the end date gets over the time the work itself needs.
# The horizon only has to be finite; overshooting costs nothing but a few
# unused availability events, while undershooting truncates the run.
DEFAULT_HORIZON_FACTOR = 10.0


def _weekly_hours(calendar: Dict[str, Any]) -> float:
    """Hours a calendar is open per week."""
    total = 0.0
    for period in calendar.get("time_periods", []):
        try:
            begin = [int(x) for x in period["beginTime"].split(":")[:2]]
            end = [int(x) for x in period["endTime"].split(":")[:2]]
        except (KeyError, ValueError):
            continue
        total += ((end[0] * 60 + end[1]) - (begin[0] * 60 + begin[1])) / 60.0
    return max(total, 0.0)


def simulation_end(model: Dict[str, Any], total_cases: int, start_iso: str,
                   factor: float = DEFAULT_HORIZON_FACTOR) -> str | None:
    """When the simulation should stop, as an ISO timestamp.

    `endDateTime` is optional to Scylla, and without it
    `scheduleNextResourceAvailableEvent` never terminates: each availability
    event schedules the next one forever. That is why some samples never
    finished; an end date took one from >120 s to 0.9 s.

    The horizon has to clear both arrivals (`mean gap x cases`) and capacity,
    which can be far longer -- one resource on a five-hour week needs 40 weeks
    for 200 one-hour cases, and an arrivals-only horizon lost four fifths of
    them. None when neither can be read, and the attribute is then omitted.
    """
    horizons = []

    arrival = model.get("arrival_time_distribution") or {}
    params = arrival.get("distribution_params") or []
    cases = max(1, int(total_cases))
    if params:
        try:
            gap = float(params[0]["value"])
        except (KeyError, TypeError, ValueError):
            gap = 0.0
        if gap > 0:
            span = gap * cases
            # Cases can only arrive while the arrival calendar is open, so the
            # wall-clock span is longer than the sum of the gaps. A calendar
            # open four hours a week stretches a 0.2-day arrival span to ten
            # days; ignoring that cut runs off mid-arrival and lost cases.
            arrival_hours = sum(_weekly_hours({"time_periods": [period]})
                                for period in model.get("arrival_time_calendar") or [])
            if arrival_hours > 0:
                span *= 168.0 / arrival_hours
            horizons.append(span)

    # Work divided by the capacity available to do it, in wall-clock seconds.
    calendars = {c.get("id"): c for c in model.get("resource_calendars", [])}
    open_hours = 0.0
    for profile in model.get("resource_profiles", []):
        for res in profile.get("resource_list", []):
            calendar = calendars.get(res.get("calendar"))
            hours = _weekly_hours(calendar) if calendar else 168.0
            try:
                amount = max(1, int(res.get("amount", 1)))
            except (TypeError, ValueError):
                amount = 1
            open_hours += hours * amount

    work = 0.0
    for task in model.get("task_resource_distribution", []):
        resources = task.get("resources") or []
        means = []
        for res in resources:
            values = D.values_of(res)
            if values:
                means.append(values[0])
        if means:
            work += sum(means) / len(means)

    if work > 0 and open_hours > 0:
        # Wall-clock time to do `work * cases` seconds of work, given resources
        # that are collectively open `open_hours` per week: capacity delivers
        # open_hours/168 seconds of work per second of real time.
        horizons.append(work * cases * 168.0 / open_hours)

    if not horizons:
        return None

    seconds = max(horizons) * factor
    # A week's floor: a model whose arrivals are seconds apart still needs long
    # enough for its weekly calendars to come round.
    seconds = max(seconds, 7 * 24 * 3600)

    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    return (start + timedelta(seconds=seconds)).isoformat()


def build_sim_config(
    model: Dict[str, Any],
    bpmn_path: str | Path,
    total_cases: int,
    start_iso: str,
    seed: int,
    buckets: int = D.DEFAULT_BUCKETS,
    n_draws: int = D.DEFAULT_DRAWS,
    weighted: bool = False,
    arrival_calendar: bool = True,
    resource_durations: bool = True,
    eligibility: bool = True,
    horizon_factor: float = DEFAULT_HORIZON_FACTOR,
) -> ET.Element:
    """Build the definitions/simulationConfiguration tree.

    `weighted=True` weights the pooled duration towards faster resources. Off by
    default: measured against Prosimos, resource selection is near-uniform, so
    weighting moves the pooled duration away from what Prosimos produces. See
    resource_weights().
    """
    bpmn = read_bpmn(bpmn_path)
    rng = random.Random(seed)

    root = ET.Element(_q("definitions"), {"targetNamespace": "http://www.hpi.de"})
    attrs = {
        "id": "bps_sim",
        "processRef": bpmn["process_id"],
        # The real case count. SimuBridge clamps this to 5000; we do not, and
        # the ceiling is tested empirically instead.
        "processInstances": str(int(total_cases)),
        "startDateTime": start_iso,
    }
    end_iso = simulation_end(model, total_cases, start_iso, horizon_factor)
    if end_iso:
        attrs["endDateTime"] = end_iso
    sim = ET.SubElement(root, _q("simulationConfiguration"), attrs)

    for task in model["task_resource_distribution"]:
        _append_task(sim, task, rng, buckets, n_draws, weighted,
                     resource_durations, eligibility)

    for gateway in model.get("gateway_branching_probabilities", []):
        _append_gateway(sim, gateway, bpmn["gateway_types"])

    _append_start_event(
        sim, bpmn["start_events"][0], model["arrival_time_distribution"],
        rng, buckets, n_draws,
        arrival_calendar=model.get("arrival_time_calendar") if arrival_calendar else None,
    )

    return root


def _append_task(parent, task, rng, buckets, n_draws, weighted,
                 resource_durations=True, eligibility=True) -> ET.Element:
    el = ET.SubElement(parent, _q("task"), id=task["task_id"])

    # Pooled duration, kept as the fallback: a Scylla build without the
    # resourceDuration plugin ignores the per-resource block below and uses
    # this, reproducing the earlier behaviour rather than failing.
    duration = ET.SubElement(el, _q("duration"), timeUnit=D.TIME_UNIT)
    weights = resource_weights(task, rng) if weighted else None
    D.append_pooled_duration(duration, task["resources"], weights, rng,
                             buckets, n_draws)

    # One distribution per resource, as Simod discovered them. This is what the
    # pooling above was standing in for; with the plugin the fastest and slowest
    # resource on an activity are no longer treated as identical.
    if resource_durations:
        block = ET.SubElement(el, _q("resourceDurations"))
        for res in task["resources"]:
            item = ET.SubElement(block, _q("resourceDuration"), {
                "resourceId": res["resource_id"],
                "timeUnit": D.TIME_UNIT,
            })
            D.append_distribution(item, res, rng, buckets, n_draws)

    # Which resources may perform this activity. The shared pool below holds
    # every resource, so without this each activity could draw on all of them --
    # measured on BPIC 2012, that removes nearly all queueing (mean waiting time
    # 422 s against Prosimos's 3914 s). The resources are listed individually
    # rather than by group because the model's capability groups overlap.
    if eligibility:
        eligible = ET.SubElement(el, _q("eligibleResources"))
        for res in task["resources"]:
            ET.SubElement(eligible, _q("eligibleResource"), {
                "resourceId": res["resource_id"],
            })

    # One unit of the single shared pool. amount="1" means "any one resource",
    # which is the alternative-resource semantics Prosimos has and Scylla's
    # multi-resource lists do not. Sharing one pool across all activities is
    # what preserves contention between them -- see build_global_config.
    resources = ET.SubElement(el, _q("resources"))
    ET.SubElement(resources, _q("resource"), {
        "id": SHARED_POOL_ID,
        "amount": "1",
    })
    return el


def _append_gateway(parent, gateway, gateway_types) -> ET.Element | None:
    gid = gateway["gateway_id"]
    kind = gateway_types.get(gid, "exclusiveGateway")
    if kind not in PROBABILISTIC_GATEWAYS:
        # Parallel and event-based gateways take no branching probabilities.
        return None

    el = ET.SubElement(parent, _q(kind), id=gid)

    # Sensitivity analysis perturbs each branch independently, so probabilities
    # arrive summing to something other than 1. Prosimos normalises internally;
    # Scylla aborts the run -- "exceeding 1 in total" killed every sample of a
    # Morris smoke run. Rounding alone reaches it: three branches at 0.333334
    # exceed 1 at six decimals.
    values = [float(branch["value"]) for branch in gateway["probabilities"]]
    total = sum(values)
    if total > 0:
        values = [v / total for v in values]
    else:
        # Every branch perturbed to zero: fall back to a uniform split rather
        # than emitting a gateway that can never fire.
        values = [1.0 / len(values)] * len(values)

    # Distribute the rounding residual onto the last branch so the written
    # figures sum to exactly 1.000000 rather than 0.999999 or 1.000001.
    written = [round(v, 6) for v in values]
    written[-1] = round(1.0 - sum(written[:-1]), 6)

    for branch, value in zip(gateway["probabilities"], written):
        flow = ET.SubElement(el, _q("outgoingSequenceFlow"), id=branch["path_id"])
        ET.SubElement(flow, _q("branchingProbability")).text = f"{value:.6f}"
    return el


def _append_start_event(parent, start_id, arrival, rng, buckets, n_draws,
                       arrival_calendar=None) -> ET.Element:
    el = ET.SubElement(parent, _q("startEvent"), id=start_id)
    rate = ET.SubElement(el, _q("arrivalRate"), timeUnit=D.TIME_UNIT)
    D.append_distribution(rate, arrival, rng, buckets, n_draws)

    # Read by our arrivalCalendar plugin, which defers a case that would arrive
    # outside these windows to the next open one. Stock Scylla ignores the
    # element and releases cases across the whole week, which is the behaviour
    # this exists to correct -- so a run without the plugin still works, just
    # with the original discrepancy.
    if arrival_calendar:
        cal = ET.SubElement(el, _q("arrivalCalendar"))
        for period in arrival_calendar:
            ET.SubElement(cal, _q("timetableItem"), {
                "from": period["from"],
                "to": period["to"],
                "beginTime": period["beginTime"],
                "endTime": period["endTime"],
            })
    return el


def validate_sim_config(root: ET.Element, model: Dict[str, Any],
                        bpmn: Dict[str, Any]) -> None:
    """Fail loudly if the emitted XML lost something.

    Scylla skips what it does not recognise, so "it ran" is never proof that it
    read what was written.
    """
    sim = root.find(_q("simulationConfiguration"))
    if sim is None:
        raise ValueError("no simulationConfiguration element")

    if sim.get("processRef") != bpmn["process_id"]:
        raise ValueError(
            f"processRef {sim.get('processRef')!r} does not match the BPMN "
            f"process id {bpmn['process_id']!r}"
        )

    written_tasks = {el.get("id") for el in sim.findall(_q("task"))}
    expected_tasks = {t["task_id"] for t in model["task_resource_distribution"]}
    if written_tasks != expected_tasks:
        raise ValueError(
            f"task mismatch; missing={sorted(expected_tasks - written_tasks)} "
            f"unexpected={sorted(written_tasks - expected_tasks)}"
        )

    for el in sim.findall(_q("task")):
        duration = el.find(_q("duration"))
        if duration is None or len(duration) == 0:
            raise ValueError(f"task {el.get('id')} has no duration distribution")
        if duration.get("timeUnit") is None:
            # A missing timeUnit is an NPE inside Scylla, not a clean error.
            raise ValueError(f"task {el.get('id')} duration has no timeUnit")

    # An eligibility list that lost entries would silently narrow the resources
    # an activity can use, which looks like a plausible result rather than a bug.
    expected_eligible = {
        t["task_id"]: {r["resource_id"] for r in t["resources"]}
        for t in model["task_resource_distribution"]
    }
    for el in sim.findall(_q("task")):
        block = el.find(_q("eligibleResources"))
        if block is None:
            continue
        written = {
            item.get("resourceId")
            for item in block.findall(_q("eligibleResource"))
        }
        expected = expected_eligible[el.get("id")]
        if written != expected:
            raise ValueError(
                f"task {el.get('id')} eligible resources mismatch; "
                f"missing={sorted(expected - written)} "
                f"unexpected={sorted(written - expected)}"
            )
        if not written:
            # Scylla would queue every instance of this activity forever.
            raise ValueError(f"task {el.get('id')} has no eligible resources")

    for kind in PROBABILISTIC_GATEWAYS:
        for el in sim.findall(_q(kind)):
            total = sum(
                float(f.findtext(_q("branchingProbability")))
                for f in el.findall(_q("outgoingSequenceFlow"))
            )
            # Scylla validates exclusive-gateway probabilities sum to (0, 1].
            if kind == "exclusiveGateway" and not 0.0 < total <= 1.0 + 1e-6:
                raise ValueError(
                    f"gateway {el.get('id')} probabilities sum to {total}"
                )

    starts = sim.findall(_q("startEvent"))
    if not starts:
        raise ValueError("no startEvent -- Scylla requires at least one")
    if not any(s.find(_q("arrivalRate")) is not None for s in starts):
        raise ValueError("no startEvent carries an arrivalRate")

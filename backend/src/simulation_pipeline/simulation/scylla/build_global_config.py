"""
Simod parameters.json -> Scylla global configuration XML.

The global config carries resources, calendars, the random seed and the time
zone. The structural decision here is how to represent resources.

Simod discovered these models in *differentiated* mode: every resource is its
own profile with its own duration distribution per activity. Scylla cannot
express that -- an activity has one duration, and the resources listed under it
are all required *simultaneously* (`QueueManager.java:154-172` blocks unless
every listed resource is available). Writing the profiles directly would
deadlock.

So every resource goes into one shared `dynamicResource` with
`defaultQuantity = N`, each as a named `<instance>` keeping its own timetable.
What survives and what does not:

    kept   per-resource calendars, so is_resource_calendars stays meaningful
    kept   total capacity, so contention between activities is real
    lost   per-resource durations -- pooled into a weighted mixture per
           activity (build_sim_config)
    lost   eligibility, which resources may perform which activity

Pooling per activity instead was the first attempt and is wrong; see
SHARED_POOL_ID.

Requires a Scylla build that includes commit f9671cb ("Fix #72: default
timetables for named resource instances are ignored"). The copy bundled with
SimuBridge predates it and silently ignores the per-instance timetables this
strategy depends on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence
from xml.etree import ElementTree as ET

from .distributions import BSIM

# Mandatory attributes on dynamicResource -- the parser calls Integer.valueOf /
# Double.valueOf / TimeUnit.valueOf on them without a null check.
DEFAULT_COST = "0.0"
DEFAULT_COST_TIME_UNIT = "HOURS"


def _q(tag: str) -> str:
    return f"{{{BSIM}}}{tag}"


def pool_id_for(task_id: str) -> str:
    """Pool identifier for an activity.

    Deprecated: kept only so older per-activity output can still be read. New
    configurations share one pool across all activities (see `SHARED_POOL_ID`).
    """
    return f"pool_{task_id}"


# One pool for the whole process, holding every resource once. Pooling per
# activity instead multiplies capacity 4.1x -- 191 instances against 47 on BPIC
# 2012, 433 against 105 on BPIC 2017 -- because most resources work on more than
# one activity, and then almost no contention between activities survives. T1
# caught it: Prosimos serialised two concurrent activities (120 s), Scylla ran
# them at once (60 s).
SHARED_POOL_ID = "resource_pool"

# Separates a resource id from its copy index in an instance name, so
# `resource_pool__10809#3` is the fourth copy of resource 10809. Anything that
# maps an instance back to a resource has to strip this too.
COPY_SEPARATOR = "#"


def resource_calendar_map(model: Dict[str, Any]) -> Dict[str, str]:
    """resource id -> calendar id, flattened across all profiles."""
    out: Dict[str, str] = {}
    for profile in model.get("resource_profiles", []):
        for res in profile.get("resource_list", []):
            out[res["id"]] = res.get("calendar")
    return out


def resource_cost_map(model: Dict[str, Any]) -> Dict[str, float]:
    """resource id -> cost per hour."""
    out: Dict[str, float] = {}
    for profile in model.get("resource_profiles", []):
        for res in profile.get("resource_list", []):
            out[res["id"]] = float(res.get("cost_per_hour", 0.0) or 0.0)
    return out


def build_global_config(
    model: Dict[str, Any],
    seed: int,
    zone_offset: str = "+02:00",
) -> ET.Element:
    """Build the globalConfiguration element.

    `zone_offset` must agree with the offset in the simulation's start
    timestamp; Prosimos runs at +02:00 by default (`simulate_samples.py:30`).
    """
    root = ET.Element(_q("globalConfiguration"), {
        "targetNamespace": "http://www.hpi.de",
        "id": "bps_global",
    })

    # Without an explicit seed Scylla draws its own, and the same configuration
    # would give different answers on every run -- unusable for a sensitivity
    # analysis. Note SimulationManager.java:127 reads only this global seed and
    # ignores any per-simulationConfiguration randomSeed attribute.
    ET.SubElement(root, _q("randomSeed")).text = str(int(seed))
    ET.SubElement(root, _q("zoneOffset")).text = zone_offset

    calendars = {c["id"]: c for c in model.get("resource_calendars", [])}
    res_to_cal = resource_calendar_map(model)
    res_to_cost = resource_cost_map(model)

    res_data = ET.SubElement(root, _q("resourceData"))
    _append_shared_pool(res_data, model, res_to_cal, res_to_cost, calendars)

    timetables = ET.SubElement(root, _q("timetables"))
    for cal in model.get("resource_calendars", []):
        _append_timetable(timetables, cal)

    return root


def resource_amounts(model: Dict[str, Any]) -> Dict[str, int]:
    """How many interchangeable copies of each resource the model declares.

    Simod writes `amount` on every resource, and the sensitivity analysis
    perturbs it -- that is the whole of the `is_resource_numbers` dimension. A
    perturbed BPIC 2012 sample reaches 611 copies of 47 resources, one of them
    13 deep.

    Ignoring it, as this converter did, silently pinned the Scylla arm at one
    copy each. That made `is_resource_numbers` unmeasurable there, and it made
    perturbed samples genuinely infeasible: at 47 resources one sample needed
    172 s of work per case against a 73 s arrival gap, so its queue grew without
    bound and the run never finished. Prosimos ran the same sample in 5.3 s
    because it honoured the 611.
    """
    amounts: Dict[str, int] = {}
    for profile in model.get("resource_profiles", []):
        for res in profile.get("resource_list", []):
            try:
                amount = int(res.get("amount", 1))
            except (TypeError, ValueError):
                amount = 1
            amounts[res["id"]] = max(1, amount)
    return amounts


def all_resource_ids(model: Dict[str, Any]) -> List[str]:
    """Every distinct resource in the model, in a stable order.

    Taken from resource_profiles rather than task_resource_distribution: the
    latter lists a resource once per activity it can perform, and counting
    those repeats is exactly the capacity inflation this pooling avoids.
    """
    seen: Dict[str, None] = {}
    for profile in model.get("resource_profiles", []):
        for res in profile.get("resource_list", []):
            seen.setdefault(res["id"], None)
    if seen:
        return list(seen)

    # Fall back to the distributions if no profiles are declared.
    for task in model["task_resource_distribution"]:
        for res in task["resources"]:
            seen.setdefault(res["resource_id"], None)
    return list(seen)


def _append_shared_pool(parent, model, res_to_cal, res_to_cost,
                        calendars) -> ET.Element:
    """One pool holding every resource once, each keeping its own calendar."""
    resource_ids = all_resource_ids(model)
    amounts = resource_amounts(model)
    total = sum(amounts.get(rid, 1) for rid in resource_ids)

    costs = [res_to_cost.get(rid, 0.0) for rid in resource_ids]
    default_cost = f"{(sum(costs) / len(costs)) if costs else 0.0:.6f}"

    el = ET.SubElement(parent, _q("dynamicResource"), {
        "id": SHARED_POOL_ID,
        "name": SHARED_POOL_ID,
        "defaultQuantity": str(total),
        "defaultCost": default_cost,
        "defaultTimeUnit": DEFAULT_COST_TIME_UNIT,
    })

    for rid in resource_ids:
        cal = res_to_cal.get(rid)
        cost = res_to_cost.get(rid)
        # One instance per declared copy. Copies are interchangeable -- same
        # calendar, same cost, same durations -- so they differ only in the
        # suffix, and eligibility matches on the resource id either way.
        count = amounts.get(rid, 1)
        for copy in range(count):
            name = f"{SHARED_POOL_ID}__{rid}"
            if count > 1:
                name = f"{name}{COPY_SEPARATOR}{copy}"
            attrs = {"name": name}
            if cal in calendars:
                # Per-instance timetable: this is what survives pooling, and it
                # is why is_resource_calendars stays meaningful here.
                attrs["timetableId"] = cal
            if cost is not None:
                attrs["cost"] = f"{cost:.6f}"
            ET.SubElement(el, _q("instance"), attrs)

    return el


def _append_timetable(parent, calendar) -> ET.Element:
    """One Prosimos calendar as a Scylla timetable.

    Field names line up almost exactly. Times are passed through unrounded --
    Scylla parses HH:MM:SS via LocalTime.parse. (SimuBridge rounds to whole
    hours here, which is its own limitation, not Scylla's.)
    """
    tt = ET.SubElement(parent, _q("timetable"), id=calendar["id"])
    for period in calendar.get("time_periods", []):
        ET.SubElement(tt, _q("timetableItem"), {
            "from": period["from"],
            "to": period["to"],
            "beginTime": period["beginTime"],
            "endTime": period["endTime"],
        })
    return tt


def pool_members(model: Dict[str, Any]) -> Dict[str, List[str]]:
    """activity id -> the resource ids that can perform it.

    Records the eligibility Simod discovered. Scylla cannot express it -- every
    activity draws from the one shared pool -- but it is what the duration
    mixture for each activity is built from.
    """
    return {
        task["task_id"]: [r["resource_id"] for r in task["resources"]]
        for task in model["task_resource_distribution"]
    }


def validate_global_config(root: ET.Element, model: Dict[str, Any]) -> None:
    """Fail loudly if the emitted XML lost something.

    Scylla never errors on XML it does not recognise -- it logs and skips
    (`GlobalConfigurationParser.java:207`). Silent drops are therefore the
    default failure mode, and the converter has to do its own checking.
    """
    pools = {el.get("id") for el in root.iter(_q("dynamicResource"))}
    if SHARED_POOL_ID not in pools:
        raise ValueError(f"shared resource pool {SHARED_POOL_ID!r} missing")

    # Capacity must equal the declared headcount -- the sum of each resource's
    # `amount`, not the number of distinct resources. The check exists to catch
    # a resource being counted once per activity it can perform, which inflated
    # BPIC 2012 from 47 to 191; it must still allow the copies `amount` asks
    # for, which a perturbed sample takes to 611.
    pool = next(el for el in root.iter(_q("dynamicResource"))
                if el.get("id") == SHARED_POOL_ID)
    amounts = resource_amounts(model)
    expected_capacity = sum(amounts.get(rid, 1) for rid in all_resource_ids(model))
    if int(pool.get("defaultQuantity")) != expected_capacity:
        raise ValueError(
            f"pool capacity {pool.get('defaultQuantity')} does not match the "
            f"{expected_capacity} resources in the model"
        )

    declared = {tt.get("id") for tt in root.iter(_q("timetable"))}
    referenced = {
        inst.get("timetableId")
        for inst in root.iter(_q("instance"))
        if inst.get("timetableId")
    }
    dangling = referenced - declared
    if dangling:
        raise ValueError(f"instances reference undeclared timetables: {sorted(dangling)}")

    for el in root.iter(_q("dynamicResource")):
        quantity = int(el.get("defaultQuantity"))
        instances = len(el.findall(_q("instance")))
        # Scylla throws if instances exceed defaultQuantity
        # (GlobalConfigurationParser.java:119-122).
        if instances > quantity:
            raise ValueError(
                f"pool {el.get('id')} declares quantity {quantity} "
                f"but lists {instances} instances"
            )

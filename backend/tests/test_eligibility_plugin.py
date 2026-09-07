"""
Tests for the Scylla resource-eligibility plugin.

Simod records which resources may perform which activity -- on BPIC 2012 one
activity can be done by 27 of the 47 resources, another by only 2. Scylla's
configuration cannot express that: a list of resources on an activity means all
of them are needed at once, and the alternative, a pool per activity, duplicates
any resource used by more than one activity (capacity 191 instead of 47).

The converter therefore put every resource in one shared pool, which keeps
capacity right but lets every activity draw on all 47. Measured against
Prosimos, that removed nearly all queueing: mean waiting time 422 s against
3914 s. This plugin takes over resource assignment so both hold at once.

Needs a scylla.jar built from a tree containing the plugin; skips without one.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.simulation_pipeline.simulation.scylla import build_sim_config as S
from src.simulation_pipeline.simulation.scylla.build_global_config import (
    SHARED_POOL_ID, all_resource_ids, build_global_config)
from src.simulation_pipeline.simulation.scylla.distributions import BSIM
from test_t1_determinism import has_jar

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "example_sensitivity_analysis_inputs" / "BPIC_2012"
START_ISO = "2023-01-01T00:00:00+02:00"
INSTANCE_PREFIX = SHARED_POOL_ID + "__"

needs_jar = pytest.mark.skipif(not has_jar(), reason="needs a built scylla.jar")


def q(tag):
    return "{" + BSIM + "}" + tag


@pytest.fixture(scope="module")
def model():
    path = MODEL_DIR / "BPIC_2012_train.json"
    if not path.exists():
        pytest.skip("BPIC 2012 model not available")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bpmn():
    return MODEL_DIR / "BPIC_2012_train.bpmn"


def build(model, bpmn, **kwargs):
    return S.build_sim_config(model, bpmn, total_cases=100, start_iso=START_ISO,
                              seed=1, buckets=20, n_draws=500, **kwargs)


# --------------------------------------------------------------------------
# The emitted XML
# --------------------------------------------------------------------------

def test_one_entry_per_eligible_resource(model, bpmn):
    sim = build(model, bpmn).find(q("simulationConfiguration"))
    by_task = {t["task_id"]: t for t in model["task_resource_distribution"]}

    for task in sim.findall(q("task")):
        block = task.find(q("eligibleResources"))
        assert block is not None, task.get("id")
        written = {item.get("resourceId")
                   for item in block.findall(q("eligibleResource"))}
        assert written == {r["resource_id"]
                           for r in by_task[task.get("id")]["resources"]}


def test_eligibility_is_narrower_than_the_pool(model, bpmn):
    """The point of the plugin.

    If every activity were eligible for every resource, the plugin would change
    nothing -- the shared pool already behaves that way.
    """
    sim = build(model, bpmn).find(q("simulationConfiguration"))
    everyone = set(all_resource_ids(model))

    sizes = []
    for task in sim.findall(q("task")):
        written = {item.get("resourceId")
                   for item in task.find(q("eligibleResources"))}
        assert written <= everyone, "eligible resource outside the pool"
        sizes.append(len(written))

    assert min(sizes) < len(everyone), (
        "no activity is restricted; eligibility carries no information")


def test_every_eligible_resource_exists_as_an_instance(model, bpmn):
    """The plugin matches on instance names, so a resource the global config
    never declares would simply never be selectable."""
    sim = build(model, bpmn).find(q("simulationConfiguration"))
    gc = build_global_config(model, seed=1)

    instances = {inst.get("name") for inst in gc.iter(q("instance"))}
    for task in sim.findall(q("task")):
        for item in task.find(q("eligibleResources")):
            name = INSTANCE_PREFIX + item.get("resourceId")
            assert name in instances, name


def test_shared_pool_is_kept(model, bpmn):
    """Assignment is taken over, but the pool still defines the instances and
    their calendars, and it is the fallback for a build without the plugin."""
    sim = build(model, bpmn).find(q("simulationConfiguration"))
    for task in sim.findall(q("task")):
        resources = task.find(q("resources"))
        assert resources is not None
        refs = resources.findall(q("resource"))
        assert [r.get("id") for r in refs] == [SHARED_POOL_ID]
        assert refs[0].get("amount") == "1"


def test_capacity_is_unchanged(model, bpmn):
    """Eligibility must not reintroduce the per-activity pooling that inflated
    capacity from 47 to 191."""
    gc = build_global_config(model, seed=1)
    total = sum(int(p.get("defaultQuantity"))
                for p in gc.iter(q("dynamicResource")))
    assert total == len(all_resource_ids(model))


def test_eligibility_can_be_switched_off(model, bpmn):
    sim = build(model, bpmn, eligibility=False).find(q("simulationConfiguration"))
    for task in sim.findall(q("task")):
        assert task.find(q("eligibleResources")) is None


def test_validation_rejects_a_dropped_resource(model, bpmn):
    """Scylla skips XML it does not understand rather than failing, so a lost
    entry would surface as a plausible number, not an error."""
    root = build(model, bpmn)
    sim = root.find(q("simulationConfiguration"))
    block = sim.find(q("task")).find(q("eligibleResources"))
    block.remove(list(block)[0])

    with pytest.raises(ValueError, match="eligible resources mismatch"):
        S.validate_sim_config(root, model, S.read_bpmn(bpmn))


# --------------------------------------------------------------------------
# What Scylla does with it
# --------------------------------------------------------------------------

def run_scylla(model, bpmn, cases, tmp_path, **kwargs):
    from src.simulation_pipeline.simulation.scylla import run_scylla as R

    original = S.build_sim_config
    if kwargs:
        S.build_sim_config = lambda *a, **k: original(*a, **{**k, **kwargs})
        R.build_sim_config = S.build_sim_config
    try:
        result = R.simulate_sample_scylla(
            sample_id=0, sample_data=model, bpmn_path=bpmn, total_cases=cases,
            start_iso=START_ISO, jar_path=R.resolve_jar(), seed=5,
            heap="1g", want_event_log=True, keep_output=tmp_path,
        )
    finally:
        S.build_sim_config = original
        R.build_sim_config = original
    assert result["error"] is None, result["error"]
    return result, Path(tmp_path) / "sample_00000"


def observed_assignments(output_dir):
    """Activity name -> the resource ids that performed it, from the XES log.

    Scylla writes org:resource as the resource type and the instance name joined
    by an underscore, so with one shared pool the value reads
    `resource_pool_resource_pool__10125`. The model's resource id is what
    follows the last separator.
    """
    root = ET.parse(output_dir / "model.xes").getroot()
    seen = {}
    for event in root.iter():
        if not event.tag.split("}")[-1] == "event":
            continue
        fields = {f.get("key"): f.get("value") for f in list(event)}
        activity = fields.get("concept:name")
        resource = fields.get("org:resource")
        if not activity or not resource:
            continue
        _, _, name = resource.rpartition("__")
        seen.setdefault(activity, set()).add(name or resource)
    return seen


def eligibility_by_activity(model, bpmn):
    names = S.read_bpmn(bpmn)["task_names"]
    return {names.get(t["task_id"]): {r["resource_id"] for r in t["resources"]}
            for t in model["task_resource_distribution"]}


@needs_jar
def test_no_activity_is_performed_by_an_ineligible_resource(model, bpmn, tmp_path):
    """The core check."""
    _, output = run_scylla(model, bpmn, 300, tmp_path)

    declared = eligibility_by_activity(model, bpmn)
    observed = observed_assignments(output)
    assert observed, "no resource assignments logged"

    checked = 0
    for activity, resources in observed.items():
        if activity not in declared:
            continue
        checked += 1
        assert resources <= declared[activity], (
            activity + " was performed by "
            + str(sorted(resources - declared[activity]))
            + ", which the model does not list for it")
    assert checked, "no activity could be matched between the log and the model"


@needs_jar
def test_without_the_plugin_ineligible_resources_do_appear(model, bpmn, tmp_path):
    """The contrast: without eligibility the shared pool lets any resource run
    any activity, which is the behaviour this plugin exists to remove."""
    _, output = run_scylla(model, bpmn, 300, tmp_path, eligibility=False)

    declared = eligibility_by_activity(model, bpmn)
    observed = observed_assignments(output)
    violations = sum(
        1 for activity, resources in observed.items()
        if activity in declared and not resources <= declared[activity])
    assert violations, (
        "the unrestricted run stayed within eligibility by chance; the "
        "comparison test above proves nothing")

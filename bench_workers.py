"""How many samples should run at once?

The fan-out is currently sized as cores minus four. That is a guess. This runs
the same batch of real samples at several worker counts and reports throughput,
so the number can come from measurement instead.

    python bench_workers.py --samples 48 --jobs 8,16,28,32,48

Samples come from a Morris chunk the pipeline has already generated, so they are
the same shape and cost as the ones a real run simulates -- a smoke chunk holds
four samples and cannot keep any worker count busy.
"""

import argparse
import json
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from joblib import Parallel, delayed

from src.simulation_pipeline.simulation.scylla.run_scylla import (
    resolve_jar, simulate_sample_scylla)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", help="samples_morris_*.json to draw from")
    ap.add_argument("--samples", type=int, default=48)
    ap.add_argument("--jobs", default="8,16,28,32,48")
    ap.add_argument("--cases", type=int, default=3000)
    ap.add_argument("--heap", default="1g")
    ap.add_argument("--bpmn",
                    default="example_sensitivity_analysis_inputs/BPIC_2012/BPIC_2012_train.bpmn")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent
    chunk = args.chunk
    if not chunk:
        found = sorted(repo.glob("backend/output/**/samples_morris_*.json"))
        if not found:
            raise SystemExit("no sample chunk found; pass --chunk")
        chunk = found[0]
    samples = json.loads(Path(chunk).read_text(encoding="utf-8"))
    ids = sorted(samples.keys(), key=int)[: args.samples]
    print(f"chunk   : {chunk}")
    print(f"samples : {len(ids)} at {args.cases} cases, {args.heap} heap")

    jar = resolve_jar()
    bpmn = repo / args.bpmn
    start_iso = "2023-01-01T00:00:00+02:00"

    print(f"\n{'workers':>8} {'wall_s':>8} {'s/sample':>9} {'speedup':>8}  {'failed':>6}")
    baseline = None
    for n in [int(x) for x in args.jobs.split(",")]:
        t0 = time.perf_counter()
        outs = Parallel(n_jobs=n, backend="loky")(
            delayed(simulate_sample_scylla)(
                int(i), samples[i], bpmn, args.cases, start_iso, jar,
                heap=args.heap)
            for i in ids
        )
        wall = time.perf_counter() - t0
        failed = sum(1 for o in outs if o.get("error"))
        per = wall / len(ids)
        if baseline is None:
            baseline = wall
        print(f"{n:>8} {wall:>8.1f} {per:>9.2f} {baseline/wall:>8.2f}x {failed:>6}")


if __name__ == "__main__":
    main()

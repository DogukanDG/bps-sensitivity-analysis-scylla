# Running the Scylla arm on the COMA cluster

The Prosimos arm runs through `run_array.sh` and is unchanged. This describes
what the Scylla arm needs on top of it.

For what Scylla is and how to run it locally, see **Module 4** in the
[main README](../../README.md). This file is the cluster detail underneath it.

## What differs

Prosimos is an in-process Python call. Scylla is a JVM per sample. Everything
below follows from that.

| | Prosimos | Scylla |
|---|---|---|
| Fan-out sized by | cores (`n_jobs=-5`) | memory and the Slurm allocation |
| Extra runtime | none | Java 11 |
| Extra files to ship | none | `scylla.jar` + `libs/` (~27 MB) |
| Output tree | `..._outputs/` | `..._outputs_scylla/` |

## One-time setup on the cluster

**1. Copy the jar and its libraries.** The manifest's `Class-Path` is relative,
so the two must stay together:

```bash
# from your machine
scp -r spike/scylla.jar spike/libs username@cluster.ginkgo-project.de:~/scylla/
```

**2. Java 11.** Try a module first:

```bash
module avail 2>&1 | grep -i -E "jdk|java|openjdk"
```

If none is available, put one in the conda env — the same approach the local
setup uses:

```bash
conda activate bps
conda install -c conda-forge openjdk=11
```

Scylla's `pom.xml` targets source/target 11. An older JVM fails at startup with
`UnsupportedClassVersionError`, and a newer one is fine.

**3. Check it runs before queueing anything.** One interactive sample beats
discovering the problem across 32 queued array tasks:

```bash
salloc -n 8 --mem=16G -t 00:30:00
cd ~/bps_clean/backend
export SCYLLA_JAR=~/scylla/scylla.jar
python run_experiments.py --dataset production --engine scylla --smoke
```

## Submitting

```bash
cd server_computing/slurm
mkdir -p logs                     # Slurm opens the log before the script runs

python ../../backend/run_experiments.py --dataset production --engine scylla --list

DATASET=production sbatch --array=0-11%2 run_array_scylla.sh
```

`%2` throttles to two concurrent tasks. On a shared cluster that is polite; six
is not.

## Sizing

Worker count comes from the memory in the allocation divided by the JVM heap,
capped by the cores. Nothing has to be set by hand:

| `--mem` | heap | workers |
|---|---|---|
| 64 GB | 1g | ~28 |
| 128 GB | 1g | ~60 |
| 64 GB | 2g | ~19 |

Raise `--mem` in the script header to raise throughput. `SCYLLA_HEAP=2g` raises
the per-sample heap if a large model needs it, and `--n-jobs` overrides the
calculation entirely.

**Why memory and not cores.** Eight concurrent JVMs on an 8 GB machine died with
"insufficient memory for the Java Runtime Environment", and the failure was
silent: SALib returns `[]` when the sample matrix stops matching the output
vector, so the run produced a plausible-looking wrong answer rather than an
error. One published ρ = 0.400 was invalid for exactly this reason. The sizing
now reads `SLURM_MEM_PER_NODE` and `SLURM_CPUS_ON_NODE`, so it follows the
allocation rather than the node — a 2 TB compute node shared between jobs must
not be treated as though all of it were ours.

## Expected wall time

Measured on the cluster, BPIC 2012 at 3000 cases:

| | per sample |
|---|---|
| One Scylla sample alone | ~31 s |
| Scylla, workers saturated | **~1.5 s** |
| Prosimos (from the earlier campaign's timings) | ~0.22 s |

So Scylla costs roughly seven times a Prosimos sample once both are running
flat out. Phase 1 alone for one dataset (12 Morris runs, 17,280 simulations) is
about six hours at two concurrent array tasks; the full 51-run campaign is
around 63 hours, over half of it in the Sobol N=2048 phase.

Two things that do **not** help, both measured rather than assumed:

- **More workers.** 16, 32 and 64 workers finish the same batch in the same
  time. Something other than worker count sets the ceiling.
- **A warm JVM.** Startup and I/O are about 1% of a run (65.8 s wall against
  65.3 s of Scylla's own reported time), so pooling JVMs has nothing to win.

Spreading array tasks across nodes is the lever that does work: one task per
node avoids the contention two tasks on the same node create.

## Results

The Scylla arm writes to
`backend/output/simulation_and_sensitivity_analysis_outputs_scylla/`, separate
from the Prosimos tree. This matters for more than tidiness: runs are skipped
when their output folder looks complete, so a shared tree would let a finished
Prosimos run mark the corresponding Scylla run as already done. Run times and
batch logs are suffixed the same way.

## If samples fail

Failures are recorded rather than swallowed — `errors_chunk_*.parquet` next to
the results, one row per failed sample with the exception text:

```python
import pandas as pd
df = pd.read_parquet("errors_chunk_00001.parquet")
print(df["error"].iloc[0])
```

Two that have actually happened:

- **`UnsupportedClassVersionError`** — the JVM is older than 11. See setup.
- **`ScyllaValidationException: ... exceeding 1 in total`** — gateway branching
  probabilities summing above 1. Sensitivity analysis perturbs each branch
  independently, so this is normal input; the converter now normalises before
  writing. If it reappears, the normalisation has regressed.

# Scylla results — BPIC 2012

Sensitivity analysis of the BPIC 2012 model simulated with **Scylla**, run on the
COMA cluster at 3000 cases per sample. The Prosimos arm of the same analysis is
the comparison these exist for.

18 of the 51 configured runs are here: phase 1 (Morris) and phase 2 (Sobol
N=512) complete. Phases 3-7 have not been run.

## Layout

Each run directory holds:

```
<run>/
  sensitivity_analysis_outputs/sa_<kpi>/     the indices -- what a comparison reads
  sensitivity_analysis_inputs/               averaged KPIs fed to SALib
  simulation_results/.../run_1/
    process_chunk_*.parquet                  per-sample KPIs
    errors_chunk_*.parquet                   failed samples, when any failed
  user_config.json                           the exact configuration
```

Sample files (`samples/`) are not included; they are regenerated from
`user_config.json` and were the bulk of the size.

## What is usable

| run | samples | lost | indices |
|---|---|---|---|
| `morris_nogw_t64_seed100` | 374 / 384 | **10** | **empty** |
| `morris_nogw_t64_seed200` | 384 | 0 | ok |
| `morris_nogw_t64_seed300` | 384 | 0 | ok |
| `morris_nogw_t128_seed{100,200,300}` | 768 | 0 | ok |
| `morris_nogw_t256_seed{100,200,300}` | 1536 | 0 | ok |
| `morris_nogw_t512_seed{100,200,300}` | 3072 | 0 | ok |
| `sobol_nogw_n512_seed{100,200,300}` | 3584 | 0 | ok |
| `sobol_gw_n512_seed100` | 4094 / 4096 | **2** | **null** |
| `sobol_gw_n512_seed200` | 4093 / 4096 | **3** | **null** |
| `sobol_gw_n512_seed300` | 4092 / 4096 | **4** | **null** |

**Eleven of twelve Morris runs and all three Sobol-without-gateways runs are
complete and carry usable indices.**

## The four runs that do not

Every loss is the same failure: `ScyllaError: Scylla timed out after 900s`. A
few samples out of thousands take longer than the timeout allows.

The consequence differs by method, and in both cases the analysis refused to
produce a number rather than producing a wrong one:

- **Morris** checks the output vector against the design matrix, sees the
  mismatch, and writes `[]`.
- **Sobol** has no such check; SALib raises, and the exception handler writes
  `null` for every index.

Neither silently misaligns, which is the failure that would have mattered.

A converter fix (`-Dscylla.resourceAvailability=off`, skipping a per-resource
statistics pass that costs more than the simulation and that nothing reads) took
one reproducing sample from a 900 s timeout to 54 s. These runs predate it;
rerunning the four should close the gap.

## Reading the numbers

**Morris** reports `mu_star` -- mean absolute elementary effect. Rank by it; the
absolute level is not comparable across models.

**Sobol** reports `S1` and `ST`. Several `ST` values here exceed 1, peaking at
2.9, which is outside the theoretical range and means N=512 has not converged
for this model. This is not specific to Scylla: the Prosimos arm shows the same
on its `production` dataset, 15 of 45 values above 1 with a maximum of 15.9.
Treat the Sobol results as provisional at this N.

## Reproducing

```bash
cd backend
export SCYLLA_JAR=~/scylla/scylla.jar
python run_experiments.py --dataset bpic2012 --engine scylla --index 0
```

`user_config.json` in each directory records the parameters that produced it.
See **Module 4** in the top-level README for setup.

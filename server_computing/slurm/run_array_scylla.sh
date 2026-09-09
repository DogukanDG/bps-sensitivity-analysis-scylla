#!/bin/bash
#SBATCH --job-name=bps-scylla
#SBATCH --output=logs/%x_%A_%a.out
#SBATCH --error=logs/%x_%A_%a.err
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=64G
#SBATCH --time=23:30:00
#SBATCH --partition=compute
#
# The Scylla arm of the engine comparison. One Slurm array task per run, the
# same shape as run_array.sh, which stays the Prosimos arm.
#
#     DATASET=production sbatch --array=0-11%2 run_array_scylla.sh
#     DATASET=datamining sbatch --array=0-11%2 run_array_scylla.sh
#
# Run numbers come from:
#
#     python run_experiments.py --dataset production --engine scylla --list
#
# Results land in output/simulation_and_sensitivity_analysis_outputs_scylla/,
# separate from the Prosimos tree, so the two arms never overwrite each other
# and a completed Prosimos run does not make a Scylla run look already done.
#
# --- What differs from the Prosimos arm ---
#
# Scylla is a JVM per sample, not an in-process call. Two consequences:
#
# 0. The allocation asks for one task with 32 cores, not 32 tasks. `-n 32` gets
#    32 single-core tasks, and Slurm then sets SLURM_CPUS_PER_TASK=1 -- which is
#    the first variable the worker sizing reads, so the whole run goes serial
#    without saying so. Measured: a t=64 run took 26 minutes at one worker.
#
# 1. Memory, not cores, sets the fan-out. run_experiments sizes the worker
#    count from SLURM_MEM_PER_NODE and the per-sample heap, so --mem above is a
#    real limit here rather than a formality: 64G with a 1g heap gives about 28
#    workers. Raise --mem to raise throughput. Eight concurrent JVMs on an 8 GB
#    machine once died with "insufficient memory for the Java Runtime
#    Environment", and the failure was silent -- SALib returns [] when the
#    sample matrix stops matching the output vector -- so this is sized
#    deliberately rather than left to joblib's -5.
#
# 2. Java 11 is required. Scylla's pom targets source/target 11, and an older
#    JVM fails at startup with UnsupportedClassVersionError. See the module
#    load below.
#
# No --gres: nothing here touches CUDA. Requesting a GPU leaves it idle and
# lengthens the queue for everyone.

set -euo pipefail

# logs/ must exist before submitting. Slurm opens the --output file before this
# script runs, and a missing directory makes the job fail with no log to say why.
cd "$SLURM_SUBMIT_DIR/../../backend"
mkdir -p "$SLURM_SUBMIT_DIR/logs"

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export PYTHONIOENCODING=utf-8
# Slurm writes stdout to a file, so Python block-buffers it and progress only
# appears when a buffer fills -- which makes a running job look stuck and hides
# the lines that say how it sized itself.
export PYTHONUNBUFFERED=1

source ~/miniforge3/etc/profile.d/conda.sh
conda activate bps

# Java 11. Prefer a cluster module; fall back to a JDK inside the conda env,
# which is how the local setup does it (conda install -c conda-forge openjdk=11).
if command -v module >/dev/null 2>&1; then
    module load openjdk/11 2>/dev/null || module load jdk/11 2>/dev/null || true
fi

if ! command -v java >/dev/null 2>&1; then
    echo "FATAL: no java on PATH. Load a Java 11 module or install one into the" >&2
    echo "conda env:  conda install -c conda-forge openjdk=11" >&2
    exit 1
fi

# Fail here rather than once per sample, with the version in the log so a
# version mismatch is obvious from the output file alone.
java -version 2>&1 | sed 's/^/java      : /'

# The jar and its libs/ directory must sit together -- the manifest's
# Class-Path is relative. Override with SCYLLA_JAR if it lives elsewhere.
: "${SCYLLA_JAR:=$HOME/scylla/scylla.jar}"
export SCYLLA_JAR
if [ ! -f "$SCYLLA_JAR" ]; then
    echo "FATAL: scylla.jar not found at $SCYLLA_JAR" >&2
    echo "Copy it and its libs/ directory to the cluster, or set SCYLLA_JAR." >&2
    exit 1
fi

echo "host      : $(hostname)"
echo "array job : $SLURM_ARRAY_JOB_ID  task $SLURM_ARRAY_TASK_ID"
echo "dataset   : ${DATASET:?set DATASET=production or DATASET=datamining}"
echo "cores      : ${SLURM_CPUS_PER_TASK:-unset} per task"
echo "memory    : ${SLURM_MEM_PER_NODE:-unset} MB"
echo "jar       : $SCYLLA_JAR"
echo "started   : $(date)"
echo

python run_experiments.py \
    --dataset "$DATASET" \
    --engine scylla \
    --heap "${SCYLLA_HEAP:-1g}" \
    --index "$SLURM_ARRAY_TASK_ID"

echo
echo "finished  : $(date)"

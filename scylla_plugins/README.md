# Scylla plugins — source patches

Three plugins written against `bptlab/scylla`, plus five follow-ups, kept here as
patches because that repository is not ours to push to. The working tree they came from is a local
clone; these files are the durable copy.

| Patch | What it adds |
|---|---|
| `0001-Add-an-arrival-calendar-plugin.patch` | Restricts case arrivals to the hours the Simod arrival calendar covers |
| `0002-Add-a-resource-dependent-task-duration-plugin.patch` | Uses the duration distribution of the resource actually performing a task |
| `0003-Add-a-resource-eligibility-plugin.patch` | Restricts each activity to the resources the model lists for it, keeping total capacity correct |
| `0004-Space-deferred-arrivals-instead-of-stacking-them-on-.patch` | Fixes 0001: deferred arrivals were all landing on the instant their window opened |
| `0005-Let-the-event-log-be-switched-off-at-run-time.patch` | `-Dscylla.xes=off`: the XES log was 16 MB a sample and nothing read it |
| `0006-Treat-a-resource-s-copies-as-one-resource-for-eligib.patch` | Fixes 0003: `<pool>__id#copy` names were not matching the eligible set |
| `0007-Stop-computing-resource-utilization-nothing-reads.patch` | `-Dscylla.resourceAvailability=off`: per-instance availability walks the whole horizon and cost more than the simulation — one sample went from a 900 s timeout to 54 s |
| `0008-Trim-comments-that-restate-their-own-code.patch` | Comment-only; no behaviour change |

The commit messages carry the reasoning and the measurements; the adapter
README (`backend/src/simulation_pipeline/simulation/scylla/README.md`) has the
results and what they mean for the comparison.

## Rebuilding from these

```bash
git clone https://github.com/bptlab/scylla.git
cd scylla
git checkout 5159b53              # the base these patches were generated against
git am --keep-cr /path/to/scylla_plugins/*.patch

# Java 11: current Scylla targets source/target 11
docker run --rm -v "$PWD":/app -w /app maven:3.9-eclipse-temurin-11 \
  sh -c 'mvn -q clean; mvn -q package -DskipTests'

cp target/scylla-0.0.1-SNAPSHOT.jar /path/to/bps_clean/spike/scylla.jar
cp -r target/libs /path/to/bps_clean/spike/
```

Three details, each load-bearing:

- **`--keep-cr`.** The tracked files use CRLF. Without it `git am` strips the
  carriage returns from the patch context, which then matches nothing and the
  first patch fails on `plugins_list`.
- **Two invocations of Maven.** The jars in `lib/` are installed by
  `install-file` goals bound to the `clean` phase, so a single `mvn package`
  cannot resolve them -- and `mvn clean package` resolves dependencies before
  `clean` runs, so that fails the same way.
- **`libs/` travels with the jar.** The manifest's `Class-Path` is relative.

Verified: all eight apply cleanly onto `5159b53` with the command above.

## Scope of the change

Patch 0001 is plugin-only — three new classes and one line in `plugins_list`.

Patch 0002 also touches two core files, because a plugin alone cannot do it:

- `ProcessSimulationComponents` gains `getDistributionSample(nodeId, resourceIds)`
  and a registry a plugin fills. With an empty registry it delegates to the
  existing single-argument method, so nothing changes for models without the new
  element.
- `TaskBeginEvent` passes the resource tuple it already holds.

Both are additive. A model without `<resourceDurations>` or `<arrivalCalendar>`
behaves exactly as before.

## Worth reporting upstream

`DateTimeUtils.getTimeTableIndexWithinOrNext` ranks candidate windows with
`getNextOrSameZonedDateTime`, which can return a window whose start already
passed earlier the same day. The resulting negative duration makes the helper
pick a window in the past. The arrival calendar plugin computes its own delay to
avoid this, but **resource calendars use the same helper**, so the same weakness
may affect them.

Observed while developing patch 0001: arrivals at 21:00 and 22:00 on Mondays
against a calendar closing at 20:00.

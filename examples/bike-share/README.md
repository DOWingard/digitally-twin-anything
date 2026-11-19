# Example: a twin of a bike-share export

A generated export of a fictional docked bike-share operator, and the manifest of a
static twin of it. Files only: no live source, no controls.

| File | What it is |
|---|---|
| `generate.py` | Writes the export. The same seed gives the same files. |
| `manifest.yaml` | The twin's manifest as an agent wrote it with the build skill, without what the toolkit derives (the source's profile, the ontology's signature, what the user confirmed, the signature). |
| `ontology/cykelbo.yaml` | The twin's types (LinkML): districts, stations, bikes, trips, weather. |

## The data

```sh
python generate.py OUT_DIR [--seed 7] [--days 21] [--stations 80]
```

It needs `numpy`, `pandas` and `pyarrow`; the image `dta-runtime` has them, so with the
plugin's images built no Python is needed on the machine:

```sh
docker run --rm -u "$(id -u):$(id -g)" \
  -v "$PWD/examples/bike-share:/example:ro" -v "$HOME/bikes:/out" \
  --entrypoint python dta-runtime:0.1.0 /example/generate.py /out/export
```

`OUT_DIR/description.md` is what the operator would say about the export; the rest is
the data: districts, stations, bikes, hourly weather, one file of trips per day (Parquet)
and one file of station status per day (JSON lines, one row per station per 15 minutes).
With the defaults: 80 stations, 797 bikes, 40,275 trips, 161,280 status rows, 19 MB.

The data comes from a simulation, so it is consistent: a trip moves one bike from a
station that had one to a station with a free dock, a station's status is the running
balance of trips and of the nightly rebalancing, and a station that is not renting
neither gives nor takes bikes.

## The twin

Four questions, each with the error its user accepts:

| Question | Kind | Tolerance |
|---|---|---|
| How many bikes and free docks did a station have at a time, and how did that change? | descriptive | 1 bike (MAE) |
| Which stations stood empty, or full, and for how long? | diagnostic | 5 % wrong |
| How many bikes will a station have one hour later? | predictive | 2 bikes (MAE) |
| Between which pairs of stations do the most trips go? | descriptive | 5 % wrong |

The forecast has three candidate models (persistence, seasonal naive, ETS); the build
backtests them and keeps the best that meets the tolerance. The repository's README
lists the commands that build this twin.

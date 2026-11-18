# A second target: a bike-share export

A generated export of a fictional docked bike-share operator, used to check that a twin
can be built of something unlike a water network: files only, no live source, no
controls.

```sh
uv run python testbed-b/generate.py OUT_DIR [--seed 7] [--days 21] [--stations 80]
```

The same seed gives the same files. `OUT_DIR/description.md` is what the operator would
say about the export; the rest is the data: districts, stations, bikes, hourly weather,
one file of trips per day (Parquet) and one file of station status per day (JSON lines,
one row per station per 15 minutes).

The data comes from a simulation, so it is consistent: a trip moves one bike from a
station that had one to a station with a free dock, a station's status is the running
balance of trips and of the nightly rebalancing, and a station that is not renting
neither gives nor takes bikes.

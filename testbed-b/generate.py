"""Generate the export of a fictional bike-share operator: a second, static target for
building a twin, in a domain unlike the water utility's.

The system is simulated, so the files agree with each other: a trip takes a bike from a
station that had one and brings it to a station that had a free dock, and the station
status files are the running balance of those trips and of the night's rebalancing.

    uv run python testbed-b/generate.py OUT_DIR [--seed 7] [--days 21] [--stations 80]

Writes into OUT_DIR: districts.csv, stations.csv, bikes.csv, weather.csv (hourly, local
time), trips/trips-<date>.parquet (one file per day), status/status-<date>.jsonl (one
row per station per 15 minutes), description.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

STEP = timedelta(minutes=15)
STEPS_PER_DAY = 96
START = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday
LOCAL = "Europe/Stockholm"
DISTRICTS = ("Harbour", "Old Town", "University", "Northside", "Riverside", "Station Quarter")
MODELS = ("classic", "classic", "classic", "electric")


def log(level: str, message: str, **context: Any) -> None:
    line = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": level,
        "context": "testbed_b.generate",
        "message": message,
        **context,
    }
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


def hourly_shape(kind: str) -> np.ndarray:
    """Share of a day's departures per hour, for a residential or a workplace station."""
    hours = np.arange(24)
    morning = np.exp(-0.5 * ((hours - 8) / 1.3) ** 2)
    evening = np.exp(-0.5 * ((hours - 17.5) / 1.6) ** 2)
    midday = 0.35 * np.exp(-0.5 * ((hours - 13) / 3.0) ** 2)
    shape = 1.6 * morning + 0.7 * evening if kind == "home" else 0.6 * morning + 1.7 * evening
    shape = shape + midday + 0.02
    return shape / shape.sum()


def make_stations(rng: np.random.Generator, count: int) -> pd.DataFrame:
    centres = rng.uniform(-1.0, 1.0, size=(len(DISTRICTS), 2)) * np.array([0.035, 0.02])
    rows = []
    for i in range(count):
        d = int(rng.integers(len(DISTRICTS)))
        lat = 59.33 + centres[d, 1] + rng.normal(0, 0.004)
        lon = 18.06 + centres[d, 0] + rng.normal(0, 0.007)
        rows.append(
            {
                "station_id": f"S{i + 1:03d}",
                "name": f"{DISTRICTS[d]} {int(rng.integers(1, 60))}",
                "district_id": f"D{d + 1}",
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "capacity": int(rng.choice([12, 15, 18, 20, 24, 30])),
                "kind": "home" if rng.random() < 0.55 else "work",
                "opened_on": (
                    datetime(2019, 4, 1) + timedelta(days=int(rng.integers(0, 2200)))
                ).date(),
            }
        )
    return pd.DataFrame(rows)


def weather(rng: np.random.Generator, days: int) -> pd.DataFrame:
    hours = pd.date_range(START, periods=days * 24, freq="h")
    daily = 4 + 3 * np.sin(np.arange(days) / 6.0) + rng.normal(0, 1.5, days)
    temp = np.repeat(daily, 24) + 4 * np.sin((np.tile(np.arange(24), days) - 9) / 24 * 2 * np.pi)
    wet_day = rng.random(days) < 0.3
    rain = np.where(np.repeat(wet_day, 24), rng.gamma(0.6, 1.2, days * 24), 0.0)
    rain[rng.random(days * 24) < 0.5] = 0.0
    return pd.DataFrame({"t": hours, "temp_c": np.round(temp, 1), "rain_mm": np.round(rain, 1)})


def simulate(
    rng: np.random.Generator, stations: pd.DataFrame, wx: pd.DataFrame, days: int, per_day: float
):
    n = len(stations)
    cap = stations["capacity"].to_numpy()
    xy = stations[["lon", "lat"]].to_numpy() * np.array([55.0, 111.0])  # km
    dist = np.linalg.norm(xy[:, None, :] - xy[None, :, :], axis=2)
    popularity = rng.gamma(2.0, 1.0, n)
    popularity /= popularity.sum()
    is_home = (stations["kind"] == "home").to_numpy()
    shapes = np.where(
        is_home[:, None], hourly_shape("home")[None, :], hourly_shape("work")[None, :]
    )
    # Where a trip goes: nearer and more popular stations more, and to the other kind.
    pull = (
        popularity[None, :]
        * np.exp(-dist / 2.5)
        * np.where(is_home[:, None] != is_home[None, :], 1.6, 1.0)
    )
    np.fill_diagonal(pull, pull.diagonal() * 0.15)
    pull /= pull.sum(axis=1, keepdims=True)
    nearest = np.argsort(dist, axis=1)

    bikes = np.minimum(np.round(cap * 0.5).astype(int), cap)
    fleet = [f"B{i + 1:04d}" for i in range(int(bikes.sum()))]
    docked: list[list[str]] = []
    it = iter(fleet)
    for s in range(n):
        docked.append([next(it) for _ in range(bikes[s])])
    arriving: dict[int, list[tuple[int, str, dict]]] = {}
    trips: list[dict] = []
    status: list[dict] = []
    out_of_service = {
        int(s): (int(rng.integers(0, days * STEPS_PER_DAY - 40)), int(rng.integers(8, 36)))
        for s in rng.choice(n, 4, replace=False)
    }
    trip_no = 0
    for k in range(days * STEPS_PER_DAY):
        now = START + k * STEP
        hour, weekday = now.hour, now.weekday()
        w = wx.iloc[k // 4]
        factor = (
            (0.55 if weekday >= 5 else 1.0)
            * (0.45 if w.rain_mm > 0.5 else 1.0)
            * (0.8 + 0.02 * max(float(w.temp_c), 0.0))
        )
        renting = np.array(
            [
                not (s in out_of_service and out_of_service[s][0] <= k < sum(out_of_service[s]))
                for s in range(n)
            ]
        )
        # Arrivals due now dock, or ride on to the nearest station with a free dock.
        for dest, bike, trip in arriving.pop(k, []):
            for candidate in nearest[dest]:
                if renting[candidate] and len(docked[candidate]) < cap[candidate]:
                    docked[candidate].append(bike)
                    trip["end_station"] = stations["station_id"].iloc[candidate]
                    break
        # The night crew evens the stations out at 03:00.
        if hour == 3 and now.minute == 0:
            pool = [b for s in range(n) for b in docked[s]]
            rng.shuffle(pool)
            target = np.floor(cap * len(pool) / cap.sum()).astype(int)
            for s in range(n):
                docked[s] = [pool.pop() for _ in range(min(target[s], len(pool)))]
            for s in rng.permutation(n):
                while pool and len(docked[s]) < cap[s]:
                    docked[s].append(pool.pop())
        wanted = rng.poisson(per_day * factor * popularity * shapes[:, hour] / 4.0)
        for s in np.nonzero(wanted)[0]:
            if not renting[s]:
                continue
            for _ in range(int(wanted[s])):
                if not docked[s]:
                    break
                bike = docked[s].pop(int(rng.integers(len(docked[s]))))
                dest = int(rng.choice(n, p=pull[s]))
                minutes = max(3.0, dist[s, dest] / 0.22 + rng.normal(4, 3))
                started = now + timedelta(seconds=int(rng.integers(0, 900)))
                ended = started + timedelta(seconds=int(minutes * 60))
                trip_no += 1
                trip = {
                    "trip_id": f"T{trip_no:07d}",
                    "bike_id": bike,
                    "started_at": started,
                    "ended_at": ended,
                    "start_station": stations["station_id"].iloc[s],
                    "end_station": None,
                    "member": bool(rng.random() < 0.72),
                }
                trips.append(trip)
                due = int((ended - START) / STEP) + 1
                arriving.setdefault(due, []).append((dest, bike, trip))
        for s in range(n):
            status.append(
                {
                    "station_id": stations["station_id"].iloc[s],
                    "ts": now + STEP,
                    "bikes_available": len(docked[s]),
                    "docks_available": int(cap[s]) - len(docked[s]),
                    "is_renting": bool(renting[s]),
                }
            )
    done = [t for t in trips if t["end_station"] is not None]
    return pd.DataFrame(done), pd.DataFrame(status), fleet


DESCRIPTION = """# Cykelbo bike share: data export

Cykelbo runs a docked bike-share scheme in one city. This folder is an export made for
analysis; nothing here is updated after the export.

- `districts.csv`: the six districts of the service area.
- `stations.csv`: one row per docking station: where it is, its district, how many docks
  it has (`capacity`), whether it mostly serves homes or workplaces (`kind`), and when it
  opened.
- `bikes.csv`: the fleet. Electric bikes joined later than classic ones.
- `trips/trips-<date>.parquet`: one file per day (by start, UTC). A trip takes one bike
  from `start_station` to `end_station`. Times are UTC.
- `status/status-<date>.jsonl`: what each station reported every 15 minutes: bikes that
  can be taken, free docks, and whether the station was renting. `ts` is UTC and is the
  end of the 15 minutes it describes. A station that is not renting neither gives nor
  takes bikes.
- `weather.csv`: hourly temperature and rain from the city's weather station. `t` is
  local time (Europe/Stockholm) without an offset.

Every night at 03:00 UTC a crew moves bikes between stations to even them out.
"""


def write(out: Path, seed: int, days: int, count: int, per_day: float) -> dict[str, int]:
    rng = np.random.default_rng(seed)
    stations = make_stations(rng, count)
    wx = weather(rng, days)
    trips, status, fleet = simulate(rng, stations, wx, days, per_day)
    out.mkdir(parents=True, exist_ok=True)
    (out / "trips").mkdir(exist_ok=True)
    (out / "status").mkdir(exist_ok=True)
    pd.DataFrame(
        {"district_id": [f"D{i + 1}" for i in range(len(DISTRICTS))], "name": DISTRICTS}
    ).to_csv(out / "districts.csv", index=False)
    stations.to_csv(out / "stations.csv", index=False)
    since = [
        (datetime(2021, 5, 1) + timedelta(days=int(d))).date()
        for d in rng.integers(0, 1500, len(fleet))
    ]
    models = rng.choice(MODELS, len(fleet))
    pd.DataFrame({"bike_id": fleet, "model": models, "in_service_since": since}).to_csv(
        out / "bikes.csv", index=False
    )
    local = wx.assign(
        t=wx["t"].dt.tz_convert(LOCAL).dt.tz_localize(None).dt.strftime("%Y-%m-%d %H:%M")
    )
    local.to_csv(out / "weather.csv", index=False)
    for day, part in trips.groupby(trips["started_at"].dt.date):
        part.to_parquet(out / "trips" / f"trips-{day}.parquet", index=False)
    status_day = (status["ts"] - STEP).dt.date
    for day, part in status.groupby(status_day):
        rows = part.assign(ts=part["ts"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        rows.to_json(out / "status" / f"status-{day}.jsonl", orient="records", lines=True)
    (out / "description.md").write_text(DESCRIPTION)
    return {
        "stations": len(stations),
        "bikes": len(fleet),
        "trips": len(trips),
        "status_rows": len(status),
        "days": days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--stations", type=int, default=80)
    parser.add_argument("--trips-per-day", type=float, default=2600.0)
    args = parser.parse_args()
    started = time.monotonic()
    log(
        "info",
        "generation started",
        out=str(args.out),
        seed=args.seed,
        days=args.days,
        stations=args.stations,
    )
    try:
        counts = write(args.out, args.seed, args.days, args.stations, args.trips_per_day)
    except OSError as exc:
        log(
            "error",
            "the export could not be written",
            out=str(args.out),
            error=repr(exc),
            stack=traceback.format_exc(),
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - reported with its context, then the process fails
        log(
            "error",
            "generation failed",
            seed=args.seed,
            error=repr(exc),
            stack=traceback.format_exc(),
        )
        return 1
    log("info", "generation finished", duration_s=round(time.monotonic() - started, 1), **counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

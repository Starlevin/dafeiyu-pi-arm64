"""Build an offline daily digest from the local model-hub usage ledger."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile

from usage_ledger import COUNTER_KEYS, CST, SCHEMA


MAX_LEDGER_BYTES = 8 * 1024 * 1024
REPORT_VERSION = 1


def read_object(path: Path, limit: int) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input too large")
    try:
        data = json.loads(raw)
    except RecursionError as error:
        raise ValueError("input too deeply nested") from error
    if not isinstance(data, dict):
        raise ValueError("input is not an object")
    return data


def valid_counters(values) -> bool:
    return (
        len(values) == len(COUNTER_KEYS)
        and all(type(n) is int and 0 <= n < 2**53 for n in values)
        and values[4] <= values[3]
    )


def daily_totals(path: Path, day: str) -> dict[str, int]:
    ledger = read_object(path, MAX_LEDGER_BYTES)
    if ledger.get("schema") != SCHEMA or not isinstance(ledger.get("buckets"), dict):
        raise ValueError("unsupported ledger")
    bucket = ledger["buckets"].get(day)
    if not isinstance(bucket, dict):
        raise ValueError("no daily bucket")
    totals = [0] * len(COUNTER_KEYS)
    for counts in bucket.values():
        if not isinstance(counts, list) or not valid_counters(counts):
            raise ValueError("invalid counter row")
        totals = [total + value for total, value in zip(totals, counts)]
    if not valid_counters(totals):
        raise ValueError("invalid aggregate")
    return dict(zip(COUNTER_KEYS, totals))


def cached_totals(path: Path, day: str) -> dict[str, int]:
    cache = read_object(path, 1024)
    if cache.get("version") != REPORT_VERSION or cache.get("date") != day:
        raise ValueError("incompatible cache")
    raw = cache.get("totals")
    if not isinstance(raw, dict) or set(raw) != set(COUNTER_KEYS):
        raise ValueError("invalid cached counters")
    totals = {key: raw[key] for key in COUNTER_KEYS}
    if not valid_counters(list(totals.values())):
        raise ValueError("invalid cached totals")
    return totals


def write_snapshot(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".daily-digest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def make_digest(ledger: Path, cache_dir: Path, day: date) -> dict:
    key = day.isoformat()
    cache = cache_dir / f"{key}.json"
    if ledger.resolve() == cache.resolve():
        raise ValueError("cache and ledger must be separate files")
    header = {"version": REPORT_VERSION, "date": key}
    try:
        totals = daily_totals(ledger, key)
    except (OSError, ValueError):
        try:
            totals = cached_totals(cache, key)
        except (OSError, ValueError):
            return {**header, "status": "unavailable", "totals": None}
        return {**header, "status": "cached", "totals": totals}

    report = {**header, "status": "fresh", "totals": totals}
    try:
        write_snapshot(cache, report)
    except OSError:
        report["cache_write"] = "failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--date", type=date.fromisoformat,
                        default=datetime.now(CST).date() - timedelta(days=1))
    args = parser.parse_args()
    try:
        report = make_digest(args.ledger, args.cache_dir, args.date)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, separators=(",", ":")))
    return 2 if report["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())

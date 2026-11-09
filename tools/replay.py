#!/usr/bin/env python3
"""Replay the correction over an archive and print how each variant would have done.

The archive is the SQLite file the integration writes to
``<config>/pv_forecast_correction/``. Every variant runs the integration's own engine
over the archived base forecasts and measurements, as it would have run live, and is
compared at the same checkpoints: day total as of 07:00, rest of the day as of 09:00 to
14:00, tomorrow as of 21:00.

Variants:
  base           the base forecast as it was published
  level          layer A (level factor), layer B off
  level+fog      layer A plus the fog layer alone, one run per gate candidate
  level+B        layer A plus layer B as it runs: the short-term part alone, and with the
                 fog layer (chosen each night, or always)
  level+weather  layer A plus layer C (DWD MOSMIX), when the archive holds weather runs

Usage:
  python tools/replay.py ARCHIVE [--from 2026-06-01] [--to 2026-10-01] [--days]

No Home Assistant installation is needed; only the Python standard library.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CORE = Path(__file__).resolve().parents[1] / "custom_components" / "pv_forecast_correction" / "core"


def _load_core():
    if "pvfc_core" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "pvfc_core", CORE / "__init__.py", submodule_search_locations=[str(CORE)]
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {CORE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["pvfc_core"] = module
        spec.loader.exec_module(module)


_load_core()

from pvfc_core.archive import Archive  # noqa: E402
from pvfc_core.engine import Settings  # noqa: E402
from pvfc_core.fog import FogGate, FogMode, FogSettings  # noqa: E402
from pvfc_core.intraday import FogPart, IntradaySettings  # noqa: E402
from pvfc_core.level import LevelSettings  # noqa: E402
from pvfc_core.replay import ReplayResult, replay  # noqa: E402
from pvfc_core.score import (  # noqa: E402
    BASE,
    CLEAR,
    CORRECTED,
    DULL,
    MIXED,
    Criterion,
    DayReport,
    classify_days,
    gate_criteria,
    spells,
)
from pvfc_core.weather import WeatherMode, WeatherSettings  # noqa: E402

WEATHER_VARIANTS = {
    "trust 0.5": {"trust": 0.5},
    "trust 1": {"trust": 1.0},
    "trust 1, only lowers": {"trust": 1.0, "max_factor": 1.0},
}

FOG_ONLY = IntradaySettings(enabled=False, fog_part=FogPart.ALWAYS)
"""Layer B as the fog layer alone (gate candidates)."""

DEFAULT_GATES = (
    FogGate(None, None),
    FogGate(0.5, None),
    FogGate(0.6, None),
    FogGate(0.7, None),
    FogGate(0.5, 0.5),
    FogGate(0.6, 0.5),
    FogGate(0.7, 0.5),
)


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:+.0f} %"


def _mae(reports: list[DayReport], variant: str, key: str | None = None) -> float | None:
    values = []
    for report in reports:
        errors = report.variants.get(variant)
        if not report.complete or errors is None:
            continue
        if key is None:
            values.extend(abs(v) for v in errors.rest.values())
        elif key == "day":
            if errors.day is not None:
                values.append(abs(errors.day))
        elif key == "day_ahead":
            if errors.day_ahead is not None:
                values.append(abs(errors.day_ahead))
        elif key in errors.rest:
            values.append(abs(errors.rest[key]))
    return sum(values) / len(values) if values else None


def _row(name: str, reports: list[DayReport], variant: str, classes: dict[date, str]) -> list[str]:
    checkpoints = [f"{hour:02d}:00" for hour in range(9, 15)]
    cells = [name, _fmt(_mae(reports, variant))]
    cells += [_fmt(_mae(reports, variant, key)) for key in checkpoints]
    for day_class in (CLEAR, MIXED, DULL):
        selected = [r for r in reports if classes.get(r.day) == day_class]
        cells.append(_fmt(_mae(selected, variant)))
    cells += [_fmt(_mae(reports, variant, "day")), _fmt(_mae(reports, variant, "day_ahead"))]
    return cells


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _criteria_table(criteria: list[Criterion]) -> str:
    rows = []
    for item in criteria:
        verdict = {True: "pass", False: "FAIL", None: "no data"}[item.passed]
        rows.append(
            [
                item.name,
                _fmt(item.reference),
                _fmt(item.candidate),
                _pct(item.change),
                f"{item.limit * 100:+.0f} %",
                verdict,
            ]
        )
    return _table(["criterion", "reference", "candidate", "change", "limit", "verdict"], rows)


def _verdict(criteria: list[Criterion]) -> str:
    if any(c.passed is None for c in criteria):
        return "not decidable (missing data)"
    return "PASS" if all(c.passed for c in criteria) else "FAIL"


def _rest_mae(report: DayReport, variant: str) -> float | None:
    errors = report.variants.get(variant)
    return None if errors is None else errors.rest_mae


def _day_table(
    level_reports: dict[date, DayReport],
    fog_runs: dict[str, tuple[FogGate | None, ReplayResult]],
    classes: dict[date, str],
) -> str:
    gates = list(fog_runs)
    header = ["day", "class", "actual", "f", "P10/P50", "base", "level"]
    header += [f"B{index + 1}" for index in range(len(gates))]
    rows = []
    for day in sorted(level_reports):
        report = level_reports[day]
        row = [
            day.isoformat(),
            classes.get(day, "-"),
            _fmt(report.actual_kwh, 1),
            _fmt(report.level_factor),
            _fmt(report.base_confidence),
            _fmt(_rest_mae(report, BASE)),
            _fmt(_rest_mae(report, CORRECTED)),
        ]
        for gate in gates:
            fog_report = fog_runs[gate][1].reports.get(day)
            if fog_report is None or CORRECTED not in fog_report.variants:
                row.append("-")
                continue
            mark = "*" if fog_report.fog_active else ""
            row.append(_fmt(_rest_mae(fog_report, CORRECTED)) + mark)
        rows.append(row)
    legend = ", ".join(f"B{index + 1} = {gate}" for index, gate in enumerate(gates))
    return (
        _table(header, rows)
        + f"\n\nRest-of-day MAE per day in kWh; * = fog layer active. {legend}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--tz", help="IANA time zone of the local days (default: as stored in the archive)"
    )
    parser.add_argument("--from", dest="first", type=date.fromisoformat, help="first day")
    parser.add_argument("--to", dest="last", type=date.fromisoformat, help="last day")
    parser.add_argument("--level-days", type=int, help="days learned from")
    parser.add_argument("--shrink-days", type=float, help="start-up weight in days")
    parser.add_argument("--level-min", type=float, help="lowest level factor")
    parser.add_argument("--level-max", type=float, help="highest level factor")
    parser.add_argument("--fog-recent-hours", type=float, help="recent window of the fog layer")
    parser.add_argument("--fog-min-factor", type=float, help="lowest fog factor")
    parser.add_argument(
        "--one-factor", action="store_true", help="level factor without the time-of-day curve"
    )
    parser.add_argument("--title", default=None, help="heading for the output")
    parser.add_argument("--days", action="store_true", help="also list every day")
    args = parser.parse_args(argv)

    archive = Archive(args.archive)
    archive.open()
    tz_name = args.tz or archive.get_meta("time_zone")
    if not tz_name:
        archive.close()
        parser.error("the archive has no time zone; pass --tz, e.g. --tz Europe/Berlin")
    tz = ZoneInfo(tz_name)
    # Defaults: the settings the integration ran with (stored in the archive), else the
    # integration's defaults; command-line options override both.
    stored = json.loads(archive.get_meta("settings") or "{}")
    stored_level = stored.get("level", {})
    stored_fog = stored.get("fog", {})

    def pick(value, stored_value, default):
        if value is not None:
            return value
        return default if stored_value is None else stored_value

    level = LevelSettings(
        days=int(pick(args.level_days, stored_level.get("days"), LevelSettings().days)),
        shrink_days=pick(
            args.shrink_days, stored_level.get("shrink_days"), LevelSettings().shrink_days
        ),
        min_factor=pick(args.level_min, stored_level.get("min_factor"), LevelSettings().min_factor),
        max_factor=pick(args.level_max, stored_level.get("max_factor"), LevelSettings().max_factor),
        by_time=not args.one_factor
        and bool(pick(None, stored_level.get("by_time"), LevelSettings().by_time)),
    )
    fog_base = FogSettings(
        recent_hours=pick(
            args.fog_recent_hours, stored_fog.get("recent_hours"), FogSettings().recent_hours
        ),
        min_factor=pick(
            args.fog_min_factor, stored_fog.get("min_factor"), FogSettings().min_factor
        ),
    )

    snapshots = archive.snapshots()
    bins = archive.bins()
    weather = archive.weather_snapshots()
    archive.close()
    has_band = any(snapshot.forecast.has_bands for snapshot in snapshots)

    def run(settings: Settings) -> ReplayResult:
        # The engine always runs over the whole archive, so the level factor is warmed
        # up when the reported range starts.
        return replay(snapshots, bins, settings, tz, weather=weather)

    level_run = run(Settings(level=level, fog=replace(fog_base, mode=FogMode.OFF)))
    one_factor_run = (
        run(Settings(level=replace(level, by_time=False), fog=replace(fog_base, mode=FogMode.OFF)))
        if level.by_time
        else None
    )
    fog_runs: dict[str, tuple[FogGate | None, ReplayResult]] = {
        f"fog {gate.name}": (
            gate,
            run(
                Settings(
                    level=level,
                    fog=replace(fog_base, mode=FogMode.ON, gate=gate),
                    intraday=FOG_ONLY,
                )
            ),
        )
        for gate in DEFAULT_GATES
    }
    # Layer B as it runs, with the gate the integration ran with (else "consistency 0.7":
    # the nightly choice keeps the fog layer out of seasons where it does not help).
    stored_gate = stored_fog.get("gate") or {}
    b_gate = FogGate(
        stored_gate.get("threshold", 0.7) if stored_gate else 0.7,
        stored_gate.get("min_confidence") if stored_gate else None,
    )
    for label, part in (
        ("short-term", FogPart.NEVER),
        (f"short-term + fog, nightly choice ({b_gate.name})", FogPart.AUTO),
        (f"short-term + fog, always ({b_gate.name})", FogPart.ALWAYS),
    ):
        fog_runs[label] = (
            None if part is FogPart.NEVER else b_gate,
            run(
                Settings(
                    level=level,
                    fog=replace(fog_base, mode=FogMode.ON, gate=b_gate),
                    intraday=IntradaySettings(fog_part=part),
                )
            ),
        )
    weather_runs = (
        {
            label: run(
                Settings(
                    level=level,
                    fog=replace(fog_base, mode=FogMode.OFF),
                    weather=WeatherSettings(mode=WeatherMode.ON, **options),
                )
            )
            for label, options in WEATHER_VARIANTS.items()
        }
        if weather
        else {}
    )

    def in_range(day: date) -> bool:
        return (args.first is None or day >= args.first) and (args.last is None or day <= args.last)

    reports = {d: r for d, r in level_run.reports.items() if in_range(d)}
    complete = [r for r in sorted(reports.values(), key=lambda r: r.day) if r.complete]
    classes = classify_days({r.day: r.actual_kwh for r in complete})

    print(f"# {args.title or 'Replay ' + str(args.archive.name)}\n")
    print(
        f"Level factor: {level.days} days, start-up weight {level.shrink_days:g} days, "
        f"limits {level.min_factor:g}-{level.max_factor:g}, "
        f"{'by time of day' if level.by_time else 'one factor per day'}. Fog layer: recent window "
        f"{fog_base.recent_hours:g} h, lowest factor {fog_base.min_factor:g}. "
        f"Time zone {tz_name}.\n"
    )
    if not complete:
        print("No complete days in the archive.")
        return 1
    counts = {c: sum(1 for d in classes.values() if d == c) for c in (CLEAR, MIXED, DULL)}
    print(
        f"{len(complete)} complete days, {complete[0].day} … {complete[-1].day} "
        f"(clear {counts[CLEAR]}, mixed {counts[MIXED]}, dull {counts[DULL]}); "
        f"{len(reports) - len(complete)} incomplete days left out.\n"
    )

    header = ["variant", "MAE all", "09", "10", "11", "12", "13", "14"]
    header += ["clear", "mixed", "dull", "day 07h", "day-ahead 21h"]
    rows = [_row("base", complete, BASE, classes)]
    if one_factor_run is not None:
        one_factor = [
            r for d, r in sorted(one_factor_run.reports.items()) if in_range(d) and r.complete
        ]
        rows.append(_row("level, one factor", one_factor, CORRECTED, classes))
    rows.append(
        _row("level, by time of day" if level.by_time else "level", complete, CORRECTED, classes)
    )
    for label, (_, fog_run) in fog_runs.items():
        fog_reports = [
            r for r in sorted(fog_run.reports.values(), key=lambda r: r.day) if r.complete
        ]
        fog_reports = [r for r in fog_reports if in_range(r.day)]
        rows.append(_row(f"level + {label}", fog_reports, CORRECTED, classes))
    for label, weather_run in weather_runs.items():
        weather_reports = [
            r for d, r in sorted(weather_run.reports.items()) if in_range(d) and r.complete
        ]
        rows.append(_row(f"level + weather ({label})", weather_reports, CORRECTED, classes))
    print("Mean absolute error in kWh (rest of day at 09-14 h, day total as of 07 h, ")
    print("tomorrow as of 21 h the evening before).\n")
    print(_table(header, rows))

    factors = [r.level_factor for r in complete]
    print(
        f"\nLevel factor: min {min(factors):.2f}, median "
        f"{sorted(factors)[len(factors) // 2]:.2f}, max {max(factors):.2f}."
    )

    dull_days = [d for d, c in classes.items() if c == DULL]
    missed = []
    for r in complete:
        errors = r.variants.get(CORRECTED)
        day_forecast = None if errors is None or errors.day is None else errors.day + r.actual_kwh
        if day_forecast and r.actual_kwh < 0.5 * day_forecast:
            missed.append(r.day)
    print(
        f"Dull days (clearness < 0.4): {len(dull_days)} in {len(spells(dull_days))} spells. "
        f"Days below half of the level-corrected forecast as of 07 h: {len(missed)} "
        f"in {len(spells(missed))} spells."
    )

    level_reports = {r.day: r for r in complete}
    print("\n## Layer A (level) against the base\n")
    criteria = gate_criteria(level_reports, level_reports, classes=classes)
    print(_criteria_table(criteria))
    print(f"\nVerdict: {_verdict(criteria)}")

    for label, (gate, fog_run) in fog_runs.items():
        fog_reports = {d: r for d, r in fog_run.reports.items() if in_range(d) and r.complete}
        active = sorted(d for d, r in fog_reports.items() if r.fog_active)
        print(f"\n## Layer B ({label}) against layer A alone\n")
        if gate is not None and gate.min_confidence is not None and not has_band:
            print("Not evaluable: the base forecast in this archive has no P10/P90 band.")
            continue
        print(f"Fog layer active on {len(active)} days in {len(spells(active))} spells.\n")
        criteria = gate_criteria(
            fog_reports,
            level_reports,
            ref_variant=CORRECTED,
            classes=classes,
            classes_checked=(CLEAR, MIXED),
        )
        print(_criteria_table(criteria))
        print(f"\nVerdict: {_verdict(criteria)}")
    if weather_runs:
        stations = sorted({snapshot.forecast.station for snapshot in weather})
        print(f"\nWeather runs: {len(weather)} from station {', '.join(stations)}.")
    for label, weather_run in weather_runs.items():
        weather_reports = {
            d: r for d, r in weather_run.reports.items() if in_range(d) and r.complete
        }
        print(f"\n## Layer C (weather, {label}) against layer A alone\n")
        criteria = gate_criteria(
            weather_reports,
            level_reports,
            ref_variant=CORRECTED,
            classes=classes,
            classes_checked=(CLEAR, MIXED),
        )
        print(_criteria_table(criteria))
        print(f"\nVerdict: {_verdict(criteria)}")
    if args.days:
        print("\n## Days\n")
        print(_day_table(level_reports, fog_runs, classes))
    print(f"\nGenerated {datetime.now().astimezone():%Y-%m-%d %H:%M}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

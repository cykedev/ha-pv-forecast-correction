"""Layer C: weather signal from DWD MOSMIX.

MOSMIX is the Deutscher Wetterdienst's statistically corrected point forecast for about
5,400 stations, issued every six hours for ten days ahead. Among its elements are the
global irradiance per hour (Rad1h), the irradiance relative to a clear sky (RRad1), the
sunshine duration (SunD1), the probability of fog (wwM) and the low cloud cover (Nl).

The layer does not make a forecast of its own. It asks, hour by hour, whether MOSMIX
sees the hour brighter or duller than the base forecast does, compared with how the two
usually relate:

    q_h = (irradiance_h / base_h) / typical_h
    w_h = clamp(q_h ** trust, min_factor, max_factor)

``typical_h`` is the ratio of the two forecasts (of record) over the last days for that
clock hour, drawn toward the ratio over all hours. When both agree as usual, w is 1 and
nothing changes; when MOSMIX expects fog or low cloud where the base expects sun, w
drops for exactly those hours, and it rises where MOSMIX expects more sun than usual.
``trust`` sets how much of the disagreement is believed. Without any learned day the
typical ratio comes from the current forecasts themselves, so the layer works from the
first day and sharpens as days accumulate.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any

from .timeline import as_utc

HOUR = timedelta(hours=1)
KJ_PER_KWH = 3600.0
MAX_DOCUMENT_BYTES = 8_000_000
"""Upper limit for an unpacked MOSMIX document of one station (about 0.4 MB in practice)."""

STATION_PATTERN = re.compile(r"^[A-Z0-9]{4,5}$")
_ELEMENTS = {
    "Rad1h": "irradiance",
    "RRad1": "relative",
    "SunD1": "sunshine",
    "wwM": "fog",
    "Nl": "low_cloud",
}
EXTRA_ELEMENTS = ("wwM6", "wwMh", "VV", "VV10", "N05", "Neff", "Nm", "Nh", "TTT", "Td")
"""Further MOSMIX elements kept in the archive for later evaluation (fog over 6 and 12
hours, visibility, cloud layers, temperature and dew point). The DWD keeps runs for two
days only, so whatever is not archived is lost."""


class WeatherError(ValueError):
    """A MOSMIX document or the station catalogue could not be read."""


class WeatherMode(StrEnum):
    """How the weather layer is used."""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


@dataclass(frozen=True, slots=True)
class WeatherSettings:
    """Parameters of layer C."""

    mode: WeatherMode = WeatherMode.OFF
    station: str | None = None
    trust: float = 0.5
    """Exponent on the disagreement: 1 believes it fully, 0.5 halfway (in log terms)."""
    days: int = 14
    shrink_days: float = 2.0
    """Pseudo-days that draw each hour's typical ratio toward the ratio of all hours."""
    min_factor: float = 0.3
    max_factor: float = 1.5
    """The level factor learns the average over dull and bright days, so the weather layer
    lowers days that MOSMIX sees duller than usual and raises those it sees brighter."""
    min_share: float = 0.1
    """Hours with less base energy than this share of the day's strongest hour stay at 1."""


@dataclass(frozen=True, slots=True)
class WeatherPoint:
    """MOSMIX values for the hour starting at ``start`` (UTC)."""

    start: datetime
    irradiance: float | None
    """Global irradiance in kWh/m² over the hour."""
    relative: float | None = None
    """Irradiance relative to a clear sky, % (MOSMIX caps it at 80)."""
    sunshine: float | None = None
    """Sunshine in hours."""
    fog: float | None = None
    """Probability of fog, %."""
    low_cloud: float | None = None
    """Low cloud cover, %."""
    extra: tuple[tuple[str, float], ...] = ()
    """Further MOSMIX elements by name (archive only; values as published)."""

    def to_list(self) -> list[Any]:
        return [
            int(self.start.timestamp()),
            self.irradiance,
            self.relative,
            self.sunshine,
            self.fog,
            self.low_cloud,
        ]

    @classmethod
    def from_list(cls, row: list[Any]) -> WeatherPoint:
        start = datetime.fromtimestamp(int(row[0]), UTC)
        values = [None if v is None else float(v) for v in row[1:6]]
        return cls(start, *values)


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    """One MOSMIX run for one station."""

    station: str
    issued_at: datetime
    points: tuple[WeatherPoint, ...] = ()

    def as_dict(self) -> dict[datetime, WeatherPoint]:
        return {point.start: point for point in self.points}

    def between(self, start: datetime, end: datetime) -> list[WeatherPoint]:
        return [p for p in self.points if start <= p.start < end]

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "issued_at": self.issued_at.isoformat(),
            "points": [p.to_list() for p in self.points],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WeatherForecast:
        return cls(
            station=str(data["station"]),
            issued_at=as_utc(datetime.fromisoformat(data["issued_at"])),
            points=tuple(WeatherPoint.from_list(row) for row in data.get("points", [])),
        )


# ---------------------------------------------------------------------- reading


def _document(content: bytes) -> str:
    """The KML text of a KMZ (zip) or plain KML download."""
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
                if not names:
                    raise WeatherError("no KML document in the download")
                info = archive.getinfo(names[0])
                if info.file_size > MAX_DOCUMENT_BYTES:
                    raise WeatherError("KML document too large")
                content = archive.read(info)
        except zipfile.BadZipFile as err:
            raise WeatherError(f"not a KMZ file: {err}") from err
    if len(content) > MAX_DOCUMENT_BYTES:
        raise WeatherError("KML document too large")
    return content.decode("latin-1")


def _number(text: str) -> float | None:
    if text in ("-", ""):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_mosmix(content: bytes, station: str) -> WeatherForecast:
    """Read a MOSMIX_L (or MOSMIX_S) single-station document, KMZ or KML.

    The document is read with plain pattern matching: it has a fixed layout, and this
    avoids handing a remote document to an XML parser.
    """
    text = _document(content)
    issued = re.search(r"<dwd:IssueTime>([^<]+)</dwd:IssueTime>", text)
    steps = re.findall(r"<dwd:TimeStep>([^<]+)</dwd:TimeStep>", text)
    if issued is None or not steps:
        raise WeatherError("no issue time or time steps in the MOSMIX document")
    try:
        issued_at = as_utc(datetime.fromisoformat(issued.group(1).replace("Z", "+00:00")))
        ends = [as_utc(datetime.fromisoformat(s.replace("Z", "+00:00"))) for s in steps]
    except ValueError as err:
        raise WeatherError(f"unreadable time in the MOSMIX document: {err}") from err
    columns: dict[str, list[float | None]] = {}
    for element in EXTRA_ELEMENTS:
        found = re.search(rf'dwd:elementName="{element}">\s*<dwd:value>([^<]*)</dwd:value>', text)
        if found is not None:
            values = [_number(v) for v in found.group(1).split()]
            if len(values) == len(ends):
                columns[element] = values
    for element, name in _ELEMENTS.items():
        found = re.search(rf'dwd:elementName="{element}">\s*<dwd:value>([^<]*)</dwd:value>', text)
        if found is None:
            continue
        values = [_number(v) for v in found.group(1).split()]
        if len(values) == len(ends):
            columns[name] = values
    if "irradiance" not in columns:
        raise WeatherError("the MOSMIX document has no global irradiance (Rad1h)")
    points = []
    for index, end in enumerate(ends):
        # Values describe the hour that ends at the time step.
        row = {name: values[index] for name, values in columns.items()}
        irradiance = row.get("irradiance")
        sunshine = row.get("sunshine")
        points.append(
            WeatherPoint(
                start=end - HOUR,
                irradiance=None if irradiance is None else max(irradiance, 0.0) / KJ_PER_KWH,
                relative=row.get("relative"),
                sunshine=None if sunshine is None else sunshine / 3600,
                fog=row.get("fog"),
                low_cloud=row.get("low_cloud"),
                extra=tuple(
                    (element, value)
                    for element in EXTRA_ELEMENTS
                    if (value := row.get(element)) is not None
                ),
            )
        )
    return WeatherForecast(station=station, issued_at=issued_at, points=tuple(points))


@dataclass(frozen=True, slots=True)
class Station:
    """A MOSMIX station of the DWD catalogue."""

    id: str
    name: str
    latitude: float
    longitude: float
    distance_km: float | None = None


def _degrees(value: str) -> float:
    """The catalogue gives degrees and minutes as ``dd.mm``."""
    number = float(value)
    whole = int(number)
    return whole + (number - whole) * 100 / 60


def parse_stations(catalogue: str) -> list[Station]:
    """Stations of the DWD MOSMIX station catalogue (``mosmix_stationskatalog.cfg``)."""
    stations = []
    for line in catalogue.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0] in ("ID", "-----"):
            continue
        station_id = parts[0]
        try:
            latitude, longitude = _degrees(parts[-3]), _degrees(parts[-2])
        except ValueError:
            continue
        if not STATION_PATTERN.match(station_id):
            continue
        stations.append(Station(station_id, " ".join(parts[2:-3]), latitude, longitude))
    return stations


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def nearest_station(stations: Iterable[Station], latitude: float, longitude: float) -> Station:
    """The station closest to a location."""
    best: Station | None = None
    for station in stations:
        km = distance_km(latitude, longitude, station.latitude, station.longitude)
        if best is None or best.distance_km is None or km < best.distance_km:
            best = Station(station.id, station.name, station.latitude, station.longitude, km)
    if best is None:
        raise WeatherError("the station catalogue is empty")
    return best


# ---------------------------------------------------------------------- learning


@dataclass(frozen=True, slots=True)
class WeatherTypical:
    """How MOSMIX irradiance and the base forecast usually relate (kWh/m² per kWh)."""

    ratio: float | None = None
    hours: tuple[tuple[int, float], ...] = field(default=())
    days_used: int = 0
    days: tuple[date, ...] = field(default=())

    def at(self, hour: int) -> float | None:
        return dict(self.hours).get(hour, self.ratio)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ratio": self.ratio,
            "hours": [[h, r] for h, r in self.hours],
            "days_used": self.days_used,
            "days": [d.isoformat() for d in self.days],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WeatherTypical:
        ratio = data.get("ratio")
        return cls(
            ratio=None if ratio is None else float(ratio),
            hours=tuple((int(h), float(r)) for h, r in data.get("hours", [])),
            days_used=int(data.get("days_used", 0)),
            days=tuple(date.fromisoformat(d) for d in data.get("days", [])),
        )


def typical_ratio(
    days: Iterable[tuple[date, Iterable[tuple[int, float, float]]]],
    settings: WeatherSettings,
) -> WeatherTypical:
    """Typical ratio from ``(day, ((local hour, irradiance, base kWh), ...))``, newest days
    last; only the last ``settings.days`` days count."""
    used = [(day, tuple(hours)) for day, hours in days][-settings.days :]
    sums: dict[int, list[float]] = {}
    for _, hours in used:
        for hour, irradiance, base in hours:
            if base <= 0 or irradiance is None:
                continue
            entry = sums.setdefault(hour, [0.0, 0.0, 0.0])
            entry[0] += irradiance
            entry[1] += base
            entry[2] += 1
    total_g = sum(g for g, _, _ in sums.values())
    total_b = sum(b for _, b, _ in sums.values())
    if total_b <= 0 or total_g <= 0:
        return WeatherTypical()
    ratio = total_g / total_b
    mean_hour = sum(b / n for _, b, n in sums.values()) / len(sums)
    weight = settings.shrink_days * mean_hour
    hours = tuple(
        (hour, (g + weight * ratio) / (b + weight)) for hour, (g, b, _) in sorted(sums.items())
    )
    days_used = len([1 for _, hours_of_day in used if hours_of_day])
    return WeatherTypical(ratio, hours, days_used, tuple(day for day, h in used if h))


def hour_factor(
    irradiance: float | None, base: float, typical: float | None, settings: WeatherSettings
) -> float:
    """Weather factor of one hour."""
    if irradiance is None or typical is None or typical <= 0 or base <= 0:
        return 1.0
    disagreement = (irradiance / base) / typical
    if disagreement <= 0:
        return settings.min_factor
    value = disagreement**settings.trust
    return min(max(value, settings.min_factor), settings.max_factor)


def hourly_pairs(
    base_kwh: Mapping[datetime, float],
    weather: Mapping[datetime, WeatherPoint],
    tz: tzinfo,
) -> tuple[tuple[int, float, float], ...]:
    """``(local hour, irradiance, base kWh)`` for every hour known to both."""
    pairs = []
    for start in sorted(base_kwh):
        point = weather.get(start)
        if point is None or point.irradiance is None or base_kwh[start] <= 0:
            continue
        pairs.append((local_hour(start, tz), point.irradiance, base_kwh[start]))
    return tuple(pairs)


def local_hour(start: datetime, tz: tzinfo) -> int:
    """Local clock hour of a UTC hour (its middle, for zones with half-hour offsets)."""
    return (as_utc(start) + HOUR / 2).astimezone(tz).hour

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

LOOKBACK_DAYS = 5
QUARTER_HOUR = timedelta(minutes=15)

# Slack when checking that price steps cover a measurement interval, so float
# rounding on second arithmetic cannot make a fully covered interval look short.
_COVERAGE_TOLERANCE_S = 1e-6


@dataclass
class MeterReading:
    zaehlpunkt: str
    daily_wh: float
    reading_date: str


@dataclass(frozen=True)
class PriceStep:
    """A price (currency per kWh) holding for [start, end). Both UTC."""

    start: datetime
    end: datetime
    price: float


@dataclass
class CostRows:
    """Hourly cost rows plus how each hour was priced, for diagnostics."""

    rows: list[tuple[datetime, float, float]] = field(default_factory=list)
    exact_hours: int = 0
    skipped_hours: int = 0


# Unit prefixes meaning "cents per kWh" rather than currency per kWh.
_CENT_UNIT_PREFIXES = ("ct", "cent", "c/")


def price_scale(unit: str | None) -> float:
    """Factor converting a price entity's unit into currency per kWh.

    Dynamic-tariff sensors commonly publish ct/kWh rather than EUR/kWh, and
    the difference is a silent factor of 100 in every cost row.
    """
    if (unit or "").strip().lower().startswith(_CENT_UNIT_PREFIXES):
        return 0.01
    return 1.0


def _parse_api_time(value: str) -> datetime:
    """Parse a Wiener Netze timestamp ('2026-06-18T08:00:00.000Z') as UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def latest_daily_reading(client, zaehlpunkt: str, *, now: datetime | None = None) -> MeterReading | None:
    now = now or datetime.now()
    von = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    bis = now.strftime("%Y-%m-%d")
    data = client.get_daily_values(zaehlpunkt, von, bis)
    if not data:
        return None

    messwerte = (data.get("zaehlwerke") or [{}])[0].get("messwerte") or []
    if not messwerte:
        return None

    latest = messwerte[-1]
    return MeterReading(
        zaehlpunkt=zaehlpunkt,
        daily_wh=latest["messwert"],
        reading_date=latest["zeitBis"][:10],
    )


def quarter_hour_messwerte(
    client,
    zaehlpunkt: str,
    von: str | None = None,
    bis: str | None = None,
    paginate: bool = False,
    chunk_days: int = 90,
) -> list[dict]:
    data = client.get_quarter_hour_values(
        zaehlpunkt, von, bis, paginate=paginate, chunk_days=chunk_days
    )
    if not data:
        return []
    return (data.get("zaehlwerke") or [{}])[0].get("messwerte") or []


def messwert_intervals(messwerte: list[dict]) -> list[tuple[datetime, datetime, float]]:
    """Raw measurements as (start_utc, end_utc, wh), sorted by start.

    Keeps the API's own resolution instead of collapsing it, so each interval
    can be priced against the price that actually applied to it.
    """
    intervals: list[tuple[datetime, datetime, float]] = []
    for m in messwerte:
        start = _parse_api_time(m["zeitVon"])
        raw_end = m.get("zeitBis")
        end = _parse_api_time(raw_end) if raw_end else start + QUARTER_HOUR
        if end <= start:
            end = start + QUARTER_HOUR
        intervals.append((start, end, m["messwert"]))
    intervals.sort(key=lambda item: item[0])
    return intervals


def bucket_hourly(messwerte: list[dict]) -> list[tuple[datetime, float]]:
    """Sum quarter-hour Wh values into (hour_start_utc, wh) buckets, sorted by time."""
    buckets: dict[datetime, float] = defaultdict(float)
    for start, _end, wh in messwert_intervals(messwerte):
        buckets[start.replace(minute=0, second=0, microsecond=0)] += wh
    return sorted(buckets.items())


def parse_price_data(data: list[dict]) -> list[PriceStep]:
    """Price steps from an EPEX Spot style 'data' attribute.

    Steps keep the source's own granularity. European day-ahead moved to
    15-minute market time units, so this attribute may carry quarter-hourly
    entries; collapsing them to hours would silently discard three prices in
    four.
    """
    parsed: list[tuple[datetime, datetime | None, float]] = []
    for entry in data or []:
        try:
            start = _parse_iso(entry["start_time"])
            price = float(entry["price_per_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if start is None:
            continue
        end = _parse_iso(entry.get("end_time"))
        parsed.append((start, end if end and end > start else None, price))

    parsed.sort(key=lambda item: item[0])

    steps: list[PriceStep] = []
    for idx, (start, end, price) in enumerate(parsed):
        if end is None:
            # A published schedule is contiguous: a price holds until the next
            # one starts. The final entry has no successor, so reuse the width
            # of the step before it.
            if idx + 1 < len(parsed):
                end = parsed[idx + 1][0]
            else:
                width = steps[-1].end - steps[-1].start if steps else timedelta(hours=1)
                end = start + width
        steps.append(PriceStep(start, end, price))
    return steps


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        return None


class PriceSchedule:
    """Sorted price steps supporting time-weighted lookup over an interval.

    Granularity-agnostic on purpose: the same lookup serves daily, hourly,
    quarter-hourly or 5-minute steps, whether they are coarser or finer than
    the measurement interval being priced.
    """

    def __init__(self, steps: Iterable[PriceStep]) -> None:
        self._steps = sorted(steps, key=lambda step: step.start)
        self._starts = [step.start for step in self._steps]

    def __bool__(self) -> bool:
        return bool(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def price_for(self, start: datetime, end: datetime) -> float | None:
        """Time-weighted mean price over [start, end).

        Returns None unless the interval is *fully* covered — a partially
        priced interval would understate cost, which is worse than a gap
        because it looks plausible.
        """
        if not self._steps or end <= start:
            return None

        span = (end - start).total_seconds()
        weighted = 0.0
        covered = 0.0
        for step in self._steps[max(bisect_right(self._starts, start) - 1, 0) :]:
            if step.start >= end:
                break
            overlap = (min(step.end, end) - max(step.start, start)).total_seconds()
            if overlap <= 0:
                continue
            weighted += overlap * step.price
            covered += overlap

        if covered < span - _COVERAGE_TOLERANCE_S:
            return None
        return weighted / covered


def _first_price(
    tiers: Sequence[PriceSchedule], start: datetime, end: datetime
) -> float | None:
    """Price from the first tier that fully covers [start, end)."""
    for schedule in tiers:
        price = schedule.price_for(start, end)
        if price is not None:
            return price
    return None


def compute_hourly_cost(
    messwerte: list[dict],
    tiers: Sequence[PriceSchedule],
    *,
    start_after: datetime | None = None,
    starting_total: float = 0.0,
) -> CostRows:
    """Cost per hour, priced at the measurement's own resolution.

    Each measurement interval is priced against the finest tier that covers
    it and the resulting costs are summed into hourly rows, so an hour whose
    price moved within it costs what it actually cost. Pricing the hourly
    total against an hourly mean instead would be wrong whenever consumption
    correlates with price, which is the whole point of a dynamic tariff.

    An hour is emitted only when every one of its intervals could be priced.
    There is deliberately no whole-hour fallback: tier coverage is monotone,
    so a tier spanning the whole hour necessarily covers each interval within
    it, and any hour that fails here would fail that lookup too. Skipping
    leaves a visible gap instead of a plausible-looking undercount.
    """
    by_hour: dict[datetime, list[tuple[datetime, datetime, float]]] = defaultdict(list)
    for start, end, wh in messwert_intervals(messwerte):
        by_hour[start.replace(minute=0, second=0, microsecond=0)].append((start, end, wh))

    result = CostRows()
    total = starting_total
    for hour in sorted(by_hour):
        if start_after is not None and hour <= start_after:
            continue
        intervals = by_hour[hour]

        costs: list[float] | None = []
        for start, end, wh in intervals:
            price = _first_price(tiers, start, end)
            if price is None:
                costs = None
                break
            costs.append(wh / 1000.0 * price)

        if costs is None:
            result.skipped_hours += 1
            continue

        cost = sum(costs)
        result.exact_hours += 1
        total += cost
        result.rows.append((hour, cost, total))
    return result

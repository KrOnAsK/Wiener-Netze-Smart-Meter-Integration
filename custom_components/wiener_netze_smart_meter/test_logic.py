from datetime import datetime, timedelta, timezone

from logic import (
    PriceSchedule,
    PriceStep,
    bucket_hourly,
    compute_hourly_cost,
    latest_daily_reading,
    parse_price_data,
    price_scale,
)


def _utc(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc)


def _mw(start: datetime, wh: float, minutes: int = 15) -> dict:
    """A quarter-hour measurement in the API's wire format."""
    end = start + timedelta(minutes=minutes)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return {
        "zeitVon": start.strftime(fmt),
        "zeitBis": end.strftime(fmt),
        "messwert": wh,
    }


def _schedule(start: datetime, prices, minutes: int) -> PriceSchedule:
    width = timedelta(minutes=minutes)
    return PriceSchedule(
        PriceStep(start + i * width, start + (i + 1) * width, price)
        for i, price in enumerate(prices)
    )


class StubClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_daily_values(self, zaehlpunkt, von, bis):
        self.calls.append((zaehlpunkt, von, bis))
        return self.payload


# --- daily reading -----------------------------------------------------------


def test_returns_latest_messwert():
    client = StubClient(
        {
            "zaehlwerke": [
                {
                    "messwerte": [
                        {"messwert": 100, "zeitBis": "2026-06-17T22:00:00.000Z"},
                        {"messwert": 200, "zeitBis": "2026-06-18T22:00:00.000Z"},
                    ]
                }
            ]
        }
    )
    reading = latest_daily_reading(client, "AT001", now=datetime(2026, 6, 19))
    assert reading.daily_wh == 200
    assert reading.reading_date == "2026-06-18"
    assert reading.zaehlpunkt == "AT001"


def test_returns_none_when_no_data():
    assert latest_daily_reading(StubClient(None), "AT001") is None
    assert latest_daily_reading(StubClient({"zaehlwerke": []}), "AT001") is None
    assert (
        latest_daily_reading(StubClient({"zaehlwerke": [{"messwerte": []}]}), "AT001")
        is None
    )


def test_uses_lookback_window():
    client = StubClient({"zaehlwerke": [{"messwerte": []}]})
    latest_daily_reading(client, "AT001", now=datetime(2026, 6, 19))
    assert client.calls[0] == ("AT001", "2026-06-14", "2026-06-19")


# --- hourly energy buckets ---------------------------------------------------


def test_bucket_hourly_sums_quarters_into_hours():
    messwerte = [
        {"messwert": 10, "zeitVon": "2026-06-18T08:00:00.000Z"},
        {"messwert": 20, "zeitVon": "2026-06-18T08:15:00.000Z"},
        {"messwert": 30, "zeitVon": "2026-06-18T08:30:00.000Z"},
        {"messwert": 40, "zeitVon": "2026-06-18T08:45:00.000Z"},
        {"messwert": 5, "zeitVon": "2026-06-18T09:00:00.000Z"},
    ]
    assert bucket_hourly(messwerte) == [
        (_utc(2026, 6, 18, 8), 100),
        (_utc(2026, 6, 18, 9), 5),
    ]


def test_bucket_hourly_empty():
    assert bucket_hourly([]) == []


# --- price schedule parsing --------------------------------------------------


def test_parse_price_data_keeps_quarter_hour_steps():
    """Regression: quarter-hour entries must not collapse into one hourly price.

    European day-ahead moved to 15-minute market time units, so the attribute
    now carries four entries per hour. Keying by hour kept only the last.
    """
    data = [
        {"start_time": "2026-08-22T16:00:00+02:00", "price_per_kwh": 0.0257},
        {"start_time": "2026-08-22T16:15:00+02:00", "price_per_kwh": 0.0384},
        {"start_time": "2026-08-22T16:30:00+02:00", "price_per_kwh": 0.0931},
        {"start_time": "2026-08-22T16:45:00+02:00", "price_per_kwh": 0.1416},
    ]
    steps = parse_price_data(data)
    assert len(steps) == 4
    assert [step.price for step in steps] == [0.0257, 0.0384, 0.0931, 0.1416]
    assert steps[0].start == _utc(2026, 8, 22, 14)
    assert steps[0].end == _utc(2026, 8, 22, 14, 15)
    # last entry has no successor, so it reuses the preceding step's width
    assert steps[3].end - steps[3].start == timedelta(minutes=15)


def test_parse_price_data_prefers_explicit_end_time():
    data = [
        {
            "start_time": "2026-06-19T00:00:00+02:00",
            "end_time": "2026-06-19T01:00:00+02:00",
            "price_per_kwh": 0.315624,
        }
    ]
    steps = parse_price_data(data)
    assert steps[0].start == _utc(2026, 6, 18, 22)
    assert steps[0].end == _utc(2026, 6, 18, 23)


def test_parse_price_data_tolerates_unusable_end_time():
    data = [
        {"start_time": "2026-06-19T00:00:00+02:00", "end_time": "x", "price_per_kwh": 0.31},
        {"start_time": "2026-06-19T01:00:00+02:00", "end_time": "x", "price_per_kwh": 0.30},
    ]
    steps = parse_price_data(data)
    assert steps[0].end == steps[1].start
    assert steps[1].end - steps[1].start == timedelta(hours=1)


def test_parse_price_data_handles_dst_offset_change():
    """Both entries read '02:00 local' but sit an hour apart in UTC."""
    data = [
        {"start_time": "2026-10-25T02:00:00+02:00", "price_per_kwh": 0.10},
        {"start_time": "2026-10-25T02:00:00+01:00", "price_per_kwh": 0.20},
    ]
    steps = parse_price_data(data)
    assert [step.start for step in steps] == [_utc(2026, 10, 25, 0), _utc(2026, 10, 25, 1)]
    assert steps[0].end == steps[1].start


def test_parse_price_data_skips_malformed_entries():
    data = [
        {"start_time": "not a date", "price_per_kwh": 0.10},
        {"start_time": "2026-06-19T00:00:00+02:00"},
        {"start_time": "2026-06-19T01:00:00+02:00", "price_per_kwh": "abc"},
        {"start_time": "2026-06-19T02:00:00+02:00", "price_per_kwh": 0.30},
    ]
    steps = parse_price_data(data)
    assert len(steps) == 1
    assert steps[0].price == 0.30


def test_parse_price_data_empty():
    assert parse_price_data([]) == []
    assert parse_price_data(None) == []


# --- price lookup ------------------------------------------------------------


def test_schedule_coarser_than_interval_returns_that_price():
    """A daily or hourly price covers a quarter-hour interval outright."""
    daily = PriceSchedule([PriceStep(_utc(2026, 8, 22, 0), _utc(2026, 8, 23, 0), 0.25)])
    assert daily.price_for(_utc(2026, 8, 22, 16, 45), _utc(2026, 8, 22, 17)) == 0.25


def test_schedule_finer_than_interval_is_time_weighted():
    five_min = _schedule(_utc(2026, 8, 22, 16), [0.10, 0.20, 0.30], minutes=5)
    price = five_min.price_for(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 16, 15))
    assert round(price, 10) == 0.20


def test_five_minute_steps_reproduce_a_quarter_hour_price_exactly():
    """Three identical 5-minute buckets rebuild the 15-minute step losslessly."""
    five_min = _schedule(_utc(2026, 8, 22, 16), [0.1416, 0.1416, 0.1416], minutes=5)
    price = five_min.price_for(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 16, 15))
    assert round(price, 10) == 0.1416


def test_schedule_requires_full_coverage():
    partial = PriceSchedule(
        [PriceStep(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 16, 10), 0.10)]
    )
    assert partial.price_for(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 16, 15)) is None
    assert PriceSchedule([]).price_for(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 17)) is None


def test_schedule_rejects_interval_spanning_a_gap():
    gapped = PriceSchedule(
        [
            PriceStep(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 16, 5), 0.10),
            PriceStep(_utc(2026, 8, 22, 16, 10), _utc(2026, 8, 22, 16, 15), 0.30),
        ]
    )
    assert gapped.price_for(_utc(2026, 8, 22, 16), _utc(2026, 8, 22, 16, 15)) is None


# --- unit scaling ------------------------------------------------------------


def test_price_scale():
    assert price_scale("ct/kWh") == 0.01
    assert price_scale("Cent/kWh") == 0.01
    assert price_scale(" ct/kWh ") == 0.01
    assert price_scale("EUR/kWh") == 1.0
    assert price_scale("€/kWh") == 1.0
    assert price_scale("CZK/kWh") == 1.0
    assert price_scale(None) == 1.0


# --- hourly cost -------------------------------------------------------------


def test_cost_prices_each_quarter_hour_not_the_hourly_mean():
    """The headline case: consumption concentrated in the expensive quarter.

    Real Vienna prices for 2026-08-22 16:00-17:00 local. Averaging them first
    and multiplying by the hourly total understates the bill by ~40%.
    """
    hour = _utc(2026, 8, 22, 16)
    quarter_prices = [0.0257, 0.0384, 0.0931, 0.1416]
    consumption = [50, 50, 50, 850]  # Wh - a load running at :45

    messwerte = [
        _mw(hour + timedelta(minutes=15 * i), wh) for i, wh in enumerate(consumption)
    ]
    tiers = [_schedule(hour, quarter_prices, minutes=15)]

    result = compute_hourly_cost(messwerte, tiers)

    assert result.exact_hours == 1
    assert result.skipped_hours == 0
    assert len(result.rows) == 1

    start, cost, cumulative = result.rows[0]
    expected = sum(wh / 1000.0 * p for wh, p in zip(consumption, quarter_prices))
    assert start == hour
    assert round(cost, 10) == round(expected, 10)
    assert round(cost, 5) == 0.12822
    assert round(cumulative, 10) == round(expected, 10)

    # what the old hourly-mean maths produced, for contrast
    hourly_mean = sum(quarter_prices) / 4
    assert round(sum(consumption) / 1000.0 * hourly_mean, 5) == 0.0747
    assert cost > 1.7 * (sum(consumption) / 1000.0 * hourly_mean)


def test_cost_uses_finest_tier_that_covers_each_interval():
    """A coarse tier still serves intervals the fine tier does not reach."""
    hour = _utc(2026, 8, 22, 16)
    fine = PriceSchedule(
        [PriceStep(hour, hour + timedelta(minutes=15), 1.0)]
    )  # only the first quarter
    coarse = PriceSchedule([PriceStep(hour, hour + timedelta(hours=1), 2.0)])

    messwerte = [_mw(hour + timedelta(minutes=15 * i), 1000) for i in range(4)]
    result = compute_hourly_cost(messwerte, [fine, coarse])

    assert result.exact_hours == 1
    # first quarter at 1.0, remaining three at 2.0
    assert round(result.rows[0][1], 10) == round(1.0 + 3 * 2.0, 10)


def test_cost_skips_hour_when_an_interval_cannot_be_priced():
    hour = _utc(2026, 8, 22, 16)
    partial = PriceSchedule([PriceStep(hour, hour + timedelta(minutes=45), 0.10)])
    messwerte = [_mw(hour + timedelta(minutes=15 * i), 1000) for i in range(4)]

    result = compute_hourly_cost(messwerte, [partial])

    assert result.rows == []
    assert result.skipped_hours == 1
    assert result.exact_hours == 0


def test_cost_skips_only_the_unpriceable_hour():
    priced = _utc(2026, 8, 22, 16)
    unpriced = _utc(2026, 8, 22, 17)
    tiers = [PriceSchedule([PriceStep(priced, priced + timedelta(hours=1), 0.10)])]
    messwerte = [_mw(priced, 1000), _mw(unpriced, 1000)]

    result = compute_hourly_cost(messwerte, tiers)

    assert [row[0] for row in result.rows] == [priced]
    assert result.exact_hours == 1
    assert result.skipped_hours == 1


def test_cost_respects_start_after_and_starting_total():
    first = _utc(2026, 8, 22, 16)
    second = _utc(2026, 8, 22, 17)
    tiers = [PriceSchedule([PriceStep(first, second + timedelta(hours=1), 0.10)])]
    messwerte = [_mw(first, 1000), _mw(second, 1000)]

    result = compute_hourly_cost(
        messwerte, tiers, start_after=first, starting_total=5.0
    )

    assert len(result.rows) == 1
    start, cost, cumulative = result.rows[0]
    assert start == second
    assert round(cost, 10) == 0.10
    assert round(cumulative, 10) == 5.10


def test_cost_accumulates_across_hours():
    hour = _utc(2026, 8, 22, 16)
    tiers = [PriceSchedule([PriceStep(hour, hour + timedelta(hours=3), 0.10)])]
    messwerte = [_mw(hour + timedelta(hours=h), 1000) for h in range(3)]

    result = compute_hourly_cost(messwerte, tiers)

    assert [round(row[2], 10) for row in result.rows] == [0.10, 0.20, 0.30]


def test_cost_with_no_measurements():
    result = compute_hourly_cost([], [])
    assert result.rows == []
    assert result.exact_hours == 0
    assert result.skipped_hours == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"ok - {len(tests)} tests passed")

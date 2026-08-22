# Wiener Netze Smart Meter — Home Assistant Integration

A [HACS](https://hacs.xyz/) custom integration that brings your **Wiener Netze**
smart meter data into Home Assistant using the **official** Wiener Netze Smart
Meter API.

It is a thin Home Assistant layer on top of
[tschoerk's `wiener-netze-smart-meter-api`](https://github.com/tschoerk/Wiener-Netze-Smart-Meter-API)
package (installed automatically from PyPI), which talks to the official
endpoint instead of recreating the web login — so it does not break on
captchas, rate limiting, or website changes.

## Table of Contents

- [Getting your API credentials](#getting-your-api-credentials)
- [Installation](#installation)
- [Configuration](#configuration)
- [What you get](#what-you-get)
- [Cost tracking (dynamic tariff)](#cost-tracking-dynamic-tariff)
- [Services](#services)
- [Notes & limitations](#notes--limitations)
- [Credits & License](#credits--license)

## Getting your API credentials

You need three values from Wiener Stadtwerke / Wiener Netze: a **client ID**, a
**client secret**, and an **API key**. These steps mirror the
[upstream API project](https://github.com/tschoerk/Wiener-Netze-Smart-Meter-API#firststeps):

1. Create an account at the
   [Wiener Stadtwerke Developer Portal](https://api-portal.wienerstadtwerke.at/).
2. [Create an application](https://api-portal.wienerstadtwerke.at/portal/applications/create)
   for the **WN_SMART_METER_API**.
3. When the application is approved you will get an e-mail from the API Developer
   Portal. The **API key** is then found in the details of the newly created
   [application](https://api-portal.wienerstadtwerke.at/portal/applications).
4. Write an e-mail to the
   [Smart Meter Portal Support](mailto:support.sm-portal@wienit.at) to connect
   the application with your Smart Meter Portal user. It usually takes **1–2
   weeks** to get a response.
5. Afterwards the **client ID** and **client secret** can be found in the
   [settings](https://smartmeter-business.wienernetze.at/einstellungen) of the
   Smart Meter Business portal.

## Installation

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/KrOnAsK/Wiener-Netze-Smart-Meter-Integration` with category **Integration**.
3. Install **Wiener Netze Smart Meter**, then restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**, search for
   **Wiener Netze Smart Meter**.
2. Enter your **client ID**, **client secret**, and **API key**. They are stored
   encrypted by Home Assistant — no YAML editing required.

## What you get

- A **Latest daily energy** sensor per meter (`zaehlpunkt`) holding the most
  recent available daily consumption value (Wh). This is informational — see
  the `reading_date` attribute for the day it belongs to.
- **Hourly energy long-term statistics** per meter for the **Energy dashboard**.
  Quarter-hour API data is summed into hourly buckets and imported with correct
  historical timestamps. On first run the last `BACKFILL_DAYS` (default 30, see
  `const.py`) of history is backfilled; use the
  [import service](#services) to backfill everything.

Add the hourly statistic on the Energy dashboard under
**Settings → Dashboards → Energy → Add consumption**, picking
`wiener_netze_smart_meter:<meter>_hourly_energy`.

## Cost tracking (dynamic tariff)

If you have a dynamic tariff with an hourly or quarter-hourly price sensor
(e.g. the [EPEX Spot](https://github.com/mampfes/ha_epex_spot) integration's
*total price* sensor), the integration can compute per-hour cost that is
accurate to the quarter hour.

1. Open the integration's **Configure** dialog and select your price sensor.
2. A new statistic `wiener_netze_smart_meter:<meter>_hourly_cost` (in €) is
   produced.
3. On the Energy dashboard, set **"Use an entity tracking the total costs"** to
   that statistic. (The *current price* and *static price* options are greyed
   out for this source — they need a live entity to multiply against, and these
   are imported historical statistics with no entity behind them.)

### How the price is matched

European day-ahead prices move every 15 minutes, and so does the meter's
quarter-hour data. Each quarter-hour measurement is therefore priced against
the price that actually applied to it, and only then are the costs summed into
hourly rows. Averaging the four prices first and multiplying by the hourly
total understates every hour in which load was shifted into a cheap quarter —
which is the entire point of a dynamic tariff.

Prices come from the first of these sources that fully covers an interval:

| Source | Resolution | Reaches back |
| --- | --- | --- |
| The sensor's published schedule attribute (EPEX Spot style `data`) | native | today + tomorrow |
| Recorder 5-minute short-term statistics | 5 min — resolves quarter-hour steps exactly | ~`purge_keep_days` (default 10) |
| Recorder hourly statistics | hourly mean | indefinitely |

Because measurements arrive 1–2 days late, routine updates are served by the
5-minute tier and are exact. Only the deep backfill from the
[import service](#services) falls back to hourly means, and the attribute
rarely applies at all since it covers today forward.

An hour whose intervals cannot all be priced is skipped rather than reported
low, so a missing price shows as a gap in the graph instead of a plausible but
wrong number.

### What the price sensor must look like

- **Numeric state with a `state_class`.** Recorder only builds statistics for
  such sensors, and with lagged meter data those statistics are the only usable
  price source. A sensor whose state is a status string, with the prices hidden
  in an attribute, yields no cost at all — the picker will still accept it.
- **Per kWh.** `ct/kWh` and `EUR/kWh` are both understood; the unit is read from
  the entity and cents are converted automatically.
- **All-in price.** The integration multiplies energy by whatever price it is
  given. A raw market price produces cost excluding grid fees (*Netzkosten*),
  levies and taxes — add those upstream in the price sensor if you want the
  Energy dashboard to show your real bill.
- Shortening recorder's `purge_keep_days` below the 1–2 day measurement lag
  makes the 5-minute tier stop covering new data, silently dropping cost back
  to hourly means.

To verify it is working, go to **Developer Tools → Statistics** and search for
`hourly_cost`. The `wiener_netze_smart_meter:<meter>_hourly_cost` entry should
appear (unit €, no issue). Add it to a **Statistics Graph** card and sanity-check
one hour by hand against that hour's quarter-hour prices. If it is missing,
check the price sensor against the requirements above and enable debug logging
for `custom_components.wiener_netze_smart_meter` — the cost import logs how many
hours were priced and how many were skipped.

## Services

### `wiener_netze_smart_meter.import_all_history`

Fetches the full available measurement history (the API default is about the
last 3 years) for all meters and rebuilds the hourly energy (and cost)
statistics from scratch. Run it once to seed history; the regular 12-hour
updates keep it current afterwards. It makes many API requests and can take a
while.

## Notes & limitations

- The official API publishes measurements with a **1–2 day delay**, so the most
  recent values always lag by a day or two.
- Home Assistant long-term statistics are bucketed **hourly**, so the Energy
  dashboard cannot display 15-minute resolution. Cost is still *computed* per
  quarter hour and only then summed into the hourly bucket, so the euros are
  exact even though the display granularity is not.
- Cost backfill only reaches as far back as your price integration retained its
  hourly price statistics.

## Credits & License

- API wrapper and credential instructions by
  [tschoerk](https://github.com/tschoerk/Wiener-Netze-Smart-Meter-API).
- Distributed under the [MIT](https://spdx.org/licenses/MIT.html) license.

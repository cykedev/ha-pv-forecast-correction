# PV Forecast Correction

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![CI](https://github.com/cykedev/ha-pv-forecast-correction/actions/workflows/ci.yml/badge.svg)](https://github.com/cykedev/ha-pv-forecast-correction/actions/workflows/ci.yml)
[![Validate](https://github.com/cykedev/ha-pv-forecast-correction/actions/workflows/validate.yml/badge.svg)](https://github.com/cykedev/ha-pv-forecast-correction/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/cykedev/ha-pv-forecast-correction)](LICENSE)

A Home Assistant integration that **corrects an existing PV forecast with what your panels
actually produce** and publishes the result in the same attribute format as the
[Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) integration. Battery and
EV optimisers, dashboards and your own automations can use it as a drop-in source.

It is not a forecast model. The base forecast supplies the weather, the shape of the day and
the uncertainty; this integration only adjusts the **level**, from measured facts. Every
published number is *base forecast × factors*, and the factors are shown next to it. The
base forecast keeps being scored next to the corrected one, so you can always see whether the
correction helps.

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities](#entities)
- [Using the corrected forecast](#using-the-corrected-forecast)
- [Archive and replay](#archive-and-replay)
- [Services](#services)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## How it works

### Level factor (on by default)

Many sites see a forecast that is consistently too high or too low, for reasons the forecast
provider cannot know: shading, soiling, inverter limits, panel ageing, a site parameter that
is slightly off. The level factor removes that bias:

```
f = (Σ measured + K · F̄) / (Σ forecast + K · F̄)      over the last N complete days
corrected = f × base                                 for today and tomorrow
```

- **Forecast of record.** "Forecast" is the value the base forecast held for an interval when
  that interval began, i.e. exactly what was published at the time — not a later revision.
- **Complete days only.** A day counts when at most 10 % of its daylight intervals lack
  measurements. Curtailed intervals (export limit, derating) are left out of both sums.
- **Start-up weight.** K days at ratio 1.0 (F̄ is the mean daily forecast) are added, so a fresh
  install starts neutral and gets close to the full factor within the first week. Defaults:
  N = 14, K = 1, limits 0.3–1.5. On the reference data K = 1 lowered the error by 6–7 %
  against K = 2; it still keeps a single odd first day from taking over.
- **Once per night.** The factor is computed after midnight and stays constant during the
  day, so consumers see a stable number.
- **Works with the base's own corrections.** The factor learns from the forecast as it was
  published, including any dampening the base integration applies. If the base already
  removes the bias, the factor settles near 1.0 and does nothing — that is the expected
  outcome, not a failure.
- **By time of day** (on by default). The same sums per clock hour give a factor for every
  hour of the day, drawn toward the day factor by two days' worth of an average daylight
  hour. Hours that regularly run below or above the rest of the day — fog that lingers in
  the morning in winter, shade at low sun that the base does not model — get their own
  value; hours with little energy stay close to the day factor. The curve only shapes the
  day: it is scaled so that its mean, weighted with each hour's forecast energy, is the
  published factor f. On the reference data it lowered the rest-of-day error by 5 % over a
  summer (107 days) and made no measurable difference over nine autumn days.

### Fog layer (shadow by default)

On days when a whole morning runs far below a confident forecast — typically fog or low
stratus that the weather models miss — the rest of the day is scaled down by what was
measured:

```
k_morning = Σ measured / Σ (f × forecast)    since the first daylight interval
k_recent  = the same over the last hours
k         = clamp(k_morning, k_min, 1.0)     once the gate has opened
```

The gate opens when both ratios are below a threshold and, by default, the base forecast was
confident for the day (P10 ÷ P50 at least 0.5, i.e. a narrow band). It only lowers the
forecast, and once open it holds for the rest of the day.

A dull morning followed by a sunny afternoon is the classic failure of any intraday ratio,
so this layer runs in **shadow mode** by default: it is computed and shown (sensor *Fog
factor*, and as the *shadow* columns of the error sensor) but not applied. Switch it on in
the options once the archive shows that it helps at your site.

### What is published

- **Today:** intervals that have passed carry the measured production (P10 = P50 = P90),
  the current interval blends what was measured with the corrected forecast, and future
  intervals carry the corrected forecast. The state is therefore the best estimate of the day
  total: measured so far plus corrected rest.
- **Remaining today:** the corrected rest of the day, with P10/P90.
- **Tomorrow:** the base forecast × level factor.
- Wherever P50 is 0, P10 and P90 are 0 too (some consumers fall back to P90).

### Built-in comparison

At fixed times the forecasts are frozen: the day total at 07:00, the rest of the day at
09:00–14:00 and tomorrow's total at 21:00. After the day they are compared with what was
measured. The two error sensors publish the result for the base and the corrected forecast
every night — the running answer to "is it better than what I had?".

## Installation

### HACS (custom repository)

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/cykedev/ha-pv-forecast-correction` with type **Integration**.
3. Install **PV Forecast Correction** and restart Home Assistant.
4. **Settings → Devices & services → Add integration → PV Forecast Correction.**

### Manual

Copy `custom_components/pv_forecast_correction` into the `custom_components` folder of your
Home Assistant configuration and restart.

Requires Home Assistant 2025.6 or newer.

## Configuration

Setup asks for the inputs; everything else is in the options (**Configure** on the
integration). Several entries are possible, e.g. one per base forecast.

### Inputs

| Input | Required | Description |
|---|---|---|
| Forecast today | yes | Sensor with a forecast list in the Solcast format: attribute `detailedForecast` (30 min) or `detailedHourly` (60 min), entries with `period_start` and `pv_estimate` (kW), optionally `pv_estimate10` / `pv_estimate90`. With Solcast: *Forecast today*. |
| Forecast tomorrow | recommended | The same for tomorrow (Solcast: *Forecast tomorrow*). |
| Production (energy) | one of the two | Energy meter of the PV production in kWh, Wh or MWh; may reset daily. Preferred. |
| Production (power) | one of the two | One or more power sensors (W, kW); they are added up and integrated. |
| Curtailment | no | `binary_sensor` that is on while the output is limited. |

### Options

| Section | Option | Default | Meaning |
|---|---|---|---|
| Level factor | Apply | on | Off: computed and shown, not applied. |
| | Factor by time of day | on | One factor per clock hour instead of one per day. |
| | Days learned from | 14 | Complete past days in the ratio. |
| | Start-up weight | 1 day | Days at ratio 1.0 added to the ratio. |
| | Lowest / highest factor | 0.3 / 1.5 | Limits of the factor. |
| Fog layer | Mode | shadow | `off`, `shadow` (compute and show) or `on` (apply). |
| | Threshold | 0.7 | Opens when the morning and the recent window produced less than this share of the forecast. |
| | Minimum forecast confidence | 0.5 | Base P10 ÷ P50 for the day must be at least this; 0 drops the condition. |
| | Recent window | 2 h | Length of the recent window. |
| | Lowest factor | 0.2 | Limit of the fog factor. |
| Archive | Keep an archive | on | See [Archive and replay](#archive-and-replay). |
| | Keep for | 400 days | Older data is deleted every night. |

## Entities

Entity IDs below are for an entry named *PV Forecast Correction* in an English Home
Assistant whose device has no area. Home Assistant derives the IDs from the translated names
and the device's area, so in another language or with an area they differ (for example
`sensor.kitchen_pv_forecast_correction_heute`); rename them in the entity settings if you
like — the integration does not depend on them.

| Entity | State | Main attributes |
|---|---|---|
| `sensor.pv_forecast_correction_today` | corrected day total (kWh) | `estimate`, `estimate10`, `estimate90`, `detailedForecast`, `detailedHourly`, `actual_so_far`, `base_estimate*`, `level_factor`, `fog_factor` |
| `sensor.pv_forecast_correction_remaining_today` | corrected rest of today (kWh) | `estimate`, `estimate10`, `estimate90`, `detailedForecast` (rest only), `base_estimate*` |
| `sensor.pv_forecast_correction_tomorrow` | corrected total tomorrow (kWh) | as *today*, without `actual_so_far` |
| `sensor.pv_forecast_correction_level_factor` | f (day factor) | `status`, `days_used`, `curve` (factor per hour), `sum_actual`, `sum_forecast`, `raw_ratio`, `shrink_weight`, `daily_ratios`, `imported_days`, `computed_at` |
| `sensor.pv_forecast_correction_fog_factor` | k applied (1 when inactive) | `mode`, `reason`, `gate`, `k_shadow`, `k_morning`, `k_recent`, `base_confidence`, `active_since` |
| `sensor.pv_forecast_correction_error_base` | yesterday's rest-of-day MAE of the base (kWh) | `rest_of_day` per time, `day_error`, `day_ahead_error`, `day_p10_held`, `day_p90_held`, `status` |
| `sensor.pv_forecast_correction_error_corrected` | the same for the corrected forecast | as above, plus `shadow_*` while the fog layer is in shadow mode |
| `sensor.pv_forecast_correction_base_age` | last refresh of the base forecast | `status`, `failures_today` |

Errors are signed *forecast − actual*: positive means the forecast was too high. `status` and
`reason` are explained in the attribute panel in your language.

The forecast lists are excluded from the recorder; states and factors have long-term
statistics, so base and corrected forecast can be compared with a statistics graph card.

## Using the corrected forecast

Any consumer that reads the Solcast attribute format can read these sensors instead: point
its *forecast today*, *forecast tomorrow* and *remaining today* inputs to the matching
sensors. `period_start` is a timezone-aware datetime in the state machine and an ISO string
over the API, exactly as with Solcast.

In templates:

```yaml
template:
  - sensor:
      - name: "PV next 3 hours"
        unit_of_measurement: kWh
        state: >
          {% set until = now() + timedelta(hours=3) %}
          {% set periods = state_attr('sensor.pv_forecast_correction_remaining_today',
                                      'detailedForecast') or [] %}
          {{ (periods | selectattr('period_start', '<', until)
                      | sum(attribute='pv_estimate') * 0.5) | round(2) }}
```

If a consumer applies its own fixed safety factor to the PV forecast, consider whether that
still makes sense: a level-corrected forecast already contains the site's bias.

## Archive and replay

With the archive on, the integration keeps every snapshot of the base forecast (including
P10/P90), the measured energy per 5 minutes, curtailment, and the applied factors every 15
minutes in `<config>/pv_forecast_correction/<entry_id>.db` (SQLite, about 10–20 MB per year).
Base forecast lists are not kept by Home Assistant's recorder and forecast providers do not
keep old runs, so without this archive no variant could be evaluated later against what was
known at the time.

`tools/replay.py` runs the same correction code over an archive and prints, per variant
(base, level factor, level factor + fog layer with several gates), the error at every
checkpoint, by day class, for the day total and for the day ahead, plus a pass/fail table
for each layer:

```bash
python3 tools/replay.py /path/to/<entry_id>.db --days
```

It uses the time zone and settings stored in the archive (options such as `--level-days`
override them) and needs only the Python standard library — no Home Assistant
installation.

## Services

| Service | Description |
|---|---|
| `pv_forecast_correction.export` | Writes the archive of an entry as CSV files into `<config>/pv_forecast_correction/export/` and returns their paths. |
| `pv_forecast_correction.learn_from_history` | Learns the level factor from the recorder's history of the last days (see below) and returns what it used per day. |
| `pv_forecast_correction.reset` | Forgets the learned days (the level factor starts neutral again); optionally clears the archive. |

### Starting with history

A fresh install starts with a neutral level factor. To start with what Home Assistant has
already recorded, call `pv_forecast_correction.learn_from_history` (default: the last 14
days). Per day it takes the measured energy from the long-term statistics of the production
inputs and, as the forecast:

- with `forecast_power`: the hourly statistics of a sensor that shows the base forecast's
  power for the current moment (Solcast: *Power Now*, Forecast.Solar: *Estimated power
  production - now*). That sensor always shows the value the base holds for the running
  interval, so its hourly mean is the forecast of record per hour. This is exact and also
  teaches the time-of-day curve. Recommended.
- without it: the base forecast's day total as it stood before production began. It
  overstates the forecast of record on days whose later hours were revised down before
  they began (fog days), so the factor comes out somewhat low, and the curve is not
  learned.

Days with gaps are skipped; days the integration measured itself are kept, days imported
earlier are replaced. Imported days are listed in the `imported_days` attribute of the level
factor and age out like any other day.

```yaml
action: pv_forecast_correction.learn_from_history
data:
  config_entry_id: <your entry>
  days: 14
  forecast_power: sensor.solcast_pv_forecast_power_now
```

## Troubleshooting

- **The level factor stays at 1.0.** It needs complete days: see the `status` and
  `days_used` attributes. Days with gaps in the production data (restarts, an unavailable
  sensor) are not used.
- **A repair issue says the forecast or the production cannot be read.** It appears after an
  hour without readable input. The correction keeps the last base forecast it had and does
  not learn from intervals without readings.
- **Diagnostics.** *Download diagnostics* on the integration shows the configuration, the
  learned days, the fog layer state, the last day's errors and the archive status.

## Development

```
custom_components/pv_forecast_correction/
  core/          plain Python, no Home Assistant import: format, time grid, measurement,
                 level factor, fog layer, scoring, archive, replay, engine
  *.py           Home Assistant glue: config flow, coordinator, sensors, diagnostics
tools/replay.py  replay over an archive file
tests/           core and tool tests (no Home Assistant), integration tests
```

```bash
python -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest
```

The integration and the replay tool use the same `core/` package, so what is replayed and
tested is what runs.

## License

[Apache License 2.0](LICENSE)

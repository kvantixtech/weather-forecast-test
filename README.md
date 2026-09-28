# Kvantix weather forecast test

Which weather forecast is right most often in Denmark?

This collector saves what **DMI**, **MET Norway**, **OpenWeatherMap** (free 3-hour forecast) and the pilots' airport forecast (**TAF**) predict for five Danish cities, four times a day. It locks every download in a SHA-256 hash chain **before** the weather happens. Afterwards it fetches what DMI's weather stations actually measured and scores every source against it, and against a lazy baseline: "tomorrow will be like today".

The scoring rules in this repository were fixed and published before the first forecast was collected. The commit history is the proof.

Results: <https://playground.kvantix.tech/weather/> · Part of the Kvantix [Data Playground](https://kvantix.tech/playground/).

## What is measured

| | Rule |
|---|---|
| Cities | Hjørring, Aalborg, Aarhus, Odense, Copenhagen. The forecast point is the nearest active DMI station that has delivered data in the last 24 hours. If no station within 20 km measures both temperature and hourly rain, temperature and rain come from two stations: rain from the gauge nearest the temperature station. |
| Schedule | Forecasts at 00, 06, 12 and 18 UTC, up to 72 hours ahead. Observations once a day. |
| Temperature | Compared every third hour (UTC). Mean absolute error and bias per lead time (0–24, 24–48, 48–72 h), only at moments where every source had a forecast. |
| Rain / no rain | Six-hour periods starting 00/06/12/18 UTC. "Rain" = at least 0.2 mm measured. Each source's own time steps are summed into the six-hour period. |
| Rain probability | Calibration and Brier score per source, in each source's own time window, next 48 hours. |
| Baseline | The most recent measurement at the same time of day that was known when the forecast was made. |
| TAF | Only at airports where DMI measures both temperature and rain at the airport station. Translation to probability, following ICAO definitions: PROB30/PROB40 = 30/40 %, TEMPO and BECMG = 60 % (ICAO: at least 50 %), precipitation in the main forecast = 90 %, none mentioned = 10 %. Showers in the vicinity (VC) do not count. |
| Corrections | If DMI later corrects an observation, the first value is kept and the correction is logged. DMI's trace code (−0.1 mm) counts as 0. |

## Integrity

Every raw response is stored gzipped and hashed, and each download is chained to the previous one before it is parsed:

```
chain_hash = sha256(prev_hash + "|" + canonical_json(kind, source, location, fetched_at, http_status, url, payload_sha256, n_rows=0, error))
```

`kvx_weather.py verify` recomputes the chain and every raw file's hash. A failed download is kept in the chain as a gap, not hidden.

## Run it

Python 3.9+, standard library only (SQLite).

```bash
python3 tests/test_offline.py          # offline test, no network
KVX_WEATHER_CONFIG=config.json python3 kvx_weather.py stations
KVX_WEATHER_CONFIG=config.json python3 kvx_weather.py probe --loc aalborg
KVX_WEATHER_CONFIG=config.json python3 kvx_weather.py collect
KVX_WEATHER_CONFIG=config.json python3 kvx_weather.py observe
KVX_WEATHER_CONFIG=config.json python3 kvx_weather.py verify
KVX_WEATHER_CONFIG=config.json python3 kvx_weather.py score
```

`config.json` needs `data_dir` and optionally `owm_api_key` (free OpenWeatherMap key). Without a key, OpenWeatherMap is skipped. `systemd/` holds the timers used in production. Code comments and console output are in Danish.

## Data sources and licences

- **DMI** (Danish Meteorological Institute), forecasts and observations: CC BY 4.0. Based on data from DMI, subsequently processed.
- **MET Norway**, locationforecast 2.0: CC BY 4.0.
- **OpenWeatherMap**, 5-day/3-hour forecast (free plan): ODbL. This repository is the method by which the published results are derived from it (ODbL §4.6b).
- **TAF** via the Aviation Weather Center (NOAA), aviationweather.gov. Danish TAFs are issued by DMI.

No data is included in this repository. Kvantix is not affiliated with any of the sources.

## Licence

Code: MIT, see `LICENSE`. © 2026 Kvantix (CVR 46296036), Hjørring, Denmark · validation@kvantix.tech

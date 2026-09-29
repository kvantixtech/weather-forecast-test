# Changelog

The scoring rules do not change while the test runs. A necessary correction gets a new `VERSION`, a line in [`METHOD.lock`](METHOD.lock), the date and the reason here, and the results page shows which version produced which numbers.

## Unreleased

- Daily public anchor of the chain head: `tools/anchor.py`, `anchors/chain-heads.csv`, `systemd/kvx-weather-anchor.{service,timer}`.
- `METHOD.lock`: SHA-256 of every published collector version. CI fails if `kvx_weather.py` changes without a new version.
- GitHub Actions: offline tests on Python 3.9, 3.11 and 3.13, plus a format check of the anchors.
- **`kvx_weather.py` is unchanged** (still 1.1, `0464d697…6360`).

## 1.1: 2026-09-28

- `f4c2993`, 07:57:51 UTC: collector and scoring rules published before the first forecast (first collection 12:08 UTC).
- `afede24`, 14:34 UTC: systemd collect timeout 30 → 50 min, because DMI's retries can take up to ~33 min in the worst case. Scoring code unchanged.

Before publication (not run in production):
- Station choice requires at least 12 hourly observations in the last 24 hours. Silent stations are skipped.
- Temperature and rain may come from two stations when no station within 20 km measures both.
- TAF is used only where the airport station measures both temperature and rain.
- Raw file names include the payload hash. A parse error is logged in its own column, outside the chain.
- DMI precipitation noise below 0.01 mm and the trace code −0.1 mm count as 0.

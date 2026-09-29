# Public anchors

Once a day, after the observation run, the server appends one line to `chain-heads.csv` and pushes it here. Each line records the head of the hash chain: the `chain_hash` of the newest download.

| Column | Meaning |
|---|---|
| `date_utc` | The day of the anchor. One line per day. |
| `anchored_at_utc` | When the head was read. |
| `runs` | Number of downloads in the chain, failed ones included. |
| `last_run_id`, `last_fetched_at` | The newest download. |
| `chain_head` | Its `chain_hash`. |
| `chain_ok` | Whether the whole chain recomputed from genesis when the head was read. A `false` is published, not hidden. |
| `code_sha256` | SHA-256 of the `kvx_weather.py` the server was running. It must be a version listed in [`METHOD.lock`](../METHOD.lock). |

## Why

The collector saves each forecast before the weather happens and chains it to the previous one. A chain alone only proves that the downloads are internally consistent. Whoever holds the database could still rebuild the whole chain with different forecasts.

The anchors close that gap. When a head is public, changing any earlier download changes every hash after it, and the recomputed chain no longer passes through the published head. The git history of this file shows when each head became public. A history rewrite would be visible to anyone who has cloned or forked the repository.

## Check it yourself

With a copy of the database (for example from the Data Explorer, once it opens):

```bash
python3 tools/anchor.py check --db weather.sqlite3
```

The script recomputes the chain from genesis and confirms that every anchor in this file lies on it. `tools/anchor.py lint` checks the format and runs on every push.

The server side is `systemd/kvx-weather-anchor.{service,timer}`. The head is read as the collector's own user with a read-only connection. The collector's code and data are never modified.

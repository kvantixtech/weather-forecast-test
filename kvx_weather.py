#!/usr/bin/env python3
"""
kvx_weather.py — Kvantix vejr-forward-test.

Logger vejrprognoser fra DMI, MET Norway (yr), OpenWeatherMap og lufthavnenes TAF (pilotprognosen)
FØR vejret sker, låser hver
hentning i en hash-kæde, henter DMI's observationer bagefter og måler, hvem der ramte bedst.
Kun standardbiblioteket. Rører intet i Kvantix' øvrige system.

    kvx_weather.py stations            # find nærmeste DMI-station for hver by (kør én gang)
    kvx_weather.py probe [--loc aalborg] [--sources dmi,taf,obs]   # hent én by og vis hvad der blev læst
    kvx_weather.py collect             # hent prognoser (timer: 4 gange i døgnet)
    kvx_weather.py observe             # hent DMI-observationer for de sidste dage (timer: dagligt)
    kvx_weather.py verify              # genberegn hash-kæden og tjek at intet er ændret
    kvx_weather.py score [--days N]    # stillingen: temperatur, regn ja/nej, regn-% kalibrering
    kvx_weather.py export              # CSV'er i toolkittets format (timestamp,group,prediction,outcome)
    kvx_weather.py page                # offentlig resultatside (statisk HTML + scores.json)

Licenser: DMI frie data (CC BY 4.0, "Baseret på data fra DMI og efterfølgende bearbejdet"),
MET Norway (CC BY 4.0), OpenWeatherMap (CC BY-SA 4.0 / ODbL), TAF via aviationweather.gov (NOAA;
danske TAF'er udstedes af DMI — gengivelsesvilkår tjekkes før offentliggørelse). Open-Meteo bruges IKKE
(gratis adgang er kun til ikke-kommerciel brug).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

VERSION = "1.1"
UTC = timezone.utc
CONFIG_PATH = os.environ.get("KVX_WEATHER_CONFIG", "/etc/kvx-weather/config.json")

DEFAULT_CONFIG = {
    "data_dir": "/var/lib/kvx-weather",
    "user_agent": "kvantix-weather/1.0 (https://kvantix.tech; validation@kvantix.tech)",
    "owm_api_key": "",
    "max_lead_hours": 72,
    # Byer (omtrentlige bykoordinater). Prognosen hentes for den NÆRMESTE DMI-STATIONS
    # koordinater, så prognose og måling handler om præcis samme punkt.
    "cities": [
        # temp_station (valgfri): foretrukken temperaturstation; bruges kun hvis den har leveret data.
        # icao + airport_station: TAF bruges KUN hvis både temperatur og regn måles på lufthavnens station
        # (TAF'en gælder lufthavnen; en regnmåler 15 km væk ville gøre sammenligningen unfair).
        {"id": "hjoerring",  "name": "Hjørring",   "lat": 57.4640, "lon": 9.9820},
        {"id": "aalborg",    "name": "Aalborg",    "lat": 57.0480, "lon": 9.9190, "icao": "EKYT", "airport_station": "06030"},
        {"id": "aarhus",     "name": "Aarhus",     "lat": 56.1570, "lon": 10.2110},
        {"id": "odense",     "name": "Odense",     "lat": 55.4030, "lon": 10.4020, "icao": "EKOD", "airport_station": "06120"},
        {"id": "koebenhavn", "name": "København",  "lat": 55.6760, "lon": 12.5680, "icao": "EKCH", "airport_station": "06180"},
    ],
    # Findes der ingen station med både temperatur og timenedbør inden for denne afstand, tages
    # temperatur og regn fra hver sin station (regn fra måleren nærmest temperaturstationen).
    "max_station_km": 20,
    "rain_threshold_mm": 0.2,
}

DMI = "https://opendataapi.dmi.dk"
MET = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
OWM = "https://api.openweathermap.org/data/2.5/forecast"
AWC = "https://aviationweather.gov/api/data/taf"

FIXTURES = None   # sat af --fixtures (offline-test): mappe med gemte svar
LAST_ATTEMPTS = 0 # antal forsøg i seneste http_get (vises i probe)

# DMI's prognosetjeneste afviser en del kald med 429 "Server is busy", når den er travl; korte, voksende
# pauser virker (DMI's eget kodeeksempel bruger 1-2-4-8-16 s). Højst ~8 kald på 2 min — langt under
# DMI's fair-use-grænse på 500 kald pr. 5 sek.
WAITS = {"dmi": (1, 2, 4, 8, 16, 30, 60)}
WAITS_RETRY = {"dmi": (2, 4, 8, 16, 30, 60, 120)}


# ============================================================ util

def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s) -> datetime:
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, UTC)
    s = str(s).replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        tz = ""
        for sep in ("+", "-"):
            if sep in tail:
                tz = sep + tail.split(sep, 1)[1]
        s = head + tz
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def http_get(url: str, cfg: dict, fixture: str | None = None, waits=(15, 45)) -> tuple[int, bytes]:
    """GET med identificerende User-Agent (krav fra MET Norway og aviationweather.gov).
    429/5xx: vent (Retry-After hvis kilden angiver det, ellers 'waits') og prøv igen.
    Offline-test: læs fixture-fil i stedet."""
    if FIXTURES is not None:
        path = os.path.join(FIXTURES, fixture or "")
        if fixture and os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read()
            if data.startswith(b"HTTP "):                 # fixture der simulerer en fejl: "HTTP 429\n..."
                return int(data[5:8]), data
            return 200, data
        return 404, b""
    req = urllib.request.Request(url, headers={"User-Agent": cfg["user_agent"],
                                               "Accept": "application/json, text/plain"})
    global LAST_ATTEMPTS
    status, body = 0, b""
    for attempt in range(len(waits) + 1):
        LAST_ATTEMPTS = attempt + 1
        retry_after = None
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read() if hasattr(e, "read") else b""
            ra = e.headers.get("Retry-After") if e.headers else None
            retry_after = int(ra) if ra and ra.strip().isdigit() else None
            if e.code not in (429, 500, 502, 503, 504):
                return status, body
        except (urllib.error.URLError, TimeoutError, OSError):
            status, body = 0, b""
        if attempt < len(waits):
            time.sleep(min(300, retry_after if retry_after is not None else waits[attempt]))
    return status, body


def redact(url: str) -> str:
    """Fjern API-nøgler fra URL'er, før de gemmes eller skrives ud."""
    p = urllib.parse.urlsplit(url)
    q = [(k, "***" if k.lower() in ("appid", "api-key", "apikey", "key") else v)
         for k, v in urllib.parse.parse_qsl(p.query)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


# ============================================================ database + hash-kæde

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,              -- forecast / observation / stations
  source TEXT NOT NULL,            -- dmi / met / owm / dmi_obs
  location TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  http_status INTEGER,
  url TEXT,                        -- uden nøgler
  payload_sha256 TEXT,
  raw_path TEXT,
  n_rows INTEGER DEFAULT 0,
  error TEXT,                      -- hentefejl (låst i kæden)
  parse_error TEXT,                -- læsefejl (ikke låst: rå-svaret kan læses igen)
  prev_hash TEXT NOT NULL,
  chain_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS forecasts (
  run_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  location TEXT NOT NULL,
  issued_at TEXT NOT NULL,         -- = hentetidspunkt (det en bruger kunne se)
  target_time TEXT NOT NULL,       -- instant: tidspunktet. vindue: vinduets START
  window_h INTEGER NOT NULL,       -- 0 = øjebliksværdi, ellers vindueslængde i timer
  variable TEXT NOT NULL,          -- temp (°C) / precip (mm) / pop (0-1)
  value REAL NOT NULL,
  lead_h REAL NOT NULL,
  PRIMARY KEY (run_id, variable, window_h, target_time)
);
CREATE INDEX IF NOT EXISTS fc_target ON forecasts(location, variable, target_time);
CREATE TABLE IF NOT EXISTS observations (
  station_id TEXT NOT NULL,
  location TEXT NOT NULL,
  variable TEXT NOT NULL,          -- temp (°C, øjeblik) / precip1h (mm, timen der slutter ved obs_time)
  obs_time TEXT NOT NULL,
  value REAL NOT NULL,
  run_id INTEGER NOT NULL,         -- første hentning
  PRIMARY KEY (station_id, variable, obs_time)
);
CREATE TABLE IF NOT EXISTS obs_revisions (
  station_id TEXT, variable TEXT, obs_time TEXT, old_value REAL, new_value REAL,
  run_id INTEGER, seen_at TEXT
);
CREATE TABLE IF NOT EXISTS stations (
  location TEXT PRIMARY KEY, station_id TEXT, station_name TEXT,   -- temperatur (og prognosepunkt)
  lat REAL, lon REAL, distance_km REAL, resolved_at TEXT,
  precip_station_id TEXT, precip_station_name TEXT, precip_km REAL -- regnmåler; precip_km = afstand til temperaturstationen
);
"""

GENESIS = "0" * 64


def db_connect(cfg) -> sqlite3.Connection:
    os.makedirs(cfg["data_dir"], exist_ok=True)
    con = sqlite3.connect(os.path.join(cfg["data_dir"], "weather.sqlite3"))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # migration fra v1.0 (kolonner tilføjes; intet slettes)
    have = {r[1] for r in con.execute("PRAGMA table_info(stations)")}
    for col, typ in (("precip_station_id", "TEXT"), ("precip_station_name", "TEXT"), ("precip_km", "REAL")):
        if col not in have:
            con.execute(f"ALTER TABLE stations ADD COLUMN {col} {typ}")
    have = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    if "parse_error" not in have:
        con.execute("ALTER TABLE runs ADD COLUMN parse_error TEXT")
    return con


def chain_record(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_hash(prev: str, fields: dict) -> str:
    return hashlib.sha256((prev + "|" + chain_record(fields)).encode()).hexdigest()


CHAIN_FIELDS = ("kind", "source", "location", "fetched_at", "http_status", "url", "payload_sha256", "n_rows", "error")


def add_run(con, cfg, *, kind, source, location, fetched_at, status, url, payload: bytes, error=None) -> int:
    """Gemmer rå-svaret komprimeret og lægger hentningen i hash-kæden, FØR noget læses ud."""
    sha = hashlib.sha256(payload).hexdigest() if payload else None
    raw_path = None
    if payload:
        day = fetched_at[:10]
        rel = os.path.join("raw", day, f"{fetched_at.replace(':', '')}_{kind}_{source}_{location}_{sha[:12]}.json.gz")
        full = os.path.join(cfg["data_dir"], rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if not os.path.exists(full):          # samme indhold = samme fil; aldrig overskriv
            with gzip.open(full, "wb") as fh:
                fh.write(payload)
        raw_path = rel
    prev = con.execute("SELECT chain_hash FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    prev = prev[0] if prev else GENESIS
    fields = {"kind": kind, "source": source, "location": location, "fetched_at": fetched_at,
              "http_status": status, "url": url, "payload_sha256": sha, "n_rows": 0, "error": error}
    h = chain_hash(prev, fields)
    cur = con.execute(
        "INSERT INTO runs (kind, source, location, fetched_at, http_status, url, payload_sha256, raw_path, n_rows, error, prev_hash, chain_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, source, location, fetched_at, status, url, sha, raw_path, 0, error, prev, h))
    with open(os.path.join(cfg["data_dir"], "chain.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{cur.lastrowid}\t{fetched_at}\t{kind}\t{source}\t{location}\t{sha or '-'}\t{h}\n")
    return cur.lastrowid


def verify_chain(con, cfg) -> tuple[bool, list[str]]:
    problems = []
    prev = GENESIS
    rows = con.execute(f"SELECT id, prev_hash, chain_hash, raw_path, {', '.join(CHAIN_FIELDS)} FROM runs ORDER BY id").fetchall()
    for r in rows:
        rid, p, h, raw_path = r[0], r[1], r[2], r[3]
        fields = dict(zip(CHAIN_FIELDS, r[4:]))
        fields["n_rows"] = 0            # n_rows låses ikke (sættes efter parsing)
        if p != prev:
            problems.append(f"run {rid}: prev_hash passer ikke")
        if chain_hash(p, fields) != h:
            problems.append(f"run {rid}: chain_hash passer ikke")
        if raw_path:
            full = os.path.join(cfg["data_dir"], raw_path)
            try:
                with gzip.open(full, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != fields["payload_sha256"]:
                        problems.append(f"run {rid}: rå-fil ændret ({raw_path})")
            except OSError:
                problems.append(f"run {rid}: rå-fil mangler ({raw_path})")
        prev = h
    return (not problems), problems


# ============================================================ parsere (ren funktion: bytes -> rækker)
# Hver række: (target_time: datetime, window_h: int, variable: str, value: float)

def parse_met(payload: bytes, ref=None):
    """MET Norway locationforecast 2.0 'complete'. Vinduer (next_1_hours/next_6_hours) starter ved 'time'."""
    j = json.loads(payload)
    out = []
    for ts in j["properties"]["timeseries"]:
        t = parse_ts(ts["time"])
        d = ts.get("data", {})
        inst = d.get("instant", {}).get("details", {})
        if "air_temperature" in inst:
            out.append((t, 0, "temp", float(inst["air_temperature"])))
        for key, w in (("next_1_hours", 1), ("next_6_hours", 6)):
            det = d.get(key, {}).get("details", {})
            if "precipitation_amount" in det:
                out.append((t, w, "precip", float(det["precipitation_amount"])))
            if "probability_of_precipitation" in det:
                out.append((t, w, "pop", float(det["probability_of_precipitation"]) / 100.0))
    return out


def parse_owm(payload: bytes, ref=None):
    """OpenWeatherMap 5 day / 3 hour. Ifølge dokumentationen gælder rain.3h 'the last 3 hours'
    før dt, så vinduet er [dt-3h, dt). pop antages at gælde samme vindue (kontrolleres i score
    ved at måle mod observationer forskudt ±3 t)."""
    j = json.loads(payload)
    out = []
    for it in j.get("list", []):
        t = parse_ts(int(it["dt"]))
        if "main" in it and "temp" in it["main"]:
            out.append((t, 0, "temp", float(it["main"]["temp"])))
        start = t - timedelta(hours=3)
        out.append((start, 3, "precip", float((it.get("rain") or {}).get("3h", 0.0)) + float((it.get("snow") or {}).get("3h", 0.0))))
        if "pop" in it:
            out.append((start, 3, "pop", float(it["pop"])))
    return out


DMI_ACC_NOISE_MM = 0.01


def parse_dmi(payload: bytes, ref=None):
    """DMI Forecast EDR (HARMONIE DINI). Læser både GeoJSON og CoverageJSON.
    temperature-2m kan være i Kelvin -> omregnes. total-precipitation er akkumuleret fra
    kørslens start -> differens mellem tidsskridt = nedbør i timen [t-1h, t)."""
    j = json.loads(payload)
    series = defaultdict(dict)   # param -> {time: value}
    if j.get("type") == "FeatureCollection":
        for f in j.get("features", []):
            p = f.get("properties", {})
            t = p.get("step") or p.get("time") or p.get("datetime")
            if t is None:
                continue
            for k, v in p.items():
                if k in ("temperature-2m", "total-precipitation") and v is not None:
                    series[k][parse_ts(t)] = float(v)
    elif j.get("type") in ("Coverage", "CoverageCollection"):
        covs = j.get("coverages", [j])
        for cov in covs:
            times = [parse_ts(x) for x in cov["domain"]["axes"]["t"]["values"]]
            for k in ("temperature-2m", "total-precipitation"):
                if k in cov.get("ranges", {}):
                    vals = cov["ranges"][k]["values"]
                    for t, v in zip(times, vals):
                        if v is not None:
                            series[k][t] = float(v)
    out = []
    temps = series.get("temperature-2m", {})
    kelvin = bool(temps) and (sum(temps.values()) / len(temps)) > 150
    for t, v in temps.items():
        out.append((t, 0, "temp", v - 273.15 if kelvin else v))
    # Den løbende sum har afrundingsstøj på ±0,005 mm (målt 28-09-2026). Differenser under 0,01 mm = 0 mm
    # (en regnmåler registrerer først 0,1 mm); et fald på mere end 0,01 mm er en fejl → timen droppes.
    acc = sorted(series.get("total-precipitation", {}).items())
    for (t0, a0), (t1, a1) in zip(acc, acc[1:]):
        d = a1 - a0
        if (t1 - t0) == timedelta(hours=1) and d >= -DMI_ACC_NOISE_MM:
            out.append((t0, 1, "precip", d if d >= DMI_ACC_NOISE_MM else 0.0))
    return out


def parse_metobs(payload: bytes):
    """DMI metObs items (GeoJSON). temp_dry: øjeblik. precip_past1h: timen der slutter ved 'observed'."""
    j = json.loads(payload)
    out = []
    for f in j.get("features", []):
        p = f.get("properties", {})
        pid, v, t = p.get("parameterId"), p.get("value"), p.get("observed")
        if v is None or t is None:
            continue
        t = parse_ts(t)
        if pid == "temp_dry" and t.minute == 0:
            out.append((t, "temp", float(v)))
        elif pid == "precip_past1h" and t.minute == 0:
            # DMI koder "spor af nedbør" (under 0,1 mm) som -0,1 → tælles som 0 mm (under regntærsklen)
            out.append((t, "precip1h", max(0.0, float(v))))
    return out


# ------------------------------------------------------------ TAF (pilotprognosen)
# TAF er ikke en sandsynlighedsprognose. Oversættelsen er lagt fast FØR testen og følger ICAO's
# definitioner: PROB30/PROB40 = 30/40 %; TEMPO og BECMG bruges kun ved mindst 50 % (vi bruger 60 %);
# nedbør i det fremherskende vejr = 90 %; nedbør ikke nævnt = under 30 % (vi bruger 10 %).
TAF_P = {"prevailing": 0.9, "change": 0.6, "tempo": 0.6, "none": 0.1}
_PRECIP_RE = re.compile(r"^[+-]?(?:SH|TS|FZ|DR|BL|MI|BC|PR)?(?:DZ|RA|SN|SG|PL|GR|GS|UP|IC)+$")
_MEASURABLE = ("DZ", "RA", "SN", "SG", "PL", "GR", "GS", "UP")      # kan give udslag i en regnmåler
_WX_RE = re.compile(r"^(?:[+-]|VC)?(?:SH|TS|FZ|DR|BL|MI|BC|PR)?"
                    r"(?:DZ|RA|SN|SG|PL|GR|GS|UP|IC|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)+$|^TS$|^NSW$")


def _taf_precip(tok: str) -> bool:
    """Nedbør VED lufthavnen (VC = i nærheden tæller ikke; TS uden nedbør tæller ikke)."""
    return bool(_PRECIP_RE.match(tok)) and any(c in tok for c in _MEASURABLE)


def _taf_time(ref: datetime, dd: int, hh: int, mm: int = 0) -> datetime | None:
    """TAF angiver kun dag+time. Vælg måneden (forrige/denne/næste) der ligger nærmest hentetidspunktet."""
    best = None
    for mo in (-1, 0, 1):
        y, m = ref.year, ref.month + mo
        if m == 0:
            y, m = y - 1, 12
        elif m == 13:
            y, m = y + 1, 1
        try:
            t = datetime(y, m, dd, tzinfo=UTC) + timedelta(hours=hh, minutes=mm)   # hh=24 → næste dag 00
        except ValueError:
            continue
        if best is None or abs((t - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = t
    return best


_PERIOD = re.compile(r"(\d\d)(\d\d)/(\d\d)(\d\d)")


def parse_taf_text(text: str, ref: datetime):
    """Returnerer liste af (issued, valid_from, valid_to, groups); groups = [(kind, a, z, prob, tokens)]."""
    toks = text.replace("=", " ").split()
    blocks, cur = [], None
    for t in toks:
        if t == "TAF":
            cur = []
            blocks.append(cur)
        elif cur is None:
            cur = [t]
            blocks.append(cur)
        else:
            cur.append(t)
    out = []
    for b in blocks:
        b = [t for t in b if t not in ("AMD", "COR", "RTD")]
        i = 0
        if i < len(b) and re.fullmatch(r"[A-Z]{4}", b[i]):
            i += 1
        m = re.fullmatch(r"(\d\d)(\d\d)(\d\d)Z", b[i]) if i < len(b) else None
        if not m:
            continue
        issued = _taf_time(ref, int(m[1]), int(m[2]), int(m[3]))
        i += 1
        m = _PERIOD.fullmatch(b[i]) if i < len(b) else None
        if not m or "CNL" in b or "NIL" in b:
            continue
        v0, v1 = _taf_time(ref, int(m[1]), int(m[2])), _taf_time(ref, int(m[3]), int(m[4]))
        i += 1
        groups = [("BASE", v0, v1, None, [])]
        while i < len(b):
            t = b[i]
            mfm = re.fullmatch(r"FM(\d\d)(\d\d)(\d\d)", t)
            mp = re.fullmatch(r"PROB(30|40)", t)
            if mfm:
                groups.append(("FM", _taf_time(ref, int(mfm[1]), int(mfm[2]), int(mfm[3])), None, None, []))
                i += 1
                continue
            if t in ("BECMG", "TEMPO") or mp:
                kind, prob, k = t, None, i + 1
                if mp:
                    kind, prob = "PROB", int(mp[1]) / 100
                    if k < len(b) and b[k] == "TEMPO":
                        k += 1
                mt = _PERIOD.fullmatch(b[k]) if k < len(b) else None
                if mt:
                    a = _taf_time(ref, int(mt[1]), int(mt[2]))
                    z = _taf_time(ref, int(mt[3]), int(mt[4]))
                    groups.append((kind, a, z, prob, []))
                    i = k + 1
                    continue
            groups[-1][4].append(t)
            i += 1
        out.append((issued, v0, v1, groups))
    return out


def taf_hourly(v0: datetime, v1: datetime, groups) -> dict:
    """Sandsynlighed for målbar nedbør ved lufthavnen pr. time [h, h+1)."""
    hours = []
    t = v0
    while t < v1:
        hours.append(t)
        t += timedelta(hours=1)
    base = any(_taf_precip(x) for x in groups[0][4])
    prev = {h: base for h in hours}
    change = {h: False for h in hours}
    over = {h: 0.0 for h in hours}
    for kind, a, z, prob, toks in groups[1:]:
        gp = any(_taf_precip(x) for x in toks)
        has_wx = any(_WX_RE.match(x) for x in toks)
        if kind == "FM":                               # FM erstatter alt fra tidspunktet
            for h in hours:
                if h >= a:
                    prev[h] = gp
                elif h + timedelta(hours=1) > a and gp != prev[h]:
                    change[h] = True
        elif kind == "BECMG" and has_wx:               # gradvis ændring i perioden [a, z)
            for h in hours:
                if a <= h < z and gp != prev[h]:
                    change[h] = True
                elif h >= z:
                    prev[h] = gp
        elif kind == "TEMPO" and gp:
            for h in hours:
                if a <= h < z:
                    over[h] = max(over[h], TAF_P["tempo"])
        elif kind == "PROB" and gp:
            for h in hours:
                if a <= h < z:
                    over[h] = max(over[h], prob)
    res = {}
    for h in hours:
        p0 = TAF_P["prevailing"] if prev[h] else (TAF_P["change"] if change[h] else TAF_P["none"])
        res[h] = max(p0, over[h])
    return res


def parse_taf(payload: bytes, ref=None):
    """aviationweather.gov format=raw. Seneste udstedte TAF bruges. Giver 6-timers vinduer (00/06/12/18 UTC):
    pop = højeste timesandsynlighed i vinduet; rainyn = 1 hvis pop >= 50 % (fremherskende, BECMG eller TEMPO)."""
    ref = ref or now_utc()
    tafs = parse_taf_text(payload.decode("utf-8", "replace"), ref)
    if not tafs:
        raise ValueError("ingen TAF i svaret")
    issued, v0, v1, groups = max(tafs, key=lambda x: x[0])
    hourly = taf_hourly(v0, v1, groups)
    out = []
    for s in sorted(hourly):
        if s.hour % 6:
            continue
        hs = [s + timedelta(hours=k) for k in range(6)]
        if all(h in hourly for h in hs):
            p = max(hourly[h] for h in hs)
            out.append((s, 6, "pop", p))
            out.append((s, 6, "rainyn", 1.0 if p >= 0.5 else 0.0))
    return out


# ============================================================ kommandoer

def resolve_stations(con, cfg, verbose=True):
    """Pr. by: nærmeste aktive DMI-station med både temperatur og timenedbør, hvis den ligger inden for
    max_station_km. Ellers temperatur fra den nærmeste (eller den faste 'temp_station') og regn fra den
    regnmåler, der ligger nærmest temperaturstationen. Prognosen hentes for temperaturstationens punkt."""
    url = f"{DMI}/v2/metObs/collections/station/items?status=Active&limit=10000"
    fetched = iso(now_utc())
    status, payload = http_get(url, cfg, "stations.json")
    add_run(con, cfg, kind="stations", source="dmi_obs", location="*", fetched_at=fetched,
            status=status, url=url, payload=payload, error=None if status == 200 else f"HTTP {status}")
    if status != 200:
        con.commit()
        sys.exit(f"Kunne ikke hente stationslisten (HTTP {status}): {payload[:300].decode('utf-8', 'replace')}")
    st = {}
    for f in json.loads(payload).get("features", []):
        p = f.get("properties", {})
        if p.get("validTo") or not f.get("geometry"):
            continue
        params = set(p.get("parameterId") or [])
        lon, lat = f["geometry"]["coordinates"][:2]
        old = st.get(p["stationId"])
        st[p["stationId"]] = {"name": p.get("name", ""), "lat": lat, "lon": lon,
                              "params": params | (old["params"] if old else set())}
    temp_ok = {k: v for k, v in st.items() if "temp_dry" in v["params"]}
    rain_ok = {k: v for k, v in st.items() if "precip_past1h" in v["params"]}
    both = {k: v for k, v in temp_ok.items() if k in rain_ok}
    checked = {}

    def delivers(sid, pid):
        """Har stationen leveret mindst 12 hele-time-målinger det seneste døgn? (stationslisten alene er ikke nok)"""
        if (sid, pid) not in checked:
            end = now_utc()
            url = (f"{DMI}/v2/metObs/collections/observation/items?stationId={sid}&parameterId={pid}"
                   f"&datetime={iso(end - timedelta(hours=24))}/{iso(end)}&limit=2000")
            status, payload = http_get(url, cfg, f"recent_{pid}_{sid}.json")
            if FIXTURES is not None and status == 404:
                ok = True                       # offline-test: ingen fixture = stationen leverer
            else:
                ok = status == 200 and len(parse_metobs(payload)) >= 12
            checked[(sid, pid)] = ok
            if not ok and verbose:
                print(f"   springer {sid} {st[sid]['name']} over: ingen {pid} det seneste døgn (HTTP {status})")
            time.sleep(0.2)
        return checked[(sid, pid)]

    def nearest(pool, lat, lon, pids, max_try=8):
        order = sorted(pool, key=lambda k: haversine_km(lat, lon, pool[k]["lat"], pool[k]["lon"]))
        for k in order[:max_try]:
            if all(delivers(k, pid) for pid in pids):
                return k, haversine_km(lat, lon, pool[k]["lat"], pool[k]["lon"])
        sys.exit(f"Ingen station med {', '.join(pids)} og data blandt de {max_try} nærmeste.")

    for c in cfg["cities"]:
        fixed = c.get("temp_station")
        t_id = None
        if fixed and fixed in temp_ok and delivers(fixed, "temp_dry"):
            t_id, t_km = fixed, haversine_km(c["lat"], c["lon"], st[fixed]["lat"], st[fixed]["lon"])
        elif fixed and verbose:
            print(f"   {c['name']}: foretrukken temp_station {fixed} bruges ikke (findes ikke eller leverer ikke data)")
        if t_id is None:
            order = sorted(both, key=lambda k: haversine_km(c["lat"], c["lon"], both[k]["lat"], both[k]["lon"]))
            near_both = [k for k in order if haversine_km(c["lat"], c["lon"], both[k]["lat"], both[k]["lon"])
                         <= cfg["max_station_km"]]
            for k in near_both[:5]:
                if delivers(k, "temp_dry") and delivers(k, "precip_past1h"):
                    t_id, t_km = k, haversine_km(c["lat"], c["lon"], st[k]["lat"], st[k]["lon"])
                    break
        if t_id is None:
            t_id, t_km = nearest(temp_ok, c["lat"], c["lon"], ["temp_dry"])
        T = st[t_id]
        if t_id in rain_ok and delivers(t_id, "precip_past1h"):
            p_id, p_km = t_id, 0.0
        else:
            p_id, p_km = nearest(rain_ok, T["lat"], T["lon"], ["precip_past1h"])
        con.execute("INSERT OR REPLACE INTO stations (location, station_id, station_name, lat, lon, distance_km, resolved_at,"
                    " precip_station_id, precip_station_name, precip_km) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], t_id, T["name"], round(T["lat"], 4), round(T["lon"], 4), round(t_km, 1), fetched,
                     p_id, st[p_id]["name"], round(p_km, 1)))
        if verbose:
            rain = "samme station" if p_id == t_id else f"regn fra {p_id} {st[p_id]['name']} ({p_km:.1f} km fra temp.-stationen)"
            print(f"{c['name']:<12} → temp {t_id} {T['name']} ({t_km:.1f} km)  {T['lat']:.4f}, {T['lon']:.4f} · {rain}"
                  + (f" · TAF {c['icao']}" if _taf_ok(c, t_id, p_id) else ""))
        if c.get("icao") and not _taf_ok(c, t_id, p_id) and verbose:
            print(f"   {c['name']}: TAF {c['icao']} bruges ikke — lufthavnens station ({c.get('airport_station', '?')}) "
                  f"er ikke både temperatur- og regnstation.")
    con.commit()


def _taf_ok(city, t_id, p_id) -> bool:
    ap = city.get("airport_station")
    return bool(city.get("icao") and ap and t_id == ap and p_id == ap)


def stations(con):
    rows = con.execute("SELECT location, station_id, station_name, lat, lon, distance_km,"
                       " precip_station_id, precip_station_name, precip_km FROM stations").fetchall()
    return {r[0]: {"station_id": r[1], "name": r[2], "lat": r[3], "lon": r[4], "km": r[5],
                   "precip_id": r[6] or r[1], "precip_name": r[7] or r[2], "precip_km": r[8] or 0.0} for r in rows}


def source_urls(cfg, lat, lon, icao=None):
    lat, lon = round(lat, 4), round(lon, 4)     # MET: max 4 decimaler
    urls = {
        "met": f"{MET}?lat={lat}&lon={lon}",
        "dmi": (f"{DMI}/v1/forecastedr/collections/harmonie_dini_sf/position?"
                f"coords=POINT({lon}%20{lat})&crs=crs84&parameter-name=temperature-2m,total-precipitation&f=GeoJSON"),
    }
    if cfg.get("owm_api_key"):
        urls["owm"] = f"{OWM}?lat={lat}&lon={lon}&units=metric&appid={cfg['owm_api_key']}"
    if icao:
        urls["taf"] = f"{AWC}?ids={icao}&format=raw"
    return urls


PARSERS = {"met": parse_met, "owm": parse_owm, "dmi": parse_dmi, "taf": parse_taf}


def _city(cfg, loc):
    return next((c for c in cfg["cities"] if c["id"] == loc), {})


def collect(con, cfg, only_location=None, quiet=False, retry_pause=120):
    """Én runde: alle byer og kilder. Kilder der fejler (typisk 429), prøves igen én gang til sidst.
    issued_at = rundens starttidspunkt (så kilderne sammenlignes på samme runde); runs.fetched_at = faktisk tid."""
    st = stations(con)
    if not st:
        sys.exit("Ingen stationer endnu — kør 'kvx_weather.py stations' først.")
    round_dt = now_utc()
    round_iso = iso(round_dt)
    max_lead = cfg["max_lead_hours"]
    todo = []
    for loc, s in st.items():
        if only_location and loc != only_location:
            continue
        c = _city(cfg, loc)
        icao = c.get("icao") if _taf_ok(c, s["station_id"], s["precip_id"]) else None
        for source, url in source_urls(cfg, s["lat"], s["lon"], icao).items():
            todo.append((loc, source, url))
    summary = {}

    def fetch(loc, source, url, waits):
        fetched = iso(now_utc())
        status, payload = http_get(url, cfg, f"{source}_{loc}.json", waits=waits)
        err = None if status == 200 else f"HTTP {status}"
        rid = add_run(con, cfg, kind="forecast", source=source, location=loc, fetched_at=fetched,
                      status=status, url=redact(url), payload=payload if status == 200 else b"", error=err)
        n = 0
        if status == 200:
            try:
                rows = PARSERS[source](payload, ref=round_dt)
            except Exception as e:           # rå-svaret er gemt; parsing kan gentages senere
                con.execute("UPDATE runs SET parse_error=? WHERE id=?", (str(e)[:300], rid))
                rows = []
            for (t, w, var, v) in rows:
                lead = (t - round_dt).total_seconds() / 3600
                if lead < 0 or lead > max_lead:
                    continue
                con.execute("INSERT OR IGNORE INTO forecasts VALUES (?,?,?,?,?,?,?,?,?)",
                            (rid, source, loc, round_iso, iso(t), w, var, round(v, 3), round(lead, 2)))
                n += 1
            con.execute("UPDATE runs SET n_rows=? WHERE id=?", (n, rid))
        con.commit()
        summary[(loc, source)] = (status, n)
        return status

    failed = []
    for loc, source, url in todo:
        if fetch(loc, source, url, waits=WAITS.get(source, (15,))) != 200:
            failed.append((loc, source, url))
        time.sleep(0.5 if source != "taf" else 2)
    if failed and retry_pause:
        time.sleep(retry_pause)
        for loc, source, url in failed:
            fetch(loc, source, url, waits=WAITS_RETRY.get(source, (30, 90)))
            time.sleep(2)
    if not quiet:
        for (loc, source), (status, n) in summary.items():
            print(f"{round_iso}  {loc:<11} {source:<4} HTTP {status}  {n} rækker")
    return [(loc, source, status, n) for (loc, source), (status, n) in summary.items()]


def observe(con, cfg, days=3, quiet=False):
    st = stations(con)
    end = now_utc().replace(minute=0, second=0)
    start = end - timedelta(days=days)
    fetched = iso(now_utc())
    tot_new = tot_rev = 0
    for loc, s in st.items():
        for pid, sid in (("temp_dry", s["station_id"]), ("precip_past1h", s["precip_id"])):
            url = (f"{DMI}/v2/metObs/collections/observation/items?stationId={sid}"
                   f"&parameterId={pid}&datetime={iso(start)}/{iso(end)}&limit=20000")
            status, payload = http_get(url, cfg, f"obs_{pid}_{loc}.json")
            rid = add_run(con, cfg, kind="observation", source="dmi_obs", location=loc, fetched_at=fetched,
                          status=status, url=url, payload=payload if status == 200 else b"",
                          error=None if status == 200 else f"HTTP {status}")
            n = 0
            if status == 200:
                for (t, var, v) in parse_metobs(payload):
                    old = con.execute("SELECT value FROM observations WHERE station_id=? AND variable=? AND obs_time=?",
                                      (sid, var, iso(t))).fetchone()
                    if old is None:
                        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                                    (sid, loc, var, iso(t), v, rid))
                        n += 1
                    elif abs(old[0] - v) > 1e-9:
                        # DMI kan rette data bagud: første værdi beholdes, rettelsen logges.
                        con.execute("INSERT INTO obs_revisions VALUES (?,?,?,?,?,?,?)",
                                    (sid, var, iso(t), old[0], v, rid, fetched))
                        tot_rev += 1
                con.execute("UPDATE runs SET n_rows=? WHERE id=?", (n, rid))
            tot_new += n
            time.sleep(0.3)
    con.commit()
    if not quiet:
        print(f"{fetched}  observationer: {tot_new} nye, {tot_rev} rettelser logget")
    return tot_new, tot_rev


# ============================================================ score

def _obs_maps(con):
    temp, prec = {}, {}
    for loc, var, t, v in con.execute("SELECT location, variable, obs_time, value FROM observations"):
        (temp if var == "temp" else prec)[(loc, t)] = v
    return temp, prec


def _obs_window_precip(prec, loc, start: datetime, hours: int):
    """Sum af timenedbør i [start, start+hours). precip1h ved tid T dækker [T-1h, T)."""
    tot = 0.0
    for h in range(1, hours + 1):
        v = prec.get((loc, iso(start + timedelta(hours=h))))
        if v is None:
            return None
        tot += v
    return tot


def _fc_window_precip(rows_by_start, start: datetime, hours: int):
    """Byg et vindue på 'hours' timer af kildens egne vinduer (1 t eller 3 t eller 6 t), kun summer af mængder."""
    if (start, hours) in rows_by_start:
        return rows_by_start[(start, hours)]
    for w in (1, 3):
        if hours % w:
            continue
        parts = [rows_by_start.get((start + timedelta(hours=k * w), w)) for k in range(hours // w)]
        if all(p is not None for p in parts):
            return sum(parts)
    return None


LEADS = [(0, 24, "0–24 t"), (24, 48, "24–48 t"), (48, 72, "48–72 t")]


def _persist_k(lead_h: float) -> int:
    """Baseline 'i morgen bliver som i dag': den seneste måling på samme klokkeslæt, som var kendt
    da prognosen blev hentet, dvs. k døgn før måltidspunktet med k = ceil(lead/24)."""
    return max(1, math.ceil(lead_h / 24 - 1e-9))


SRC_ORDER = ("dmi", "met", "owm", "taf", "persist")
NAMES = {"dmi": "DMI", "met": "MET Norway (yr)", "owm": "OpenWeatherMap", "taf": "TAF (pilot)", "persist": "I morgen = i dag"}


def compute_scores(con, cfg, days=None) -> dict:
    """Alle tal til score og export i én struktur (så en offentlig side kan bygges af samme tal)."""
    temp_obs, prec_obs = _obs_maps(con)
    thr = cfg["rain_threshold_mm"]
    since = iso(now_utc() - timedelta(days=days)) if days else "0000"
    rows = con.execute(
        "SELECT run_id, source, location, issued_at, target_time, window_h, variable, value, lead_h"
        " FROM forecasts WHERE issued_at >= ?", (since,)).fetchall()

    # --- temperatur på fælles tidspunkter (hver 3. time UTC)
    paired = defaultdict(dict)    # (lab, iss, loc, tgt) -> {src: fejl}
    for run_id, src, loc, iss, tgt, w, var, val, lead in rows:
        if var != "temp" or w != 0:
            continue
        t = parse_ts(tgt)
        if t.hour % 3 or t.minute:
            continue
        o = temp_obs.get((loc, tgt))
        if o is None:
            continue
        for lo, hi, lab in LEADS:
            if lo <= lead < hi:
                cell = paired[(lab, iss, loc, tgt)]
                cell[src] = val - o
                p = temp_obs.get((loc, iso(t - timedelta(hours=24 * _persist_k(lead)))))
                if p is not None:
                    cell["persist"] = p - o
    temp_all = defaultdict(list)
    temp_same = defaultdict(list)
    present = {s for c in paired.values() for s in c}
    for (lab, *_k), cell in paired.items():
        for s, e in cell.items():
            temp_all[(s, lab)].append(e)
        if present and present <= set(cell):          # kun tidspunkter hvor ALLE kilder havde et bud
            for s, e in cell.items():
                temp_same[(s, lab)].append(e)

    # --- regn ja/nej i 6-timers vinduer 00/06/12/18 UTC
    per_run, yn, meta = defaultdict(dict), defaultdict(dict), {}
    for run_id, src, loc, iss, tgt, w, var, val, lead in rows:
        if var == "precip":
            per_run[run_id][(parse_ts(tgt), w)] = val
        elif var == "rainyn" and w == 6:
            yn[run_id][parse_ts(tgt)] = val >= 0.5
        else:
            continue
        meta[run_id] = (src, loc, iss)
    cells = defaultdict(dict)   # (lab, iss, loc, start) -> {src: (sagde regn, regnede)}
    for run_id, (src, loc, iss) in meta.items():
        iss_dt = parse_ts(iss)
        if run_id in yn:
            fc = dict(yn[run_id])
        else:
            windows = per_run[run_id]
            fc = {}
            for st_ in {s_ for (s_, _w) in windows if s_.hour % 6 == 0}:
                mm = _fc_window_precip(windows, st_, 6)
                if mm is not None:
                    fc[st_] = mm >= thr
        for st_, f_yes in fc.items():
            o = _obs_window_precip(prec_obs, loc, st_, 6)
            if o is None:
                continue
            lead = (st_ - iss_dt).total_seconds() / 3600 + 3        # vinduets midte
            lab = next((lab for lo, hi, lab in LEADS if lo <= lead < hi), None)
            if lab is None:
                continue
            cell = cells[(lab, iss, loc, st_)]
            cell[src] = (f_yes, o >= thr)
            if "persist" not in cell:
                pw = _obs_window_precip(prec_obs, loc, st_ - timedelta(hours=24 * _persist_k(lead + 3)), 6)
                if pw is not None:
                    cell["persist"] = (pw >= thr, o >= thr)

    def rain_table(require):
        acc = defaultdict(lambda: [0, 0, 0, 0])   # [ramt regn, overset regn, falsk alarm, ramt tørvejr]
        for (lab, *_k), cell in cells.items():
            if require and not require <= set(cell):
                continue
            for src, (f_yes, o_yes) in cell.items():
                if require and src not in require:
                    continue
                acc[(src, lab)][{(True, True): 0, (False, True): 1, (True, False): 2, (False, False): 3}[(f_yes, o_yes)]] += 1
        return acc

    present_rain = {s_ for c in cells.values() for s_ in c}
    rain = rain_table(None)
    rain_same = rain_table(present_rain - {"taf"})            # alle byer, alle kilder undtagen TAF
    rain_airports = rain_table(present_rain) if "taf" in present_rain else {}   # kun lufthavne, inkl. TAF

    # --- regn-%: kalibrering pr. kilde i kildens eget vindue
    calib = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    brier = defaultdict(lambda: [0.0, 0])
    for run_id, src, loc, iss, tgt, w, var, val, lead in rows:
        if var != "pop" or lead > 48:
            continue
        o = _obs_window_precip(prec_obs, loc, parse_ts(tgt), w)
        if o is None:
            continue
        y = 1 if o >= thr else 0
        b = min(9, int(val * 10))
        calib[(src, w)][b][0] += 1
        calib[(src, w)][b][1] += y
        brier[(src, w)][0] += (val - y) ** 2
        brier[(src, w)][1] += 1

    def stats(errs):
        return {"n": len(errs), "mae": sum(abs(x) for x in errs) / len(errs), "bias": sum(errs) / len(errs)}

    out = {"generated_at": iso(now_utc()), "days": days, "rain_threshold_mm": thr,
           "temp": {}, "temp_same_times": {}, "rain": {}, "rain_same": {}, "rain_airports": {}, "pop": {},
           "taf_mapping": TAF_P}
    for lo, hi, lab in LEADS:
        for s in SRC_ORDER:
            if temp_all.get((s, lab)):
                out["temp"].setdefault(lab, {})[s] = stats(temp_all[(s, lab)])
            if temp_same.get((s, lab)):
                out["temp_same_times"].setdefault(lab, {})[s] = stats(temp_same[(s, lab)])
            for key, table in (("rain", rain), ("rain_same", rain_same), ("rain_airports", rain_airports)):
                h, m, fa, cn = table.get((s, lab), [0, 0, 0, 0])
                n = h + m + fa + cn
                if n:
                    out[key].setdefault(lab, {})[s] = {
                        "n": n, "correct": (h + cn) / n,
                        "caught": h / (h + m) if h + m else None,
                        "false_alarm": fa / (h + fa) if h + fa else None,
                        "counts": [h, m, fa, cn]}
    for (src, w), bins in sorted(calib.items()):
        bs, bn = brier[(src, w)]
        out["pop"][f"{src}_{w}h"] = {
            "source": src, "window_h": w, "n": bn, "brier": bs / bn,
            "bins": {f"{b*10}-{b*10+10}": {"n": n, "rained": r / n} for b, (n, r) in sorted(bins.items())}}
    return out


def score(con, cfg, days=None, as_json=False):
    r = compute_scores(con, cfg, days)
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return r
    pct = lambda x: "   –  " if x is None else f"{x:6.1%}"
    print(f"KVANTIX VEJR-TEST · stilling {r['generated_at']}" + (f" · seneste {days} dage" if days else ""))
    print("\nTEMPERATUR — gennemsnitlig fejl i °C (MAE) og skævhed (bias, + = for varmt)")
    print("  kun tidspunkter hvor alle kilder havde et bud (fair sammenligning):")
    for lab, by in r["temp_same_times"].items():
        for s, v in by.items():
            print(f"  {lab:<8} {NAMES[s]:<17} MAE {v['mae']:5.2f}  bias {v['bias']:+5.2f}  (n={v['n']})")
    print(f"\nREGN JA/NEJ — 6-timers vinduer, regn = mindst {r['rain_threshold_mm']} mm")
    for key, title in (("rain_same", "alle byer, samme perioder for alle kilder (uden TAF)"),
                       ("rain_airports", "lufthavnene, samme perioder inkl. pilotprognosen (TAF)")):
        if not r[key]:
            continue
        print(f"  {title}:")
        for lab, by in r[key].items():
            for s, v in by.items():
                print(f"  {lab:<8} {NAMES[s]:<17} ramt {pct(v['correct'])}  fanget regn {pct(v['caught'])}"
                      f"  falsk alarm {pct(v['false_alarm'])}  (n={v['n']})")
    print("\nREGN-% — sker '30 %' 3 af 10 gange? (første 48 t, kildens eget vindue)")
    for k, v in r["pop"].items():
        print(f"  {NAMES[v['source']]} · {v['window_h']}-timers vindue · Brier {v['brier']:.3f} (n={v['n']})")
        for b, bv in v["bins"].items():
            if bv["n"] >= 10:
                print(f"     sagt {b:>7} %  skete {bv['rained']:6.1%}  (n={bv['n']})")
    print("\n'I morgen = i dag' er baseline: den seneste måling på samme klokkeslæt. En prognose skal slå den.")
    print("Kilder: DMI (CC BY 4.0, 'Baseret på data fra DMI og efterfølgende bearbejdet'), "
          "MET Norway (CC BY 4.0), OpenWeatherMap (CC BY-SA 4.0), TAF via aviationweather.gov (NOAA).")
    return r


def export(con, cfg, out_dir):
    """Toolkit-format: én fil pr. kilde og variabel. temp = prognose (forecast), pop = sandsynlighed."""
    os.makedirs(out_dir, exist_ok=True)
    temp_obs, prec_obs = _obs_maps(con)
    thr = cfg["rain_threshold_mm"]
    files = {}
    for src, loc, iss, tgt, w, var, val, lead in con.execute(
            "SELECT source, location, issued_at, target_time, window_h, variable, value, lead_h FROM forecasts"):
        if var == "temp" and w == 0 and 20 <= lead < 28:
            o = temp_obs.get((loc, tgt))
            if o is not None:
                files.setdefault(f"temp_24h_{src}.csv", []).append((iss, loc, val, o))
        elif var == "pop" and lead < 48:
            o = _obs_window_precip(prec_obs, loc, parse_ts(tgt), w)
            if o is not None:
                files.setdefault(f"pop_{w}h_{src}.csv", []).append((iss, loc, val, 1 if o >= thr else 0))
    for name, rows in files.items():
        with open(os.path.join(out_dir, name), "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["timestamp", "group", "prediction", "outcome"])
            wr.writerows(sorted(rows))
        print(f"{os.path.join(out_dir, name)}  {len(rows)} rækker")


# ============================================================ offentlig side (statisk HTML, ingen JS)

PAGE_NAMES = {"dmi": "DMI", "met": "MET Norway", "owm": "OpenWeatherMap (free 3-hour)", "taf": "Pilot forecast (TAF)", "persist": "“Same as yesterday”"}
PRELIMINARY_DAYS = 30


def _h(x) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def page(con, cfg, out_path):
    r = compute_scores(con, cfg)
    with open(os.path.abspath(__file__), "rb") as fh:
        code_sha = hashlib.sha256(fh.read()).hexdigest()
    r["code_sha256"] = code_sha
    first = con.execute("SELECT MIN(fetched_at) FROM runs WHERE kind='forecast' AND http_status=200").fetchone()[0]
    n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    head = con.execute("SELECT chain_hash FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    ok, _ = verify_chain(con, cfg)
    st = stations(con)
    days_running = (now_utc() - parse_ts(first)).days if first else 0
    prelim = days_running < PRELIMINARY_DAYS
    city = {c["id"]: c["name"] for c in cfg["cities"]}

    def lead_en(lab):
        return lab.replace(" t", " h").replace("–", "–")

    def temp_table():
        rows = []
        for lab, by in r["temp_same_times"].items():
            best = min((v["mae"] for s, v in by.items()), default=None)
            for s, v in by.items():
                cls = ' class="best"' if v["mae"] == best else ""
                rows.append(f"<tr{cls}><td>{_h(lead_en(lab))}</td><td>{_h(PAGE_NAMES[s])}</td>"
                            f"<td>{v['mae']:.2f} °C</td><td>{v['bias']:+.2f} °C</td><td>{v['n']}</td></tr>")
        return "\n".join(rows) or '<tr><td colspan="5">No scored forecasts yet.</td></tr>'

    def rain_table(key):
        rows = []
        pct = lambda x: "–" if x is None else f"{x:.0%}"
        for lab, by in r[key].items():
            best = max((v["correct"] for v in by.values()), default=None)
            for s, v in by.items():
                cls = ' class="best"' if v["correct"] == best else ""
                rows.append(f"<tr{cls}><td>{_h(lead_en(lab))}</td><td>{_h(PAGE_NAMES[s])}</td><td>{pct(v['correct'])}</td>"
                            f"<td>{pct(v['caught'])}</td><td>{pct(v['false_alarm'])}</td><td>{v['n']}</td></tr>")
        return "\n".join(rows) or '<tr><td colspan="6">No scored forecasts yet.</td></tr>'

    def pop_blocks():
        out = []
        for k, v in r["pop"].items():
            cells = "".join(f"<tr><td>{_h(b)} %</td><td>{bv['rained']:.0%}</td><td>{bv['n']}</td></tr>"
                            for b, bv in v["bins"].items() if bv["n"] >= 10)
            out.append(f"<h3>{_h(PAGE_NAMES[v['source']])} · {v['window_h']}-hour window · Brier score {v['brier']:.3f}</h3>"
                       f"<table><tr><th>Forecast said</th><th>It rained</th><th>n</th></tr>{cells}</table>")
        return "\n".join(out) or "<p>No scored rain probabilities yet.</p>"

    def loc_line(l, s):
        rain = ("" if s["precip_id"] == s["station_id"] else
                f"; rain from station {_h(s['precip_id'])} {_h(s['precip_name'])}, {s['precip_km']} km from the temperature station")
        return (f"<li>{_h(city.get(l, l))}: DMI station {_h(s['station_id'])} {_h(s['name'])} "
                f"({s['km']} km from the city centre){rain}</li>")
    locs = "".join(loc_line(l, s) for l, s in st.items())
    airports = [f"{city.get(l, l)} ({_city(cfg, l)['icao']})" for l, s in st.items()
                if _taf_ok(_city(cfg, l), s["station_id"], s["precip_id"])]
    banner = (f'<p class="warn">Preliminary: the test has run for {days_running} days. Results are shown from day one, '
              f'but read nothing into them before {PRELIMINARY_DAYS} days.</p>' if prelim else "")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weather Forecast Test | Kvantix</title>
<meta name="description" content="Which weather forecast is right most often in Denmark? DMI, MET Norway and OpenWeatherMap, logged before the weather happens and scored against DMI measurements.">
<style>
:root{{--bg:#fff;--fg:#14171a;--mute:#5b6570;--line:#e3e7eb;--acc:#0b6bcb;--best:#eaf4ff;--warn:#fff4d6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1215;--fg:#e8ecef;--mute:#9aa5b1;--line:#252c33;--acc:#5aa9ff;--best:#13263a;--warn:#3a3014}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:1.8rem;margin:.2em 0}} h2{{margin-top:2em;font-size:1.25rem}} h3{{font-size:1rem;margin:1.4em 0 .4em}}
p,li{{color:var(--fg)}} .mute{{color:var(--mute);font-size:.9rem}}
.warn{{background:var(--warn);padding:10px 14px;border-radius:8px}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;margin:.4em 0;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--mute);font-weight:600;font-size:.85rem}} tr.best td{{background:var(--best);font-weight:600}}
code{{font-size:.85rem;word-break:break-all}} a{{color:var(--acc)}}
</style></head><body><main>
<p class="mute"><a href="https://kvantix.tech/">Kvantix</a> · independent forecast validation</p>
<h1>Which weather forecast is right most often?</h1>
<p>Every six hours we save what DMI, MET Norway and OpenWeatherMap forecast for {len(st)} Danish cities, plus the pilots'
airport forecast (TAF), and lock each download
in a hash chain <em>before</em> the weather happens. Afterwards we compare with what DMI's own weather stations measured.
Nobody can change a forecast after the fact, including us.</p>
{banner}
<p class="mute">Updated {_h(r['generated_at'])} · running since {_h(first or '–')} · {n_runs} locked downloads ·
chain {'intact' if ok else '<strong>BROKEN</strong>'}</p>

<h2>Temperature</h2>
<p>Average error in °C (lower is better). Bias shows whether a source runs warm (+) or cold (−).
Only moments where every source had a forecast are counted, so the comparison is fair.</p>
<div class="tw"><table><tr><th>Ahead</th><th>Source</th><th>Avg. error</th><th>Bias</th><th>n</th></tr>
{temp_table()}</table></div>

<h2>Rain or no rain</h2>
<p>Six-hour periods. “Rain” means at least {r['rain_threshold_mm']} mm measured. Caught: share of rainy periods the
forecast saw coming. False alarm: share of rain forecasts where it stayed dry.</p>
<h3>All cities, same periods for every source</h3>
<div class="tw"><table><tr><th>Ahead</th><th>Source</th><th>Right</th><th>Caught</th><th>False alarm</th><th>n</th></tr>
{rain_table("rain_same")}</table></div>
<h3>Airports: the pilots' forecast against the apps</h3>
<p>Pilots get their own forecast, the TAF, for each airport. We score it only where DMI measures both temperature and rain
at the airport itself ({_h(", ".join(airports) or "none yet")}), on the same periods as the other sources.</p>
<div class="tw"><table><tr><th>Ahead</th><th>Source</th><th>Right</th><th>Caught</th><th>False alarm</th><th>n</th></tr>
{rain_table("rain_airports")}</table></div>

<h2>Does “30 % chance of rain” mean 3 times out of 10?</h2>
<p>A well-calibrated forecast says 30 % and it then rains about 30 % of the time. Next 48 hours, each source in its own time window.</p>
<div class="tw">{pop_blocks()}</div>

<h2>The yardstick: “same as yesterday”</h2>
<p>A forecast is only worth something if it beats the lazy guess that tomorrow will be like today. That guess uses the most
recent measurement at the same time of day that was known when the forecast was made. It is shown in every table.</p>

<h2>Method, fixed before the first forecast</h2>
<ul>
<li>Forecasts are fetched at 00, 06, 12 and 18 UTC for the coordinates of the nearest DMI station, up to 72 hours ahead.</li>
<li>Each raw response is stored compressed, hashed (SHA-256) and chained to the previous download before it is read.</li>
<li>Temperature is compared at every third hour (UTC). Rain amounts are summed into six-hour periods from each source's own time steps.</li>
<li>Measurements come from DMI's station network. If DMI later corrects a measurement, the first value is kept and the correction logged.</li>
<li>Note: DMI is both a participant and the source of the measurements. All sources are scored against the same stations, but DMI's own model may have a home advantage near its stations.</li>
<li>“Rain” needs at least {r['rain_threshold_mm']} mm, so a single 0.1 mm tip of the rain gauge does not count.</li>
<li>The TAF is not a probability forecast. We translate it with ICAO's own definitions, fixed before the start:
PROB30 and PROB40 count as 30 % and 40 %; TEMPO and BECMG are only used for at least 50 % (we use 60 %); precipitation in
the main forecast counts as 90 %; no precipitation mentioned counts as 10 %. “Rain” means 50 % or more. Showers “in the
vicinity” (VC) do not count, because the rain gauge is at the airport.</li>
<li>OpenWeatherMap is scored on its free 5-day/3-hour forecast, the product anyone can use without paying. Its own app
may use a finer, paid product.</li>
<li>Scoring code: <code>kvx_weather.py</code>, version {VERSION}, SHA-256 <code>{_h(code_sha)}</code>. Compare it with the
published source code. The rules do not change while the test runs.</li>
</ul>
<ul>{locs}</ul>
<p class="mute">Chain head: <code>{_h(head[0] if head else '–')}</code></p>

<h2>Sources and licences</h2>
<p class="mute">Forecast and observation data from DMI (Danish Meteorological Institute), CC BY 4.0: based on data from DMI,
subsequently processed. Forecast data from MET Norway, CC BY 4.0. Forecast data from OpenWeatherMap, CC BY-SA 4.0; the
results tables on this page are shared under the same licence. TAF via the Aviation Weather Center (NOAA),
aviationweather.gov; Danish TAFs are issued by DMI. Kvantix is not affiliated with any of the sources.</p>
<p class="mute">This page shows how the same method we use to validate trading signals works on something everyone checks
every day. <a href="https://kvantix.tech/">Kvantix</a> · <a href="mailto:validation@kvantix.tech">validation@kvantix.tech</a></p>
</main></body></html>
"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, out_path)
    with open(os.path.join(os.path.dirname(out_path) or ".", "scores.json"), "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False, indent=1)
    print(f"{out_path}  ({'foreløbig' if prelim else 'endelig'}, {days_running} dage)")


def _probe_source(source, url, cfg, loc):
    status, payload = http_get(url, cfg, f"{source}_{loc}.json", waits=WAITS.get(source, (15, 45)))
    print(f"\n[{source}] HTTP {status}  {len(payload)} bytes  {LAST_ATTEMPTS} forsøg  {redact(url)}")
    if status != 200:
        print("   svar:", payload[:300].decode("utf-8", "replace"))
        return
    if source == "taf":
        print("   rå:", " ".join(payload.decode("utf-8", "replace").split())[:400])
    try:
        rows = PARSERS[source](payload, ref=now_utc())
    except Exception as e:
        print("   PARSE-FEJL:", e)
        print("   start af svar:", payload[:600].decode("utf-8", "replace"))
        return
    by = defaultdict(list)
    for t, w, var, v in rows:
        by[(var, w)].append((t, v))
    for (var, w), vals in sorted(by.items()):
        vals.sort()
        print(f"   {var:<6} vindue {w} t: {len(vals):>3} værdier  {iso(vals[0][0])} → {iso(vals[-1][0])}"
              f"  første {vals[0][1]:.2f}  min {min(v for _, v in vals):.2f}  max {max(v for _, v in vals):.2f}")


def probe(con, cfg, loc=None, sources=None):
    st = stations(con)
    if not st:
        sys.exit("Ingen stationer endnu — kør 'kvx_weather.py stations' først.")
    loc = loc or next(iter(st))
    if loc not in st:
        sys.exit(f"Ukendt by '{loc}'. Vælg en af: {', '.join(st)}")
    s = st[loc]
    c = _city(cfg, loc)
    icao = c.get("icao") if _taf_ok(c, s["station_id"], s["precip_id"]) else None
    print(f"Probe: {loc} (temp {s['station_id']} {s['name']}, {s['lat']}, {s['lon']}; "
          f"regn {s['precip_id']} {s['precip_name']}" + (f"; TAF {icao}" if icao else "") + ")")
    for source, url in source_urls(cfg, s["lat"], s["lon"], icao).items():
        if sources and source not in sources:
            continue
        _probe_source(source, url, cfg, loc)
    if not sources or "obs" in sources:
        a, b = iso(now_utc() - timedelta(hours=6)), iso(now_utc())
        for pid, sid in (("temp_dry", s["station_id"]), ("precip_past1h", s["precip_id"])):
            url = (f"{DMI}/v2/metObs/collections/observation/items?stationId={sid}&parameterId={pid}"
                   f"&datetime={a}/{b}&limit=200")
            status, payload = http_get(url, cfg, f"obs_{pid}_{loc}.json")
            obs = sorted(parse_metobs(payload)) if status == 200 else []
            raw_n = len(json.loads(payload).get("features", [])) if status == 200 else 0
            print(f"\n[dmi_obs {pid} @ {sid}] HTTP {status}  {raw_n} målinger, {len(obs)} på hele timer"
                  + (f", seneste {iso(obs[-1][0])} = {obs[-1][2]}" if obs else ""))


def main():
    global FIXTURES
    ap = argparse.ArgumentParser(description="Kvantix vejr-forward-test")
    ap.add_argument("cmd", choices=["stations", "probe", "collect", "observe", "verify", "score", "export", "page"])
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--loc", default=None, help="probe: by-id, fx aalborg")
    ap.add_argument("--sources", default=None, help="probe: kun disse, fx dmi,taf,obs")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true", help="score: skriv tallene som JSON")
    ap.add_argument("--fixtures", default=None, help="offline-test: mappe med gemte API-svar")
    a = ap.parse_args()
    FIXTURES = a.fixtures
    cfg = load_config()
    con = db_connect(cfg)
    if a.cmd == "stations":
        resolve_stations(con, cfg)
    elif a.cmd == "probe":
        probe(con, cfg, a.loc, set(a.sources.split(",")) if a.sources else None)
    elif a.cmd == "collect":
        collect(con, cfg)
    elif a.cmd == "observe":
        observe(con, cfg, days=a.days or 3)
    elif a.cmd == "verify":
        ok, problems = verify_chain(con, cfg)
        n = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        head = con.execute("SELECT chain_hash FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        print(f"{n} hentninger i kæden · hoved {head[0] if head else '-'}")
        print("KÆDEN ER INTAKT" if ok else "PROBLEMER:\n  " + "\n  ".join(problems[:50]))
        sys.exit(0 if ok else 1)
    elif a.cmd == "score":
        score(con, cfg, days=a.days, as_json=a.json)
    elif a.cmd == "page":
        page(con, cfg, a.out or os.path.join(cfg["data_dir"], "public", "index.html"))
    elif a.cmd == "export":
        export(con, cfg, a.out or os.path.join(cfg["data_dir"], "export"))


if __name__ == "__main__":
    main()

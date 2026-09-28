#!/usr/bin/env python3
"""Offline-test af kvx_weather.py: syntetiske API-svar i kildernes dokumenterede formater.
Kør:  python3 tests/test_offline.py      (ingen netværk, skriver kun i en midlertidig mappe)"""
import gzip, json, math, os, random, shutil, sqlite3, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import kvx_weather as kw  # noqa: E402

UTC = timezone.utc
rng = random.Random(7)
T0 = datetime(2026, 9, 20, 6, 7, tzinfo=UTC)
CLOCK = [T0]
kw.now_utc = lambda: CLOCK[0].replace(microsecond=0)

TMP = tempfile.mkdtemp(prefix="kvxw_")
cfg_path = os.path.join(TMP, "config.json")
json.dump({"data_dir": os.path.join(TMP, "data"), "owm_api_key": "TESTKEY123",
           "cities": [dict(kw.DEFAULT_CONFIG["cities"][0], temp_station="06034"),   # foretrukken, men tavs
                      kw.DEFAULT_CONFIG["cities"][1]]}, open(cfg_path, "w"))
kw.CONFIG_PATH = cfg_path

STATIONS = {  # id: (navn, lat, lon, params, validTo)
    "06041": ("Skagen Fyr", 57.7364, 10.6316, ["temp_dry", "precip_past1h"], None),
    "06042": ("Hjørring gl.", 57.4700, 9.9800, ["temp_dry", "precip_past1h"], "2019-01-01T00:00:00Z"),
    "06043": ("Hjørring temp", 57.4650, 9.9830, ["temp_dry"], None),      # tættest, men byen har fast temp_station
    "06034": ("Sindal Lufthavn", 57.5035, 10.2294, ["temp_dry"], None),
    "05015": ("Lendum", 57.4400, 10.2000, ["precip_past1h"], None),
    "05005": ("Uggerby", 57.5700, 10.1300, ["precip_past1h"], None),
    "06031": ("Tylstrup", 57.1856, 9.9544, ["temp_dry", "precip_past1h"], None),
    "06030": ("Flyvestation Aalborg", 57.0964, 9.8503, ["temp_dry", "precip_past1h"], None),
}


def truth_temp(loc, t):
    base = 12 if loc == "hjoerring" else 13
    return base + 5 * math.sin(2 * math.pi * (t.hour - 9) / 24) + 2 * math.sin(t.timestamp() / 86400 / 3)


def truth_rain(loc, t_end):  # mm i timen [t_end-1h, t_end)
    x = (t_end.timestamp() // 3600) % 37
    return round(0.4 + (x % 5) * 0.3, 1) if x < 8 else 0.0


def w(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def forecast_fixtures(fx, now, dmi_format="geojson", broken=None):
    os.makedirs(fx, exist_ok=True)
    w(os.path.join(fx, "stations.json"), {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"stationId": sid, "name": n, "parameterId": p, "validTo": vt, "status": "Active"}}
        for sid, (n, lat, lon, p, vt) in STATIONS.items()]})
    start = now.replace(minute=0, second=0) + timedelta(hours=1)
    for loc in ("hjoerring", "aalborg"):
        # MET: timeværdier i 60 t, derefter hver 6. time
        ts = []
        for h in range(0, 90):
            t = start + timedelta(hours=h)
            if h >= 60 and t.hour % 6:
                continue
            d = {"instant": {"details": {"air_temperature": round(truth_temp(loc, t) + rng.gauss(0, 1.0), 1)}}}
            if h < 60:
                r = truth_rain(loc, t + timedelta(hours=1))
                d["next_1_hours"] = {"details": {"precipitation_amount": r, "probability_of_precipitation": 80.0 if r else 10.0}}
            r6 = sum(truth_rain(loc, t + timedelta(hours=k)) for k in range(1, 7))
            d["next_6_hours"] = {"details": {"precipitation_amount": round(r6, 1), "probability_of_precipitation": 70.0 if r6 else 15.0}}
            ts.append({"time": kw.iso(t), "data": d})
        w(os.path.join(fx, f"met_{loc}.json"), {"type": "Feature", "properties": {"timeseries": ts}})
        # OWM: dt på hele 3-timer; rain.3h = de 3 timer FØR dt
        lst = []
        t = now.replace(minute=0, second=0) + timedelta(hours=3 - now.hour % 3)
        for i in range(40):
            tt = t + timedelta(hours=3 * i)
            r3 = sum(truth_rain(loc, tt - timedelta(hours=k)) for k in range(0, 3))
            it = {"dt": int(tt.timestamp()), "main": {"temp": round(truth_temp(loc, tt) + 0.8 + rng.gauss(0, 1.2), 2)},
                  "pop": 0.75 if r3 else 0.1}
            if r3:
                it["rain"] = {"3h": round(r3, 2)}
            lst.append(it)
        w(os.path.join(fx, f"owm_{loc}.json"), {"cod": "200", "list": lst})
        # DMI: Kelvin, akkumuleret nedbør fra kørslens start
        steps, acc = [], 0.0
        for h in range(0, 61):
            t = start + timedelta(hours=h)
            steps.append((t, round(truth_temp(loc, t) + 273.15 + rng.gauss(0, 0.7), 2), round(acc, 2)))
            acc += truth_rain(loc, t + timedelta(hours=1)) * (1 + rng.gauss(0, 0.1))
        if dmi_format == "geojson":
            obj = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [9.98, 57.46]},
                 "properties": {"step": kw.iso(t), "temperature-2m": k, "total-precipitation": a}} for t, k, a in steps]}
        else:
            obj = {"type": "Coverage", "domain": {"axes": {"t": {"values": [kw.iso(t) for t, _, _ in steps]}}},
                   "ranges": {"temperature-2m": {"values": [k for _, k, _ in steps]},
                              "total-precipitation": {"values": [a for _, _, a in steps]}}}
        w(os.path.join(fx, f"dmi_{loc}.json"), obj)
        if loc == "aalborg":
            # TAF 30 t: grundvejr uden nedbør, TEMPO over timer hvor det regner; PROB30 over en tør periode
            v0 = now.replace(minute=0, second=0) + timedelta(hours=1)
            v1 = v0 + timedelta(hours=30)
            ddhh = lambda t: f"{t.day:02d}{t.hour:02d}"
            parts = [f"TAF EKYT {now.day:02d}{now.hour:02d}{now.minute:02d}Z {ddhh(v0)}/{ddhh(v1)} 24012KT 9999 SCT030"]
            t, run = v0, None
            while t <= v1:
                wet = t < v1 and truth_rain(loc, t + timedelta(hours=1)) > 0
                if wet and run is None:
                    run = t
                if not wet and run is not None:
                    parts.append(f"TEMPO {ddhh(run)}/{ddhh(t)} RA BKN012")
                    run = None
                t += timedelta(hours=1)
            parts.append("BECMG 0000/0000 VCSH")   # ugyldig periode ignoreres ikke-destruktivt
            open(os.path.join(fx, f"taf_{loc}.json"), "w").write("\n  ".join(parts[:1] + parts[1:-1]) + "=\n")
        if broken == "dmi429" and loc == "aalborg":
            os.replace(os.path.join(fx, f"dmi_{loc}.json"), os.path.join(fx, f"dmi_{loc}.ok"))
            open(os.path.join(fx, f"dmi_{loc}.json"), "w").write('HTTP 429\n{"message":"Server is busy"}')
        elif broken and loc == "aalborg":
            open(os.path.join(fx, f"{broken}_{loc}.json"), "w").write('{"noget": "helt andet"')


def obs_fixtures(fx, a, b):
    os.makedirs(fx, exist_ok=True)
    sid = {"hjoerring": "06043?", "aalborg": "06030"}
    for loc in ("hjoerring", "aalborg"):
        for pid in ("temp_dry", "precip_past1h"):
            feats, t = [], a
            while t <= b:
                for minute in (0, 10) if pid == "temp_dry" else (0,):
                    tt = t + timedelta(minutes=minute)
                    v = round(truth_temp(loc, tt), 1) if pid == "temp_dry" else truth_rain(loc, tt)
                    feats.append({"type": "Feature", "properties": {"parameterId": pid, "stationId": sid[loc],
                                                                    "observed": kw.iso(tt), "value": v}})
                t += timedelta(hours=1)
            w(os.path.join(fx, f"obs_{pid}_{loc}.json"), {"type": "FeatureCollection", "features": feats})


def ok(cond, msg):
    print(("  OK   " if cond else "  FEJL ") + msg)
    if not cond:
        global FAILS
        FAILS += 1


FAILS = 0
cfg = kw.load_config()
con = kw.db_connect(cfg)

print("1. stationer")
fx = os.path.join(TMP, "fx0")
forecast_fixtures(fx, T0)
kw.FIXTURES = fx
w(os.path.join(fx, "recent_temp_dry_06034.json"), {"type": "FeatureCollection", "features": []})   # Sindal: tavs
kw.resolve_stations(con, cfg)
st = kw.stations(con)
ok(st["hjoerring"]["station_id"] == "06043" and st["hjoerring"]["precip_id"] == "05015",
   "Hjørring: Sindal (ingen data) springes over; temperatur fra nærmeste station med data, regn fra måleren nærmest den")
ok(st["aalborg"]["station_id"] == "06030", "Aalborg → Flyvestation Aalborg")

print("2. collect ×8 over 2 døgn (hver 6. time), DMI skiftevis GeoJSON og CoverageJSON")
for i in range(8):
    CLOCK[0] = T0 + timedelta(hours=6 * i)
    fx = os.path.join(TMP, f"fx{i+1}")
    forecast_fixtures(fx, CLOCK[0], "geojson" if i % 2 == 0 else "coverage",
                      broken="met" if i == 3 else ("dmi429" if i == 2 else None))
    kw.FIXTURES = fx
    real_sleep = kw.time.sleep
    if i == 2:   # DMI er "busy" i første forsøg; under pausen bliver serveren ledig igen
        kw.time.sleep = lambda sec, fx=fx: sec >= 100 and os.path.exists(os.path.join(fx, "dmi_aalborg.ok")) and \
            os.replace(os.path.join(fx, "dmi_aalborg.ok"), os.path.join(fx, "dmi_aalborg.json"))
    else:
        kw.time.sleep = lambda sec: None
    summ = kw.collect(con, cfg, quiet=True)
    kw.time.sleep = real_sleep
    if i == 0:
        for loc, src, status, n in summ:
            print(f"     {loc:<10} {src:<4} HTTP {status} {n} rækker")
    ok(all(s == 200 for _, _, s, _ in summ), f"hentning {i+1}: alle HTTP 200")

r429 = con.execute("SELECT COUNT(*) FROM runs WHERE source='dmi' AND http_status=429").fetchone()[0]
ok(r429 == 1, "DMI 429: første forsøg logget som hul i kæden, anden runde lykkedes")
n_taf = con.execute("SELECT COUNT(*) FROM forecasts WHERE source='taf'").fetchone()[0]
ok(n_taf > 0, f"TAF læst for Aalborg ({n_taf} rækker)")
ok(con.execute("SELECT COUNT(*) FROM forecasts WHERE source='taf' AND location='hjoerring'").fetchone()[0] == 0,
   "ingen TAF for Hjørring (ingen lufthavn med TAF)")
rounds = con.execute("SELECT COUNT(DISTINCT issued_at) FROM forecasts").fetchone()[0]
ok(rounds == 8, "issued_at = rundens start, også for genforsøg (8 runder)")
r = con.execute("SELECT COUNT(*) FROM runs WHERE parse_error IS NOT NULL").fetchone()[0]
ok(r == 1, "ødelagt MET-svar: parse_error registreret, resten kørte videre")
urls = [u for (u,) in con.execute("SELECT url FROM runs WHERE source='owm'")]
ok(all("TESTKEY123" not in u and "appid=%2A%2A%2A" in u for u in urls), "OWM-nøglen er redigeret ud af gemte URL'er")
ok("TESTKEY123" not in open(os.path.join(cfg["data_dir"], "chain.log")).read(), "nøglen står ikke i chain.log")
leads = con.execute("SELECT MIN(lead_h), MAX(lead_h) FROM forecasts").fetchone()
ok(leads[0] >= 0 and leads[1] <= 72, f"lead mellem 0 og 72 t ({leads[0]}–{leads[1]})")
dmi_t = con.execute("SELECT AVG(value) FROM forecasts WHERE source='dmi' AND variable='temp'").fetchone()[0]
ok(0 < dmi_t < 30, f"DMI Kelvin omregnet til °C (gennemsnit {dmi_t:.1f})")
neg = con.execute("SELECT COUNT(*) FROM forecasts WHERE variable='precip' AND value < 0").fetchone()[0]
ok(neg == 0, "ingen negativ nedbør")
owm_w = con.execute("SELECT DISTINCT window_h FROM forecasts WHERE source='owm' AND variable='precip'").fetchall()
ok(owm_w == [(3,)], "OWM-nedbør i 3-timers vinduer")

print("3. observe (5 døgn efter start)")
CLOCK[0] = T0 + timedelta(days=5)
fx = os.path.join(TMP, "fxobs")
obs_fixtures(fx, (T0 - timedelta(days=3)).replace(minute=0), CLOCK[0])
kw.FIXTURES = fx
n_new, n_rev = kw.observe(con, cfg, days=8, quiet=True)
ok(n_new > 0, f"{n_new} observationer (kun hele timer)")
mins = con.execute("SELECT COUNT(*) FROM observations WHERE substr(obs_time,15,2) != '00'").fetchone()[0]
ok(mins == 0, "10-minutters målinger er filtreret fra")
# DMI retter en måling bagud
p = os.path.join(fx, "obs_temp_dry_aalborg.json")
j = json.load(open(p)); j["features"][30]["properties"]["value"] += 1.5; json.dump(j, open(p, "w"))
n_new2, n_rev2 = kw.observe(con, cfg, days=8, quiet=True)
ok(n_new2 == 0 and n_rev2 == 1, "rettelse bagud: første værdi beholdt, rettelsen logget")

tr = kw.parse_metobs(json.dumps({"features": [{"properties": {"parameterId": "precip_past1h", "observed": "2026-09-20T10:00:00Z", "value": -0.1}}]}).encode())
ok(tr[0][2] == 0.0, "DMI's spor-kode -0,1 mm tælles som 0")

# rigtige værdier fra DMI 28-09-2026 (Aalborg): støj på ±0,005 mm i den løbende sum
real = [("2026-09-29T09:00:00.000Z", 0.85315704), ("2026-09-29T10:00:00.000Z", 0.8531647),
        ("2026-09-29T11:00:00.000Z", 0.8521881), ("2026-09-29T12:00:00.000Z", 0.85269165),
        ("2026-09-29T13:00:00.000Z", 1.1086884), ("2026-09-29T14:00:00.000Z", 1.9056854),
        ("2026-09-29T15:00:00.000Z", 2.592865), ("2026-09-29T16:00:00.000Z", 2.6098785),
        ("2026-09-29T17:00:00.000Z", 2.6080017), ("2026-09-29T18:00:00.000Z", 2.3)]
pr = {kw.iso(t): v for t, w_, var, v in kw.parse_dmi(json.dumps({"type": "FeatureCollection", "features": [
    {"properties": {"step": t, "total-precipitation": a, "temperature-2m": 288.0}} for t, a in real]}).encode()) if var == "precip"}
ok(len(pr) == 8 and pr["2026-09-29T10:00:00Z"] == 0.0 and abs(pr["2026-09-29T13:00:00Z"] - 0.797) < 0.001
   and pr["2026-09-29T16:00:00Z"] == 0.0 and "2026-09-29T17:00:00Z" not in pr,
   "DMI-nedbør: støj under 0,01 mm = 0, rigtig regn beholdes, stort fald droppes")

print("4. verify")
good, probs = kw.verify_chain(con, cfg)
ok(good, "kæden er intakt" + ("" if good else f": {probs[:3]}"))

print("5. score")
res = kw.score(con, cfg)
same = res["temp_same_times"]
ok(bool(same), "fair temperatur-sammenligning har data")
lab = next(iter(same))
maes = {s: v["mae"] for s, v in same[lab].items()}
ok(maes.get("dmi", 9) < maes.get("owm", 0), f"{lab}: DMI (støj 0.7) slår OWM (bias +0.8, støj 1.2): {maes}")
ok("persist" in maes, "baseline 'i morgen = i dag' er med")
ok(abs(same[lab]["owm"]["bias"] - 0.8) < 0.4, f"OWM-bias fundet ({same[lab]['owm']['bias']:+.2f}, sand +0.8)")
rain = res["rain"]
ok(bool(res["rain_same"]) and bool(res["rain_airports"]), "regn: tabel for alle byer og for lufthavne med TAF")
ok(all("taf" not in by for by in res["rain_same"].values()) and all("taf" in by for by in res["rain_airports"].values()),
   "TAF kun i lufthavnstabellen")
ok(all(v["correct"] > 0.95 for by in rain.values() for s, v in by.items() if s != "persist"),
   "regn ja/nej: kilder bygget af sandheden rammer >95 % → vinduer og tidsforskydning passer (også OWM's [dt-3h, dt))")
ok(res["pop"], "regn-% kalibrering har data")

print("6. export")
out = os.path.join(TMP, "export")
kw.export(con, cfg, out)
files = sorted(os.listdir(out))
ok(any(f.startswith("temp_24h_") for f in files) and any(f.startswith("pop_") for f in files), f"filer: {files}")
hdr = open(os.path.join(out, files[0])).readline().strip()
ok(hdr == "timestamp,group,prediction,outcome", "toolkit-header")

print("6b. page")
pg = os.path.join(TMP, "public", "index.html")
kw.page(con, cfg, pg)
html = open(pg, encoding="utf-8").read()
ok("<script" not in html and "Same as yesterday" in html and "CC BY 4.0" in html and "Preliminary" in html, "side: ingen JS, baseline, kreditering, foreløbig-banner")
ok("TESTKEY123" not in html, "ingen nøgle på siden")
import hashlib as _hl
ok(_hl.sha256(open(kw.__file__, "rb").read()).hexdigest() in html, "siden viser SHA-256 af den kørende kode")
if os.environ.get("KVXW_SAMPLE"):
    shutil.copy(pg, os.environ["KVXW_SAMPLE"])

print("7. manipulation opdages")
raw = con.execute("SELECT raw_path FROM runs WHERE source='met' AND raw_path IS NOT NULL LIMIT 1").fetchone()[0]
full = os.path.join(cfg["data_dir"], raw)
data = gzip.open(full).read().replace(b'"air_temperature": 1', b'"air_temperature": 2', 1)
gzip.open(full, "wb").write(data)
good, probs = kw.verify_chain(con, cfg)
ok(not good and any("rå-fil ændret" in p for p in probs), "ændret rå-fil opdages")
gzip.open(full, "wb").write(data.replace(b'"air_temperature": 2', b'"air_temperature": 1', 1))
con.execute("UPDATE runs SET fetched_at='2026-01-01T00:00:00Z' WHERE id=5"); con.commit()
good, probs = kw.verify_chain(con, cfg)
ok(not good and any("run 5" in p for p in probs), "ændret tidsstempel i databasen opdages")

print("7b. migration af en v1.0-database (som den på serveren)")
old_db = os.path.join(TMP, "v10")
os.makedirs(old_db)
c10 = sqlite3.connect(os.path.join(old_db, "weather.sqlite3"))
c10.executescript("""CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, location TEXT NOT NULL,
  fetched_at TEXT NOT NULL, http_status INTEGER, url TEXT, payload_sha256 TEXT, raw_path TEXT, n_rows INTEGER DEFAULT 0,
  error TEXT, prev_hash TEXT NOT NULL, chain_hash TEXT NOT NULL UNIQUE);
CREATE TABLE stations (location TEXT PRIMARY KEY, station_id TEXT, station_name TEXT, lat REAL, lon REAL, distance_km REAL, resolved_at TEXT);
INSERT INTO stations VALUES ('hjoerring','06031','Tylstrup',57.1852,9.9527,31.1,'2026-09-28T05:19:00Z');""")
c10.commit(); c10.close()
c11 = kw.db_connect(dict(cfg, data_dir=old_db))
cols = {r[1] for r in c11.execute("PRAGMA table_info(stations)")} | {r[1] for r in c11.execute("PRAGMA table_info(runs)")}
ok({"precip_station_id", "precip_km", "parse_error"} <= cols, "nye kolonner tilføjet, intet slettet")
ok(kw.stations(c11)["hjoerring"]["precip_id"] == "06031", "gammel stationsrække læses (regn = samme station)")

print("7c. TAF-læseren")
od = kw.DEFAULT_CONFIG["cities"][3]
ok(kw._taf_ok(od, "06120", "06120") and not kw._taf_ok(od, "06126", "06126") and not kw._taf_ok(od, "06120", "05999"),
   "TAF kun når lufthavnens station måler både temperatur og regn (Odense/Årslev → ingen TAF)")
ref = datetime(2026, 9, 28, 5, 20, tzinfo=UTC)
rows = kw.parse_taf(b"TAF EKCH 280500Z 2806/2912 24012KT 9999 SCT030 TEMPO 2809/2813 SHRA BKN014 "
                    b"PROB30 TEMPO 2815/2819 -TSRA BECMG 2820/2822 -RA BKN010 FM290600 27008KT CAVOK=", ref)
pop = {kw.iso(t): v for t, w_, var, v in rows if var == "pop"}
ok(pop == {"2026-09-28T06:00:00Z": 0.6, "2026-09-28T12:00:00Z": 0.6, "2026-09-28T18:00:00Z": 0.9,
           "2026-09-29T00:00:00Z": 0.9, "2026-09-29T06:00:00Z": 0.1}, f"TEMPO/PROB30/BECMG/FM → {pop}")
h = kw.taf_hourly(*kw.parse_taf_text("TAF AMD EKYT 302300Z 0100/0124 20010KT 9999 VCSH BKN025 PROB40 0106/0110 RA",
                                     datetime(2026, 9, 30, 23, 10, tzinfo=UTC))[0][1:])
ok(min(h).month == 10 and sorted(set(h.values())) == [0.1, 0.4], "månedsskifte, AMD, VCSH tæller ikke, PROB40 = 40 %")
ok(kw.parse_taf_text("TAF EKCH 280500Z NIL=", ref) == [], "NIL-TAF springes over")

print("8. kommandolinjen (score --json via CLI)")
env = dict(os.environ, KVX_WEATHER_CONFIG=cfg_path)
p = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "kvx_weather.py"), "score", "--json"],
                   env=env, capture_output=True, text=True)
ok(p.returncode == 0 and '"temp_same_times"' in p.stdout, "CLI score --json" + ("" if p.returncode == 0 else p.stderr[-400:]))

shutil.rmtree(TMP)
print(f"\n{'ALT OK' if not FAILS else f'{FAILS} FEJL'}")
sys.exit(1 if FAILS else 0)

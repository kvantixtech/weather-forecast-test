#!/usr/bin/env python3
"""Offline test of tools/anchor.py: builds a small chain with the collector's own add_run,
anchors it into a throw-away git repository, then tampers with a run and checks that the
anchors catch it.  Run:  python3 tests/test_anchor.py   (no network)"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import kvx_weather as kw  # noqa: E402
import anchor  # noqa: E402

TMP = tempfile.mkdtemp(prefix="kvxa_")
fails = 0


def ok(cond, label):
    global fails
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        fails += 1


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


try:
    cfg = {"data_dir": os.path.join(TMP, "data")}
    con = kw.db_connect(cfg)
    for i in range(5):
        kw.add_run(con, cfg, kind="forecast", source="met", location="aalborg",
                   fetched_at=f"2026-09-2{i}T06:07:00Z", status=200, url="https://example.invalid/",
                   payload=json.dumps({"i": i}).encode())
    con.commit()
    head_expected = con.execute("SELECT chain_hash FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.close()
    db = os.path.join(cfg["data_dir"], "weather.sqlite3")

    # a local "GitHub": bare remote + clone
    remote, repo = os.path.join(TMP, "remote.git"), os.path.join(TMP, "repo")
    sh("git", "init", "--quiet", "--bare", "-b", "main", remote)
    sh("git", "clone", "--quiet", remote, repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "--allow-empty", "-m", "init", cwd=repo)
    sh("git", "push", "--quiet", "origin", "HEAD:main", cwd=repo)

    print("1. snapshot")
    snap = os.path.join(TMP, "head.json")
    ok(anchor.main(["snapshot", "--db", db, "--code", os.path.join(ROOT, "kvx_weather.py"), "--out", snap]) == 0, "snapshot exits 0")
    s = json.load(open(snap))
    ok(s["chain_head"] == head_expected, "head equals the collector's own chain_hash")
    ok(s["runs"] == 5 and s["chain_ok"] == "true", "5 runs, chain_ok=true")
    ok(s["code_sha256"] == anchor.sha256_file(os.path.join(ROOT, "kvx_weather.py")), "code SHA-256 recorded")

    print("2. publish (twice the same day = one line)")
    ok(anchor.main(["publish", "--snapshot", snap, "--repo", repo, "--push"]) == 0, "publish exits 0")
    ok(anchor.main(["publish", "--snapshot", snap, "--repo", repo, "--push"]) == 0, "second publish is a no-op")
    csv_path = os.path.join(repo, anchor.CSV_REL)
    lines = open(csv_path).read().splitlines()
    ok(lines[0] == ",".join(anchor.COLUMNS) and len(lines) == 2, "header + exactly one anchor line")
    log = subprocess.run(["git", "--git-dir", remote, "log", "--oneline", "main"], text=True, capture_output=True).stdout
    ok("anchor " in log and head_expected[:12] in log, "commit reached the remote")

    ok(anchor.main(["lint", "--anchors", csv_path]) == 0, "lint accepts the anchors file")
    bad_csv = os.path.join(TMP, "bad.csv")
    open(bad_csv, "w").write(open(csv_path).read().replace(s["code_sha256"], "f" * 64))
    ok(anchor.main(["lint", "--anchors", bad_csv]) == 1, "lint rejects a code hash that is not in METHOD.lock")

    print("3. check against an untouched copy")
    ok(anchor.main(["check", "--db", db, "--anchors", csv_path]) == 0, "all anchors match")

    print("4. tamper with an old run")
    raw = sqlite3.connect(db)
    raw.execute("UPDATE runs SET http_status=500 WHERE id=2")
    raw.commit()
    raw.close()
    ok(anchor.main(["check", "--db", db, "--anchors", csv_path]) == 1, "check reports MISMATCH")
    anchor.main(["snapshot", "--db", db, "--code", "", "--out", snap])
    ok(json.load(open(snap))["chain_ok"] == "false", "next snapshot says chain_ok=false")

    print("5. a shorter chain is refused")
    s = json.load(open(snap))
    s.update(date_utc="2099-01-01", runs=3)
    json.dump(s, open(snap, "w"))
    try:
        anchor.main(["publish", "--snapshot", snap, "--repo", repo])
        ok(False, "publish refuses a chain shorter than the last anchor")
    except SystemExit as e:
        ok("shorter" in str(e), "publish refuses a chain shorter than the last anchor")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)

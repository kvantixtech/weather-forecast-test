#!/usr/bin/env python3
"""Daily public anchor of the weather-test hash chain.

Once a day the head of the chain (the hash of the newest download) is appended to
anchors/chain-heads.csv in this public repository and pushed to GitHub. After that,
nobody, including Kvantix, can rewrite an earlier download without the recomputed
chain disagreeing with a head that is already public.

The script never changes the collector or its data. It has three commands:

  snapshot  read the chain head from the database (read-only) and write it to a JSON file.
            Run as the collector's own user, so SQLite's WAL side files keep the right owner.
  publish   append the snapshot to anchors/chain-heads.csv in a clone of this repo,
            commit and push. Idempotent: one line per UTC day.
  check     for third parties: recompute the whole chain from a copy of the database and
            confirm that every published anchor lies on it.
  lint      format check of anchors/chain-heads.csv and METHOD.lock (runs in CI): hex hashes,
            one line per day in order, a chain that only grows, and a code hash that was
            published in METHOD.lock.

Standard library only. Python 3.9+.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

GENESIS = "0" * 64
CHAIN_FIELDS = ("kind", "source", "location", "fetched_at", "http_status", "url", "payload_sha256", "n_rows", "error")
COLUMNS = ["date_utc", "anchored_at_utc", "runs", "last_run_id", "last_fetched_at",
           "chain_head", "chain_ok", "code_sha256"]
CSV_REL = os.path.join("anchors", "chain-heads.csv")


# Same definition as kvx_weather.py (chain_record / chain_hash). Kept separate on purpose,
# so the check does not depend on the collector's code.
def chain_hash(prev: str, fields: dict) -> str:
    rec = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + "|" + rec).encode()).hexdigest()


def open_ro(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        sys.exit(f"database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def walk_chain(con):
    """Yields (run_id, chain_hash, ok_so_far) for every run, recomputed from genesis."""
    prev, ok = GENESIS, True
    q = f"SELECT id, prev_hash, chain_hash, {', '.join(CHAIN_FIELDS)} FROM runs ORDER BY id"
    for row in con.execute(q):
        rid, p, h = row[0], row[1], row[2]
        fields = dict(zip(CHAIN_FIELDS, row[3:]))
        fields["n_rows"] = 0  # n_rows is set after parsing and is not part of the chain
        if p != prev or chain_hash(p, fields) != h:
            ok = False
        prev = h
        yield rid, h, ok


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def cmd_snapshot(a) -> int:
    con = open_ro(a.db)
    runs, last_id, head, ok = 0, None, GENESIS, True
    for rid, h, ok_so_far in walk_chain(con):
        runs, last_id, head, ok = runs + 1, rid, h, ok_so_far
    last_fetched = None
    if last_id is not None:
        last_fetched = con.execute("SELECT fetched_at FROM runs WHERE id=?", (last_id,)).fetchone()[0]
    con.close()
    t = now_utc()
    snap = {
        "date_utc": t.strftime("%Y-%m-%d"),
        "anchored_at_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": runs,
        "last_run_id": last_id,
        "last_fetched_at": last_fetched,
        "chain_head": head,
        "chain_ok": "true" if ok else "false",
        "code_sha256": sha256_file(a.code) if a.code else "",
    }
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=1)
    os.replace(tmp, a.out)
    print(f"snapshot: {runs} runs, head {head[:16]}…, chain_ok={snap['chain_ok']}")
    return 0  # a broken chain is still anchored (and reported); publish then exits 1


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cmd_publish(a) -> int:
    with open(a.snapshot, encoding="utf-8") as fh:
        snap = json.load(fh)
    missing = [c for c in COLUMNS if c not in snap]
    if missing:
        sys.exit(f"snapshot is missing {missing}")
    if a.push:
        r = git(a.repo, "pull", "--rebase", "--quiet", check=False)
        if r.returncode:
            sys.exit("git pull failed:\n" + r.stdout)
    path = os.path.join(a.repo, CSV_REL)
    rows = read_rows(path)
    if any(r["date_utc"] == snap["date_utc"] for r in rows):
        print(f"already anchored for {snap['date_utc']}; nothing to do")
        return 0
    if rows:
        last = rows[-1]
        if int(snap["runs"]) < int(last["runs"]):
            sys.exit(f"refusing: chain is shorter than the last anchor ({snap['runs']} < {last['runs']})")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    if new_file:
        w.writeheader()
    w.writerow({c: ("" if snap[c] is None else snap[c]) for c in COLUMNS})
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    msg = (f"anchor {snap['date_utc']}: chain head {snap['chain_head'][:12]} "
           f"(run {snap['last_run_id']}, {snap['runs']} runs, chain_ok={snap['chain_ok']})")
    git(a.repo, "add", CSV_REL)
    git(a.repo, "-c", f"user.name={a.author_name}", "-c", f"user.email={a.author_email}",
        "commit", "--quiet", "-m", msg)
    print(msg)
    if a.push:
        r = git(a.repo, "push", "--quiet", check=False)
        if r.returncode:
            sys.exit("git push failed:\n" + r.stdout)
        print("pushed")
    return 0 if snap["chain_ok"] == "true" else 1


def cmd_check(a) -> int:
    rows = read_rows(a.anchors)
    if not rows:
        sys.exit(f"no anchors in {a.anchors}")
    con = open_ro(a.db)
    heads, broken_at = {}, None
    for rid, h, ok in walk_chain(con):
        heads[rid] = (h, ok)
        if not ok and broken_at is None:
            broken_at = rid
    con.close()
    bad = 0
    for r in rows:
        rid = int(r["last_run_id"]) if r["last_run_id"] else None
        if rid is None:
            continue
        if rid not in heads:
            print(f"  ?    {r['date_utc']}: run {rid} not in this database (copy is older than the anchor)")
            continue
        got, chain_ok = heads[rid]
        if got != r["chain_head"]:
            bad += 1
            print(f"  FAIL {r['date_utc']}: run {rid} anchored {r['chain_head'][:16]}…, stored {got[:16]}…")
        elif not chain_ok:
            bad += 1
            print(f"  FAIL {r['date_utc']}: run {rid} head matches, but the runs before it no longer recompute to it")
        else:
            print(f"  OK   {r['date_utc']}: run {rid} {got[:16]}…")
    if broken_at is not None:
        print(f"chain does not recompute from run {broken_at} onwards")
    print("ALL ANCHORS MATCH" if not bad and broken_at is None else "MISMATCH")
    return 0 if not bad and broken_at is None else 1


def read_method_lock(path):
    """METHOD.lock: one line per published version: '<version> <sha256 of kvx_weather.py> <date>'."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                ver, sha = line.split()[:2]
                out[sha] = ver
    return out


def is_hex64(x) -> bool:
    return len(x) == 64 and all(c in "0123456789abcdef" for c in x)


def cmd_lint(a) -> int:
    errors = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lock = read_method_lock(a.method_lock or os.path.join(root, "METHOD.lock"))
    code = os.path.join(root, "kvx_weather.py")
    code_sha = sha256_file(code)
    ver = None
    with open(code, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VERSION = "):
                ver = line.split("=", 1)[1].strip().strip('"\'')
                break
    if lock.get(code_sha) != ver:
        errors.append(f"kvx_weather.py (VERSION {ver}, sha256 {code_sha}) is not recorded in METHOD.lock. "
                      "A change to the collector needs a new VERSION and a new METHOD.lock line.")
    path = a.anchors
    with open(path, encoding="utf-8") as fh:
        header = fh.readline().strip()
    if header != ",".join(COLUMNS):
        errors.append(f"header must be: {','.join(COLUMNS)}")
    prev = None
    for n, r in enumerate(read_rows(path), start=2):
        where = f"line {n} ({r.get('date_utc')})"
        try:
            datetime.strptime(r["date_utc"], "%Y-%m-%d")
            datetime.strptime(r["anchored_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
            runs, rid = int(r["runs"]), int(r["last_run_id"])
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"{where}: {e}")
            continue
        if not r["anchored_at_utc"].startswith(r["date_utc"]):
            errors.append(f"{where}: anchored_at_utc is not on date_utc")
        if not is_hex64(r["chain_head"]):
            errors.append(f"{where}: chain_head is not 64 lowercase hex")
        if r["chain_ok"] not in ("true", "false"):
            errors.append(f"{where}: chain_ok must be true or false")
        if r["code_sha256"] and r["code_sha256"] not in lock:
            errors.append(f"{where}: code_sha256 {r['code_sha256'][:16]}… is not a published version in METHOD.lock")
        if prev:
            if r["date_utc"] <= prev["date_utc"]:
                errors.append(f"{where}: dates must increase, one line per day")
            if runs < int(prev["runs"]) or rid < int(prev["last_run_id"]):
                errors.append(f"{where}: the chain may only grow")
            if rid == int(prev["last_run_id"]) and r["chain_head"] != prev["chain_head"]:
                errors.append(f"{where}: same last run as the day before, but a different head")
        prev = r
    for e in errors:
        print("  FAIL " + e)
    if not errors:
        print(f"  OK   METHOD.lock: kvx_weather.py = version {ver}")
        rel = os.path.relpath(path)
        print(f"  OK   {rel}: {n - 1} anchor(s), well-formed" if prev else f"  OK   {rel}: no anchors yet")
    return 1 if errors else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="read the chain head (run as the collector's user)")
    s.add_argument("--db", default="/var/lib/kvx-weather/weather.sqlite3")
    s.add_argument("--code", default="/opt/kvantix/weather/kvx_weather.py",
                   help="collector file whose SHA-256 is recorded next to the head")
    s.add_argument("--out", required=True)
    s = sub.add_parser("publish", help="append the snapshot to anchors/chain-heads.csv, commit, push")
    s.add_argument("--snapshot", required=True)
    s.add_argument("--repo", required=True, help="clone of this repository")
    s.add_argument("--push", action="store_true")
    s.add_argument("--author-name", default="Kvantix anchor")
    s.add_argument("--author-email", default="validation@kvantix.tech")
    s = sub.add_parser("check", help="confirm every published anchor against a copy of the database")
    s.add_argument("--db", required=True)
    s.add_argument("--anchors", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CSV_REL))
    s = sub.add_parser("lint", help="format check of the anchors file and METHOD.lock (CI)")
    s.add_argument("--anchors", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CSV_REL))
    s.add_argument("--method-lock", default=None)
    a = p.parse_args(argv)
    return {"snapshot": cmd_snapshot, "publish": cmd_publish, "check": cmd_check, "lint": cmd_lint}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

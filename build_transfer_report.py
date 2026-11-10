#!/usr/bin/env python3
"""Build an interactive per-JD report of Karoo -> NRAO transfers from two Librarian databases.

run_daily.py runs this every day; use this script directly only to rebuild a page by hand.

Inputs are SQLite copies of the two Librarian databases (made by librarian_sqlite.py):
  --karoo  Karoo Librarian (the source)
  --nrao   NRAO Librarian (the destination)

For each night (JD) and each file kind below, it counts the Karoo files and how
many are at NRAO. A file counts as at NRAO when the NRAO Librarian has a record
with the same name and md5 and at least one copy on an NRAO disk. It also
reports which disks hold each night at both sites and, for every Karoo disk, how
much of what was on it (at the time of the Karoo backup) is at NRAO.

Outputs: one self-contained HTML page and a CSV of every file that is not
(correctly) at NRAO.

Example, with SQLite files that run_daily.py left in its work directory (the
Karoo calls its NRAO connection "aoc-uploads"):
  python build_transfer_report.py --karoo WORK/karoo/maint.20260904.karoo.librarian.dbbackup.sqlite
    --nrao WORK/nrao.sqlite --work WORK/report_work.sqlite --connection aoc-uploads
    --min-jd 2459847 --night-set analysis_nights.json
    --html karoo_nrao_transfer_report.html --csv karoo_nrao_transfer_problems.csv
"""

import argparse
import csv
import datetime
import json
import os
import re
import sqlite3
import sys
import time

from librarian_sqlite import sqlite_uri

KINDS = [
    dict(key="sum", label=".sum.uvh5", regex=r"^zen\.(\d{7})\.\d+\.sum\.uvh5$",
         like="%.sum.uvh5", desc="zen.<JD>.<frac>.sum.uvh5"),
    dict(key="diff", label=".diff.uvh5", regex=r"^zen\.(\d{7})\.\d+\.diff\.uvh5$",
         like="%.diff.uvh5", desc="zen.<JD>.<frac>.diff.uvh5"),
    dict(key="ant", label="ant_class.csv.tar.gz", regex=r"^zen\.(\d{7})\.\d+\.sum\.ant_class\.csv\.tar\.gz$",
         like="%.ant_class.csv.tar.gz", desc="zen.<JD>.<frac>.sum.ant_class.csv.tar.gz (zipped antenna classification CSVs)"),
]
_RX = [re.compile(k["regex"]) for k in KINDS]
PROBLEM_LABELS = {1: "not at NRAO", 2: "at NRAO, md5 differs", 3: "NRAO record, no copy on disk"}
MAX_PROBLEMS_PER_JD = 300
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")


def tkind(name):
    for i, rx in enumerate(_RX):
        if rx.match(name):
            return i
    return None


def tjd(name):
    try:
        return int(name[4:11])
    except ValueError:  # not a zen.<JD>. name
        return None


# What NRAO received since data taking ended, by category. 0-2 are the report's kinds (KINDS order).
INCOMING_CATS = ["sum", "diff", "ant", "report kinds, earlier nights", "autos", "other zen.* products",
                 "database backups (maint.*)", "other files", "extra copies"]


def incoming_category(name, min_jd):
    """Category of a file's first copy at NRAO, and its night (0 if it has none)."""
    jd = (tjd(name) or 0) if name.startswith("zen.") else 0
    k = tkind(name)
    if k is not None:
        return (k if jd >= min_jd else 3), jd
    if name.startswith("zen."):
        return (4 if "autos" in name else 5), jd
    return (6 if name.startswith("maint.") else 7), 0


def log(msg, t0=[time.time()]):
    print(f"[{time.time() - t0[0]:6.0f} s] {msg}", flush=True)


def jd_to_datetime(jd):
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(days=jd - 2440587.5)


def jd_to_date(jd):
    return jd_to_datetime(jd).strftime("%Y-%m-%d")


def build(con, karoo_nrao_conns, min_jd=0):
    q = lambda sql, *a: con.execute(sql, *a).fetchall()
    con.create_function("tkind", 1, tkind, deterministic=True)
    con.create_function("tjd", 1, tjd, deterministic=True)
    like = " OR ".join(f"name LIKE '{k['like']}'" for k in KINDS)

    for db in ("k", "n"):
        log(f"selecting target files from {db}.file")
        con.execute(f"DROP TABLE IF EXISTS {db}t")
        con.execute(f"""CREATE TABLE {db}t AS SELECT name, tkind(name) AS kind, tjd(name) AS jd, size, md5
                        FROM {db}.file WHERE name LIKE 'zen.%' AND ({like}) AND tkind(name) IS NOT NULL
                        AND tjd(name) >= ?""", (min_jd,))
        con.execute(f"CREATE UNIQUE INDEX {db}t_name ON {db}t(name)")
        log(f"  {q(f'SELECT count(*) FROM {db}t')[0][0]:,} files")
        log(f"selecting their instances from {db}.file_instance")
        con.execute(f"DROP TABLE IF EXISTS {db}i")
        con.execute(f"CREATE TABLE {db}i AS SELECT name, store FROM {db}.file_instance WHERE name IN (SELECT name FROM {db}t)")
        con.execute(f"CREATE INDEX {db}i_name ON {db}i(name)")
        log(f"  {q(f'SELECT count(*) FROM {db}i')[0][0]:,} instances")

    # Which Karoo-side connection names mean NRAO, and which files the Karoo logged as copied there.
    log("finding Karoo copy destinations")
    conns = q("""SELECT json_extract(payload, '$.connection_name') AS c, count(*) FROM k.file_event
                 WHERE type = 'copy_finished' AND name IN (SELECT name FROM kt) GROUP BY c ORDER BY 2 DESC""")
    log("  " + ", ".join(f"{c} ({n:,})" for c, n in conns))
    chosen = karoo_nrao_conns or [c for c, _ in conns if c and "nrao" in c.lower()]
    log(f"  treating as NRAO: {chosen}")
    orders = q("SELECT name FROM k.standing_order WHERE conn_name IN (%s)" % ",".join("?" * len(chosen)), chosen) if chosen else []
    con.execute("DROP TABLE IF EXISTS kev")
    con.execute("CREATE TABLE kev (name TEXT PRIMARY KEY)")
    if chosen:
        con.execute(f"""INSERT OR IGNORE INTO kev SELECT name FROM k.file_event
                        WHERE type = 'copy_finished' AND json_extract(payload, '$.error_code') = 0
                        AND json_extract(payload, '$.connection_name') IN ({",".join("?" * len(chosen))})
                        AND name IN (SELECT name FROM kt)""", chosen)
    for (o,) in orders:
        con.execute("INSERT OR IGNORE INTO kev SELECT name FROM k.file_event WHERE type = ? AND name IN (SELECT name FROM kt)",
                    ("standing_order_succeeded:" + o,))
    log(f"  {q('SELECT count(*) FROM kev')[0][0]:,} target files logged as copied to NRAO")

    # When each Karoo file first got a copy on an NRAO disk (the NRAO Librarian's first create_instance event; its
    # event times are UTC), and when data taking ended (the start time of the last data file, from its name).
    log("finding when files arrived at NRAO")
    con.execute("DROP TABLE IF EXISTS nfirst")
    con.execute("""CREATE TABLE nfirst AS SELECT e.name, min(e.time) AS t FROM n.file_event e JOIN kt ON kt.name = e.name
                   WHERE e.type = 'create_instance' GROUP BY e.name""")
    con.execute("CREATE UNIQUE INDEX nfirst_name ON nfirst(name)")
    last_jd = q("SELECT max(jd) FROM kt")[0][0]
    name_jd = lambda nm: float(".".join(nm.split(".")[1:3]))
    last_file = max((r[0] for r in q("SELECT name FROM kt WHERE jd = ?", (last_jd,))), key=name_jd)
    t0 = jd_to_datetime(name_jd(last_file))
    t0s = t0.strftime("%Y-%m-%d %H:%M:%S")
    day0 = t0.date()
    arrivals = {}  # (day since day0, jd, kind) -> [files, bytes] for files that reached NRAO after t0

    # Stores.
    def stores(db):
        rows = q(f"SELECT id, name, ssh_host, path_prefix, available FROM {db}.store ORDER BY name")
        rows.sort(key=lambda r: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", r[1])])
        idx = {r[0]: i for i, r in enumerate(rows)}
        out = [dict(name=r[1], host=r[2], prefix=r[3], available=str(r[4]).lower() not in ("f", "false", "0")) for r in rows]
        return out, idx
    kstores, kidx = stores("k")
    nstores, nidx = stores("n")

    nk = len(KINDS)
    jds = {}

    def jd_entry(jd):
        e = jds.get(jd)
        if e is None:
            e = jds[jd] = dict(k=[[0] * 8 for _ in range(nk)], kd={}, nd={}, no=[0] * nk)
        return e

    kdisk = [dict(any=False, jds={}) for _ in kstores]
    ndisk = [dict(any=False, jds={}) for _ in nstores]
    problems, problems_total = {}, {}
    problem_rows = []

    log("classifying every Karoo file")
    rows = con.execute("""
        SELECT kt.name, kt.kind, kt.jd, kt.size,
          CASE WHEN nt.name IS NULL THEN 1 WHEN nt.md5 != kt.md5 THEN 2
               WHEN NOT EXISTS (SELECT 1 FROM ni WHERE ni.name = kt.name) THEN 3 ELSE 0 END,
          (SELECT group_concat(store) FROM ki WHERE ki.name = kt.name),
          EXISTS (SELECT 1 FROM kev WHERE kev.name = kt.name), nfirst.t
        FROM kt LEFT JOIN nt ON nt.name = kt.name LEFT JOIN nfirst ON nfirst.name = kt.name""")
    n = 0
    for name, kind, jd, size, code, kst, logged, arrived in rows:
        n += 1
        size = size or 0
        if code == 0 and arrived and arrived > t0s:
            day = (datetime.date.fromisoformat(arrived[:10]) - day0).days
            a = arrivals.setdefault((day, jd, kind), [0, 0])
            a[0] += 1
            a[1] += size
        e = jd_entry(jd)
        k = e["k"][kind]
        ok = code == 0
        k[0] += 1
        k[5] += size
        if ok:
            k[1] += 1
        else:
            k[code + 1] += 1  # 1 -> missing (idx 2), 2 -> mismatch (idx 3), 3 -> noinst (idx 4)
            k[6] += size
        sidx = sorted({kidx[int(s)] for s in kst.split(",")}) if kst else []
        if not ok and not sidx:
            k[7] += 1
        for s in sidx:
            e["kd"].setdefault(s, [0] * nk)[kind] += 1
            d = kdisk[s]
            d["any"] = True
            # Same layout as the per-night arrays, but only for this disk's files (lost is always 0 here).
            per = d["jds"].setdefault(jd, [[0] * 8 for _ in range(nk)])[kind]
            per[0] += 1
            per[5] += size
            if ok:
                per[1] += 1
            else:
                per[code + 1] += 1
                per[6] += size
        if not ok:
            problems_total[jd] = problems_total.get(jd, 0) + 1
            lst = problems.setdefault(jd, [])
            if len(lst) < MAX_PROBLEMS_PER_JD:
                lst.append([name, kind, size, code, sidx, int(logged)])
            problem_rows.append((name, jd, KINDS[kind]["label"], size, PROBLEM_LABELS[code],
                                 " ".join(kstores[s]["name"] for s in sidx), "yes" if logged else "no"))
    log(f"  {n:,} Karoo files classified; {len(problem_rows):,} not (correctly) at NRAO")

    log("NRAO disks and NRAO-only files")
    for kind, jd, size, st in con.execute("SELECT nt.kind, nt.jd, nt.size, ni.store FROM nt JOIN ni ON ni.name = nt.name"):
        e = jd_entry(jd)
        s = nidx[int(st)]
        e["nd"].setdefault(s, [0] * nk)[kind] += 1
        d = ndisk[s]
        d["any"] = True
        per = d["jds"].setdefault(jd, [[0, 0] for _ in range(nk)])[kind]
        per[0] += 1
        per[1] += size or 0
    for kind, jd, cnt in con.execute("""SELECT nt.kind, nt.jd, count(*) FROM nt LEFT JOIN kt ON kt.name = nt.name
                                        WHERE kt.name IS NULL GROUP BY 1, 2"""):
        jd_entry(jd)["no"][kind] = cnt

    # A DB loaded with only some events keeps the latest time of every event type in file_event_summary.
    def asof(db):
        if q(f"SELECT 1 FROM {db}.sqlite_master WHERE name = 'file_event_summary'"):
            return q(f"SELECT max(max_time) FROM {db}.file_event_summary")[0][0]
        return q(f"SELECT max(time) FROM {db}.file_event")[0][0]
    karoo_asof, nrao_asof = asof("k"), asof("n")
    excluded = q(f"""SELECT count(*) FROM n.file WHERE name LIKE 'zen.%' AND ({like}) AND tkind(name) IS NULL
                     AND CAST(substr(name, 5, 7) AS INTEGER) >= ?""", (min_jd,))[0][0]

    out_jds = [dict(jd=jd, date=jd_to_date(jd), k=e["k"], no=e["no"],
                    kd={str(s): v for s, v in e["kd"].items()}, nd={str(s): v for s, v in e["nd"].items()})
               for jd, e in sorted(jds.items())]
    for d in kdisk + ndisk:
        d["jds"] = {str(jd): v for jd, v in d["jds"].items()}
    end_day = (datetime.date.fromisoformat(nrao_asof[:10]) - day0).days if nrao_asof else max((k[0] for k in arrivals), default=0)
    log(f"  data taking ended {t0s} UTC ({last_file}); {sum(a[0] for a in arrivals.values()):,} files reached NRAO since")
    arrival_data = dict(t0=t0.strftime("%Y-%m-%d %H:%M") + " UTC", t0_file=last_file, day0=day0.isoformat(), days=end_day,
                        rows=[[d, jd, kind, a[0], a[1]] for (d, jd, kind), a in sorted(arrivals.items())])

    # Everything NRAO recorded a new copy of since data taking ended, not just the report's files: a file's first
    # copy is a new arrival (by category); any later copy of a file NRAO already had is an extra copy.
    log("tallying everything NRAO received since data taking ended")
    for sql in ("DROP TABLE IF EXISTS nev", "DROP TABLE IF EXISTS nevf", "DROP TABLE IF EXISTS nevs"):
        con.execute(sql)
    con.execute("CREATE TABLE nev AS SELECT name, time FROM n.file_event WHERE type = 'create_instance' AND time > ?", (t0s,))
    con.execute("CREATE INDEX nev_name ON nev(name)")
    con.execute("""CREATE TABLE nevf AS SELECT name, min(time) AS t FROM n.file_event
                   WHERE type = 'create_instance' AND name IN (SELECT name FROM nev) GROUP BY name""")
    con.execute("CREATE UNIQUE INDEX nevf_name ON nevf(name)")
    con.execute("CREATE TABLE nevs AS SELECT name, max(size) AS size FROM n.file WHERE name IN (SELECT name FROM nevf) GROUP BY name")
    con.execute("CREATE UNIQUE INDEX nevs_name ON nevs(name)")
    incoming = {}
    for name, t, first, size in con.execute("SELECT nev.name, nev.time, nevf.t, nevs.size FROM nev JOIN nevf USING (name) "
                                            "LEFT JOIN nevs USING (name)"):
        cat, jd = incoming_category(name, min_jd)
        if t != first:
            cat = 8
        a = incoming.setdefault(((datetime.date.fromisoformat(t[:10]) - day0).days, cat, jd), [0, 0])
        a[0] += 1
        a[1] += size or 0
    log(f"  {sum(a[0] for a in incoming.values()):,} new copies, {len(incoming):,} (day, category, night) groups")
    incoming_data = dict(cats=INCOMING_CATS, rows=[[d, c, jd, a[0], a[1]] for (d, c, jd), a in sorted(incoming.items())])
    return dict(
        arrivals=arrival_data, incoming=incoming_data,
        meta=dict(karoo_asof=(karoo_asof or "?")[:16] + " UTC", nrao_asof=(nrao_asof or "?")[:16] + " UTC",
                  generated=datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                  kinds=[dict(key=k["key"], label=k["label"], desc=k["desc"]) for k in KINDS],
                  nrao_connections=chosen, problems_csv="",
                  notes=[]),
        kstores=kstores, nstores=nstores, jds=out_jds, kdisk=kdisk, ndisk=ndisk,
        problems={str(k): v for k, v in problems.items()}, problems_total={str(k): v for k, v in problems_total.items()},
        _excluded=excluded, _problem_rows=problem_rows,
    )


def read_night_set(path):
    with open(path) as fh:
        jds = sorted({int(x) for x in re.findall(r"\d{7}", fh.read())})
    if not jds:
        sys.exit(f"no 7-digit JDs found in {path}")
    return jds


def write_report(karoo, nrao, work, html, csv_path, template=DEFAULT_TEMPLATE, connections=(), min_jd=0,
                 night_set=None, night_set_label="Worth analyzing", extra_notes=()):
    """Build the report from SQLite copies of the two Librarians; write the HTML page and the CSV of problem files.

    night_set: path of a file listing JDs the page can filter to (or None). extra_notes go first in the page's notes.
    work is a scratch SQLite file; it is overwritten and left for the caller to delete.
    """
    night_jds = read_night_set(night_set) if night_set else None

    if os.path.exists(work):
        os.remove(work)
    con = sqlite3.connect(sqlite_uri(work), uri=True)  # URI mode so the read-only ATTACHes work
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("ATTACH DATABASE ? AS k", (sqlite_uri(karoo, readonly=True),))
    con.execute("ATTACH DATABASE ? AS n", (sqlite_uri(nrao, readonly=True),))
    try:
        data = build(con, list(connections), min_jd)
    finally:
        con.close()
    excluded = data.pop("_excluded")
    problem_rows = data.pop("_problem_rows")
    data["meta"]["problems_csv"] = os.path.basename(csv_path)
    notes = data["meta"]["notes"] = list(extra_notes) + [
        "A file counts as at NRAO when the NRAO Librarian has a record with the same name and md5 and at least one copy on an "
        "NRAO disk. md5s are the ones the two Librarians recorded at ingest; files were not re-checksummed.",
        "Karoo disk contents are as of the Karoo backup (" + data["meta"]["karoo_asof"] + "). A file deleted from the Karoo "
        "after it was copied shows up on no Karoo disk.",
        "Only names of the form " + "; ".join(k["desc"] for k in KINDS) + " are counted. Similar names such as "
        "*.HH.diff.uvh5 and *.autos.sum.uvh5 would be left out" +
        (f" ({excluded:,} such files at NRAO)." if excluded else "; NRAO has none from these nights."),
        "Only Karoo-to-NRAO transfers are considered. 'At NRAO, no Karoo record' marks nights NRAO has files for that the "
        "Karoo backup has no record of.",
    ]
    at = len(extra_notes)
    if night_jds:
        have = {j["jd"] for j in data["jds"]}
        absent = [jd for jd in night_jds if jd not in have]
        data["meta"]["night_set"] = dict(label=night_set_label, jds=night_jds)
        notes.insert(at, f"'{night_set_label}' is a list of {len(night_jds):,} nights "
                         f"({os.path.basename(night_set)}). The switch at the start of the filters "
                         "limits the whole page to them, and the CSV's worth_analyzing column marks them." +
                         (f" {len(absent):,} of them have no files in this report: "
                          + ", ".join(map(str, absent[:20])) + (" ..." if len(absent) > 20 else "") if absent else ""))
    if min_jd:
        notes.insert(at, f"Only nights from JD {min_jd} ({jd_to_date(min_jd)}) onward are included; "
                         "disk totals count only files from those nights.")

    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        header = ["name", "jd", "kind", "size_bytes", "problem", "karoo_disks_at_backup", "karoo_logged_copy_to_nrao"]
        rows = sorted(problem_rows, key=lambda r: (r[1], r[0]))
        if night_jds:
            listed = set(night_jds)
            header.append("worth_analyzing")
            rows = [tuple(r) + ("yes" if r[1] in listed else "no",) for r in rows]
        w.writerow(header)
        w.writerows(rows)

    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    with open(template) as fh:
        page = fh.read()
    if "/*__DATA__*/" not in page:
        sys.exit("template has no /*__DATA__*/ placeholder")
    with open(html, "w") as fh:
        fh.write(page.replace("/*__DATA__*/", payload))
    log(f"wrote {html} ({os.path.getsize(html) / 1e6:.1f} MB) and {csv_path} ({len(problem_rows):,} rows)")
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--karoo", required=True, help="SQLite copy of the Karoo Librarian DB")
    p.add_argument("--nrao", required=True, help="SQLite copy of the NRAO Librarian DB")
    p.add_argument("--work", required=True, help="SQLite file for intermediate tables (created/overwritten)")
    p.add_argument("--template", default=DEFAULT_TEMPLATE)
    p.add_argument("--html", required=True, help="output HTML path")
    p.add_argument("--csv", required=True, help="output CSV of files not (correctly) at NRAO")
    p.add_argument("--connection", action="append", default=[], help="Karoo connection name(s) that mean NRAO")
    p.add_argument("--min-jd", type=int, default=0, help="leave out nights before this JD")
    p.add_argument("--night-set", help="file listing nights (JDs, as a JSON list or separated by commas/whitespace) "
                                       "that the page can filter to")
    p.add_argument("--night-set-label", default="Worth analyzing", help="name of that list on the page (default: %(default)s)")
    args = p.parse_args()
    write_report(args.karoo, args.nrao, args.work, args.html, args.csv, args.template, args.connection, args.min_jd,
                 args.night_set, args.night_set_label)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Daily Karoo -> NRAO transfer report for the HERA Librarian, meant for cron on herastore01.

Each run:
  1. Backs up the NRAO Librarian database with pg_dump to output/nrao-librarian.pgdump
     (about 2 minutes), replacing the previous day's backup once the new one is
     complete, so backups don't pile up.
  2. Loads from that backup the tables the report needs (files, copies, disks, and
     the events that record each copy's arrival) into WORKDIR/nrao.sqlite.
  3. Uses the Karoo Librarian backup kept in output/
     (maint.YYYYMMDD.karoo.librarian.dbbackup.pgdump). The first run copies there
     the newest one the NRAO Librarian holds; later runs copy again only if NRAO
     lists a newer one (data taking is over). Loads it into WORKDIR/karoo/, which
     takes ~10 minutes and ~12 GB, so only when the backup changes.
  4. Builds the report, an HTML page and a CSV of every file not at NRAO, and
     puts both in output/.
  5. Copies both into the web directory.
Every file is replaced only once its new version is complete, so a failed run
leaves the previous backup and page in place (and exits non-zero).

output/ and work/ (the default WORKDIR) are inside this repository and ignored
by git. WORKDIR needs ~20 GB and output/ ~5 GB; --workdir and --output-dir move them. Run as a
user that can read the database without a password (heramgr on herastore01).

Example crontab line (06:15 every day):
  15 6 * * * MINIFORGE/envs/librarian_transfer_monitor/bin/python REPO/run_daily.py >> REPO/output/run_daily.log 2>&1

Testing from backup files instead of the live database:
  python run_daily.py --nrao-dump nrao.pgdump --karoo-dump maint.20260904.karoo.librarian.dbbackup.pgdump --no-publish
"""

import argparse
import datetime
import fcntl
import glob
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # cron may run us with -I, which leaves the script's directory off sys.path

import build_transfer_report as report  # noqa: E402
import librarian_sqlite as lsql  # noqa: E402

DEFAULT_PUBLISH_DIR = "/lustre/aoc/projects/hera/www/librarian"
KAROO_BACKUP_PATTERN = "maint.%.karoo.librarian.dbbackup.pgdump"
NRAO_BACKUP_NAME = "nrao-librarian.pgdump"
HTML_NAME = "karoo_nrao_transfer_report.html"
CSV_NAME = "karoo_nrao_transfer_problems.csv"


def say(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


def lock_workdir(workdir):
    fh = open(os.path.join(workdir, ".lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another run_daily.py is still using {workdir}; not starting a second one")
    except OSError as e:  # e.g. a filesystem mounted without lock support
        say(f"warning: cannot lock {fh.name} ({e}); make sure runs don't overlap")
    return fh  # keep it open for the life of the process


def backup_nrao(args, path):
    """pg_dump the whole NRAO Librarian database to path, replacing the previous backup once the new one is complete."""
    pg_dump = lsql.find_tool("pg_dump", args.pg_dump, prefer_system=True)
    say(f"backing up the NRAO Librarian database '{args.nrao_db}' to {path} with {pg_dump}")
    tmp = path + ".partial"
    t0 = time.time()
    res = subprocess.run([pg_dump, "-F", "custom", "-f", tmp, "-d", args.nrao_db], capture_output=True, text=True)
    if res.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        sys.exit(f"pg_dump failed (exit {res.returncode}); the previous backup is untouched:\n{res.stderr.strip()}")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    say(f"  {os.path.getsize(path) / 1e6:,.0f} MB in {time.time() - t0:.0f} s")


def load_nrao(args, dump, db_path):
    pg_restore = lsql.find_tool("pg_restore", args.pg_restore)
    say(f"loading the NRAO Librarian from {dump} with {pg_restore}")
    tables = lsql.NRAO_TABLES
    lsql.load_into_sqlite(lsql.dump_command(pg_restore, dump, tables), db_path, tables,
                          event_keep=lsql.nrao_events, event_payload=lsql.no_payload, info=dict(path=dump))


def karoo_backups(nrao_db):
    """[(name, [paths of its copies])] for the Karoo Librarian backups NRAO holds, newest first."""
    con = sqlite3.connect(lsql.sqlite_uri(nrao_db, readonly=True), uri=True)
    rows = con.execute("""SELECT fi.name, s.path_prefix, fi.parent_dirs FROM file_instance fi JOIN store s ON s.id = fi.store
                          WHERE fi.name LIKE ? ORDER BY fi.name DESC""", (KAROO_BACKUP_PATTERN,)).fetchall()
    con.close()
    out = {}
    for name, prefix, parent in rows:
        out.setdefault(name, []).append(os.path.join(prefix or "", parent or "", name))
    return list(out.items())


def newest_readable(backups):
    """Path of the newest of the Karoo backups [(name, [paths])] that is readable here, or None."""
    for i, (name, paths) in enumerate(backups):
        readable = [p for p in paths if os.path.isfile(p) and os.access(p, os.R_OK)]
        if readable:
            if i:
                say(f"warning: the {i} newest Karoo backup(s) are not readable here (e.g. {backups[0][1][0]}); using {name}")
            return readable[0]
    if backups:
        say(f"warning: none of the Karoo backups NRAO holds is readable here (newest: {', '.join(backups[0][1])})")
    return None


def pick_karoo_dump(args, nrao_db, output_dir):
    """Path of the Karoo backup to use, kept in output_dir; None to fall back on the copy loaded earlier.

    Data taking is over, so the Karoo backup rarely changes: it is copied into output_dir once and used from
    there. It is copied again only if --karoo-dump names a different one or the NRAO Librarian lists a newer
    one, and the older copy is then deleted.
    """
    local = sorted(glob.glob(os.path.join(output_dir, KAROO_BACKUP_PATTERN.replace("%", "*"))))
    have = local[-1] if local else None
    if args.karoo_dump:
        if not os.access(args.karoo_dump, os.R_OK):
            sys.exit(f"cannot read {args.karoo_dump}")
        src = args.karoo_dump
        if have and os.path.basename(src) == os.path.basename(have):
            return have
    else:
        backups = karoo_backups(nrao_db)
        if not backups:
            say(f"warning: the NRAO Librarian has no file named like {KAROO_BACKUP_PATTERN}")
        if have and (not backups or backups[0][0] <= os.path.basename(have)):
            return have  # nothing newer at NRAO
        src = newest_readable(backups)
        if src is None or (have and os.path.basename(src) <= os.path.basename(have)):
            return have

    size = os.path.getsize(src)
    free = shutil.disk_usage(output_dir).free
    if free < 1.2 * size:
        sys.exit(f"only {free / 1e9:.1f} GB free in {output_dir}; copying {src} needs {size / 1e9:.1f} GB")
    say(f"copying the Karoo backup {src} into {output_dir} ({size / 1e9:.1f} GB)")
    name = os.path.basename(src)
    install(src, output_dir, name)
    for old in local:
        if os.path.basename(old) != name:
            say(f"removing the older Karoo backup {old}")
            os.remove(old)
    return os.path.join(output_dir, name)


def karoo_db_for(args, nrao_db, cache_dir, output_dir):
    """Load (if needed) and return (sqlite path, backup name) for the Karoo side."""
    dump = pick_karoo_dump(args, nrao_db, output_dir)
    cached = sorted(glob.glob(os.path.join(cache_dir, "*.sqlite")))
    if dump is None:
        if not cached:
            sys.exit("no readable Karoo backup and none loaded before; pass --karoo-dump PATH")
        db = max(cached, key=os.path.getmtime)
        say(f"warning: using the Karoo backup loaded earlier, {db}")
        return db, (lsql.load_info(db) or {}).get("name", os.path.basename(db))

    name = os.path.basename(dump)
    size = os.path.getsize(dump)
    db = os.path.join(cache_dir, name.rsplit(".pgdump", 1)[0] + ".sqlite")
    info = lsql.load_info(db)
    if info and info.get("name") == name and info.get("size") == str(size):
        say(f"Karoo backup {name} already loaded ({db})")
        return db, name

    free = shutil.disk_usage(cache_dir).free
    if free < 4 * size:
        sys.exit(f"only {free / 1e9:.0f} GB free in {cache_dir}; loading {name} ({size / 1e9:.1f} GB) needs about "
                 f"{4 * size / 1e9:.0f} GB")
    pg_restore = lsql.find_tool("pg_restore", args.pg_restore)
    say(f"loading the Karoo Librarian backup {dump} with {pg_restore} (about 10 minutes)")
    lsql.load_into_sqlite(lsql.dump_command(pg_restore, dump, lsql.KAROO_TABLES), db, lsql.KAROO_TABLES,
                          event_keep=lsql.karoo_events, event_payload=lsql.karoo_payload,
                          info=dict(name=name, size=size, path=dump))
    for old in cached:
        if old != db:
            say(f"removing the older Karoo copy {old}")
            os.remove(old)
    return db, name


def install(src, dest_dir, name):
    """Copy src to dest_dir/name, replacing any old file only once the copy is complete."""
    tmp = os.path.join(dest_dir, f".{name}.partial")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(dest_dir, name))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workdir", default=os.path.join(HERE, "work"),
                   help="directory for the SQLite copies, ~20 GB (default: work/ in this repository)")
    p.add_argument("--output-dir", default=os.path.join(HERE, "output"),
                   help="where the NRAO backup and the latest page and CSV are kept (default: output/ in this repository)")
    p.add_argument("--publish-dir", default=DEFAULT_PUBLISH_DIR, help="web directory for the page (default: %(default)s)")
    p.add_argument("--no-publish", action="store_true", help="don't copy the page to the web directory")
    p.add_argument("--nrao-db", default="librarian",
                   help="NRAO Librarian database, as pg_dump -d takes it: a name, connection string or URI "
                        "(default: %(default)s)")
    p.add_argument("--nrao-dump", help="read the NRAO Librarian from this existing pg_dump file instead of backing up "
                                       "the live database (for testing)")
    p.add_argument("--karoo-dump", help="use this Karoo Librarian backup (copied into the output directory) instead of "
                                        "the newest one NRAO holds")
    p.add_argument("--pg-dump", help="pg_dump to use (default: /usr/pgsql-*/bin/pg_dump if present, else the environment's)")
    p.add_argument("--pg-restore", help="pg_restore to use (default: the environment's)")
    p.add_argument("--connection", action="append", help="Karoo connection name(s) that mean NRAO (default: aoc-uploads)")
    p.add_argument("--min-jd", type=int, default=2459847, help="leave out nights before this JD (default: %(default)s)")
    p.add_argument("--night-set", default=os.path.join(HERE, "analysis_nights.json"),
                   help="nights the page can filter to (default: analysis_nights.json in this repository)")
    p.add_argument("--night-set-label", default="Worth analyzing")
    args = p.parse_args()

    t0 = time.time()
    os.umask(0o022)
    workdir = os.path.abspath(os.path.expanduser(args.workdir))
    output_dir = os.path.abspath(os.path.expanduser(args.output_dir))
    cache_dir, build_dir, tmp_dir = (os.path.join(workdir, d) for d in ("karoo", "build", "tmp"))
    for d in (workdir, cache_dir, build_dir, tmp_dir, output_dir):
        os.makedirs(d, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = tmp_dir  # big sorts and indexes; /tmp can be small
    if not args.no_publish and not os.access(args.publish_dir, os.W_OK):
        sys.exit(f"cannot write to {args.publish_dir}")
    _lock = lock_workdir(workdir)  # noqa: F841
    say(f"run_daily.py starting on {socket.gethostname()}; output {output_dir}, work directory {workdir}")

    if args.nrao_dump:
        nrao_dump = os.path.abspath(args.nrao_dump)
        nrao_source = f"the NRAO Librarian backup {os.path.basename(nrao_dump)}"
    else:
        nrao_dump = os.path.join(output_dir, NRAO_BACKUP_NAME)
        backup_nrao(args, nrao_dump)
        nrao_source = f"the NRAO Librarian database on {socket.gethostname()}"
    nrao_db = os.path.join(workdir, "nrao.sqlite")
    load_nrao(args, nrao_dump, nrao_db)
    karoo_db, karoo_name = karoo_db_for(args, nrao_db, cache_dir, output_dir)

    say("building the report")
    html, csv_path = os.path.join(build_dir, HTML_NAME), os.path.join(build_dir, CSV_NAME)
    work = os.path.join(workdir, "report_work.sqlite")
    note = f"Sources: {nrao_source}, and the Karoo Librarian backup {karoo_name}."
    try:
        data = report.write_report(karoo_db, nrao_db, work, html, csv_path, connections=args.connection or ["aoc-uploads"],
                                   min_jd=args.min_jd, night_set=args.night_set, night_set_label=args.night_set_label,
                                   extra_notes=[note])
    finally:
        if os.path.exists(work):
            os.remove(work)

    files = sum(k[0] for j in data["jds"] for k in j["k"])
    ok = sum(k[1] for j in data["jds"] for k in j["k"])
    say(f"{ok:,} of {files:,} Karoo files are at NRAO ({100 * ok / max(files, 1):.1f}%); "
        f"{files - ok:,} are not; NRAO as of {data['meta']['nrao_asof']}")

    # The CSV goes first each time, so the page's link to it always works.
    for dest in [output_dir] + ([] if args.no_publish else [args.publish_dir]):
        install(csv_path, dest, CSV_NAME)
        install(html, dest, HTML_NAME)
        say(f"wrote {os.path.join(dest, HTML_NAME)} and {CSV_NAME}")
    say(f"done in {(time.time() - t0) / 60:.1f} minutes")


if __name__ == "__main__":
    main()

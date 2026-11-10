"""Copy HERA Librarian (v1) tables from a pg_dump backup into a local SQLite file.

The rows come out of the backup as COPY text (pg_restore --data-only -f - -t TABLE ... DUMP),
so no PostgreSQL server is needed, only the pg_restore program.

A busy Librarian has tens or hundreds of millions of file events. Pass
event_keep to load only the events that matter (and event_payload to shrink
their JSON payloads); a file_event_summary table then still records the count
and latest time of every event type seen.
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse

KAROO_TABLES = ["file", "file_event", "file_instance", "store", "standing_order"]
NRAO_TABLES = ["file", "file_event", "file_instance", "store"]
INT_COLS = {"obsid", "size", "id", "store", "session_id", "deletion_policy"}
REAL_COLS = {"start_time_jd", "stop_time_jd", "start_lst_hr"}
COPY_RE = re.compile(r'^COPY (?:"?[\w]+"?\.)?"?(\w+)"? \((.*)\) FROM stdin;$')


def sqlite_uri(path, readonly=False):
    """URI for opening a SQLite file with uri=True, with SQLite's file locking off.

    Only one run uses these files at a time (run_daily.py holds its own lock), and some network filesystems
    (Lustre, NFS) don't support the locks SQLite asks for.
    """
    return "file:" + urllib.parse.quote(os.path.abspath(path)) + "?nolock=1" + ("&mode=ro" if readonly else "")


def find_tool(name, explicit=None, prefer_system=False):
    """Path to a PostgreSQL client program.

    Looks next to this Python first (the mamba environment's bin/, which cron's bare PATH would miss), then
    on PATH, then in the system PostgreSQL install (/usr/pgsql-*/bin). prefer_system checks that one first.
    """
    if explicit:
        return explicit
    here = os.path.join(os.path.dirname(sys.executable), name)
    system = sorted(glob.glob(f"/usr/pgsql-*/bin/{name}"), reverse=True)
    candidates = [here, shutil.which(name)] + system
    if prefer_system:
        candidates = system + candidates
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    sys.exit(f"{name} not found; activate the librarian_transfer_monitor environment or pass its path")


def tables_in_dump(pg_restore, dump):
    out = subprocess.run([pg_restore, "-l", dump], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"pg_restore could not read {dump}:\n{out.stderr.strip()}\n"
                 "(If it complains about an unsupported version, use a newer pg_restore.)")
    have = set()
    for line in out.stdout.splitlines():
        m = re.search(r"\bTABLE DATA \S+ (\S+) ", line)
        if m:
            have.add(m.group(1).strip('"'))
    return have


def dump_command(pg_restore, dump, tables):
    """Command that writes the tables' rows from a pg_dump custom-format file as COPY text."""
    have = tables_in_dump(pg_restore, dump)
    if "file" not in have:
        sys.exit(f"{dump} has no 'file' table data; is it a Librarian backup? Tables seen: {sorted(have)}")
    cmd = [pg_restore, "--data-only", "-f", "-"]
    for t in tables:
        if t in have:
            cmd += ["-t", t]
        else:
            print(f"  note: {os.path.basename(dump)} has no data for table {t}", flush=True)
    return cmd + [dump]


_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(field):
    """Decode one field of PostgreSQL COPY text format."""
    if field == r"\N":
        return None
    if "\\" not in field:
        return field
    out, i, n = [], 0, len(field)
    while i < n:
        c = field[i]
        if c != "\\" or i + 1 == n:
            out.append(c)
            i += 1
            continue
        nxt = field[i + 1]
        if nxt in _ESC:
            out.append(_ESC[nxt])
            i += 2
        elif nxt in "01234567":
            j = i + 1
            while j < n and j < i + 4 and field[j] in "01234567":
                j += 1
            out.append(chr(int(field[i + 1:j], 8)))
            i = j
        elif nxt == "x" and i + 2 < n and field[i + 2] in "0123456789abcdefABCDEF":
            j = i + 2
            while j < n and j < i + 4 and field[j] in "0123456789abcdefABCDEF":
                j += 1
            out.append(chr(int(field[i + 2:j], 16)))
            i = j
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


# ---------------------------------------------------------------- which events to keep


def karoo_events(name, etype):
    """The Karoo events that say a data file was copied somewhere."""
    return ((etype == "copy_finished" or etype.startswith("standing_order_succeeded"))
            and (name.endswith(".uvh5") or "ant_class" in name))


_CONN_RE = re.compile(r'"connection_name":\s*"([^"\\]*)"')
_ERR_RE = re.compile(r'"error_code":\s*(-?\d+)')


def karoo_payload(etype, raw):
    """A copy event's payload (still COPY text) cut to its connection name and error code (it can carry kB of rsync output)."""
    if etype != "copy_finished":
        return "{}"
    c, e = _CONN_RE.search(raw), _ERR_RE.search(raw)
    return json.dumps({"connection_name": c.group(1) if c else None, "error_code": int(e.group(1)) if e else None},
                      ensure_ascii=False)


def nrao_events(name, etype):
    """At NRAO only the arrival of each copy matters."""
    return etype == "create_instance"


def no_payload(etype, raw):
    return "{}"


# ---------------------------------------------------------------- loading


def load_into_sqlite(cmd, db_path, tables, event_keep=None, event_payload=None, info=None):
    """Run cmd (which prints COPY text) and load the listed tables into a new SQLite file at db_path.

    The file is built as <db_path>.partial and renamed only when complete, so a failed load leaves any
    previous db_path in place. info (a dict) is saved in a load_info table, for deciding later whether the
    file is still current.
    """
    tmp_path = db_path + ".partial"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    con = sqlite3.connect(sqlite_uri(tmp_path), uri=True)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    errfile = tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile, text=True, encoding="utf-8", errors="replace")

    t0 = time.time()
    table = cols = None
    batch, counts = [], {}
    summary = {}  # event type -> [count, latest time], when filtering events
    seen = 0
    filtering = False

    def flush():
        if batch:
            con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})', batch)
            counts[table] = counts.get(table, 0) + len(batch)
            batch.clear()

    for line in proc.stdout:
        line = line.rstrip("\n")
        if table is None:
            m = COPY_RE.match(line)
            if m and m.group(1) in tables:
                table = m.group(1)
                cols = [c.strip().strip('"') for c in m.group(2).split(",")]
                decl = ", ".join(
                    f'"{c}" ' + ("INTEGER" if c in INT_COLS else "REAL" if c in REAL_COLS else "TEXT") for c in cols)
                con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({decl})')
                filtering = table == "file_event" and event_keep is not None
                if filtering:
                    iname, itype, itime = cols.index("name"), cols.index("type"), cols.index("time")
                    ipay = cols.index("payload") if event_payload else None
                print(f"  loading {table}{' (selected events only)' if filtering else ''} ...", flush=True)
            continue
        if line == "\\.":
            flush()
            extra = f" kept of {seen:,} seen" if filtering else ""
            print(f"    {counts.get(table, 0):,} rows{extra}", flush=True)
            table = cols = None
            filtering = False
            continue
        fields = line.split("\t")
        if filtering:
            seen += 1
            etype, etime = fields[itype], fields[itime]
            s = summary.get(etype)
            if s is None:
                summary[etype] = [1, etime]
            else:
                s[0] += 1
                if etime > s[1]:
                    s[1] = etime
            if seen % 5_000_000 == 0:
                print(f"    {seen:,} events read, {counts.get(table, 0) + len(batch):,} kept, "
                      f"{time.time() - t0:.0f} s", flush=True)
            if not event_keep(fields[iname], etype):
                continue
            if ipay is not None:
                fields[ipay] = event_payload(etype, fields[ipay])
        batch.append(tuple(unescape(f) for f in fields))
        if len(batch) >= 50000:
            flush()

    status = proc.wait()
    errfile.seek(0)
    err = errfile.read().strip()
    if status != 0:
        con.close()
        os.remove(tmp_path)
        sys.exit(f"{os.path.basename(cmd[0])} failed (exit {status}):\n{err}")
    missing = [t for t in tables if t not in counts]
    if "file" in missing or "file_instance" in missing:
        con.close()
        os.remove(tmp_path)
        sys.exit(f"{os.path.basename(cmd[0])} printed no rows for {missing}" + (f":\n{err}" if err else ""))

    for t in missing:  # make sure every table exists, even if empty or absent
        con.execute(f'CREATE TABLE IF NOT EXISTS "{t}" (name TEXT)')
    if event_keep is not None:
        con.execute("CREATE TABLE file_event_summary (type TEXT, n INTEGER, max_time TEXT)")
        con.executemany("INSERT INTO file_event_summary VALUES (?, ?, ?)",
                        [(t, n, unescape(mt)) for t, (n, mt) in sorted(summary.items())])
    con.execute("CREATE TABLE load_info (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO load_info VALUES (?, ?)", [(k, str(v)) for k, v in (info or {}).items()])
    con.execute("CREATE INDEX IF NOT EXISTS file_event_name ON file_event(name)")
    con.execute("CREATE INDEX IF NOT EXISTS file_event_type ON file_event(type)")
    con.commit()
    con.close()
    os.replace(tmp_path, db_path)
    print(f"  done in {time.time() - t0:.0f} s -> {db_path}", flush=True)


def load_info(db_path):
    """The info dict saved by load_into_sqlite, or None if db_path is missing or was made another way."""
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect(sqlite_uri(db_path, readonly=True), uri=True)
        try:
            return dict(con.execute("SELECT key, value FROM load_info"))
        finally:
            con.close()
    except sqlite3.Error:
        return None

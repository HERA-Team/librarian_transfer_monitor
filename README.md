# librarian_transfer_monitor

A daily report on which HERA files from the Karoo Librarian have reached the
NRAO Librarian. It covers `zen.<JD>.<frac>.sum.uvh5`, `.diff.uvh5` and
`.sum.ant_class.csv.tar.gz` files, from JD 2459847 on.

The output is one self-contained HTML page with these sections:

- **Per night:** how many of each kind of file are at NRAO, which are missing,
  and which disks hold that night at each site.
- **Per Karoo disk:** how much of what is on it has reached NRAO.
- **Since data taking ended:** how fast the missing files have been arriving.
- **What NRAO is receiving:** wanted files vs. everything else, including extra
  copies of files it already had.
- **"Worth analyzing" switch:** limits the whole page to the nights in
  `analysis_nights.json`.

Next to the page goes a CSV listing every file not (correctly) at NRAO.

A file counts as at NRAO when the NRAO Librarian has a record with the same
name and md5 and at least one copy on an NRAO disk.

## How it works

`run_daily.py` does everything, once a day:

1. **Backs up the NRAO Librarian database** with `pg_dump` to
   `output/nrao-librarian.pgdump`. This takes about 2 minutes and 600 MB. The
   new backup replaces the previous day's once it is complete, so backups don't
   pile up.
2. **Loads from that backup** the tables the report needs into SQLite
   (`work/nrao.sqlite`): files, copies, disks, and the events that record when
   each copy arrived.
3. **Finds the newest Karoo Librarian backup** that has reached NRAO
   (`maint.YYYYMMDD.karoo.librarian.dbbackup.pgdump`, located through the NRAO
   Librarian's own records) and loads its copy events into `work/karoo/`.
   - The load takes about 10 minutes and 12 GB.
   - It happens only when a new backup appears. Data taking is over, so that
     should be rare, and other days reuse the loaded copy.
   - If no backup is readable, the run falls back on the one loaded before.
4. **Builds the page and the CSV**, and puts them in `output/`
   (`karoo_nrao_transfer_report.html` and `karoo_nrao_transfer_problems.csv`).
5. **Copies both to the web directory**,
   `/lustre/aoc/projects/hera/www/librarian/`.

Every file is replaced only once its new version is completely written. A
failed run leaves the previous backup and page in place and exits non-zero.

`output/` and `work/` are ignored by git.

| File | What it is |
|---|---|
| `run_daily.py` | The cron entry point (`--help` lists the options) |
| `build_transfer_report.py` | Builds the page from the two SQLite copies; can also be run by hand |
| `librarian_sqlite.py` | Loads Librarian tables from a `pg_dump` backup into SQLite |
| `report_template.html` | The page; the data are inserted at `/*__DATA__*/` |
| `analysis_nights.json` | The nights worth analyzing (a JSON list of JDs) |
| `environment.yml` | The mamba environment: Python plus the PostgreSQL client programs |

There is nothing to install. The scripts use only the Python standard library
and the `pg_dump`/`pg_restore` programs.

## Setting it up on herastore01

Do all of this as `heramgr`. The NRAO database (PostgreSQL 9.5, database
`librarian`) lets `heramgr` connect without a password, and other users
can't connect at all.

1. **Clone the repository.** The examples below assume
   `/lustre/aoc/projects/hera/heramgr/librarian_transfer_monitor`. It needs
   about 20 GB free.

   ```bash
   cd /lustre/aoc/projects/hera/heramgr
   git clone https://github.com/HERA-Team/librarian_transfer_monitor.git
   ```

2. **Create the environment:**

   ```bash
   cd librarian_transfer_monitor
   mamba env create -f environment.yml
   ```

3. **Run it once by hand** and check the output. The first run also loads the
   Karoo backup, so it takes longest.

   ```bash
   /lustre/aoc/projects/hera/heramgr/miniforge3/envs/librarian_transfer_monitor/bin/python run_daily.py
   ```

4. **Add it to `heramgr`'s crontab** (`crontab -e`). This runs it at 06:15
   every day, logging to `output/run_daily.log`:

   ```
   15 6 * * * /lustre/aoc/projects/hera/heramgr/miniforge3/envs/librarian_transfer_monitor/bin/python /lustre/aoc/projects/hera/heramgr/librarian_transfer_monitor/run_daily.py >> /lustre/aoc/projects/hera/heramgr/librarian_transfer_monitor/output/run_daily.log 2>&1
   ```

   Adjust the paths if you cloned or installed elsewhere. Use full paths:
   cron's `PATH` is minimal. The script finds `pg_restore` next to the
   environment's Python by itself.

### Notes

- **Where the SQLite copies go:** they take about 17 GB in `work/`. A local
  disk may be faster than Lustre, if one has room (`df -h` lists them; don't use
  the Librarian's own store disks, `/export/hera/herastore*`). To use one, pass
  `--workdir DIR`. The scripts turn off SQLite's file locking, which some
  Lustre mounts don't support; `run_daily.py` makes sure only one run uses the
  directory at a time.
- **`pg_dump`:** the script uses the server's own `/usr/pgsql-*/bin/pg_dump`
  when it exists, which is known to connect. Pass `--pg-dump` to use another
  one. Pass `--nrao-db` to point it at a database other than the local
  `librarian`; it takes anything `pg_dump -d` accepts.
- **Karoo backup:** the script reads it straight from the NRAO disk it sits on
  (for example `/export/hera/herastore03-1/maint/2026.09/`). If those disks
  aren't mounted where the script runs, copy the backup somewhere readable and
  pass `--karoo-dump PATH`.
- **Nights worth analyzing:** to change the list, edit `analysis_nights.json`
  (or pass `--night-set FILE`).
- **Other settings:** the first night (`--min-jd`) and the Karoo's name for its
  NRAO connection (`--connection`, default `aoc-uploads`) are options too.
- **Manual or test runs:** `--nrao-dump FILE` reads the NRAO side from an
  existing `pg_dump` file instead of backing up the live database.
  `--no-publish` skips the copy to the web directory. `--output-dir` puts the
  outputs somewhere other than `output/`.

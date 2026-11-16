#!/usr/bin/env python3
"""imsticker - put any PNG / GIF / APNG into the native iMessage sticker library.

The image goes in exactly as it is: no subject lift, no cut-out. Animated GIF
and APNG files become animated stickers and keep their transparency and their
frame timing.

It works by adding to the Core Data store that `stickersd` keeps on a Mac (see
README.md for the format). By default the stickers are saved through Core Data
with the model Apple ships on the Mac, so the change lands in the store's
history, which is what iCloud sync reads. `--raw-sql` writes the rows directly
instead. Needs macOS 14 or later, signed in to iMessage, with Apple's command
line tools installed (`python3`, `swift`).

    python3 imsticker.py add cat.png party.gif wave.apng
    python3 imsticker.py list -v
    python3 imsticker.py export ./dump
    python3 imsticker.py restore

Written for Python 3.9 (what Apple ships) and the standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

COCOA_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01

# Where stickersd keeps the store. It moved between macOS releases: 15.1 has it
# in ~/Library/Stickers, 15.7 in the group container.
DB_CANDIDATES = (
    ("Library", "Group Containers", "com.apple.stickersd.group", "Stickers", "stickers.stickerdb"),
    ("Library", "Stickers", "stickers.stickerdb"),
)
BACKUP_PARTS = ("Library", "Application Support", "imsticker", "Backups")

# The compiled Core Data model of the sticker store, shipped inside macOS.
APPLE_MODEL = "/System/Library/PrivateFrameworks/Stickers.framework/Versions/A/Resources/Stickers.momd"

STICKER_TABLE = "ZMANAGEDSTICKER"
REP_TABLE = "ZMANAGEDREPRESENTATION"
STICKER_ENTITY = "ManagedSticker"
REP_ENTITY = "ManagedRepresentation"

# Columns this tool knows how to fill. Columns the store has beyond these are
# left at their default; columns listed here that the store lacks are skipped.
STICKER_KNOWN = (
    "Z_PK", "Z_ENT", "Z_OPT", "ZATTRIBUTIONADAMID", "ZBYTECOUNT", "ZEFFECT", "ZTYPE",
    "ZVERSION", "ZCREATIONDATE", "ZLASTUSEDDATE", "ZLIBRARYINDEX", "ZACCESSIBILITYNAME",
    "ZATTRIBUTIONBUNDLEIDENTIFIER", "ZATTRIBUTIONNAME", "ZEXTERNALURI", "ZNAME",
    "ZPROMPTPRIMARYLANGUAGEIDENTIFIER", "ZSANITIZEDPROMPT", "ZSEARCHTEXT", "ZIDENTIFIER",
    "ZMETADATA",
)
REP_KNOWN = (
    "Z_PK", "Z_ENT", "Z_OPT", "ZBYTECOUNT", "ZINDEX", "ZISPREFERRED", "ZVERSION",
    "ZSTICKER", "ZSIZE_H", "ZSIZE_W", "ZROLE", "ZUTI", "ZIDENTIFIER", "ZDATA",
)
STICKER_REQUIRED = ("Z_PK", "Z_ENT", "ZTYPE", "ZIDENTIFIER")
REP_REQUIRED = ("Z_PK", "Z_ENT", "ZSTICKER", "ZROLE", "ZUTI", "ZDATA")

USER_STICKER_TYPE = 1
LIBRARY_INDEX_STEP = 1024

# Smaller edges to fall back to when an encoded sticker is over the size budget.
SHRINK_STEPS = (768, 640, 512, 448, 384, 320, 256, 192, 160, 128)

EXTENSIONS = {
    "public.heic": ".heic",
    "public.heics": ".heics",
    "public.png": ".png",
    "com.compuserve.gif": ".gif",
    "public.jpeg": ".jpg",
}

COMMANDS = ("add", "list", "export", "restore")


class ToolError(Exception):
    """A failure with a message meant for the person running the tool."""


# --------------------------------------------------------------------------
# Locating and opening the store
# --------------------------------------------------------------------------

def _last_write(path):
    newest = 0.0
    for suffix in ("", "-wal"):
        try:
            newest = max(newest, os.stat(str(path) + suffix).st_mtime)
        except OSError:
            pass
    return newest


def find_db(explicit=None, home=None):
    """Return the sticker store to use: --db, $IMSTICKER_DB, or the live one."""
    chosen = explicit or os.environ.get("IMSTICKER_DB")
    if chosen:
        path = Path(chosen).expanduser()
        if not path.is_file():
            raise ToolError("No sticker database at {}".format(path))
        return path
    home = Path(home) if home else Path.home()
    found = [home.joinpath(*parts) for parts in DB_CANDIDATES]
    found = [path for path in found if path.is_file()]
    if not found:
        looked = "\n".join("  ~/" + "/".join(parts) for parts in DB_CANDIDATES)
        raise ToolError(
            "Could not find the sticker database. Looked in:\n{}\n"
            "Open Messages on this Mac and make one sticker the normal way first, "
            "or point at the file with --db.".format(looked)
        )
    # If an old copy was left behind by an OS upgrade, the live one is the one
    # stickersd wrote to most recently.
    found.sort(key=_last_write, reverse=True)
    return found[0]


def connect(path, readonly=False):
    attempts = [(str(path), False)]
    if readonly:
        # A read-only open can fail on a write-ahead-log store with no -shm file
        # yet; the plain open that follows still only reads.
        attempts.insert(0, (Path(path).resolve().as_uri() + "?mode=ro", True))
    error = None
    for target, is_uri in attempts:
        conn = None
        try:
            conn = sqlite3.connect(target, uri=is_uri, timeout=10, isolation_level=None)
            conn.execute("select count(*) from sqlite_master").fetchone()
        except sqlite3.Error as exc:
            error = exc
            if conn is not None:
                conn.close()
            continue
        conn.row_factory = sqlite3.Row
        return conn
    raise ToolError(
        "Cannot open {}: {}\n"
        "If macOS blocked it, allow your terminal app under System Settings > "
        "Privacy & Security > Full Disk Access and try again.".format(path, error)
    )


def backup_dir(home=None):
    override = os.environ.get("IMSTICKER_BACKUP_DIR")
    if override:
        return Path(override).expanduser()
    home = Path(home) if home else Path.home()
    return home.joinpath(*BACKUP_PARTS)


def backup_db(db_path, directory):
    """Snapshot the store with SQLite's backup API (safe while stickersd has it open)."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    target = directory / "stickers.stickerdb.{}.backup".format(stamp)
    source = sqlite3.connect(str(db_path), timeout=10)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def restart_stickersd():
    """stickersd caches the library; launchd starts a fresh one on demand."""
    if sys.platform != "darwin":
        return False
    subprocess.run(["killall", "stickersd"], capture_output=True)
    return True


# --------------------------------------------------------------------------
# Reading the schema
# --------------------------------------------------------------------------

def table_info(conn, table):
    rows = conn.execute("pragma table_info({})".format(table)).fetchall()
    return {
        row[1]: {"notnull": bool(row[3]), "default": row[4], "pk": bool(row[5])}
        for row in rows
    }


def _looks_like_image(head):
    return head[4:8] == b"ftyp" or head[:4] == b"\x89PNG" or head[:4] == b"GIF8"


def _strip_marker(blob):
    """Return (image bytes, had a Core Data inline-storage marker in front)."""
    if blob[:1] == b"\x01" and not _looks_like_image(blob) and _looks_like_image(blob[1:]):
        return blob[1:], True
    return blob, False


class Plan:
    """What the store on this machine looks like, checked before anything is written."""

    def __init__(self, conn):
        try:
            self.sticker_cols = table_info(conn, STICKER_TABLE)
            self.rep_cols = table_info(conn, REP_TABLE)
            entities = dict(
                (row[0], row[1])
                for row in conn.execute("select Z_NAME, Z_ENT from Z_PRIMARYKEY").fetchall()
            )
        except sqlite3.Error as exc:
            raise ToolError("This does not look like a sticker database: {}".format(exc))

        problems = []
        for table, cols, required, known in (
            (STICKER_TABLE, self.sticker_cols, STICKER_REQUIRED, STICKER_KNOWN),
            (REP_TABLE, self.rep_cols, REP_REQUIRED, REP_KNOWN),
        ):
            if not cols:
                problems.append("table {} is missing".format(table))
                continue
            missing = [name for name in required if name not in cols]
            if missing:
                problems.append("{} lacks {}".format(table, ", ".join(missing)))
            blocking = [
                name for name, info in cols.items()
                if name not in known and info["notnull"] and info["default"] is None
                and not info["pk"]
            ]
            if blocking:
                problems.append(
                    "{} has required columns this tool does not know: {}".format(
                        table, ", ".join(sorted(blocking)))
                )
        for entity in (STICKER_ENTITY, REP_ENTITY):
            if entity not in entities:
                problems.append("Z_PRIMARYKEY has no entry for {}".format(entity))
        if problems:
            raise ToolError(
                "The sticker database on this macOS version has a layout this tool "
                "does not handle, so nothing was changed:\n  - " + "\n  - ".join(problems)
            )

        self.sticker_ent = entities[STICKER_ENTITY]
        self.rep_ent = entities[REP_ENTITY]
        self.unknown = sorted(
            [name for name in self.sticker_cols if name not in STICKER_KNOWN]
            + [name for name in self.rep_cols if name not in REP_KNOWN]
        )
        self.prefix = self._blob_prefix(conn)

    @staticmethod
    def _blob_prefix(conn):
        """Store image bytes the way the rows already in the store do.

        Core Data can put a one-byte 0x01 marker in front of inline binary data.
        The stores seen so far hold the bare file, but copy the marker if the
        existing rows have it.
        """
        rows = conn.execute(
            "select substr(ZDATA, 1, 16) from {} where ZDATA is not null "
            "order by Z_PK desc limit 50".format(REP_TABLE)
        ).fetchall()
        bare = marked = 0
        for row in rows:
            head = bytes(row[0])
            if _looks_like_image(head):
                bare += 1
            elif _strip_marker(head)[1]:
                marked += 1
        return b"\x01" if marked > bare else b""

    def next_pk(self, conn, table, entity):
        in_table = conn.execute("select coalesce(max(Z_PK), 0) from {}".format(table)).fetchone()[0]
        tracked = conn.execute(
            "select coalesce(max(Z_MAX), 0) from Z_PRIMARYKEY where Z_NAME = ?", (entity,)
        ).fetchone()[0]
        return max(in_table, tracked) + 1


# --------------------------------------------------------------------------
# Writing a sticker
# --------------------------------------------------------------------------

def _insert(conn, table, available, values):
    columns = [name for name in values if name in available]
    sql = "insert into {} ({}) values ({})".format(
        table, ", ".join(columns), ", ".join("?" for _ in columns))
    conn.execute(sql, [values[name] for name in columns])


def insert_sticker(conn, plan, encoded, name, now):
    """Add one encoded sticker. The caller holds the write transaction."""
    blobs = [Path(rep["path"]).read_bytes() for rep in encoded["representations"]]

    sticker_pk = plan.next_pk(conn, STICKER_TABLE, STICKER_ENTITY)
    rep_pk = plan.next_pk(conn, REP_TABLE, REP_ENTITY)
    library_index = None
    if "ZLIBRARYINDEX" in plan.sticker_cols:
        top = conn.execute(
            "select coalesce(max(ZLIBRARYINDEX), 0) from {} where ZTYPE = ?".format(STICKER_TABLE),
            (USER_STICKER_TYPE,),
        ).fetchone()[0]
        library_index = top + LIBRARY_INDEX_STEP

    sticker_uuid = uuid.uuid4()
    external_uri = "sticker:///user/identifier/{}".format(str(sticker_uuid).upper())
    _insert(conn, STICKER_TABLE, plan.sticker_cols, {
        "Z_PK": sticker_pk,
        "Z_ENT": plan.sticker_ent,
        "Z_OPT": 1,
        "ZATTRIBUTIONADAMID": 0,
        "ZBYTECOUNT": sum(len(blob) for blob in blobs),
        "ZEFFECT": 0,
        "ZTYPE": USER_STICKER_TYPE,
        "ZVERSION": 1,
        "ZCREATIONDATE": now,
        "ZLASTUSEDDATE": now,
        "ZLIBRARYINDEX": library_index,
        "ZACCESSIBILITYNAME": None,
        "ZATTRIBUTIONBUNDLEIDENTIFIER": None,
        "ZATTRIBUTIONNAME": None,
        "ZEXTERNALURI": external_uri,
        "ZNAME": "",
        "ZPROMPTPRIMARYLANGUAGEIDENTIFIER": None,
        "ZSANITIZEDPROMPT": None,
        "ZSEARCHTEXT": name,
        "ZIDENTIFIER": sticker_uuid.bytes,
        "ZMETADATA": None,
    })

    for offset, (rep, blob) in enumerate(zip(encoded["representations"], blobs)):
        _insert(conn, REP_TABLE, plan.rep_cols, {
            "Z_PK": rep_pk + offset,
            "Z_ENT": plan.rep_ent,
            "Z_OPT": 1,
            "ZBYTECOUNT": len(blob),
            "ZINDEX": int(rep["index"]),
            "ZISPREFERRED": 1 if rep["preferred"] else 0,
            "ZVERSION": 1,
            "ZSTICKER": sticker_pk,
            "ZSIZE_H": float(rep["height"]),
            "ZSIZE_W": float(rep["width"]),
            "ZROLE": rep["role"],
            "ZUTI": rep["uti"],
            "ZIDENTIFIER": uuid.uuid4().bytes,
            "ZDATA": plan.prefix + blob,
        })

    conn.execute("update Z_PRIMARYKEY set Z_MAX = ? where Z_NAME = ?", (sticker_pk, STICKER_ENTITY))
    conn.execute(
        "update Z_PRIMARYKEY set Z_MAX = ? where Z_NAME = ?",
        (rep_pk + len(blobs) - 1, REP_ENTITY),
    )
    return {"pk": sticker_pk, "uri": external_uri}


# --------------------------------------------------------------------------
# The Swift helper: encodes with Apple's ImageIO, saves with Apple's Core Data
# --------------------------------------------------------------------------

def require_mac():
    if os.environ.get("IMSTICKER_ENCODER"):  # the tests bring their own helper
        return
    if sys.platform != "darwin":
        raise ToolError(
            "Importing needs a Mac: the images are written with Apple's ImageIO and "
            "the sticker library lives in the Mac's user folder."
        )


def helper_command(work_dir):
    override = os.environ.get("IMSTICKER_ENCODER")  # a JSON argv list; used by the tests
    if override:
        return json.loads(override)
    require_mac()
    if not shutil.which("swift"):
        raise ToolError("`swift` was not found. Install Apple's command line tools: xcode-select --install")
    script = Path(work_dir) / "imsticker_helper.swift"
    script.write_text(SWIFT_HELPER, encoding="utf-8")
    # Building it once is far quicker than interpreting the script per image.
    # If the build does not work here, fall back to interpreting.
    binary = Path(work_dir) / "imsticker_helper"
    if shutil.which("swiftc"):
        try:
            built = subprocess.run(
                ["swiftc", "-o", str(binary), str(script)], capture_output=True, text=True)
        except OSError:
            built = None
        if built is not None and built.returncode == 0 and binary.is_file():
            return [str(binary)]
    return ["swift", str(script)]


def apple_model():
    """Where Apple's model of the sticker store is, or why it cannot be used."""
    path = Path(os.environ.get("IMSTICKER_MODEL") or APPLE_MODEL)
    if not path.exists():
        raise ToolError(
            "Apple's sticker model was not found at {}.\n"
            "It is what lets the stickers be saved through Core Data. Without it you can "
            "still import with --raw-sql, which writes the rows directly; those may not "
            "reach iCloud on their own.".format(path)
        )
    return path


def core_data_insert(command, db_path, model_path, stickers, work_dir):
    """Save stickers through Core Data, in one transaction. With an empty list
    this only checks that Apple's model opens the store."""
    manifest_path = Path(work_dir) / "manifest-{}.json".format(len(stickers))
    manifest_path.write_text(json.dumps({"stickers": stickers}), encoding="utf-8")
    args = list(command) + [
        "insert", "--store", str(db_path), "--model", str(model_path), "--manifest", str(manifest_path),
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except OSError as exc:
        raise ToolError("could not run the helper: {}".format(exc))
    if proc.returncode != 0:
        raise ToolError(
            "{}\nNothing was changed. If Apple's model does not fit this library, "
            "--raw-sql writes the rows directly instead.".format(_encoder_failure(proc, "Core Data save failed"))
        )
    try:
        saved = json.loads(proc.stdout)["stickers"]
        for item in saved:
            item["primaryKey"], item["uri"]
    except (ValueError, KeyError, TypeError):
        raise ToolError("the helper printed something unexpected: {}".format(proc.stdout.strip()[:200]))
    if len(saved) != len(stickers):
        raise ToolError("the helper saved {} stickers, expected {}".format(len(saved), len(stickers)))
    return saved


def _encoder_failure(proc, what="encoder failed"):
    detail = (proc.stderr or proc.stdout or "").strip() or "exit {}".format(proc.returncode)
    hint = ""
    if "SwiftBridging" in detail:
        hint = (
            "\nThis is a known fault in some Command Line Tools installs (two module maps "
            "define SwiftBridging), not a problem with the image. The workaround is in "
            "https://github.com/AdamEXu/StickerHack/issues/2"
        )
    lines = detail.splitlines()
    if len(lines) > 12:
        detail = "\n".join(lines[:12]) + "\n... ({} more lines)".format(len(lines) - 12)
    return "{}: {}{}".format(what, detail, hint)


def run_encoder(command, source, out_dir, max_edge, resample, no_upscale):
    args = list(command) + [
        "--input", str(source), "--output-dir", str(out_dir),
        "--max-edge", str(max_edge), "--resample", resample,
    ]
    if no_upscale:
        args.append("--no-upscale")
    try:
        proc = subprocess.run(args, capture_output=True, text=True)
    except OSError as exc:
        raise ToolError("could not run the encoder: {}".format(exc))
    if proc.returncode != 0:
        raise ToolError(_encoder_failure(proc))
    try:
        encoded = json.loads(proc.stdout)
    except ValueError:
        raise ToolError("encoder printed something unexpected: {}".format(proc.stdout.strip()[:200]))
    return encoded


REP_FIELDS = ("role", "uti", "width", "height", "preferred", "index", "path")


def validate_encoded(encoded):
    reps = encoded.get("representations") if isinstance(encoded, dict) else None
    if not reps or not all(isinstance(rep, dict) for rep in reps):
        raise ToolError("encoder returned no images")
    for rep in reps:
        absent = [name for name in REP_FIELDS if name not in rep]
        if absent:
            raise ToolError("encoder result lacks {}".format(", ".join(absent)))
        path = Path(rep["path"])
        if not path.is_file() or path.stat().st_size < 12:
            raise ToolError("encoder output is missing for {}".format(rep["role"]))
        with open(str(path), "rb") as handle:
            head = handle.read(12)
        if rep["uti"] == "public.heics" and head[4:12] != b"ftypmsf1":
            raise ToolError("animated output is not an HEIF image sequence (msf1)")
        if rep["uti"] == "public.heic" and head[4:8] != b"ftyp":
            raise ToolError("still output is not an HEIC file")
        rep["byteCount"] = path.stat().st_size


def encode_within_budget(command, source, work_dir, max_edge, resample, no_upscale, max_bytes):
    """Encode, stepping the size down until the largest image fits the budget.

    Stickers that are too heavy show up in the picker but fail to send, so by
    default nothing over about 1 MB goes in.
    """
    edges = [max_edge] + [edge for edge in SHRINK_STEPS if edge < max_edge]
    encoded = None
    previous_size = None
    for edge in edges:
        out_dir = Path(work_dir) / "edge-{}".format(edge)
        out_dir.mkdir(parents=True, exist_ok=True)
        encoded = run_encoder(command, source, out_dir, edge, resample, no_upscale)
        validate_encoded(encoded)
        encoded["maxEdge"] = edge
        heaviest = max(encoded["representations"], key=lambda rep: rep["byteCount"])
        encoded["overBudget"] = bool(max_bytes) and heaviest["byteCount"] > max_bytes
        if not encoded["overBudget"]:
            break
        size = (heaviest["width"], heaviest["height"])
        if size == previous_size:  # --no-upscale and already at the source size
            break
        previous_size = size
    return encoded


def _kb(count):
    return "{:.1f} KB".format(count / 1024.0)


def describe(encoded):
    main = [rep for rep in encoded["representations"] if rep["preferred"]] or encoded["representations"]
    rep = main[0]
    text = "{} {}x{}, {}".format(
        encoded.get("mode", "sticker"), int(rep["width"]), int(rep["height"]), _kb(rep["byteCount"]))
    if encoded.get("frameCount", 1) > 1:
        text += ", {} frames".format(encoded["frameCount"])
    return text


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def sql_layout_check(db_path):
    """Check the store's layout for --raw-sql. Returns the columns left alone."""
    conn = connect(db_path)
    try:
        return Plan(conn).unknown
    finally:
        conn.close()


def sql_insert(db_path, encoded_stickers, name):
    """Write the rows directly, all in one transaction."""
    conn = connect(db_path)
    try:
        plan = Plan(conn)
        now = time.time() - COCOA_EPOCH
        try:
            conn.execute("begin immediate")
        except sqlite3.Error as exc:
            raise ToolError("The sticker database is busy ({}). Quit Messages and retry.".format(exc))
        try:
            rows = [insert_sticker(conn, plan, encoded, name, now) for encoded in encoded_stickers]
            conn.execute("commit")
        except Exception as exc:
            if conn.in_transaction:
                conn.execute("rollback")
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise ToolError("Import failed and was rolled back: {}".format(exc))
            raise
        return rows
    finally:
        conn.close()


def cmd_add(args):
    files = [Path(item).expanduser() for item in args.files]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise ToolError("File not found: {}".format(", ".join(missing)))
    if args.name and len(files) > 1:
        raise ToolError("--name names one sticker; pass a single file with it")

    db_path = find_db(args.db)
    work_dir = Path(tempfile.mkdtemp(prefix="imsticker-"))
    failures = []
    try:
        print("Sticker library: {}".format(db_path))
        require_mac()
        # Find out whether the library can be written before spending time encoding.
        model = None
        if args.raw_sql:
            unknown = sql_layout_check(db_path)
            if unknown:
                print("note: leaving columns new to this tool at their defaults: {}".format(
                    ", ".join(unknown)))
        else:
            model = apple_model()
        command = helper_command(work_dir)
        if model is not None:
            core_data_insert(command, db_path, model, [], work_dir)

        ready = []
        for position, source in enumerate(files):
            try:
                encoded = encode_within_budget(
                    command, source, work_dir / str(position), args.max_edge,
                    args.resample, args.no_upscale, args.max_bytes)
            except ToolError as exc:
                failures.append(source)
                print("FAILED  {}: {}".format(source.name, exc), file=sys.stderr)
                continue
            ready.append((source, encoded))
        if not ready:
            raise ToolError("Nothing could be encoded, so the library was not touched.")

        if args.dry_run:
            for source, encoded in ready:
                print("would add  {}: {}".format(source.name, describe(encoded)))
            print("Dry run: the library was not changed.")
            return 1 if failures else 0

        backup = backup_db(db_path, backup_dir())
        if model is None:
            rows = sql_insert(db_path, [encoded for _, encoded in ready], args.name)
        else:
            saved = core_data_insert(command, db_path, model, [
                {
                    "searchText": args.name,
                    "representations": [
                        dict((field, rep[field]) for field in REP_FIELDS)
                        for rep in encoded["representations"]
                    ],
                }
                for _, encoded in ready
            ], work_dir)
            rows = [{"pk": item["primaryKey"], "uri": item["uri"]} for item in saved]
        added = [(source, encoded, row) for (source, encoded), row in zip(ready, rows)]
    finally:
        shutil.rmtree(str(work_dir), ignore_errors=True)

    for source, encoded, row in added:
        note = "  (still heavy; may not send, try a smaller --max-edge)" if encoded["overBudget"] else ""
        print("added  {}: {} -> sticker #{}{}".format(source.name, describe(encoded), row["pk"], note))
    if failures:
        print("{} of {} files were not imported (see FAILED above).".format(len(failures), len(files)))
    print("Backup of the library before this import: {}".format(backup))
    if args.no_restart:
        print("stickersd was not restarted. Run `killall stickersd` or reopen Messages to see them.")
    elif restart_stickersd():
        print("Restarted stickersd. Open the sticker drawer in Messages to use them.")
    return 1 if failures else 0


def _cocoa_date(value):
    if value is None:
        return "-"
    try:
        moment = datetime.datetime.fromtimestamp(float(value) + COCOA_EPOCH)
    except (ValueError, OverflowError, OSError):
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M")


def cmd_list(args):
    db_path = find_db(args.db)
    conn = connect(db_path, readonly=True)
    try:
        sticker_cols = table_info(conn, STICKER_TABLE)
        rep_cols = table_info(conn, REP_TABLE)
        if not sticker_cols or not rep_cols:
            raise ToolError("This does not look like a sticker database: {}".format(db_path))
        stickers = conn.execute(
            "select * from {} order by Z_PK".format(STICKER_TABLE)).fetchall()
        rep_fields = ", ".join(name for name in rep_cols if name != "ZDATA")
        reps = conn.execute(
            "select {}, length(ZDATA) as DATA_LEN, substr(ZDATA, 1, 16) as DATA_HEAD "
            "from {} order by ZSTICKER, Z_PK".format(rep_fields, REP_TABLE)).fetchall()
    finally:
        conn.close()

    by_sticker = {}
    for rep in reps:
        by_sticker.setdefault(rep["ZSTICKER"], []).append(rep)

    print("{}  ({} stickers)".format(db_path, len(stickers)))
    for sticker in stickers:
        keys = sticker.keys()

        def field(name):
            return sticker[name] if name in keys else None

        kind = "user" if field("ZTYPE") == USER_STICKER_TYPE else "type {}".format(field("ZTYPE"))
        parts = ["#{}".format(sticker["Z_PK"]), kind, _cocoa_date(field("ZCREATIONDATE"))]
        for name in ("ZSEARCHTEXT", "ZSANITIZEDPROMPT", "ZATTRIBUTIONBUNDLEIDENTIFIER"):
            if field(name):
                parts.append('"{}"'.format(field(name)))
        if field("ZEFFECT"):
            parts.append("effect {}".format(field("ZEFFECT")))
        mine = by_sticker.get(sticker["Z_PK"], [])
        if not args.verbose:
            roles = [str(rep["ZROLE"]).rsplit(".", 1)[-1] for rep in mine]
            parts.append("[{}]".format(", ".join(roles)))
        print("  ".join(parts))
        if not args.verbose:
            continue
        if field("ZEXTERNALURI"):
            print("      {}".format(field("ZEXTERNALURI")))
        for rep in mine:
            rep_keys = rep.keys()
            head = bytes(rep["DATA_HEAD"] or b"")
            flags = []
            if "ZISPREFERRED" in rep_keys and rep["ZISPREFERRED"]:
                flags.append("preferred")
            if _strip_marker(head)[1]:
                flags.append("0x01 marker")
            elif head and not _looks_like_image(head):
                flags.append("data starts {}".format(head[:8].hex()))
            print("      [{}] {:<34} {:<13} {:>4}x{:<4} {:>10}  {}".format(
                rep["ZINDEX"] if "ZINDEX" in rep_keys else "-",
                str(rep["ZROLE"]).replace("com.apple.stickers.role.", ""),
                str(rep["ZUTI"]),
                int(rep["ZSIZE_W"] or 0) if "ZSIZE_W" in rep_keys else 0,
                int(rep["ZSIZE_H"] or 0) if "ZSIZE_H" in rep_keys else 0,
                _kb(rep["DATA_LEN"] or 0),
                ", ".join(flags)).rstrip())
    return 0


def cmd_export(args):
    db_path = find_db(args.db)
    out_dir = Path(args.outdir).expanduser()
    conn = connect(db_path, readonly=True)
    try:
        rep_cols = table_info(conn, REP_TABLE)
        missing = [name for name in ("ZSTICKER", "ZROLE", "ZUTI", "ZDATA") if name not in rep_cols]
        if missing:
            raise ToolError("This does not look like a sticker database: {}".format(db_path))
        order = "ZINDEX" if "ZINDEX" in rep_cols else "Z_PK"
        sql = "select Z_PK, ZSTICKER, ZROLE, ZUTI, ZDATA, {} as POSITION from {}".format(order, REP_TABLE)
        params = []
        if args.sticker is not None:
            sql += " where ZSTICKER = ?"
            params.append(args.sticker)
        rows = conn.execute(sql + " order by ZSTICKER, Z_PK", params).fetchall()
    finally:
        conn.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in rows:
        if row["ZDATA"] is None:
            continue
        data = _strip_marker(bytes(row["ZDATA"]))[0]
        role = re.sub(r"[^A-Za-z0-9_-]", "_", str(row["ZROLE"]).rsplit(".", 1)[-1])
        name = "sticker{}_{}_{}{}".format(
            row["ZSTICKER"], row["POSITION"], role, EXTENSIONS.get(row["ZUTI"], ".bin"))
        (out_dir / name).write_bytes(data)
        written += 1
    print("Exported {} image file(s) to {}".format(written, out_dir))
    return 0


def cmd_restore(args):
    db_path = find_db(args.db)
    if args.backup:
        chosen = Path(args.backup).expanduser()
    else:
        available = sorted(backup_dir().glob("stickers.stickerdb.*.backup"))
        if not available:
            raise ToolError("No backups found in {}".format(backup_dir()))
        chosen = available[-1]
    if not chosen.is_file():
        raise ToolError("No backup at {}".format(chosen))

    print("This replaces the sticker library\n  {}\nwith the backup\n  {}".format(db_path, chosen))
    if not args.yes:
        try:
            answer = input("Stickers added since that backup will be gone. Continue? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "y":
            print("Left the library alone.")
            return 1
    safety = backup_db(db_path, backup_dir())
    restart_stickersd()
    source = sqlite3.connect(str(chosen))
    destination = sqlite3.connect(str(db_path), timeout=10)
    try:
        source.backup(destination)
    except sqlite3.Error as exc:
        raise ToolError("Restore failed: {}".format(exc))
    finally:
        destination.close()
        source.close()
    restart_stickersd()
    print("Restored. The library as it was a moment ago is saved at {}".format(safety))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="imsticker",
        description="Add PNG / GIF / APNG files to the native iMessage sticker library "
                    "exactly as they are (no subject cut-out).")
    parser.add_argument("--db", help="path to stickers.stickerdb (default: find the live one)")
    commands = parser.add_subparsers(dest="command")

    add = commands.add_parser("add", help="import one or more images as stickers")
    add.add_argument("files", nargs="+", help="PNG, GIF or APNG files")
    add.add_argument("--name", help="search text for the sticker (single file only)")
    add.add_argument("--max-edge", type=int, default=768, help="longest side in pixels (default 768)")
    add.add_argument("--max-bytes", type=int, default=1000000,
                     help="shrink until the sticker is under this size; 0 turns it off (default 1000000)")
    add.add_argument("--no-upscale", action="store_true", help="never enlarge small images")
    add.add_argument("--resample", choices=("auto", "nearest", "smooth"), default="auto",
                     help="nearest keeps pixel art crisp (default: auto)")
    add.add_argument("--raw-sql", action="store_true",
                     help="write the database rows directly instead of saving through Core Data "
                          "(works without Apple's model, but the change is not recorded in the "
                          "store's history, which iCloud sync reads)")
    add.add_argument("--no-restart", action="store_true", help="do not restart stickersd afterwards")
    add.add_argument("--dry-run", action="store_true", help="encode and check, but change nothing")
    add.set_defaults(run=cmd_add)

    listing = commands.add_parser("list", help="show what is in the sticker library")
    listing.add_argument("-v", "--verbose", action="store_true", help="show every stored image")
    listing.set_defaults(run=cmd_list)

    export = commands.add_parser("export", help="write the stored images out as files")
    export.add_argument("outdir", help="folder to write into")
    export.add_argument("--sticker", type=int, help="only this sticker number (see list)")
    export.set_defaults(run=cmd_export)

    restore = commands.add_parser("restore", help="put back a backup made before an import")
    restore.add_argument("backup", nargs="?", help="backup file (default: the newest one)")
    restore.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    restore.set_defaults(run=cmd_restore)
    return parser


def with_default_command(argv):
    """`imsticker.py cat.png` means `imsticker.py add cat.png`."""
    start = 0
    while start < len(argv):
        if argv[start] == "--db":
            start += 2
        elif argv[start].startswith("--db="):
            start += 1
        else:
            break
    rest = argv[start:]
    first = next((item for item in rest if not item.startswith("-")), None)
    if first is None or first in COMMANDS or "-h" in rest or "--help" in rest:
        return argv
    return argv[:start] + ["add"] + rest


def main(argv=None):
    argv = with_default_command(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "run", None):
        parser.print_help()
        return 2
    if getattr(args, "max_edge", 2) < 2:
        parser.error("--max-edge must be 2 or more")
    try:
        return args.run(args)
    except ToolError as exc:
        print("imsticker: {}".format(exc), file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print("imsticker: database error: {}".format(exc), file=sys.stderr)
        return 1


# The helper, in Swift because both jobs need Apple's frameworks.
#
# Encoding: it decodes with ImageIO (which already composites GIF and APNG
# frames), draws each frame untouched into an RGBA canvas, and writes HEIC for
# stills or an HEIF image sequence for animations.
#
# Saving ("insert"): it opens the sticker store with Core Data and the model
# Apple ships, and saves the stickers as managed objects. Core Data then writes
# the rows, the keys and the persistent history itself.
SWIFT_HELPER = r'''
import Foundation
import ImageIO
import CoreGraphics
import CoreData

struct Representation: Encodable {
    let role: String
    let uti: String
    let width: Int
    let height: Int
    let byteCount: Int
    let preferred: Bool
    let index: Int
    let path: String
}

struct Output: Encodable {
    let mode: String
    let sourceWidth: Int
    let sourceHeight: Int
    let frameCount: Int
    let resample: String
    let representations: [Representation]
}

struct Options {
    var input = ""
    var outputDir = ""
    var maxEdge = 768
    var upscale = true
    var resample = "auto"
}

struct StillSpec {
    let role: String
    let edge: Int
    let preferred: Bool
    let upscale: Bool
}

func die(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func parseOptions(_ arguments: [String]) -> Options {
    var options = Options()
    var index = 1
    while index < arguments.count {
        let argument = arguments[index]
        if argument == "--no-upscale" {
            options.upscale = false
            index += 1
            continue
        }
        guard index + 1 < arguments.count else { die("missing value for \(argument)") }
        let value = arguments[index + 1]
        switch argument {
        case "--input":
            options.input = value
        case "--output-dir":
            options.outputDir = value
        case "--max-edge":
            guard let edge = Int(value), edge >= 2 else { die("--max-edge must be an integer of 2 or more") }
            options.maxEdge = edge
        case "--resample":
            guard ["auto", "nearest", "smooth"].contains(value) else { die("--resample must be auto, nearest or smooth") }
            options.resample = value
        default:
            die("unknown argument: \(argument)")
        }
        index += 2
    }
    if options.input.isEmpty || options.outputDir.isEmpty {
        die("--input and --output-dir are required")
    }
    return options
}

func fitSize(width: Int, height: Int, maxEdge: Int, upscale: Bool) -> (Int, Int) {
    var scale = Double(maxEdge) / Double(max(width, height))
    if !upscale && scale > 1.0 {
        scale = 1.0
    }
    let fittedWidth = max(1, Int((Double(width) * scale).rounded()))
    let fittedHeight = max(1, Int((Double(height) * scale).rounded()))
    return (fittedWidth, fittedHeight)
}

// Draws the whole frame, alpha included, into a clear canvas. Nothing is masked.
func render(_ image: CGImage, width: Int, height: Int, smooth: Bool) -> CGImage {
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
        die("cannot create the sRGB color space")
    }
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        die("cannot create a bitmap context")
    }
    let bounds = CGRect(x: 0, y: 0, width: width, height: height)
    context.clear(bounds)
    context.interpolationQuality = smooth ? .high : .none
    context.draw(image, in: bounds)
    guard let rendered = context.makeImage() else {
        die("cannot render the image")
    }
    return rendered
}

func byteCount(_ url: URL) -> Int {
    guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
          let size = attributes[.size] as? NSNumber else {
        die("cannot read the size of \(url.path)")
    }
    return size.intValue
}

// Per-frame delay in seconds, from whichever container the source is.
func frameDelay(_ source: CGImageSource, _ index: Int) -> Double {
    guard let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any] else {
        return 0.1
    }
    let containers: [(CFString, CFString, CFString)] = [
        (kCGImagePropertyGIFDictionary, kCGImagePropertyGIFUnclampedDelayTime, kCGImagePropertyGIFDelayTime),
        (kCGImagePropertyPNGDictionary, kCGImagePropertyAPNGUnclampedDelayTime, kCGImagePropertyAPNGDelayTime),
        (kCGImagePropertyHEICSDictionary, kCGImagePropertyHEICSUnclampedDelayTime, kCGImagePropertyHEICSDelayTime)
    ]
    for (dictionaryKey, unclampedKey, clampedKey) in containers {
        guard let dictionary = properties[dictionaryKey] as? [CFString: Any] else { continue }
        if let unclamped = dictionary[unclampedKey] as? Double, unclamped >= 0.01 {
            return unclamped
        }
        if let clamped = dictionary[clampedKey] as? Double, clamped >= 0.01 {
            return clamped
        }
    }
    return 0.1
}

func encodeStill(source: CGImageSource, options: Options, outputDir: URL) -> Output {
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        die("cannot decode the image")
    }
    let smooth = options.resample != "nearest"
    let specs: [StillSpec] = [
        StillSpec(role: "com.apple.stickers.role.still", edge: options.maxEdge, preferred: true, upscale: true),
        StillSpec(role: "com.apple.stickers.role.keyboard", edge: 100, preferred: false, upscale: false),
        StillSpec(role: "com.apple.stickers.role.stillVariant_320", edge: max(options.maxEdge, 320), preferred: false, upscale: true),
        StillSpec(role: "com.apple.stickers.role.stillVariant_160", edge: 160, preferred: false, upscale: false),
        StillSpec(role: "com.apple.stickers.role.stillVariant_96", edge: 96, preferred: false, upscale: false),
        StillSpec(role: "com.apple.stickers.role.stillVariant_64", edge: 64, preferred: false, upscale: false),
        StillSpec(role: "com.apple.stickers.role.stillVariant_40", edge: 40, preferred: false, upscale: false)
    ]
    var representations: [Representation] = []
    for (index, spec) in specs.enumerated() {
        let (width, height) = fitSize(
            width: image.width,
            height: image.height,
            maxEdge: spec.edge,
            upscale: spec.upscale && options.upscale
        )
        let rendered = render(image, width: width, height: height, smooth: smooth)
        let url = outputDir.appendingPathComponent("rep-\(index).heic")
        guard let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.heic" as CFString, 1, nil) else {
            die("cannot create an HEIC writer")
        }
        let properties: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 0.9]
        CGImageDestinationAddImage(destination, rendered, properties as CFDictionary)
        if !CGImageDestinationFinalize(destination) {
            die("failed to write \(url.path)")
        }
        representations.append(Representation(
            role: spec.role,
            uti: "public.heic",
            width: width,
            height: height,
            byteCount: byteCount(url),
            preferred: spec.preferred,
            index: index,
            path: url.path
        ))
    }
    return Output(
        mode: "static",
        sourceWidth: image.width,
        sourceHeight: image.height,
        frameCount: 1,
        resample: smooth ? "smooth" : "nearest",
        representations: representations
    )
}

func encodeAnimated(source: CGImageSource, frameCount: Int, isGIF: Bool, options: Options, outputDir: URL) -> Output {
    guard let first = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        die("cannot decode the first frame")
    }
    var smooth = options.resample != "nearest"
    if options.resample == "auto" && isGIF && max(first.width, first.height) <= 256 {
        smooth = false
    }
    let (fittedWidth, fittedHeight) = fitSize(width: first.width, height: first.height, maxEdge: options.maxEdge, upscale: options.upscale)
    // The sequence is HEVC video underneath, so keep both sides even.
    let width = max(2, fittedWidth - fittedWidth % 2)
    let height = max(2, fittedHeight - fittedHeight % 2)
    // Which source frame goes where, and for how long.
    var frames: [Int] = []
    var delays: [Double] = []
    for index in 0..<frameCount {
        frames.append(index)
        delays.append(frameDelay(source, index))
    }
    // An HEIF sequence written by ImageIO plays its last frame for as long as
    // the one before it, whatever delay was asked for. Where those two differ,
    // write the last picture twice at half its delay each: the copy then
    // inherits the right half, and the picture is on screen for the right time.
    if frameCount >= 2 && abs(delays[frameCount - 1] - delays[frameCount - 2]) > 0.0005 {
        let half = max(delays[frameCount - 1] / 2, 0.005)
        delays[frameCount - 1] = half
        delays.append(half)
        frames.append(frameCount - 1)
    }

    let url = outputDir.appendingPathComponent("animated.heics")
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, "public.heics" as CFString, frames.count, nil) else {
        die("cannot create an HEIF image sequence writer")
    }
    let sequenceProperties: [CFString: Any] = [
        kCGImagePropertyHEICSDictionary: [kCGImagePropertyHEICSLoopCount: 0],
        kCGImageDestinationLossyCompressionQuality: 0.85
    ]
    CGImageDestinationSetProperties(destination, sequenceProperties as CFDictionary)
    for (position, index) in frames.enumerated() {
        guard let frame = CGImageSourceCreateImageAtIndex(source, index, nil) else {
            die("cannot decode frame \(index)")
        }
        let rendered = render(frame, width: width, height: height, smooth: smooth)
        let frameProperties: [CFString: Any] = [
            kCGImagePropertyHEICSDictionary: [kCGImagePropertyHEICSDelayTime: delays[position]],
            kCGImageDestinationLossyCompressionQuality: 0.85
        ]
        CGImageDestinationAddImage(destination, rendered, frameProperties as CFDictionary)
    }
    if !CGImageDestinationFinalize(destination) {
        die("failed to write \(url.path)")
    }
    return Output(
        mode: "animated",
        sourceWidth: first.width,
        sourceHeight: first.height,
        frameCount: frameCount,
        resample: smooth ? "smooth" : "nearest",
        representations: [
            Representation(
                role: "com.apple.stickers.role.animated",
                uti: "public.heics",
                width: width,
                height: height,
                byteCount: byteCount(url),
                preferred: true,
                index: 2,
                path: url.path
            )
        ]
    )
}

func printJSON<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    guard let json = try? encoder.encode(value) else {
        die("cannot serialize the result")
    }
    FileHandle.standardOutput.write(json)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func runEncode(_ arguments: [String]) -> Never {
    let options = parseOptions(arguments)
    let inputURL = URL(fileURLWithPath: options.input)
    let outputURL = URL(fileURLWithPath: options.outputDir, isDirectory: true)
    try? FileManager.default.createDirectory(at: outputURL, withIntermediateDirectories: true)

    guard let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil) else {
        die("cannot open image: \(options.input)")
    }
    let frameCount = CGImageSourceGetCount(source)
    if frameCount < 1 {
        die("no image found in \(options.input)")
    }
    if frameCount > 1 {
        printJSON(encodeAnimated(
            source: source,
            frameCount: frameCount,
            isGIF: inputURL.pathExtension.lowercased() == "gif",
            options: options,
            outputDir: outputURL
        ))
    } else {
        printJSON(encodeStill(source: source, options: options, outputDir: outputURL))
    }
    exit(0)
}

// ---- saving through Core Data ----

struct ManifestRepresentation: Decodable {
    let path: String
    let role: String
    let uti: String
    let width: Double
    let height: Double
    let preferred: Bool
    let index: Int
}

struct ManifestSticker: Decodable {
    let searchText: String?
    let representations: [ManifestRepresentation]
}

struct Manifest: Decodable {
    let stickers: [ManifestSticker]
}

struct SavedSticker: Encodable {
    let identifier: String
    let uri: String
    let primaryKey: Int
    let libraryIndex: Double
}

struct SaveResult: Encodable {
    let stickers: [SavedSticker]
}

struct PendingSticker {
    let object: NSManagedObject
    let identifier: UUID
    let libraryIndex: Double
}

// What the Core Data callbacks report back.
final class SaveState {
    var loadError: Error? = nil
    var saved: [SavedSticker] = []
    var failure: String? = nil
}

func runInsert(_ arguments: [String]) -> Never {
    var storePath = ""
    var modelPath = ""
    var manifestPath = ""
    var mayCreate = false
    var index = 0
    while index < arguments.count {
        let argument = arguments[index]
        if argument == "--create" {
            mayCreate = true
            index += 1
            continue
        }
        guard index + 1 < arguments.count else { die("missing value for \(argument)") }
        let value = arguments[index + 1]
        switch argument {
        case "--store":
            storePath = value
        case "--model":
            modelPath = value
        case "--manifest":
            manifestPath = value
        default:
            die("unknown argument: \(argument)")
        }
        index += 2
    }
    if storePath.isEmpty || modelPath.isEmpty || manifestPath.isEmpty {
        die("insert needs --store, --model and --manifest")
    }
    guard let manifestData = FileManager.default.contents(atPath: manifestPath),
          let manifest = try? JSONDecoder().decode(Manifest.self, from: manifestData) else {
        die("cannot read the manifest at \(manifestPath)")
    }
    // Read every image before the store is touched.
    var images: [[Data]] = []
    for sticker in manifest.stickers {
        var loaded: [Data] = []
        for representation in sticker.representations {
            guard let data = FileManager.default.contents(atPath: representation.path), !data.isEmpty else {
                die("cannot read \(representation.path)")
            }
            loaded.append(data)
        }
        images.append(loaded)
    }
    if !mayCreate && !FileManager.default.fileExists(atPath: storePath) {
        die("there is no sticker store at \(storePath)")
    }
    guard let model = NSManagedObjectModel(contentsOf: URL(fileURLWithPath: modelPath)) else {
        die("cannot load Apple's sticker model at \(modelPath)")
    }

    let container = NSPersistentContainer(name: "Stickers", managedObjectModel: model)
    let storeDescription = NSPersistentStoreDescription(url: URL(fileURLWithPath: storePath))
    storeDescription.type = NSSQLiteStoreType
    storeDescription.shouldAddStoreAsynchronously = false
    // Never change the store's layout. If Apple's model does not fit it, stop.
    storeDescription.shouldMigrateStoreAutomatically = false
    storeDescription.shouldInferMappingModelAutomatically = false
    // The store records its history; opened without this it would be read-only.
    storeDescription.setOption(NSNumber(value: true), forKey: NSPersistentHistoryTrackingKey)
    storeDescription.setOption(NSNumber(value: true), forKey: NSPersistentStoreRemoteChangeNotificationPostOptionKey)
    container.persistentStoreDescriptions = [storeDescription]
    let state = SaveState()
    container.loadPersistentStores { _, error in
        state.loadError = error
    }
    if let loadError = state.loadError {
        die("Apple's model did not open the sticker store: \(loadError)")
    }

    let context = container.newBackgroundContext()
    context.transactionAuthor = "imsticker"
    context.performAndWait {
        do {
            let request = NSFetchRequest<NSManagedObject>(entityName: "ManagedSticker")
            request.predicate = NSPredicate(format: "type == 1")
            request.sortDescriptors = [NSSortDescriptor(key: "libraryIndex", ascending: false)]
            request.fetchLimit = 1
            var topIndex = 0.0
            if let first = try context.fetch(request).first, let value = first.value(forKey: "libraryIndex") as? Double {
                topIndex = value
            }
            let now = Date().timeIntervalSinceReferenceDate
            var pending: [PendingSticker] = []
            for (position, item) in manifest.stickers.enumerated() {
                let identifier = UUID()
                let libraryIndex = topIndex + 1024.0 * Double(position + 1)
                var totalBytes = 0
                for data in images[position] {
                    totalBytes += data.count
                }
                let sticker = NSEntityDescription.insertNewObject(forEntityName: "ManagedSticker", into: context)
                sticker.setValue(identifier, forKey: "identifier")
                sticker.setValue(NSNumber(value: Int32(1)), forKey: "type")
                sticker.setValue(NSNumber(value: Int32(0)), forKey: "effect")
                sticker.setValue(NSNumber(value: Int32(1)), forKey: "version")
                sticker.setValue(NSNumber(value: Int32(totalBytes)), forKey: "byteCount")
                sticker.setValue(NSNumber(value: now), forKey: "creationDate")
                sticker.setValue(NSNumber(value: now), forKey: "lastUsedDate")
                sticker.setValue(NSNumber(value: libraryIndex), forKey: "libraryIndex")
                sticker.setValue("sticker:///user/identifier/\(identifier.uuidString)", forKey: "externalURI")
                sticker.setValue("", forKey: "name")
                if let text = item.searchText, !text.isEmpty {
                    sticker.setValue(text, forKey: "searchText")
                }
                for (slot, representation) in item.representations.enumerated() {
                    let data = images[position][slot]
                    let object = NSEntityDescription.insertNewObject(forEntityName: "ManagedRepresentation", into: context)
                    object.setValue(UUID(), forKey: "identifier")
                    object.setValue(data, forKey: "data")
                    object.setValue(representation.uti, forKey: "uti")
                    object.setValue(representation.role, forKey: "role")
                    object.setValue(NSNumber(value: representation.width), forKey: "size_w")
                    object.setValue(NSNumber(value: representation.height), forKey: "size_h")
                    object.setValue(NSNumber(value: representation.preferred), forKey: "isPreferred")
                    object.setValue(NSNumber(value: Int32(representation.index)), forKey: "index")
                    object.setValue(NSNumber(value: Int32(data.count)), forKey: "byteCount")
                    object.setValue(NSNumber(value: Int32(1)), forKey: "version")
                    object.setValue(sticker, forKey: "sticker")
                }
                pending.append(PendingSticker(object: sticker, identifier: identifier, libraryIndex: libraryIndex))
            }
            if !pending.isEmpty {
                try context.save()
            }
            for item in pending {
                // After the save the object has its permanent key: .../ManagedSticker/p12
                let tail = item.object.objectID.uriRepresentation().lastPathComponent
                let key = Int(tail.dropFirst()) ?? 0
                state.saved.append(SavedSticker(
                    identifier: item.identifier.uuidString,
                    uri: "sticker:///user/identifier/\(item.identifier.uuidString)",
                    primaryKey: key,
                    libraryIndex: item.libraryIndex
                ))
            }
        } catch {
            state.failure = "\(error)"
            context.rollback()
        }
    }
    if let failure = state.failure {
        die("the stickers were not saved: \(failure)")
    }
    printJSON(SaveResult(stickers: state.saved))
    exit(0)
}

if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "insert" {
    runInsert(Array(CommandLine.arguments.dropFirst(2)))
} else {
    runEncode(CommandLine.arguments)
}
'''


if __name__ == "__main__":
    sys.exit(main())

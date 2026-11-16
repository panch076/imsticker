"""Tests for imsticker's database logic.

They run anywhere: the sticker store is a stand-in built with the column layout
StickerHack's source and its issue #2 document for macOS 15.7 and 15.1, and the
ImageIO encoder is replaced by tests/fake_encoder.py. What they cannot cover is
the real Swift encoder and a real stickersd, which need a Mac.

    python -m unittest discover -s tests -v
"""
import ast
import contextlib
import io
import json
import os
import shlex
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imsticker  # noqa: E402

HEIC = b"\x00\x00\x00\x18ftypheic"
ROLES = [
    "com.apple.stickers.role.still",
    "com.apple.stickers.role.keyboard",
    "com.apple.stickers.role.stillVariant_320",
    "com.apple.stickers.role.stillVariant_160",
    "com.apple.stickers.role.stillVariant_96",
    "com.apple.stickers.role.stillVariant_64",
    "com.apple.stickers.role.stillVariant_40",
]


def make_store(path, prompt_columns=True, extra_column="", sticker_ent=2, rep_ent=1):
    """Build an empty store shaped like the Core Data one stickersd keeps."""
    prompt = "ZPROMPTPRIMARYLANGUAGEIDENTIFIER VARCHAR, ZSANITIZEDPROMPT VARCHAR," if prompt_columns else ""
    extra = extra_column + "," if extra_column else ""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        create table ZMANAGEDSTICKER (
            Z_PK integer primary key, Z_ENT integer, Z_OPT integer,
            ZATTRIBUTIONADAMID integer, ZBYTECOUNT integer, ZEFFECT integer, ZTYPE integer,
            ZVERSION integer, ZCREATIONDATE timestamp, ZLASTUSEDDATE timestamp,
            ZLIBRARYINDEX integer, ZACCESSIBILITYNAME varchar,
            ZATTRIBUTIONBUNDLEIDENTIFIER varchar, ZATTRIBUTIONNAME varchar,
            ZEXTERNALURI varchar, ZNAME varchar, {prompt} {extra}
            ZSEARCHTEXT varchar, ZIDENTIFIER blob, ZMETADATA blob);
        create table ZMANAGEDREPRESENTATION (
            Z_PK integer primary key, Z_ENT integer, Z_OPT integer, ZBYTECOUNT integer,
            ZINDEX integer, ZISPREFERRED integer, ZVERSION integer, ZSTICKER integer,
            ZSIZE_H float, ZSIZE_W float, ZROLE varchar, ZUTI varchar,
            ZIDENTIFIER blob, ZDATA blob);
        create table Z_PRIMARYKEY (
            Z_ENT integer primary key, Z_NAME varchar, Z_SUPER integer, Z_MAX integer);
    """.format(prompt=prompt, extra=extra))
    conn.execute("insert into Z_PRIMARYKEY values (?, 'ManagedRepresentation', 0, 0)", (rep_ent,))
    conn.execute("insert into Z_PRIMARYKEY values (?, 'ManagedSticker', 0, 0)", (sticker_ent,))
    conn.commit()
    conn.close()


def seed_sticker(path, pk, library_index, marker=False, kind=1):
    """Put one pre-existing sticker with a single image into the store."""
    data = (b"\x01" if marker else b"") + HEIC + b"seed"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "insert into ZMANAGEDSTICKER (Z_PK, Z_ENT, Z_OPT, ZTYPE, ZLIBRARYINDEX, ZIDENTIFIER) "
        "values (?, 2, 1, ?, ?, ?)", (pk, kind, library_index, uuid.uuid4().bytes))
    conn.execute(
        "insert into ZMANAGEDREPRESENTATION (Z_PK, Z_ENT, Z_OPT, ZINDEX, ZSTICKER, ZROLE, ZUTI, ZDATA) "
        "values (?, 1, 1, 0, ?, 'com.apple.stickers.role.still', 'public.heic', ?)", (pk, pk, data))
    conn.execute("update Z_PRIMARYKEY set Z_MAX = max(Z_MAX, ?)", (pk,))
    conn.commit()
    conn.close()


def rows(path, sql, params=()):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class StoreCase(unittest.TestCase):
    # Most tests are about the rows, so they use the route that writes them
    # with SQL. CoreDataRoute below covers the default route's own logic.
    raw_sql = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "stickers.stickerdb"
        self.backups = self.dir / "backups"
        self.model = self.dir / "Stickers.momd"
        self.model.mkdir()
        env = mock.patch.dict(os.environ, {
            "IMSTICKER_ENCODER": json.dumps([sys.executable, str(ROOT / "tests" / "fake_encoder.py")]),
            "IMSTICKER_BACKUP_DIR": str(self.backups),
            "IMSTICKER_MODEL": str(self.model),
        })
        env.start()
        self.addCleanup(env.stop)
        for name in ("IMSTICKER_DB", "FAKE_BAD_BRAND", "FAKE_GARBAGE", "FAKE_INSERT_FAIL"):
            os.environ.pop(name, None)
        # On a Mac the tool restarts stickersd after a change. Tests must not.
        restart = mock.patch.object(imsticker, "restart_stickersd", return_value=False)
        restart.start()
        self.addCleanup(restart.stop)

    def source(self, name):
        path = self.dir / name
        path.write_bytes(b"not a real image; the fake encoder only looks at the name")
        return str(path)

    def cli(self, *argv):
        argv = list(argv)
        if self.raw_sql and argv[:1] == ["add"]:
            argv.insert(1, "--raw-sql")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = imsticker.main(["--db", str(self.db)] + argv)
        return code, out.getvalue(), err.getvalue()

    def counts(self):
        return (
            rows(self.db, "select count(*) from ZMANAGEDSTICKER")[0][0],
            rows(self.db, "select count(*) from ZMANAGEDREPRESENTATION")[0][0],
        )


class AddStatic(StoreCase):
    def test_writes_one_sticker_with_seven_images(self):
        make_store(self.db)
        before = time.time() - imsticker.COCOA_EPOCH
        code, out, err = self.cli("add", self.source("cat.png"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("added  cat.png: static 768x768", out)

        sticker = rows(self.db, "select * from ZMANAGEDSTICKER")
        self.assertEqual(len(sticker), 1)
        sticker = sticker[0]
        self.assertEqual(
            (sticker["Z_PK"], sticker["Z_ENT"], sticker["Z_OPT"], sticker["ZTYPE"],
             sticker["ZEFFECT"], sticker["ZVERSION"], sticker["ZATTRIBUTIONADAMID"],
             sticker["ZNAME"], sticker["ZLIBRARYINDEX"]),
            (1, 2, 1, 1, 0, 1, 0, "", 1024))
        for empty in ("ZSEARCHTEXT", "ZSANITIZEDPROMPT", "ZMETADATA", "ZATTRIBUTIONBUNDLEIDENTIFIER"):
            self.assertIsNone(sticker[empty])
        self.assertAlmostEqual(sticker["ZCREATIONDATE"], before, delta=60)
        self.assertEqual(sticker["ZCREATIONDATE"], sticker["ZLASTUSEDDATE"])

        identifier = uuid.UUID(bytes=bytes(sticker["ZIDENTIFIER"]))
        self.assertEqual(sticker["ZEXTERNALURI"], "sticker:///user/identifier/" + str(identifier).upper())

        reps = rows(self.db, "select * from ZMANAGEDREPRESENTATION order by Z_PK")
        self.assertEqual([rep["ZROLE"] for rep in reps], ROLES)
        self.assertEqual([rep["ZINDEX"] for rep in reps], list(range(7)))
        self.assertEqual([rep["Z_PK"] for rep in reps], list(range(1, 8)))
        self.assertEqual([rep["ZISPREFERRED"] for rep in reps], [1, 0, 0, 0, 0, 0, 0])
        self.assertEqual([rep["ZSIZE_W"] for rep in reps], [768.0, 100.0, 768.0, 160.0, 96.0, 64.0, 40.0])
        for position, rep in enumerate(reps):
            data = bytes(rep["ZDATA"])
            self.assertEqual(data, HEIC + bytes([position]) * (int(rep["ZSIZE_W"]) * 3))
            self.assertEqual(rep["ZBYTECOUNT"], len(data))
            self.assertEqual((rep["Z_ENT"], rep["ZSTICKER"], rep["ZUTI"]), (1, 1, "public.heic"))
            self.assertEqual(len(bytes(rep["ZIDENTIFIER"])), 16)
        self.assertEqual(sticker["ZBYTECOUNT"], sum(rep["ZBYTECOUNT"] for rep in reps))

        tracked = dict(rows(self.db, "select Z_NAME, Z_MAX from Z_PRIMARYKEY"))
        self.assertEqual(tracked, {"ManagedSticker": 1, "ManagedRepresentation": 7})

    def test_store_without_prompt_columns(self):
        # macOS 15.1 layout: StickerHack's fixed insert fails here (its issue #2).
        make_store(self.db, prompt_columns=False)
        code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.counts(), (1, 7))

    def test_unknown_optional_column_is_left_alone(self):
        make_store(self.db, extra_column="ZFUTURE varchar")
        code, out, _ = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 0)
        self.assertIn("leaving columns new to this tool at their defaults: ZFUTURE", out)
        self.assertIsNone(rows(self.db, "select ZFUTURE from ZMANAGEDSTICKER")[0][0])

    def test_unknown_required_column_stops_before_any_change(self):
        make_store(self.db, extra_column="ZFUTURE integer not null")
        code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("required columns this tool does not know: ZFUTURE", err)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.backups.exists())

    def test_entity_numbers_are_read_from_the_store(self):
        make_store(self.db, sticker_ent=7, rep_ent=4)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        self.assertEqual(rows(self.db, "select distinct Z_ENT from ZMANAGEDSTICKER")[0][0], 7)
        self.assertEqual(rows(self.db, "select distinct Z_ENT from ZMANAGEDREPRESENTATION")[0][0], 4)

    def test_keys_and_order_continue_from_what_is_there(self):
        make_store(self.db)
        seed_sticker(self.db, 3, 4096)
        seed_sticker(self.db, 5, 9000, kind=2)  # not a user sticker: must not set the order
        conn = sqlite3.connect(str(self.db))
        conn.execute("update Z_PRIMARYKEY set Z_MAX = 10 where Z_NAME = 'ManagedSticker'")
        conn.commit()
        conn.close()

        code, out, _ = self.cli("add", self.source("a.png"), self.source("b.png"))
        self.assertEqual(code, 0)
        added = rows(self.db, "select Z_PK, ZLIBRARYINDEX from ZMANAGEDSTICKER where Z_PK > 5 order by Z_PK")
        self.assertEqual([tuple(row) for row in added], [(11, 5120), (12, 6144)])
        self.assertIn("sticker #11", out)
        self.assertIn("sticker #12", out)
        owners = rows(self.db, "select ZSTICKER, count(*), min(Z_PK) from ZMANAGEDREPRESENTATION "
                               "where ZSTICKER > 5 group by ZSTICKER order by ZSTICKER")
        self.assertEqual([tuple(row) for row in owners], [(11, 7, 6), (12, 7, 13)])
        tracked = dict(rows(self.db, "select Z_NAME, Z_MAX from Z_PRIMARYKEY"))
        self.assertEqual(tracked, {"ManagedSticker": 12, "ManagedRepresentation": 19})

    def test_name_becomes_search_text(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"), "--name", "Troll")[0], 0)
        self.assertEqual(rows(self.db, "select ZSEARCHTEXT from ZMANAGEDSTICKER")[0][0], "Troll")

    def test_name_with_several_files_is_refused(self):
        make_store(self.db)
        code, _, err = self.cli("add", self.source("a.png"), self.source("b.png"), "--name", "x")
        self.assertEqual(code, 1)
        self.assertIn("--name names one sticker", err)
        self.assertEqual(self.counts(), (0, 0))

    def test_command_word_is_optional(self):
        make_store(self.db)
        self.assertEqual(self.cli(self.source("cat.png"), "--raw-sql")[0], 0)
        self.assertEqual(self.counts(), (1, 7))


class CoreDataRoute(StoreCase):
    """The default route hands the stickers to the helper to save. Here the
    helper is the stand-in, so this covers what imsticker itself does around it:
    checking the library first, the manifest it sends, and what it reports."""

    raw_sql = False

    def test_saves_through_the_helper(self):
        make_store(self.db)
        code, out, err = self.cli("add", self.source("cat.png"), self.source("anim.gif"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("added  cat.png: static 768x768", out)
        self.assertIn("-> sticker #1", out)
        self.assertIn("added  anim.gif: animated 448x448", out)
        self.assertIn("-> sticker #2", out)
        self.assertEqual(self.counts(), (2, 10))
        roles = [row[0] for row in rows(self.db, "select ZROLE from ZMANAGEDREPRESENTATION where ZSTICKER = 1 order by ZINDEX")]
        self.assertEqual(roles, ROLES)
        self.assertEqual(len(list(self.backups.glob("*.backup"))), 1)

    def test_name_reaches_the_helper(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"), "--name", "Troll")[0], 0)
        self.assertEqual(rows(self.db, "select ZSEARCHTEXT from ZMANAGEDSTICKER")[0][0], "Troll")

    def test_library_is_checked_before_anything_is_encoded_or_backed_up(self):
        make_store(self.db)
        with mock.patch.dict(os.environ, {"FAKE_INSERT_FAIL": "1"}):
            code, out, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("Core Data save failed: Apple's model did not open the sticker store", err)
        self.assertIn("Nothing was changed", err)
        self.assertIn("--raw-sql", err)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.backups.exists())

    def test_missing_model_points_to_the_other_route(self):
        make_store(self.db)
        with mock.patch.dict(os.environ, {"IMSTICKER_MODEL": str(self.dir / "nowhere.momd")}):
            code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("Apple's sticker model was not found", err)
        self.assertIn("--raw-sql", err)
        self.assertEqual(self.counts(), (0, 0))

    def test_dry_run_checks_the_library_but_changes_nothing(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("cat.png"), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would add  cat.png", out)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.backups.exists())

    def test_a_library_that_cannot_be_read_stops_before_the_helper_is_built(self):
        self.db.write_bytes(b"not a database at all, " * 60)
        with mock.patch.object(imsticker, "helper_command") as helper:
            code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("Cannot open", err)
        self.assertIn("Full Disk Access", err)
        helper.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_needs_a_mac(self):
        make_store(self.db)
        os.environ.pop("IMSTICKER_ENCODER")
        with mock.patch.object(imsticker.sys, "platform", "win32"):
            code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("needs a Mac", err)
        self.assertEqual(self.counts(), (0, 0))


class AddAnimated(StoreCase):
    def test_shrinks_until_under_the_budget(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("anim.gif"))
        self.assertEqual(code, 0)
        # Fake sizes: 768 -> 2.36 MB, 640 -> 1.64 MB, 512 -> 1.05 MB, 448 -> 0.80 MB.
        self.assertIn("animated 448x448", out)
        self.assertIn("12 frames", out)
        self.assertNotIn("still heavy", out)
        # The animation comes with a still of the first frame and a keyboard preview.
        reps = rows(self.db, "select * from ZMANAGEDREPRESENTATION order by ZINDEX")
        self.assertEqual(
            [(rep["ZROLE"].rsplit(".", 1)[-1], rep["ZUTI"], rep["ZINDEX"], rep["ZISPREFERRED"], rep["ZSIZE_W"]) for rep in reps],
            [("still", "public.heic", 0, 0, 448.0), ("keyboard", "public.heic", 1, 0, 100.0),
             ("animated", "public.heics", 2, 1, 448.0)])
        data = bytes(reps[2]["ZDATA"])
        self.assertEqual(data[4:12], b"ftypmsf1")
        self.assertEqual(len(data), 12 + 448 * 448 * 4)
        self.assertLessEqual(len(data), 1000000)

    def test_animation_alone_on_request(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("anim.gif"), "--animation-only")
        self.assertEqual(code, 0)
        reps = rows(self.db, "select ZROLE, ZINDEX, ZISPREFERRED from ZMANAGEDREPRESENTATION")
        self.assertEqual([tuple(rep) for rep in reps], [("com.apple.stickers.role.animated", 2, 1)])

    def test_budget_can_be_turned_off(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("anim.gif"), "--max-bytes", "0")
        self.assertEqual(code, 0)
        self.assertIn("animated 768x768", out)

    def test_says_so_when_it_cannot_get_under_the_budget(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("anim.gif"), "--no-upscale", "--max-bytes", "1000")
        self.assertEqual(code, 0)
        # It went all the way down to the smallest size it tries before giving up.
        self.assertIn("animated 128x128", out)
        self.assertIn("still heavy", out)

    def test_dry_run_also_says_when_it_is_still_heavy(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("anim.gif"), "--no-upscale", "--max-bytes", "1000", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would add  anim.gif: animated 128x128", out)
        self.assertIn("still heavy", out)
        self.assertEqual(self.counts(), (0, 0))

    def test_shrinks_below_the_source_size_when_not_enlarging(self):
        # The stand-in source is 200 px wide: 200 px is 160 KB, 192 px is 147 KB, 160 px is 102 KB.
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("anim.gif"), "--no-upscale", "--max-bytes", "120000")
        self.assertEqual(code, 0)
        self.assertIn("animated 160x160", out)
        self.assertNotIn("still heavy", out)

    def test_wrong_container_changes_nothing(self):
        make_store(self.db)
        with mock.patch.dict(os.environ, {"FAKE_BAD_BRAND": "1"}):
            code, _, err = self.cli("add", self.source("anim.gif"))
        self.assertEqual(code, 1)
        self.assertIn("not an HEIF image sequence", err)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.backups.exists())


class Failures(StoreCase):
    def test_one_bad_file_does_not_block_the_rest(self):
        make_store(self.db)
        code, out, err = self.cli("add", self.source("good.png"), self.source("broken.png"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED  broken.png", err)
        self.assertIn("added  good.png", out)
        self.assertEqual(self.counts(), (1, 7))

    def test_toolchain_fault_is_explained_not_dumped(self):
        make_store(self.db)
        code, _, err = self.cli("add", self.source("bridging.png"))
        self.assertEqual(code, 1)
        self.assertIn("known fault in some Command Line Tools installs", err)
        self.assertIn("(29 more lines)", err)
        self.assertLess(err.count("note: more"), 15)
        self.assertEqual(self.counts(), (0, 0))

    def test_unexpected_encoder_output_changes_nothing(self):
        make_store(self.db)
        with mock.patch.dict(os.environ, {"FAKE_GARBAGE": "1"}):
            code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("encoder result lacks", err)
        self.assertEqual(self.counts(), (0, 0))

    def test_missing_file(self):
        make_store(self.db)
        code, _, err = self.cli("add", str(self.dir / "nope.png"))
        self.assertEqual(code, 1)
        self.assertIn("File not found", err)

    def test_a_picture_macos_will_not_show_is_not_called_missing(self):
        make_store(self.db)
        picture = self.source("cat.png")
        with mock.patch.object(imsticker.os, "stat", side_effect=_refusing(picture)):
            code, _, err = self.cli("add", picture)
        self.assertEqual(code, 1)
        self.assertIn("did not allow access", err)
        self.assertIn("Full Disk Access", err)
        self.assertNotIn("File not found", err)
        self.assertEqual(self.counts(), (0, 0))

    def test_some_other_database_is_refused(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("create table notes (body text)")
        conn.commit()
        conn.close()
        code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("does not look like a sticker database", err)

    def test_dry_run_changes_nothing(self):
        make_store(self.db)
        code, out, _ = self.cli("add", self.source("cat.png"), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would add  cat.png", out)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.backups.exists())

    def test_needs_a_mac_without_the_test_encoder(self):
        make_store(self.db)
        os.environ.pop("IMSTICKER_ENCODER")
        with mock.patch.object(imsticker.sys, "platform", "win32"):
            code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("needs a Mac", err)
        self.assertEqual(self.counts(), (0, 0))


class BackupRestore(StoreCase):
    def test_round_trip(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        self.assertEqual(self.counts(), (2, 8))
        backups = sorted(self.backups.glob("stickers.stickerdb.*.backup"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(rows(backups[0], "select count(*) from ZMANAGEDSTICKER")[0][0], 1)

        code, out, _ = self.cli("restore", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.counts(), (1, 1))
        # The state just before the restore was kept too, under another name,
        # so it can be put back by hand but is never picked by a plain restore.
        self.assertEqual(len(list(self.backups.glob("*.backup"))), 1)
        kept = list(self.backups.glob("*.before-restore"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(rows(kept[0], "select count(*) from ZMANAGEDSTICKER")[0][0], 2)
        self.assertEqual(list(self.backups.glob("*.partial")), [])

        # A second plain restore goes back to the same pre-import state again.
        self.assertEqual(self.cli("restore", "--yes")[0], 0)
        self.assertEqual(self.counts(), (1, 1))
        # And the kept copy can be restored by name.
        self.assertEqual(self.cli("restore", str(kept[0]), "--yes")[0], 0)
        self.assertEqual(self.counts(), (2, 8))

    def test_restore_refuses_a_file_that_is_not_a_library(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        other = self.dir / "notes.db"
        conn = sqlite3.connect(str(other))
        conn.execute("create table notes (body text)")
        conn.commit()
        conn.close()
        junk = self.dir / "junk.backup"
        junk.write_bytes(b"this is not a database at all")
        for wrong in (other, junk):
            code, _, err = self.cli("restore", str(wrong), "--yes")
            self.assertEqual(code, 1)
            self.assertIn("is not a backup of a sticker library", err)
            self.assertEqual(self.counts(), (1, 7))

    def test_restore_never_offers_another_library_its_backups(self):
        make_store(self.db)
        other = self.dir / "other.stickerdb"
        make_store(other)
        seed_sticker(other, 1, 1024)
        imsticker.backup_db(other, self.backups)  # a newer backup, but of a different library
        code, _, err = self.cli("restore", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No backups of this library", err)
        self.assertEqual(self.counts(), (0, 0))

    def test_a_save_that_fails_does_not_leave_a_backup_restore_would_offer(self):
        make_store(self.db)
        other = sqlite3.connect(str(self.db), isolation_level=None)
        self.addCleanup(other.close)
        other.execute("begin immediate")  # someone else is mid-write, so the save cannot start
        with mock.patch.object(imsticker.sqlite3, "connect", wraps=lambda *a, **k: _quick(*a, **k)):
            code, _, err = self.cli("add", self.source("cat.png"))
        other.execute("rollback")
        self.assertEqual(code, 1)
        self.assertEqual(list(self.backups.glob("*.backup")), [])
        self.assertEqual(len(list(self.backups.glob("*.not-imported"))), 1)
        code, _, err = self.cli("restore", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No backups of this library", err)

    def test_an_unwritable_folder_is_reported_plainly(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        blocker = self.dir / "a-file-not-a-folder"
        blocker.write_bytes(b"x")
        code, _, err = self.cli("export", str(blocker / "inside"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("imsticker: "), err)
        self.assertNotIn("Traceback", err)
        with mock.patch.dict(os.environ, {"IMSTICKER_BACKUP_DIR": str(blocker / "backups")}):
            code, _, err = self.cli("add", self.source("cat.png"))
        self.assertEqual(code, 1)
        self.assertIn("Could not back up the library, so nothing was changed", err)
        self.assertEqual(self.counts(), (1, 7))

    def test_a_backup_that_fails_leaves_nothing_behind(self):
        broken = self.dir / "broken.stickerdb"
        broken.write_bytes(b"not a database, so the copy cannot be made" * 40)
        with self.assertRaises(imsticker.ToolError) as caught:
            imsticker.backup_db(broken, self.backups)
        self.assertIn("nothing was changed", str(caught.exception))
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_restore_asks_first(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        with mock.patch("builtins.input", return_value="n"):
            code, out, _ = self.cli("restore")
        self.assertEqual(code, 1)
        self.assertIn("Left the library alone", out)
        self.assertEqual(self.counts(), (1, 7))

    def test_restore_without_backups(self):
        make_store(self.db)
        code, _, err = self.cli("restore", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No backups of this library", err)


class BackupByHand(StoreCase):
    """`backup` saves a copy whenever it is asked to, apart from the one that
    is made before every import."""

    def by_hand(self):
        return sorted(self.backups.glob("stickers.stickerdb.*.manual"))

    def stickers_in(self, path):
        return rows(path, "select count(*) from ZMANAGEDSTICKER")[0][0]

    def test_saves_a_copy_and_changes_nothing(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        before = self.db.read_bytes()
        code, out, err = self.cli("backup")
        self.assertEqual((code, err), (0, ""))
        saved = self.by_hand()
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.stickers_in(saved[0]), 1)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertIn("Backed up 1 sticker (1 image, ", out)
        self.assertIn(str(saved[0]), out)
        # Nothing else is left in the folder: no half-written file, and no
        # backup of a kind that a plain restore would pick up.
        self.assertEqual(list(self.backups.iterdir()), saved)

    def test_it_says_how_to_put_it_back_and_that_works(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        code, out, _ = self.cli("backup")
        self.assertEqual(code, 0)
        saved = self.by_hand()[0]
        command = [line.strip() for line in out.splitlines() if " restore " in line]
        self.assertEqual(len(command), 1)
        words = shlex.split(command[0])
        self.assertEqual(words[:2], ["python3", "imsticker.py"])
        self.assertEqual(words[2:], ["--db", os.path.abspath(str(self.db)), "restore", os.path.abspath(str(saved))])

        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        self.assertEqual(self.cli("add", self.source("dog.png"))[0], 0)
        self.assertEqual(self.counts(), (3, 15))
        self.assertEqual(self.cli(*(words[4:] + ["--yes"]))[0], 0)
        self.assertEqual(self.counts(), (1, 1))

    def test_a_plain_restore_still_undoes_the_last_import(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        time.sleep(0.01)  # the names carry the time, to the millisecond
        self.assertEqual(self.cli("backup")[0], 0)  # newer than the import's own backup
        code, out, _ = self.cli("restore", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.counts(), (0, 0))
        # It says that a newer one exists, and how to put that one back.
        self.assertIn("made with `backup`", out)
        self.assertIn(os.path.abspath(str(self.by_hand()[0])), out)

    def test_an_older_one_is_not_mentioned(self):
        make_store(self.db)
        self.assertEqual(self.cli("backup")[0], 0)
        time.sleep(0.01)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        code, out, _ = self.cli("restore", "--yes")
        self.assertEqual(code, 0)
        self.assertNotIn("made with `backup`", out)

    def test_restore_with_only_backups_made_by_hand_names_the_newest(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        self.assertEqual(self.cli("backup")[0], 0)
        time.sleep(0.01)
        self.assertEqual(self.cli("backup")[0], 0)
        code, out, err = self.cli("restore", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No backup from before an import", err)
        self.assertIn(os.path.abspath(str(self.by_hand()[-1])), err)
        self.assertNotIn(self.by_hand()[0].name, err)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(list(self.backups.glob("*.before-restore")), [])

    def test_another_library_its_backups_are_not_offered(self):
        make_store(self.db)
        other = self.dir / "other.stickerdb"
        make_store(other)
        imsticker.backup_db(other, self.backups, kind=imsticker.BY_HAND)
        code, _, err = self.cli("restore", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No backups of this library", err)

    def test_into_a_folder(self):
        make_store(self.db)
        folder = self.dir / "kept"
        folder.mkdir()
        code, out, _ = self.cli("backup", str(folder))
        self.assertEqual(code, 0)
        inside = list(folder.iterdir())
        self.assertEqual(len(inside), 1)
        self.assertTrue(inside[0].name.endswith(".manual"), inside[0].name)
        self.assertFalse(self.backups.exists())
        # A name that ends in a slash is a folder too, and is made if need be.
        code, _, _ = self.cli("backup", str(self.dir / "new" / "deeper") + "/")
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.dir / "new" / "deeper").iterdir())), 1)

    def test_to_a_named_file_and_never_over_one(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        target = self.dir / "elsewhere" / "my stickers.db"
        code, out, _ = self.cli("backup", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(self.stickers_in(target), 1)
        self.assertEqual(list(target.parent.iterdir()), [target])

        seed_sticker(self.db, 2, 2048)
        kept = target.read_bytes()
        code, _, err = self.cli("backup", str(target))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(target.read_bytes(), kept)
        self.assertEqual(list(target.parent.iterdir()), [target])
        # Nor over the library itself, or anything else that is there.
        code, _, err = self.cli("backup", str(self.db))
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(self.counts(), (2, 2))

    def test_a_file_that_appears_meanwhile_is_not_written_over(self):
        make_store(self.db)
        target = self.dir / "copy.db"

        def late(source, destination):
            raise AssertionError("the copy must not be moved over {}".format(destination))

        real_lexists = os.path.lexists
        looks = []

        def lexists(path):
            if str(path) == str(target):
                looks.append(path)
                if len(looks) == 2:  # the look just before the copy gets its name
                    target.write_bytes(b"someone else's file")
            return real_lexists(path)

        with mock.patch.object(imsticker.os.path, "lexists", side_effect=lexists), \
                mock.patch.object(imsticker.os, "replace", side_effect=late):
            code, _, err = self.cli("backup", str(target))
        self.assertEqual(len(looks), 2)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(target.read_bytes(), b"someone else's file")
        self.assertEqual(sorted(path.name for path in self.dir.glob("copy.db*")), ["copy.db"])

    def test_only_a_sticker_library_is_backed_up(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("create table notes (body text)")
        conn.commit()
        conn.close()
        code, _, err = self.cli("backup")
        self.assertEqual(code, 1)
        self.assertIn("does not look like a sticker database", err)
        self.assertFalse(self.backups.exists())

    def test_a_copy_that_fails_the_check_is_kept_but_not_called_a_backup(self):
        make_store(self.db)
        with mock.patch.object(imsticker, "check_backup",
                               side_effect=imsticker.ToolError("it is damaged (row 3 missing)")):
            code, out, err = self.cli("backup")
        self.assertEqual(code, 1)
        self.assertIn("does not pass the check", err)
        self.assertIn("it is damaged (row 3 missing)", err)
        self.assertIn(str(self.by_hand()[0]), err)
        self.assertNotIn("Backed up", out)

    def test_what_is_still_in_the_write_ahead_log_is_in_the_copy(self):
        make_store(self.db)
        other = sqlite3.connect(str(self.db))  # stands in for stickersd
        self.addCleanup(other.close)
        self.assertEqual(other.execute("pragma journal_mode=wal").fetchone()[0], "wal")
        other.execute("pragma wal_autocheckpoint=0")
        other.execute("insert into ZMANAGEDSTICKER (Z_PK, Z_ENT, ZTYPE, ZIDENTIFIER) values (1, 2, 1, ?)",
                      (uuid.uuid4().bytes,))
        other.commit()
        self.assertGreater(Path(str(self.db) + "-wal").stat().st_size, 0)
        code, out, _ = self.cli("backup", str(self.dir / "copy.db"))
        self.assertEqual(code, 0)
        self.assertIn("Backed up 1 sticker (0 images, ", out)
        # The copy stands on its own: no log of its own is left beside it.
        self.assertEqual(sorted(path.name for path in self.dir.glob("copy.db*")), ["copy.db"])
        self.assertEqual(self.stickers_in(self.dir / "copy.db"), 1)

    def test_reading_a_backup_leaves_nothing_beside_it(self):
        # A copy of a library that was in write-ahead-log mode says so in its
        # own header. Checking it, counting what is in it and putting it back
        # must not leave a log or an index next to it.
        make_store(self.db)
        conn = sqlite3.connect(str(self.db))
        conn.execute("pragma journal_mode=wal")
        conn.close()
        seed_sticker(self.db, 1, 1024)
        self.assertEqual(self.cli("backup")[0], 0)
        saved = self.by_hand()[0]
        self.assertEqual(saved.read_bytes()[18:20], b"\x02\x02")  # the header's log-mode mark
        self.assertEqual(list(self.backups.iterdir()), [saved])
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(self.cli("restore", str(saved))[0], 1)
        self.assertEqual(list(self.backups.iterdir()), [saved])
        seed_sticker(self.db, 2, 2048)
        self.assertEqual(self.cli("restore", str(saved), "--yes")[0], 0)
        self.assertEqual(self.counts(), (1, 1))
        left = sorted(path.name for path in self.backups.iterdir() if path.name.startswith(saved.name))
        self.assertEqual(left, [saved.name])

    def test_a_backup_changed_after_it_was_made_is_read_with_its_log(self):
        # Not something imsticker does, but if another program wrote to a
        # backup and left a log beside it, what is in the log counts.
        make_store(self.db)
        conn = sqlite3.connect(str(self.db))
        conn.execute("pragma journal_mode=wal")
        conn.close()
        self.assertEqual(self.cli("backup", str(self.dir / "copy.db"))[0], 0)
        other = sqlite3.connect(str(self.dir / "copy.db"))
        self.addCleanup(other.close)
        other.execute("pragma wal_autocheckpoint=0")
        other.execute("insert into ZMANAGEDSTICKER (Z_PK, Z_ENT, ZTYPE, ZIDENTIFIER) values (1, 2, 1, ?)",
                      (uuid.uuid4().bytes,))
        other.commit()
        self.assertTrue(imsticker._has_log(self.dir / "copy.db"))
        self.assertEqual(imsticker.library_counts(self.dir / "copy.db", backup=True), (1, 0))
        self.assertEqual(self.cli("restore", str(self.dir / "copy.db"), "--yes")[0], 0)
        self.assertEqual(self.counts(), (1, 0))

    def test_the_library_is_not_a_backup_of_itself(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        code, _, err = self.cli("restore", str(self.db), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("is the sticker library itself", err)
        self.assertEqual(self.counts(), (1, 1))
        self.assertFalse(self.backups.exists())

    def test_the_command_it_prints_names_a_library_chosen_by_the_environment(self):
        make_store(self.db)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"IMSTICKER_DB": str(self.db)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = imsticker.main(["backup"])
        self.assertEqual((code, err.getvalue()), (0, ""))
        command = [line.strip() for line in out.getvalue().splitlines() if " restore " in line]
        self.assertEqual(shlex.split(command[0])[2:4], ["--db", os.path.abspath(str(self.db))])

    def test_backup_is_a_command_word(self):
        self.assertEqual(imsticker.with_default_command(["backup"]), ["backup"])
        self.assertEqual(imsticker.with_default_command(["backup.png"]), ["add", "backup.png"])


class ReadBack(StoreCase):
    def test_marker_convention_is_copied_and_export_removes_it(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024, marker=True)
        seed_sticker(self.db, 2, 2048, marker=True)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        new = rows(self.db, "select ZDATA, ZBYTECOUNT from ZMANAGEDREPRESENTATION where ZSTICKER = 3")
        for rep in new:
            data = bytes(rep["ZDATA"])
            self.assertEqual(data[:1], b"\x01")
            self.assertEqual(data[5:9], b"ftyp")
            self.assertEqual(rep["ZBYTECOUNT"], len(data) - 1)

        out_dir = self.dir / "dump"
        code, out, _ = self.cli("export", str(out_dir))
        self.assertEqual(code, 0)
        files = sorted(out_dir.iterdir())
        self.assertEqual(len(files), 9)
        for path in files:
            self.assertEqual(path.read_bytes()[4:8], b"ftyp")
        self.assertIn("sticker3_0_still.heic", [path.name for path in files])

    def test_bare_convention_is_the_default(self):
        make_store(self.db)
        seed_sticker(self.db, 1, 1024)
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        new = rows(self.db, "select ZDATA from ZMANAGEDREPRESENTATION where ZSTICKER = 2")
        self.assertTrue(all(bytes(rep[0])[4:8] == b"ftyp" for rep in new))

    def test_list_and_export_one_sticker(self):
        make_store(self.db)
        self.assertEqual(self.cli("add", self.source("cat.png"), "--name", "Troll")[0], 0)
        self.assertEqual(self.cli("add", self.source("anim.gif"))[0], 0)

        code, out, _ = self.cli("list")
        self.assertEqual(code, 0)
        self.assertIn("(2 stickers)", out)
        self.assertIn('#1  user', out)
        self.assertIn('"Troll"', out)
        self.assertIn("[still, keyboard, animated]", out)

        code, out, _ = self.cli("list", "-v")
        self.assertEqual(code, 0)
        self.assertIn("sticker:///user/identifier/", out)
        self.assertIn("stillVariant_320", out)
        self.assertIn("public.heics", out)
        self.assertIn("preferred", out)

        out_dir = self.dir / "one"
        code, out, _ = self.cli("export", str(out_dir), "--sticker", "2")
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(path.name for path in out_dir.iterdir()),
            ["sticker2_0_still.heic", "sticker2_1_keyboard.heic", "sticker2_2_animated.heics"])


class LiveStore(StoreCase):
    """The real store is in write-ahead-log mode and stickersd keeps it open."""

    def test_works_while_another_connection_has_the_store_open(self):
        make_store(self.db)
        other = sqlite3.connect(str(self.db))  # stands in for stickersd
        self.addCleanup(other.close)
        self.assertEqual(other.execute("pragma journal_mode=wal").fetchone()[0], "wal")
        self.assertEqual(other.execute("select count(*) from ZMANAGEDSTICKER").fetchone()[0], 0)

        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        self.assertEqual(other.execute("select count(*) from ZMANAGEDSTICKER").fetchone()[0], 1)
        self.assertEqual(self.cli("list", "-v")[0], 0)
        self.assertEqual(self.cli("export", str(self.dir / "dump"))[0], 0)
        self.assertEqual(len(list((self.dir / "dump").iterdir())), 7)

        self.assertEqual(self.cli("restore", "--yes")[0], 0)
        self.assertEqual(other.execute("select count(*) from ZMANAGEDSTICKER").fetchone()[0], 0)
        self.assertEqual(other.execute("pragma integrity_check").fetchone()[0], "ok")

    def test_reads_a_log_mode_store_nobody_has_open(self):
        make_store(self.db)
        conn = sqlite3.connect(str(self.db))
        conn.execute("pragma journal_mode=wal")
        conn.close()
        self.assertEqual(self.cli("add", self.source("cat.png"))[0], 0)
        code, out, err = self.cli("list")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(1 stickers)", out)

    def test_busy_store_is_reported_and_left_alone(self):
        make_store(self.db)
        other = sqlite3.connect(str(self.db), isolation_level=None)
        self.addCleanup(other.close)
        other.execute("begin immediate")  # someone else is mid-write
        with mock.patch.object(imsticker.sqlite3, "connect", wraps=lambda *a, **k: _quick(*a, **k)):
            code, _, err = self.cli("add", self.source("cat.png"))
        other.execute("rollback")
        self.assertEqual(code, 1)
        self.assertIn("busy", err)
        self.assertEqual(self.counts(), (0, 0))


_real_connect = sqlite3.connect


def _quick(*args, **kwargs):
    """sqlite3.connect with a short lock wait, so the busy test does not take 10 s."""
    kwargs["timeout"] = 0.2
    return _real_connect(*args, **kwargs)


class Locating(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("IMSTICKER_DB", None)

    def place(self, parts, age):
        path = self.home.joinpath(*parts)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        stamp = time.time() - age
        os.utime(str(path), (stamp, stamp))
        return path

    def test_old_location(self):
        old = self.place(imsticker.DB_CANDIDATES[1], 10)
        self.assertEqual(imsticker.find_db(home=self.home), old)

    def test_most_recently_written_copy_wins(self):
        group = self.place(imsticker.DB_CANDIDATES[0], 10)
        old = self.place(imsticker.DB_CANDIDATES[1], 5000)
        self.assertEqual(imsticker.find_db(home=self.home), group)
        # A write-ahead log counts as activity on its database.
        wal = Path(str(old) + "-wal")
        wal.write_bytes(b"")
        self.assertEqual(imsticker.find_db(home=self.home), old)

    def test_nothing_there(self):
        with self.assertRaises(imsticker.ToolError) as caught:
            imsticker.find_db(home=self.home)
        self.assertIn("Library/Stickers/stickers.stickerdb", str(caught.exception))

    def test_environment_override(self):
        path = self.home / "copy.stickerdb"
        path.write_bytes(b"")
        os.environ["IMSTICKER_DB"] = str(path)
        self.assertEqual(imsticker.find_db(home=self.home), path)

    def test_a_library_macos_will_not_show_is_not_called_missing(self):
        # What a Mac does when the terminal has not been allowed into another
        # program's data: the first look at the file is refused.
        group = self.place(imsticker.DB_CANDIDATES[0], 10)
        self.place(imsticker.DB_CANDIDATES[1], 5000)  # an old copy must not be used instead
        with mock.patch.object(imsticker.os, "stat", side_effect=_refusing(group)):
            with self.assertRaises(imsticker.ToolError) as caught:
                imsticker.find_db(home=self.home)
        message = str(caught.exception)
        self.assertIn("Operation not permitted", message)
        self.assertIn("Full Disk Access", message)
        self.assertNotIn("Could not find", message)

    def test_nor_is_one_named_with_db(self):
        path = self.home / "copy.stickerdb"
        path.write_bytes(b"")
        with mock.patch.object(imsticker.os, "stat", side_effect=_refusing(path)):
            with self.assertRaises(imsticker.ToolError) as caught:
                imsticker.find_db(str(path), home=self.home)
        self.assertIn("Full Disk Access", str(caught.exception))
        self.assertNotIn("No sticker database", str(caught.exception))


_real_stat = os.stat


def _refusing(refused):
    """os.stat as macOS answers it for one file the program may not look at."""
    def stat(path, *args, **kwargs):
        if str(path) == str(refused):
            raise PermissionError(1, "Operation not permitted", str(path))
        return _real_stat(path, *args, **kwargs)
    return stat


class CommandLine(unittest.TestCase):
    def test_default_command(self):
        fill = imsticker.with_default_command
        self.assertEqual(fill(["cat.png"]), ["add", "cat.png"])
        self.assertEqual(fill(["--max-edge", "512", "cat.png"]), ["add", "--max-edge", "512", "cat.png"])
        self.assertEqual(fill(["--db", "x.db", "cat.png"]), ["--db", "x.db", "add", "cat.png"])
        self.assertEqual(fill(["--db=x.db", "cat.png"]), ["--db=x.db", "add", "cat.png"])
        self.assertEqual(fill(["--db", "x.db", "list", "-v"]), ["--db", "x.db", "list", "-v"])
        self.assertEqual(fill(["list"]), ["list"])
        self.assertEqual(fill(["--help"]), ["--help"])
        self.assertEqual(fill([]), [])


class Portability(unittest.TestCase):
    def test_parses_as_python_3_9(self):
        # macOS ships Python 3.9 with the command line tools.
        source = (ROOT / "imsticker.py").read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))

    def test_embedded_swift_survives_the_python_string(self):
        swift = imsticker.SWIFT_HELPER
        self.assertIn('die("missing value for \\(argument)")', swift)
        self.assertIn('(message + "\\n").data(using: .utf8)', swift)
        self.assertEqual(swift.count("{"), swift.count("}"))
        self.assertEqual(swift.count("("), swift.count(")"))
        self.assertEqual(swift.count("["), swift.count("]"))


if __name__ == "__main__":
    unittest.main()

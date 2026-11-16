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
        self.assertIn("animated 200x200", out)
        self.assertIn("still heavy", out)

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
        # The state just before the restore was kept too, so it can be undone.
        backups = sorted(self.backups.glob("stickers.stickerdb.*.backup"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(rows(backups[-1], "select count(*) from ZMANAGEDSTICKER")[0][0], 2)

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
        self.assertIn("No backups found", err)


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

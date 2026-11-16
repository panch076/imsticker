#!/usr/bin/env python3
"""Assertions about the sticker library stickersd keeps on a Mac, used by
ci/verify_real_library.sh.

    real_library.py snapshot <store> <snapshot.json>
        Note what is in the library before anything is imported.
    real_library.py check <store> <snapshot.json> <imported.txt> <expected count>
        Fail unless exactly that many stickers were added since the snapshot,
        each with the values Apple's own code gives a sticker, and each object
        of each recorded in the store's history. Writes their identifiers.
    real_library.py fetched <fetch output> <imported.txt> <animated count>
        Fail unless Apple's store code returned every imported sticker with
        its still and keyboard images, and the animation for the last
        <animated count> of them.
    real_library.py same <store> <backup>
        Fail unless the library and a backup of it hold the same stickers,
        with the same images byte for byte, the backup passes SQLite's full
        integrity check, and the library's history has lost nothing the
        backup's has.
"""
import hashlib
import json
import pathlib
import re
import sqlite3
import sys
import uuid


def connect(path, backup=False):
    if backup:
        # A backup is one complete file. Read as such, SQLite makes no log or
        # index beside it, so this check leaves the folder as it found it.
        uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=20)
    else:
        conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot(store, out):
    conn = connect(store)
    data = {
        "stickers": [row[0] for row in conn.execute("select Z_PK from ZMANAGEDSTICKER")],
        "saves": conn.execute("select count(*) from ATRANSACTION").fetchone()[0],
    }
    with open(out, "w") as handle:
        json.dump(data, handle)
    print("before: {} stickers, {} saves in the history".format(len(data["stickers"]), data["saves"]))


def check(store, snapshot_path, imported_path, expected):
    with open(snapshot_path) as handle:
        before = json.load(handle)
    conn = connect(store)
    entities = dict(conn.execute("select Z_NAME, Z_ENT from Z_PRIMARYKEY").fetchall())
    stickers = [
        row for row in conn.execute("select * from ZMANAGEDSTICKER order by ZLIBRARYINDEX").fetchall()
        if row["Z_PK"] not in before["stickers"]
    ]
    assert len(stickers) == expected, "expected {} new stickers, found {}".format(expected, len(stickers))
    saves = conn.execute("select count(*) from ATRANSACTION").fetchone()[0] - before["saves"]
    assert saves >= expected, "expected at least {} new saves in the history, found {}".format(expected, saves)

    def in_history(entity, key):
        return conn.execute(
            "select count(*) from ACHANGE where ZCHANGETYPE = 0 and ZENTITY = ? and ZENTITYPK = ?",
            (entities[entity], key)).fetchone()[0] == 1

    identifiers = []
    for row in stickers:
        identifier = str(uuid.UUID(bytes=bytes(row["ZIDENTIFIER"]))).upper()
        where = "sticker {}".format(identifier)
        assert (row["ZTYPE"], row["ZEFFECT"], row["ZVERSION"]) == (1, 0, 1), where + ": type, effect, version"
        assert row["ZEXTERNALURI"] == "sticker:///user/identifier/" + identifier, where + ": " + row["ZEXTERNALURI"]
        assert in_history("ManagedSticker", row["Z_PK"]), where + ": not in the store's history"
        images = conn.execute(
            "select Z_PK, ZROLE, ZUTI, ZISPREFERRED, ZBYTECOUNT, length(ZDATA) as BYTES "
            "from ZMANAGEDREPRESENTATION where ZSTICKER = ?", (row["Z_PK"],)).fetchall()
        assert images, where + ": no images"
        for image in images:
            assert in_history("ManagedRepresentation", image["Z_PK"]), where + ": an image is not in the history"
            assert image["ZBYTECOUNT"] == image["BYTES"] > 0, where + ": image byte count"
        assert sum(image["ZISPREFERRED"] for image in images) == 1, where + ": exactly one preferred image"
        identifiers.append(identifier)
        print("{}: {} images, all in the history".format(where, len(images)))
    with open(imported_path, "w") as handle:
        handle.write(" ".join(identifiers) + "\n")
    print("the library holds the {} imported stickers, each recorded in its history".format(expected))


def fetched(fetch_path, imported_path, animated_count):
    with open(imported_path) as handle:
        imported = handle.read().split()
    with open(fetch_path) as handle:
        text = handle.read()
    got = re.search(r"^fetch: got (\d+) stickers", text, re.M)
    assert got and int(got.group(1)) == len(imported), "Apple's code returned {} of {} stickers".format(
        got.group(1) if got else "?", len(imported))
    roles = {}
    current = None
    for line in text.splitlines():
        started = re.match(r"\s+sticker ([0-9A-F-]{36}) ", line)
        if started:
            current = started.group(1)
            roles[current] = []
        found = re.match(r"\s+role=com\.apple\.stickers\.role\.(\w+) type=(\S+) .* bytes=(\d+)", line)
        if found and current:
            assert int(found.group(3)) > 0, "an image came back empty"
            roles[current].append((found.group(1), found.group(2)))
    for position, identifier in enumerate(imported):
        have = roles.get(identifier)
        assert have is not None, "{} did not come back".format(identifier)
        assert ("still", "public.heic") in have and ("keyboard", "public.heic") in have, (
            "{} came back without its still and keyboard images: {}".format(identifier, have))
        if position >= len(imported) - animated_count:
            assert ("animated", "public.heics") in have, "{} came back without its animation: {}".format(identifier, have)
        print("{} came back with {}".format(identifier, ", ".join(name for name, _ in have)))
    print("Apple's store code returned all {} imported stickers".format(len(imported)))


def contents(path, backup=False):
    """Every sticker in a library by identifier, with a digest of each of its
    images; how much history it has; and what SQLite's integrity check says."""
    conn = connect(path, backup)
    try:
        stickers = {}
        for row in conn.execute("select Z_PK, ZIDENTIFIER from ZMANAGEDSTICKER").fetchall():
            identifier = str(uuid.UUID(bytes=bytes(row["ZIDENTIFIER"]))).upper()
            assert identifier not in stickers, "two stickers share the identifier " + identifier
            stickers[identifier] = sorted(
                (image["ZROLE"], image["ZUTI"], hashlib.sha256(bytes(image["ZDATA"] or b"")).hexdigest())
                for image in conn.execute(
                    "select ZROLE, ZUTI, ZDATA from ZMANAGEDREPRESENTATION where ZSTICKER = ?",
                    (row["Z_PK"],)).fetchall())
        history = tuple(
            conn.execute("select count(*) from " + table).fetchone()[0]
            for table in ("ATRANSACTION", "ACHANGE"))
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
    finally:
        conn.close()
    return stickers, history, integrity


def same(store, backup):
    in_store, store_history, _ = contents(store)
    in_backup, backup_history, integrity = contents(backup, backup=True)
    assert integrity == "ok", "the backup fails SQLite's integrity check: " + integrity
    assert in_backup, "the backup holds no stickers, so comparing it would show nothing"
    assert sorted(in_store) == sorted(in_backup), "different stickers: library {} / backup {}".format(
        sorted(in_store), sorted(in_backup))
    for identifier in sorted(in_store):
        assert in_store[identifier] == in_backup[identifier], "the images of {} differ".format(identifier)
    assert all(have >= kept for have, kept in zip(store_history, backup_history)), (
        "the library's history {} is missing some of the backup's {}".format(store_history, backup_history))
    images = sum(len(found) for found in in_backup.values())
    print("library and backup hold the same {} stickers and {} images, byte for byte".format(
        len(in_backup), images))
    print("history (saves, changes): library {}, backup {}; backup integrity check: {}".format(
        store_history, backup_history, integrity))


def main():
    command = sys.argv[1]
    if command == "snapshot":
        snapshot(sys.argv[2], sys.argv[3])
    elif command == "check":
        check(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif command == "fetched":
        fetched(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif command == "same":
        same(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("unknown command " + command)


if __name__ == "__main__":
    main()

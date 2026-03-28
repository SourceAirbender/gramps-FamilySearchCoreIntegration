import json
import os
import sqlite3
import tempfile
import unittest

from gramps.gen.db import DbTxn
from gramps.gen.lib import Person
from gramps.gui.fs.datab_familysearch import FSStatusDB
from gramps.plugins.db.dbapi.sqlite import SQLite


class FakeDb:
    """
    Tiny fake DB for unit tests of FSStatusDB.

    Only implements the API methods used by FSStatusDB.
    """

    def __init__(self):
        self.rows = {}

    def get_familysearch_person_status(self, person_handle, default=None):
        if person_handle in self.rows:
            return dict(self.rows[person_handle])
        return {} if default is None else default

    def set_familysearch_person_status(self, person_handle, status, transaction=None):
        self.rows[person_handle] = dict(status)

    def delete_familysearch_person_status(self, person_handle, transaction=None):
        self.rows.pop(person_handle, None)


class FamilySearchSyncPersonModelTest(unittest.TestCase):
    def test_person_serialize_unserialize_round_trip_with_familysearch_sync(self):
        person = Person()
        sync = person.get_familysearch_sync()
        sync.from_status_dict(
            {
                "fsid": "GSVF-SGV",
                "is_root": True,
                "status_ts": 123,
                "confirmed_ts": 456,
                "gramps_modified_ts": 789,
                "fs_modified_ts": 999,
                "essential_conflict": True,
                "conflict": True,
            }
        )
        person.set_familysearch_sync(sync)

        loaded = Person()
        loaded.unserialize(person.serialize())

        self.assertEqual(
            loaded.get_familysearch_sync().to_status_dict(),
            {
                "fsid": "GSVF-SGV",
                "is_root": True,
                "status_ts": 123,
                "confirmed_ts": 456,
                "gramps_modified_ts": 789,
                "fs_modified_ts": 999,
                "essential_conflict": True,
                "conflict": True,
            },
        )

    def test_person_unserialize_v21_tuple_defaults_empty_familysearch_sync(self):
        person = Person()

        # Simulate the old v21 tuple shape without the appended
        # familysearch_sync field.
        old_v21_data = person.serialize()[:-1]

        loaded = Person()
        loaded.unserialize(old_v21_data)

        self.assertEqual(loaded.get_familysearch_sync().to_status_dict(), {})


class FSStatusDBUnitTest(unittest.TestCase):
    def test_commit_and_get_round_trip_with_fake_db(self):
        db = FakeDb()

        status = FSStatusDB(db, "P1")
        status.fsid = "GSVF-SGV"
        status.is_root = True
        status.status_ts = 123
        status.confirmed_ts = 456
        status.gramps_modified_ts = 789
        status.fs_modified_ts = 999
        status.essential_conflict = True
        status.conflict = False

        # Pass a dummy txn so FSStatusDB won't try to open a real DbTxn on FakeDb
        status.commit(txn=object())

        self.assertEqual(
            db.rows["P1"],
            {
                "fsid": "GSVF-SGV",
                "is_root": True,
                "status_ts": 123,
                "confirmed_ts": 456,
                "gramps_modified_ts": 789,
                "fs_modified_ts": 999,
                "essential_conflict": True,
            },
        )

        loaded = FSStatusDB(db)
        loaded.get("P1")

        self.assertEqual(loaded.p_handle, "P1")
        self.assertEqual(loaded.fsid, "GSVF-SGV")
        self.assertTrue(loaded.is_root)
        self.assertEqual(loaded.status_ts, 123)
        self.assertEqual(loaded.confirmed_ts, 456)
        self.assertEqual(loaded.gramps_modified_ts, 789)
        self.assertEqual(loaded.fs_modified_ts, 999)
        self.assertTrue(loaded.essential_conflict)
        self.assertFalse(loaded.conflict)

    def test_empty_commit_deletes_status_row(self):
        db = FakeDb()
        db.rows["P1"] = {
            "fsid": "OLD-ID",
            "is_root": True,
            "status_ts": 1,
        }

        status = FSStatusDB(db, "P1")
        status.commit(txn=object())

        self.assertNotIn("P1", db.rows)


class FamilySearchSyncSQLiteIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.dbdir = self.tempdir.name
        self.db = SQLite()
        self.db.load(self.dbdir)

    def tearDown(self):
        try:
            if getattr(self.db, "db_is_open", False):
                self.db.close()
        finally:
            self.tempdir.cleanup()

    def _sqlite_path(self):
        return os.path.join(self.dbdir, "sqlite.db")

    def _create_person(self):
        person = Person()
        with DbTxn("Add test person", self.db) as txn:
            self.db.add_person(person, txn)
        return person

    def test_person_table_has_no_familysearch_sync_column(self):
        with sqlite3.connect(self._sqlite_path()) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info('person')").fetchall()}

        self.assertNotIn("familysearch_sync_data", cols)

    def test_db_api_round_trip_and_delete(self):
        person = self._create_person()

        self.db.set_familysearch_person_status(
            person.handle,
            {
                "fsid": "ABCD-EFG",
                "is_root": True,
                "status_ts": 111,
                "confirmed_ts": 222,
                "gramps_modified_ts": 333,
                "fs_modified_ts": 444,
                "essential_conflict": True,
                "conflict": False,
            },
        )

        row = self.db.get_familysearch_person_status(person.handle, {})
        self.assertEqual(
            row,
            {
                "fsid": "ABCD-EFG",
                "is_root": True,
                "status_ts": 111,
                "confirmed_ts": 222,
                "gramps_modified_ts": 333,
                "fs_modified_ts": 444,
                "essential_conflict": True,
                "conflict": False,
            },
        )

        self.db.delete_familysearch_person_status(person.handle)
        self.assertEqual(self.db.get_familysearch_person_status(person.handle, {}), {})

    def test_person_object_round_trip(self):
        person = self._create_person()

        self.db.set_familysearch_person_status(
            person.handle,
            {
                "fsid": "WXYZ-123",
                "is_root": True,
                "status_ts": 1000,
                "confirmed_ts": 2000,
                "gramps_modified_ts": 3000,
                "fs_modified_ts": 4000,
                "essential_conflict": True,
                "conflict": True,
            },
        )

        loaded = self.db.get_person_from_handle(person.handle)
        sync = loaded.get_familysearch_sync()

        self.assertEqual(
            sync.to_status_dict(),
            {
                "fsid": "WXYZ-123",
                "is_root": True,
                "status_ts": 1000,
                "confirmed_ts": 2000,
                "gramps_modified_ts": 3000,
                "fs_modified_ts": 4000,
                "essential_conflict": True,
                "conflict": True,
            },
        )

    def test_fsstatusdb_integration_round_trip(self):
        person = self._create_person()

        status = FSStatusDB(self.db, person.handle)
        status.fsid = "WXYZ-123"
        status.is_root = True
        status.status_ts = 1000
        status.confirmed_ts = 2000
        status.gramps_modified_ts = 3000
        status.fs_modified_ts = 4000
        status.essential_conflict = True
        status.conflict = True
        status.commit()

        loaded = FSStatusDB(self.db)
        loaded.get(person.handle)

        self.assertEqual(loaded.fsid, "WXYZ-123")
        self.assertTrue(loaded.is_root)
        self.assertEqual(loaded.status_ts, 1000)
        self.assertEqual(loaded.confirmed_ts, 2000)
        self.assertEqual(loaded.gramps_modified_ts, 3000)
        self.assertEqual(loaded.fs_modified_ts, 4000)
        self.assertTrue(loaded.essential_conflict)
        self.assertTrue(loaded.conflict)

        cleared = FSStatusDB(self.db, person.handle)
        cleared.commit()

        self.assertEqual(self.db.get_familysearch_person_status(person.handle, {}), {})


class FamilySearchSyncUpgradeIntegrationTest(unittest.TestCase):
    def _remove_familysearch_sync_from_person_json(self, sqlite_path, handle):
        with sqlite3.connect(sqlite_path) as con:
            row = con.execute(
                "SELECT json_data FROM person WHERE handle = ?",
                [handle],
            ).fetchone()
            self.assertIsNotNone(row)

            data = json.loads(row[0])
            data.pop("familysearch_sync", None)

            con.execute(
                "UPDATE person SET json_data = ? WHERE handle = ?",
                [json.dumps(data, separators=(",", ":")), handle],
            )
            con.commit()

    def test_upgrade_from_v21_rewrites_person_json_with_familysearch_sync(self):
        with tempfile.TemporaryDirectory() as dbdir:
            db = SQLite()
            db.load(dbdir)

            person = Person()
            with DbTxn("Add test person", db) as txn:
                db.add_person(person, txn)

            person_handle = person.handle
            db.close()

            sqlite_path = os.path.join(dbdir, "sqlite.db")
            self._remove_familysearch_sync_from_person_json(sqlite_path, person_handle)

            with sqlite3.connect(sqlite_path) as con:
                row = con.execute(
                    "SELECT json_data FROM person WHERE handle = ?",
                    [person_handle],
                ).fetchone()
                data = json.loads(row[0])
                self.assertNotIn("familysearch_sync", data)

            downgraded = SQLite()
            downgraded.load(dbdir)
            downgraded.set_schema_version(21)
            downgraded.close(update=False)

            upgraded = SQLite()
            upgraded.load(dbdir, force_schema_upgrade=True)

            try:
                self.assertEqual(upgraded.get_schema_version(), 22)

                loaded = upgraded.get_person_from_handle(person_handle)
                self.assertEqual(loaded.get_familysearch_sync().to_status_dict(), {})

                with sqlite3.connect(sqlite_path) as con:
                    row = con.execute(
                        "SELECT json_data FROM person WHERE handle = ?",
                        [person_handle],
                    ).fetchone()
                    data = json.loads(row[0])

                self.assertIn(
                    "familysearch_sync",
                    data,
                    "familysearch_sync was not added back to person JSON by upgrade",
                )
            finally:
                upgraded.close()

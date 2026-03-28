import os
import sqlite3
import tempfile
import unittest

from gramps.gen.db import DbTxn
from gramps.gen.lib import Person
from gramps.gui.fs.datab_familysearch import FSStatusDB
from gramps.plugins.db.dbapi.sqlite import SQLite

EXPECTED_COLUMNS = {
    "p_handle",
    "fsid",
    "is_root",
    "status_ts",
    "confirmed_ts",
    "gramps_modified_ts",
    "fs_modified_ts",
    "essential_conflict",
    "conflict",
}


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
        # leave all fields at defaults => should delete
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

    def test_schema_table_exists_with_expected_columns(self):
        with sqlite3.connect(self._sqlite_path()) as con:
            cur = con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='familysearch_sync'"
            )
            row = cur.fetchone()
            self.assertIsNotNone(row, "familysearch_sync table was not created")

            cols = {
                r[1]
                for r in con.execute(
                    "PRAGMA table_info('familysearch_sync')"
                ).fetchall()
            }

        self.assertEqual(cols, EXPECTED_COLUMNS)

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

        # Now clear everything and ensure the row is removed
        cleared = FSStatusDB(self.db, person.handle)
        cleared.commit()

        self.assertEqual(self.db.get_familysearch_person_status(person.handle, {}), {})


class FamilySearchSyncUpgradeIntegrationTest(unittest.TestCase):
    def test_upgrade_from_v21_recreates_familysearch_sync_table(self):
        with tempfile.TemporaryDirectory() as dbdir:
            db = SQLite()
            db.load(dbdir)

            # Simulate an older v21 database and preserve that version on disk
            db.set_schema_version(21)
            db.close(update=False)

            sqlite_path = os.path.join(dbdir, "sqlite.db")
            with sqlite3.connect(sqlite_path) as con:
                con.execute("DROP TABLE IF EXISTS familysearch_sync")
                con.commit()

                row = con.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='familysearch_sync'"
                ).fetchone()
                self.assertIsNone(row)

            upgraded = SQLite()
            upgraded.load(dbdir, force_schema_upgrade=True)

            try:
                self.assertEqual(upgraded.get_schema_version(), 22)

                with sqlite3.connect(sqlite_path) as con:
                    row = con.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='familysearch_sync'"
                    ).fetchone()
                    self.assertIsNotNone(
                        row, "familysearch_sync table was recreated by upgrade"
                    )

                    cols = {
                        r[1]
                        for r in con.execute(
                            "PRAGMA table_info('familysearch_sync')"
                        ).fetchall()
                    }
                    self.assertEqual(cols, EXPECTED_COLUMNS)
            finally:
                upgraded.close()

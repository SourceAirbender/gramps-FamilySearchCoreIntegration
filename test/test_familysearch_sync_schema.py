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

    def test_person_table_has_familysearch_sync_column(self):
        with sqlite3.connect(self._sqlite_path()) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info('person')").fetchall()}

        self.assertIn("familysearch_sync_data", cols)

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

        # Now clear everything and ensure the stored data is removed
        cleared = FSStatusDB(self.db, person.handle)
        cleared.commit()

        self.assertEqual(self.db.get_familysearch_person_status(person.handle, {}), {})


class FamilySearchSyncUpgradeIntegrationTest(unittest.TestCase):
    def _rebuild_person_table_without_fs_column(self, sqlite_path):
        """
        Simulate a v21 database by rebuilding the person table without the
        familysearch_sync_data column, preserving all other columns.
        """
        with sqlite3.connect(sqlite_path) as con:
            cols = con.execute("PRAGMA table_info('person')").fetchall()

            old_columns = []
            create_defs = []

            for cid, name, col_type, notnull, default_value, pk in cols:
                if name == "familysearch_sync_data":
                    continue

                old_columns.append(name)

                col_def = f'"{name}" {col_type}' if col_type else f'"{name}"'
                if pk:
                    col_def += " PRIMARY KEY"
                if notnull:
                    col_def += " NOT NULL"
                if default_value is not None:
                    col_def += f" DEFAULT {default_value}"
                create_defs.append(col_def)

            con.execute("ALTER TABLE person RENAME TO person_old")
            con.execute(f"CREATE TABLE person ({', '.join(create_defs)})")
            con.execute(
                f'INSERT INTO person ({", ".join(old_columns)}) '
                f'SELECT {", ".join(old_columns)} FROM person_old'
            )
            con.execute("DROP TABLE person_old")
            con.commit()

    def test_upgrade_from_v21_adds_familysearch_sync_person_column(self):
        with tempfile.TemporaryDirectory() as dbdir:
            db = SQLite()
            db.load(dbdir)
            db.close()

            sqlite_path = os.path.join(dbdir, "sqlite.db")
            self._rebuild_person_table_without_fs_column(sqlite_path)

            with sqlite3.connect(sqlite_path) as con:
                cols = {
                    r[1] for r in con.execute("PRAGMA table_info('person')").fetchall()
                }
                self.assertNotIn("familysearch_sync_data", cols)

            downgraded = SQLite()
            downgraded.load(dbdir)
            downgraded.set_schema_version(21)
            downgraded.close(update=False)

            upgraded = SQLite()
            upgraded.load(dbdir, force_schema_upgrade=True)

            try:
                self.assertEqual(upgraded.get_schema_version(), 22)

                with sqlite3.connect(sqlite_path) as con:
                    cols = {
                        r[1]
                        for r in con.execute("PRAGMA table_info('person')").fetchall()
                    }

                self.assertIn(
                    "familysearch_sync_data",
                    cols,
                    "familysearch_sync_data column was not added by upgrade",
                )
            finally:
                upgraded.close()

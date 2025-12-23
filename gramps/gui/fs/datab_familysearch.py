# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2024-2025  Gabriel Rios
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <https://www.gnu.org/licenses/>.
#

from __future__ import annotations

from typing import Any


_TABLE = "statistics_grampsfs_sync"
_SCHEMA_FLAG_ATTR = "_grampsfs_sync_schema_ready"


def _is_no_such_table_error(exc: BaseException) -> bool:
    """
    Gramps DBAPI wrappers may raise sqlite3.OperationalError or a generic Exception....
    """
    try:
        msg = str(exc) or ""
    except Exception:
        return False
    msg_l = msg.lower()
    return ("no such table" in msg_l) and (_TABLE.lower() in msg_l)


def ensure_status_schema(db) -> None:
    """
    Ensure the statistics table exists for this DB connection
    """
    if db is None:
        return

    try:
        if getattr(db, _SCHEMA_FLAG_ATTR, False):
            return
    except Exception:
        pass

    # create (idempotent) and mark
    try:
        create_status_schema(db)
    finally:
        try:
            setattr(db, _SCHEMA_FLAG_ATTR, True)
        except Exception:
            pass


def create_status_schema(db) -> None:
    """
    create the 'statistics_grampsfs_sync' table if missing
    """
    if db is None:
        return
    dbapi = getattr(db, "dbapi", None)
    if dbapi is None:
        return

    try:
        exists = False
        try:
            exists = bool(dbapi.table_exists(_TABLE))
        except Exception:
            exists = False

        if not exists:
            dbapi.execute(
                "CREATE TABLE %s ("
                "p_handle VARCHAR(50) PRIMARY KEY NOT NULL, "
                "fsid CHAR(8), "
                "is_root INTEGER, "
                "status_ts INTEGER, "
                "confirmed_ts INTEGER, "
                "gramps_modified_ts INTEGER, "
                "fs_modified_ts INTEGER, "
                "essential_conflict INTEGER, "
                "conflict INTEGER"
                ")" % _TABLE
            )
    except Exception:
        pass


class FSStatusDB:
    """
    Row object for statistics_grampsfs_sync

    Attributes:
        p_handle: str
        fsid: str | None
        is_root: bool (stored as 0/1)
        status_ts: int | None
        confirmed_ts: int | None
        gramps_modified_ts: int | None
        fs_modified_ts: int | None
        essential_conflict: bool (stored as 0/1)
        conflict: bool (stored as 0/1)
    """

    def __init__(self, db, p_handle: str | None = None):
        self.db = db
        self.p_handle = p_handle
        self.fsid: str | None = None
        self.is_root: bool = False
        self.status_ts: int | None = None
        self.confirmed_ts: int | None = None
        self.gramps_modified_ts: int | None = None
        self.fs_modified_ts: int | None = None
        self.essential_conflict: bool = False
        self.conflict: bool = False

        ensure_status_schema(self.db)

    def commit(self, txn=None) -> None:
        """
        if transaction is provided by the caller, it will be handled upstream.
        """
        if not self.p_handle:
            print("datab_familysearch.FSStatusDB.commit: missing p_handle")
            return

        ensure_status_schema(self.db)

        vals = [
            self.fsid,
            1 if self.is_root else 0,
            self.status_ts,
            self.confirmed_ts,
            self.gramps_modified_ts,
            self.fs_modified_ts,
            1 if self.essential_conflict else 0,
            1 if self.conflict else 0,
        ]

        for attempt in (0, 1):
            try:
                self.db.dbapi.execute(
                    f"SELECT 1 FROM {_TABLE} WHERE p_handle=?",
                    [self.p_handle],
                )
                row = self.db.dbapi.fetchone()

                if row:
                    sql = (
                        f"UPDATE {_TABLE} SET "
                        "fsid=?, is_root=?, status_ts=?, confirmed_ts=?, "
                        "gramps_modified_ts=?, fs_modified_ts=?, "
                        "essential_conflict=?, conflict=? "
                        "WHERE p_handle=?"
                    )
                    self.db.dbapi.execute(sql, vals + [self.p_handle])
                else:
                    sql = (
                        f"INSERT INTO {_TABLE} ("
                        "p_handle, fsid, is_root, status_ts, confirmed_ts, "
                        "gramps_modified_ts, fs_modified_ts, essential_conflict, conflict"
                        ") VALUES (?,?,?,?,?,?,?,?,?)"
                    )
                    self.db.dbapi.execute(sql, [self.p_handle] + vals)
                return
            except Exception as e:
                if attempt == 0 and _is_no_such_table_error(e):
                    create_status_schema(self.db)
                    continue
                raise

    def get(self, person_handle: str | None = None) -> None:
        """
        Load row by handle into this object; if not found, keep defaults and set handle
        """
        if not person_handle:
            person_handle = self.p_handle
        if not person_handle:
            print("datab_familysearch.FSStatusDB.get: missing person_handle")
            return

        ensure_status_schema(self.db)

        for attempt in (0, 1):
            try:
                self.db.dbapi.execute(
                    "SELECT p_handle, fsid, is_root, status_ts, confirmed_ts, "
                    "gramps_modified_ts, fs_modified_ts, essential_conflict, conflict "
                    f"FROM {_TABLE} WHERE p_handle=?",
                    [person_handle],
                )
                row = self.db.dbapi.fetchone()
                if row:
                    (
                        self.p_handle,
                        self.fsid,
                        is_root,
                        self.status_ts,
                        self.confirmed_ts,
                        self.gramps_modified_ts,
                        self.fs_modified_ts,
                        essential_conflict,
                        conflict,
                    ) = row
                    self.is_root = bool(is_root)
                    self.essential_conflict = bool(essential_conflict)
                    self.conflict = bool(conflict)
                else:
                    self.p_handle = person_handle
                return
            except Exception as e:
                if attempt == 0 and _is_no_such_table_error(e):
                    create_status_schema(self.db)
                    continue
                raise


__all__ = ["create_status_schema", "ensure_status_schema", "FSStatusDB"]

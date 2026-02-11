# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2024-2026  Gabriel Rios
# Copyright (C) 2025-2026  Nick Hall
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

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator

LOG = logging.getLogger(__name__)

# instead of creating schema/accessing dbapi directly, store status on Person via a single attribute containing a json, until extention of db is possible
_STATUS_ATTR = "_GRAMPSFS_SYNC_STATUS"

from gramps.gen.db import DbTxn  
from gramps.gen.lib import Attribute, AttributeType 
from gramps.gen.errors import HandleError  

def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_bool(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _read_status_blob(person) -> dict[str, Any]:
    """
    Load status JSON dict from person's attribute _GRAMPSFS_SYNC_STATUS.
    """
    if not person:
        return {}

    try:
        attrs = person.get_attribute_list() or []
    except (AttributeError, TypeError):
        return {}

    for a in attrs:
        try:
            if str(a.get_type()) != _STATUS_ATTR:
                continue
            raw = (a.get_value() or "").strip()
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}
        except (AttributeError, TypeError):
            # malformed attribute object; ignore and continue
            continue

    return {}


def _write_status_blob(person, data: dict[str, Any]) -> None:
    """
    Write status JSON dict into person's attribute _GRAMPSFS_SYNC_STATUS
    Removes attribute if the data is empty too
    """
    if not person:
        return
    if Attribute is None or AttributeType is None:
        return

    cleaned: dict[str, Any] = {}
    for k, v in (data or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            if v:
                cleaned[k] = True
            continue
        # drop empty strings
        if isinstance(v, str) and not v.strip():
            continue
        cleaned[k] = v

    try:
        current = person.get_attribute_list() or []
    except (AttributeError, TypeError):
        return

    try:
        attrs = []
        for a in current:
            try:
                if str(a.get_type()) == _STATUS_ATTR:
                    continue
            except (AttributeError, TypeError):
                attrs.append(a)
                continue
            attrs.append(a)

        if cleaned:
            a = Attribute()
            a.set_type(AttributeType(_STATUS_ATTR))
            a.set_value(json.dumps(cleaned, sort_keys=True, separators=(",", ":")))
            attrs.append(a)

        person.set_attribute_list(attrs)
    except Exception:
        # do not break caller if attribute writes fail
        LOG.debug("Failed to write status blob to person attribute list", exc_info=True)


@contextmanager
def _txn_or_none(db, txn, label: str) -> Iterator[Any]:
    """
    use caller provided txn, s
    """
    if txn is not None:
        yield txn
        return

    if DbTxn is None or db is None:
        yield None
        return

    with DbTxn(label, db) as t:
        yield t


class FSStatusDB:
    """
    Status store backed by a Person Attribute JSON blob

    Stored under attribute type: _GRAMPSFS_SYNC_STATUS

    Keys (in JSON):
        fsid: str
        is_root: bool
        status_ts: int
        confirmed_ts: int
        gramps_modified_ts: int
        fs_modified_ts: int
        essential_conflict: bool
        conflict: bool
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

    def _get_person(self):
        if not self.db or not self.p_handle:
            return None

        try:
            return self.db.get_person_from_handle(self.p_handle)
        except Exception as err:
            if HandleError is not None and isinstance(err, HandleError):
                return None
            LOG.debug("Failed to get person from handle=%s", self.p_handle, exc_info=True)
            return None

    def commit(self, txn=None) -> None:
        """
        Persist current fields & preserves the person's existing change timestamp
        """
        if not self.p_handle:
            LOG.warning("FSStatusDB.commit called without p_handle")
            return

        person = self._get_person()
        if not person:
            LOG.debug("FSStatusDB.commit: person not found for handle=%s", self.p_handle)
            return

        blob = _read_status_blob(person)

        if self.fsid:
            blob["fsid"] = str(self.fsid).strip()
        else:
            blob.pop("fsid", None)

        if self.is_root:
            blob["is_root"] = True
        else:
            blob.pop("is_root", None)

        # ints
        for k, v in (
            ("status_ts", self.status_ts),
            ("confirmed_ts", self.confirmed_ts),
            ("gramps_modified_ts", self.gramps_modified_ts),
            ("fs_modified_ts", self.fs_modified_ts),
        ):
            iv = _as_int(v)
            if iv and iv > 0:
                blob[k] = iv
            else:
                blob.pop(k, None)

        # bool flags
        if self.essential_conflict:
            blob["essential_conflict"] = True
        else:
            blob.pop("essential_conflict", None)

        if self.conflict:
            blob["conflict"] = True
        else:
            blob.pop("conflict", None)

        # Writeattribute list
        _write_status_blob(person, blob)

        # original change timestamp
        old_change = getattr(person, "change", None)

        with _txn_or_none(self.db, txn, "FamilySearch: status update") as t:
            try:
                if t is not None:
                    self.db.commit_person(person, t, old_change)
                else:
                    try:
                        self.db.commit_person(person, None, old_change)
                    except TypeError:
                        try:
                            self.db.commit_person(person, None)
                        except Exception:
                            LOG.debug(
                                "FSStatusDB.commit fallback commit_person failed (txn=None)",
                                exc_info=True,
                            )
                    except Exception:
                        LOG.debug(
                            "FSStatusDB.commit fallback commit_person failed (txn=None, change preserved)",
                            exc_info=True,
                        )
            except TypeError:
                try:
                    if t is not None:
                        self.db.commit_person(person, t)
                    else:
                        self.db.commit_person(person, None)
                except Exception:
                    LOG.debug("FSStatusDB.commit: commit_person signature fallback failed", exc_info=True)
            except Exception:
                LOG.debug("FSStatusDB.commit: commit_person failed", exc_info=True)

    def get(self, person_handle: str | None = None) -> None:
        if not person_handle:
            person_handle = self.p_handle
        if not person_handle:
            LOG.warning("FSStatusDB.get called without person_handle")
            return

        self.p_handle = person_handle
        person = self._get_person()
        if not person:
            LOG.debug("FSStatusDB.get: person not found for handle=%s", self.p_handle)
            return

        blob = _read_status_blob(person)

        self.fsid = (str(blob.get("fsid")).strip() if blob.get("fsid") else None)
        self.is_root = _as_bool(blob.get("is_root"))

        self.status_ts = _as_int(blob.get("status_ts"))
        self.confirmed_ts = _as_int(blob.get("confirmed_ts"))
        self.gramps_modified_ts = _as_int(blob.get("gramps_modified_ts"))
        self.fs_modified_ts = _as_int(blob.get("fs_modified_ts"))

        self.essential_conflict = _as_bool(blob.get("essential_conflict"))
        self.conflict = _as_bool(blob.get("conflict"))


__all__ = ["create_status_schema", "ensure_status_schema", "FSStatusDB"]

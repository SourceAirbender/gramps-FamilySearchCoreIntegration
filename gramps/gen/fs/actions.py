# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2025-2026 Gabriel Rios
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

import logging
import os
from typing import Any

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.errors import HandleError
from gramps.gen.lib import Attribute, AttributeType, ChildRef, Family, Person

logger = logging.getLogger(__name__)

_ = glocale.translation.gettext

FS_ATTR_CANON = "_FSFTID"
FS_ATTR_OLD = "_FSTID"
FS_ATTR_HUMAN = "FamilySearch ID"


def _dbg(msg: str) -> None:
    if os.environ.get("GRAMPS_FS_DEBUG", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        logger.debug("%s", msg)


def _bind_global_session(session) -> None:
    # many FS code paths still rely on module-global tree._fs_session
    # kept for windows compat
    try:
        from gramps.gen.fs import tree as fs_tree

        fs_tree._fs_session = session
    except Exception:
        pass


def _get_fs_id(person) -> str:
    if not person:
        return ""
    for attr in person.get_attribute_list() or []:
        atype = str(attr.get_type())
        if atype in (FS_ATTR_CANON, FS_ATTR_OLD, FS_ATTR_HUMAN):
            value = (attr.get_value() or "").strip()
            if value:
                return value
    return ""


def _set_fs_id(person, fsid: str) -> None:
    if not person:
        return

    fsid = (fsid or "").strip()

    attrs = []
    for attr in person.get_attribute_list() or []:
        if str(attr.get_type()) in (FS_ATTR_CANON, FS_ATTR_OLD, FS_ATTR_HUMAN):
            continue
        attrs.append(attr)

    if fsid:
        attr = Attribute()
        attr.set_type(AttributeType(FS_ATTR_CANON))
        attr.set_value(fsid)
        attrs.append(attr)

    person.set_attribute_list(attrs)


def _platform_json(session, endpoint: str) -> dict:
    """
    Fetch GEDCOM JSON from a /platform/... endpoint using the Session object.
    """
    for name in ("get_jsonurl", "get_json", "get_gedcomx"):
        fn = getattr(session, name, None)
        if callable(fn):
            try:
                data = fn(endpoint)
                return (
                    data
                    if isinstance(data, dict)
                    else (data.json() if hasattr(data, "json") else {})
                )
            except Exception:
                pass

    fn = getattr(session, "get_url", None)
    if callable(fn):
        try:
            resp = fn(endpoint, {"Accept": "application/x-gedcomx-v1+json"})
            if resp and hasattr(resp, "json"):
                return resp.json() or {}
        except TypeError:
            try:
                resp = fn(
                    endpoint, headers={"Accept": "application/x-gedcomx-v1+json"}
                )
                if resp and hasattr(resp, "json"):
                    return resp.json() or {}
            except Exception:
                pass
        except Exception:
            pass

    import requests

    base = (
        getattr(session, "api_url", "")
        or getattr(session, "API_URL", "")
        or "https://apibeta.familysearch.org"
    )
    base = str(base).rstrip("/")
    token = getattr(session, "access_token", "") or ""
    url = base + (endpoint if endpoint.startswith("/") else ("/" + endpoint))
    headers = {"Accept": "application/x-gedcomx-v1+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json() or {}


def _fs_display_name(person_data: dict) -> str:
    display = (person_data or {}).get("display") or {}
    return (display.get("name") or display.get("fullName") or "").strip()


def _ensure_child_ref(child_handle: str) -> ChildRef:
    child_ref = ChildRef()
    if hasattr(child_ref, "set_reference_handle"):
        child_ref.set_reference_handle(child_handle)
    else:
        child_ref.ref = child_handle
    return child_ref


def _family_other_parent_handle(fam: Family, me_handle: str):
    father_handle = fam.get_father_handle()
    mother_handle = fam.get_mother_handle()
    if father_handle == me_handle:
        return mother_handle
    if mother_handle == me_handle:
        return father_handle
    return None


def _ensure_person_has_family_handle(db, txn, person: Person, fam_handle: str) -> None:
    fams = list(person.get_family_handle_list() or [])
    if fam_handle not in fams:
        fams.append(fam_handle)
        person.set_family_handle_list(fams)
        db.commit_person(person, txn)


def _ensure_person_has_parent_family_handle(
    db, txn, person: Person, fam_handle: str
) -> None:
    fams = list(person.get_parent_family_handle_list() or [])
    if fam_handle not in fams:
        fams.append(fam_handle)
        person.set_parent_family_handle_list(fams)
        db.commit_person(person, txn)


def _ensure_child_in_family(db, fam: Family, child_handle: str) -> bool:
    for child_ref in fam.get_child_ref_list() or []:
        try:
            if getattr(child_ref, "ref", None) == child_handle:
                return False
            if (
                hasattr(child_ref, "get_reference_handle")
                and child_ref.get_reference_handle() == child_handle
            ):
                return False
        except Exception:
            pass

    fam.add_child_ref(_ensure_child_ref(child_handle))
    return True


def _place_parent_in_family(db, fam: Family, parent_handle: str) -> None:
    try:
        person = db.get_person_from_handle(parent_handle)
        gender = person.get_gender()
    except Exception:
        gender = Person.UNKNOWN

    father_handle = fam.get_father_handle()
    mother_handle = fam.get_mother_handle()

    if gender == Person.MALE:
        if not father_handle:
            fam.set_father_handle(parent_handle)
        elif not mother_handle and father_handle != parent_handle:
            fam.set_mother_handle(parent_handle)
        return

    if gender == Person.FEMALE:
        if not mother_handle:
            fam.set_mother_handle(parent_handle)
        elif not father_handle and mother_handle != parent_handle:
            fam.set_father_handle(parent_handle)
        return

    if not father_handle:
        fam.set_father_handle(parent_handle)
    elif not mother_handle:
        fam.set_mother_handle(parent_handle)


def _family_parent_set(fam: Family) -> set[str]:
    return set(filter(None, [fam.get_father_handle(), fam.get_mother_handle()]))


def _find_existing_family_for_parents(db, parent_handles: set[str]) -> Family | None:
    if not parent_handles:
        return None

    for parent_handle in parent_handles:
        try:
            person = db.get_person_from_handle(parent_handle)
        except HandleError:
            continue
        except Exception:
            continue

        if not person:
            continue

        for fam_handle in person.get_family_handle_list() or []:
            if not fam_handle:
                continue
            try:
                fam = db.get_family_from_handle(fam_handle)
            except HandleError:
                fam = None
            except Exception:
                fam = None

            if fam and _family_parent_set(fam) == parent_handles:
                return fam

    return None


def _find_person_by_fsid(db, fsid: str):
    fsid = (fsid or "").strip()
    if not fsid:
        return None

    try:
        from gramps.gen.fs import utilities as fs_utilities

        idx = getattr(fs_utilities, "FS_INDEX_PEOPLE", {})
        handle = idx.get(fsid)
        if handle:
            try:
                return db.get_person_from_handle(handle)
            except HandleError:
                idx.pop(fsid, None)
    except Exception:
        pass

    try:
        from gramps.gen.fs import utilities as fs_utilities

        get_fsftid = getattr(fs_utilities, "get_fsftid", None)
    except Exception:
        get_fsftid = None

    for handle in db.get_person_handles():
        person = db.get_person_from_handle(handle)
        if not person:
            continue
        try:
            pid = get_fsftid(person) if callable(get_fsftid) else ""
        except Exception:
            pid = ""
        if pid == fsid:
            return person

    return None


def _strip_unknowns_inplace(data: Any) -> None:
    key = "PersonInfo:visibleToAllWhenUsingFamilySearchApps"
    if isinstance(data, dict):
        data.pop(key, None)
        for value in list(data.values()):
            _strip_unknowns_inplace(value)
    elif isinstance(data, list):
        for value in list(data):
            _strip_unknowns_inplace(value)


def _resolve_redirected_fsid(session, fsid: str) -> str:
    fsid = (fsid or "").strip()
    if not fsid:
        return fsid

    head = getattr(session, "head_url", None)
    if not callable(head):
        return fsid

    try:
        path = f"/platform/tree/persons/{fsid}"
        resp = head(path)
        while resp is not None and getattr(resp, "status_code", None) == 301:
            headers = getattr(resp, "headers", {}) or {}
            forwarded = headers.get("X-Entity-Forwarded-Id") or headers.get(
                "x-entity-forwarded-id"
            )
            if not forwarded:
                break
            fsid = str(forwarded).strip()
            path = f"/platform/tree/persons/{fsid}"
            resp = head(path)
    except Exception:
        pass

    return fsid


def _ensure_status_schema(db) -> None:
    # status is stored on Person now, not in a DB table. 
    return


__all__ = [
    "FS_ATTR_CANON",
    "FS_ATTR_OLD",
    "FS_ATTR_HUMAN",
    "_dbg",
    "_bind_global_session",
    "_get_fs_id",
    "_set_fs_id",
    "_platform_json",
    "_fs_display_name",
    "_ensure_child_ref",
    "_family_other_parent_handle",
    "_ensure_person_has_family_handle",
    "_ensure_person_has_parent_family_handle",
    "_ensure_child_in_family",
    "_place_parent_in_family",
    "_family_parent_set",
    "_find_existing_family_for_parents",
    "_find_person_by_fsid",
    "_strip_unknowns_inplace",
    "_resolve_redirected_fsid",
    "_ensure_status_schema",
]
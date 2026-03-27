# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2026 Gabriel Rios
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <https://www.gnu.org/licenses/>.
#
# Push rules:
# - Default is always keep FamilySearch.
# - No deletions.
# - Only overwrite an existing FS conclusion when we have its FS conclusion id.
# - Notes: create new notes, update existing FS notes by note id, never delete.
# - Sources: create new SourceDescriptions, then attach them as Person SourceReferences.
#
# Export rules:
# - Create new FS people when the Gramps person has no usable FSID.
# - Basic export only: primary name + birth/death facts when available.
# - Optionally do the same for parents, spouse, and children.
# - Then create relationships.
# - Write created FSIDs back into Gramps through _FSFTID.

from __future__ import annotations

import mimetypes
import os
import re
import urllib.parse
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, cast

from gi.repository import Gtk

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.db import DbTxn
from gramps.gen.lib import Attribute, AttributeType, Family, Person
from gramps.gui.dialog import WarningDialog
from gramps.gui.listmodel import COLOR, NOSORT, TOGGLE, ListModel

from . import compare as fs_compare
from . import utilities as fs_utilities
from .import_ import deserializer as deserialize

_ = glocale.translation.gettext


# These match the ListModel layout built in _make_overview_model().
COL_PROP = 1
COL_GR_DATE = 2
COL_GR_VAL = 3
COL_FS_DATE = 4
COL_FS_VAL = 5
COL_XTYPE = 8
COL_XGR_ID = 9
COL_XFS_ID = 10
COL_XGR2 = 11
COL_XFS2 = 12


# ----------------------------
# small response helpers
# ----------------------------


def _response_status(resp: Any) -> int:
    try:
        return int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _response_headers(resp: Any) -> Dict[str, Any]:
    headers = getattr(resp, "headers", None)
    return headers if headers is not None else {}


def _debug_enabled() -> bool:
    return os.environ.get("GRAMPS_FS_DEBUG", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _person_handle(obj: Any) -> Optional[str]:
    handle = getattr(obj, "handle", None)
    if handle:
        return str(handle)

    getter = getattr(obj, "get_handle", None)
    if callable(getter):
        value = getter()
        return str(value) if value else None

    return None


# ----------------------------
# FamilySearch / cache helpers
# ----------------------------


def _extract_sdid_from_refish(obj: Any) -> str:
    # Source refs come back in a few shapes depending on endpoint and payload style.
    if obj is None:
        return ""

    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return ""
        match = re.search(r"/descriptions/([^/?#]+)", text)
        return match.group(1).strip() if match else ""

    if not isinstance(obj, dict):
        return ""

    value = str(obj.get("descriptionId") or "").strip()
    if value:
        return value

    desc = obj.get("description")
    if isinstance(desc, dict):
        value = str(desc.get("resourceId") or desc.get("descriptionId") or "").strip()
        if value:
            return value

        ref = str(desc.get("resource") or desc.get("href") or "").strip()
        if ref:
            match = re.search(r"/descriptions/([^/?#]+)", ref)
            if match:
                return match.group(1).strip()

    elif isinstance(desc, str):
        match = re.search(r"/descriptions/([^/?#]+)", desc)
        if match:
            return match.group(1).strip()

    for key in ("resource", "href", "about"):
        ref = str(obj.get(key) or "").strip()
        if not ref:
            continue
        match = re.search(r"/descriptions/([^/?#]+)", ref)
        if match:
            return match.group(1).strip()

    return ""


def _load_attached_person_source_description_ids(
    session: Any, person_fsid: str
) -> Optional[Set[str]]:
    # Returns None only when we cannot safely tell what is attached right now.
    person_fsid = (person_fsid or "").strip()
    if not person_fsid:
        return set()

    data = _session_get_json(session, f"/platform/tree/persons/{person_fsid}/sources")
    if not isinstance(data, dict) or not data:
        return None

    attached: Set[str] = set()

    for person_data in data.get("persons") or []:
        if not isinstance(person_data, dict):
            continue

        pid = str(person_data.get("id") or "").strip()
        if pid and pid != person_fsid:
            continue

        for source_ref in person_data.get("sources") or []:
            sdid = _extract_sdid_from_refish(source_ref)
            if sdid:
                attached.add(sdid)

    if attached:
        return attached

    # The deserialize fallback is still worth keeping because FS payload shapes vary.
    try:
        gx = deserialize.Gedcomx()
        try:
            gx.deserialize_json(data)
        except Exception:
            deserialize.deserialize_json(gx, data)

        for person_obj in list(getattr(gx, "persons", []) or []):
            if getattr(person_obj, "id", None) != person_fsid:
                continue

            for source_ref in list(getattr(person_obj, "sources", []) or []):
                sdid = str(getattr(source_ref, "descriptionId", "") or "").strip()
                if sdid:
                    attached.add(sdid)
            break
    except Exception:
        pass

    return attached


def _get_bound_fs_session() -> Any:
    try:
        from . import tree as fs_tree_mod
    except Exception:
        return None

    return getattr(fs_tree_mod, "_fs_session", None)


def _head_source_description(session: Any, sdid: str) -> Tuple[str, Any]:
    sdid = (sdid or "").strip()
    if not sdid:
        return sdid, None

    head = getattr(session, "head_url", None)
    if not callable(head):
        return sdid, None

    path = f"/platform/sources/descriptions/{sdid}"
    try:
        resp = head(path)
    except Exception:
        return sdid, None

    # Follow forwarded ids when FamilySearch tells us a source was merged.
    while resp is not None and _response_status(resp) == 301:
        headers = _response_headers(resp)

        new_id = str(headers.get("X-Entity-Forwarded-Id") or "").strip()
        if not new_id:
            location = str(
                headers.get("Location") or headers.get("location") or ""
            ).strip()
            if location:
                match = re.search(r"/descriptions/([^/?#]+)", location)
                if match:
                    new_id = match.group(1).strip()

        if not new_id:
            break

        sdid = new_id
        path = f"/platform/sources/descriptions/{sdid}"
        try:
            resp = head(path)
        except Exception:
            break

    return sdid, resp


def _resolve_active_source_description(session: Any, sdid: str) -> Tuple[str, str]:
    # Returns (resolved_id, state)
    # state: ok / merged / deleted / missing / unknown
    original = (sdid or "").strip()
    if not original:
        return "", "missing"

    resolved, resp = _head_source_description(session, original)
    status = _response_status(resp)

    if status == 410:
        return original, "deleted"
    if status == 404:
        return original, "missing"
    if status in (200, 204):
        if resolved and resolved != original:
            return resolved, "merged"
        return resolved or original, "ok"
    if status == 301 and resolved and resolved != original:
        return resolved, "merged"

    data = _session_get_json(session, f"/platform/sources/descriptions/{original}")
    if isinstance(data, dict) and data:
        return original, "ok"

    return resolved or original, "unknown"


def _bind_global_session(session: Any) -> None:
    # Older FS code still expects the live session on the tree module.
    try:
        from . import tree as fs_tree_mod
    except Exception:
        return

    setattr(fs_tree_mod, "_fs_session", session)


def _ensure_fs_tree(session: Any) -> Optional[Any]:
    _bind_global_session(session)

    try:
        import gramps.gui.fs.person.fsg_sync as fsg_sync
    except Exception:
        return None

    existing_tree: Optional[Any] = getattr(fsg_sync.FSG_Sync, "fs_Tree", None)
    if existing_tree is not None:
        return existing_tree

    try:
        from . import tree as fs_tree_mod
    except Exception:
        return None

    new_tree: Any = fs_tree_mod.Tree()
    try:
        setattr(new_tree, "_getsources", False)
    except Exception:
        pass

    fsg_sync.FSG_Sync.fs_Tree = new_tree
    return new_tree


def _prime_person_cache(session: Any, fsid: str, force: bool = False) -> Optional[Any]:
    # The compare layer leans on shared cache state. If we do not refresh it
    # after pushes, we end up comparing against stale FamilySearch data.
    fsid = (fsid or "").strip()
    if not fsid:
        return None

    tree_obj = _ensure_fs_tree(session)
    if tree_obj is None:
        return None

    if force:
        idx = getattr(deserialize.Person, "_index", None)
        if isinstance(idx, dict):
            idx.pop(fsid, None)

        for attr_name in ("_persons", "persons", "_person", "_people"):
            cache = getattr(tree_obj, attr_name, None)
            if isinstance(cache, dict):
                cache.pop(fsid, None)

    add_persons = getattr(tree_obj, "add_persons", None)
    if callable(add_persons):
        try:
            add_persons({fsid})
            return tree_obj
        except TypeError:
            try:
                add_persons([fsid])
                return tree_obj
            except Exception:
                pass
        except Exception:
            pass

    for method_name in ("add_person", "add_persons"):
        method = getattr(tree_obj, method_name, None)
        if not callable(method):
            continue
        try:
            method(fsid)
            return tree_obj
        except Exception:
            continue

    return tree_obj


def _unwrap_tree_model(model: Any) -> Optional[Any]:
    # Gramps ListModel is a wrapper. Most row readers want the raw tree model.
    if model is None:
        return None

    def looks_like_tree_model(obj: Any) -> bool:
        return (
            obj is not None
            and callable(getattr(obj, "get_iter_first", None))
            and callable(getattr(obj, "get_value", None))
            and callable(getattr(obj, "iter_children", None))
            and callable(getattr(obj, "iter_next", None))
        )

    if looks_like_tree_model(model):
        return model

    for attr_name in ("model", "_model", "store", "treestore", "liststore"):
        raw = getattr(model, attr_name, None)
        if looks_like_tree_model(raw):
            return raw

    for attr_name in ("treeview", "_treeview", "widget", "view"):
        treeview = getattr(model, attr_name, None)
        if treeview is None or not callable(getattr(treeview, "get_model", None)):
            continue
        try:
            raw = treeview.get_model()
        except Exception:
            raw = None
        if looks_like_tree_model(raw):
            return raw

    return None


def _walk(model: Any) -> Iterable[Any]:
    raw = _unwrap_tree_model(model)
    if raw is None:
        return

    try:
        current = raw.get_iter_first()
    except Exception:
        return

    stack = [current] if current else []
    while stack:
        current = stack.pop()
        yield current

        try:
            child = raw.iter_children(current)
        except Exception:
            child = None

        children = []
        while child:
            children.append(child)
            try:
                child = raw.iter_next(child)
            except Exception:
                child = None

        for entry in reversed(children):
            stack.append(entry)


# ----------------------------
# memory helpers
# ----------------------------


def _session_post_binary(
    session: Any, endpoint: str, data: bytes, headers: Dict[str, str]
) -> Any:
    endpoint = endpoint if endpoint.startswith("/") else ("/" + endpoint)

    post = getattr(session, "post", None)
    if callable(post):
        try:
            return post(endpoint, data=data, headers=headers)
        except TypeError:
            try:
                return post(endpoint, data, headers)
            except Exception:
                pass
        except Exception:
            pass

    post_url = getattr(session, "post_url", None)
    if callable(post_url):
        try:
            return post_url(endpoint, data=data, headers=headers)
        except TypeError:
            try:
                return post_url(endpoint, data, headers)
            except Exception:
                pass
        except Exception:
            pass

    # Raw upload fallback for sessions that do not expose a binary-friendly helper.
    try:
        import requests  # type: ignore
    except Exception:
        return None

    base = (
        getattr(session, "api_url", "")
        or getattr(session, "API_URL", "")
        or "https://api.familysearch.org"
    )
    url = str(base).rstrip("/") + endpoint
    token = getattr(session, "access_token", "") or ""

    final_headers = dict(headers)
    if (
        token
        and "Authorization" not in final_headers
        and "authorization" not in final_headers
    ):
        final_headers["Authorization"] = f"Bearer {token}"

    try:
        return requests.post(url, data=data, headers=final_headers, timeout=90)
    except Exception:
        return None


def _extract_memory_ids_from_upload_response(resp: Any) -> Tuple[str, str]:
    mem_id = ""
    mem_ref_id = ""

    headers = _response_headers(resp)
    mem_id = str(headers.get("X-Entity-Id") or headers.get("X-entity-id") or "").strip()

    location = str(headers.get("Location") or headers.get("location") or "").strip()
    if location:
        match = re.search(r"/memory-references/([^/?#]+)", location)
        if match:
            mem_ref_id = match.group(1).strip()
        else:
            tail = re.search(r"/([^/?#]+)$", location)
            if tail:
                mem_ref_id = tail.group(1).strip()

    return mem_id, mem_ref_id


def _resolve_media_path(db: Any, media_obj: Any) -> Optional[str]:
    path = ""

    for method_name in ("get_path", "get_file_path", "get_filename", "get_file_name"):
        method = getattr(media_obj, method_name, None)
        if not callable(method):
            continue

        try:
            path = str(method() or "").strip()
        except Exception:
            path = ""

        if path:
            break

    if not path:
        path = str(
            getattr(media_obj, "path", "") or getattr(media_obj, "filename", "") or ""
        ).strip()

    if not path:
        return None

    if path.startswith("http://") or path.startswith("https://"):
        return None

    path = os.path.expanduser(path)

    if os.path.isabs(path) and os.path.exists(path):
        return path

    media_base = ""
    for method_name in ("get_mediapath", "get_media_path"):
        method = getattr(db, method_name, None)
        if not callable(method):
            continue

        try:
            media_base = str(method() or "").strip()
        except Exception:
            media_base = ""

        if media_base:
            break

    if media_base:
        candidate = os.path.join(media_base, path)
        if os.path.exists(candidate):
            return candidate

    if os.path.exists(path):
        return os.path.abspath(path)

    return None


def _collect_memory_push_items(dbstate: Any, person: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    db = dbstate.db

    media_refs = list(person.get_media_list() or [])

    for media_ref in media_refs:
        handle = str(getattr(media_ref, "ref", "") or "").strip()
        if not handle:
            ref_getter = getattr(media_ref, "get_reference_handle", None)
            if callable(ref_getter):
                value = ref_getter()
                handle = str(value or "").strip()

        if not handle:
            continue

        media_obj = None
        for getter_name in ("get_media_from_handle", "get_media_object_from_handle"):
            getter = getattr(db, getter_name, None)
            if not callable(getter):
                continue
            media_obj = getter(handle)
            if media_obj:
                break

        if not media_obj:
            continue

        existing_fsid = (fs_utilities.get_fsftid(media_obj) or "").strip()
        if existing_fsid:
            continue

        file_path = _resolve_media_path(db, media_obj)
        if not file_path:
            continue

        description = ""
        for method_name in ("get_description", "get_desc"):
            method = getattr(media_obj, method_name, None)
            if not callable(method):
                continue

            try:
                description = str(method() or "").strip()
            except Exception:
                description = ""

            if description:
                break

        if not description:
            description = os.path.basename(file_path)

        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        artifact_type = "Photo" if mime.startswith("image/") else "Document"

        items.append(
            {
                "kind": "memory_create",
                "label": _("Memory: {t}").format(t=description),
                "fs_val": _("(missing)"),
                "gr_val": file_path,
                "media_handle": handle,
                "file_path": file_path,
                "mime": mime,
                "artifact_type": artifact_type,
                "title": description,
                "description": description,
                "filename": os.path.basename(file_path),
            }
        )

    return items


def _upload_person_memory(
    session: Any,
    person_fsid: str,
    file_path: str,
    *,
    title: str,
    description: str,
    filename: str,
    person_name: str,
    artifact_type: str,
) -> Tuple[Optional[str], Optional[str], str]:
    person_fsid = (person_fsid or "").strip()
    if not person_fsid:
        return None, None, "Missing person FSID"

    if not file_path or not os.path.exists(file_path):
        return None, None, "File not found"

    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    final_filename = filename or os.path.basename(file_path)

    params: Dict[str, str] = {}
    if title:
        params["title"] = title
    if description:
        params["description"] = description
    if person_name:
        params["personName"] = person_name
    if final_filename:
        params["filename"] = final_filename
    if artifact_type:
        params["type"] = artifact_type

    endpoint = f"/platform/tree/persons/{person_fsid}/memories"
    if params:
        endpoint = f"{endpoint}?{urllib.parse.urlencode(params)}"

    try:
        with open(file_path, "rb") as handle:
            data = handle.read()
    except OSError as err:
        return None, None, f"Failed to read file: {err}"

    headers = {
        "accept": "application/x-fs-v1+json",
        "content-type": mime,
        "content-disposition": f'attachment; filename="{final_filename}"',
    }

    resp = _session_post_binary(session, endpoint, data, headers=headers)
    if resp is None:
        return None, None, "No response from FamilySearch"

    status = _response_status(resp)
    if status not in (200, 201):
        return None, None, f"HTTP {status}: {_err_text(resp)}"

    mem_id, mem_ref_id = _extract_memory_ids_from_upload_response(resp)
    if not mem_id:
        return None, None, "Upload succeeded but no memory id returned"

    return mem_id, (mem_ref_id or None), ""


# ----------------------------
# HTTP wrappers
# ----------------------------


def _debug_dump_compare_model(model: Any) -> None:
    if not _debug_enabled():
        return

    raw = _unwrap_tree_model(model)
    if raw is None:
        print("[FS SYNC] compare model dump: could not unwrap model")
        return

    print("[FS SYNC] ----- compare model dump begin -----")
    index = 0

    for row_iter in _walk(raw):
        try:
            row = {
                "xtype": str(raw.get_value(row_iter, COL_XTYPE) or ""),
                "prop": str(raw.get_value(row_iter, COL_PROP) or ""),
                "gr_date": str(raw.get_value(row_iter, COL_GR_DATE) or ""),
                "gr_val": str(raw.get_value(row_iter, COL_GR_VAL) or ""),
                "fs_date": str(raw.get_value(row_iter, COL_FS_DATE) or ""),
                "fs_val": str(raw.get_value(row_iter, COL_FS_VAL) or ""),
                "xgr": str(raw.get_value(row_iter, COL_XGR_ID) or ""),
                "xfs": str(raw.get_value(row_iter, COL_XFS_ID) or ""),
                "xgr2": str(raw.get_value(row_iter, COL_XGR2) or ""),
                "xfs2": str(raw.get_value(row_iter, COL_XFS2) or ""),
            }
        except Exception as err:
            print(f"[FS SYNC] row {index} <error reading row: {err}>")
            index += 1
            continue

        print(
            "[FS SYNC] row %d xtype=%r prop=%r gr_date=%r gr_val=%r fs_date=%r fs_val=%r xgr=%r xfs=%r xgr2=%r xfs2=%r"
            % (
                index,
                row["xtype"],
                row["prop"],
                row["gr_date"],
                row["gr_val"],
                row["fs_date"],
                row["fs_val"],
                row["xgr"],
                row["xfs"],
                row["xgr2"],
                row["xfs2"],
            )
        )
        index += 1

    print("[FS SYNC] ----- compare model dump end -----")


def _session_get_json(session: Any, endpoint: str) -> Optional[dict]:
    endpoint = endpoint if endpoint.startswith("/") else ("/" + endpoint)

    for method_name in ("get_jsonurl", "get_json", "get_gedcomx"):
        method = getattr(session, method_name, None)
        if not callable(method):
            continue

        try:
            data = method(endpoint)
        except Exception:
            continue

        if isinstance(data, dict):
            return data

        if hasattr(data, "json"):
            try:
                return data.json() or {}
            except Exception:
                pass

    get_url = getattr(session, "get_url", None)
    if callable(get_url):
        try:
            resp = get_url(endpoint, {"Accept": "application/x-gedcomx-v1+json"})
            if resp and hasattr(resp, "json"):
                return resp.json() or {}
        except TypeError:
            try:
                resp = get_url(
                    endpoint, headers={"Accept": "application/x-gedcomx-v1+json"}
                )
                if resp and hasattr(resp, "json"):
                    return resp.json() or {}
            except Exception:
                pass
        except Exception:
            pass

    try:
        import requests  # type: ignore
    except Exception:
        return None

    base = (
        getattr(session, "api_url", "")
        or getattr(session, "API_URL", "")
        or "https://api.familysearch.org"
    )
    token = getattr(session, "access_token", "") or ""
    url = str(base).rstrip("/") + endpoint

    headers = {"Accept": "application/x-gedcomx-v1+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json() or {}
    except Exception:
        return None


def _session_post_json(
    session: Any, endpoint: str, payload: dict, *, headers: Optional[dict] = None
) -> Any:
    endpoint = endpoint if endpoint.startswith("/") else ("/" + endpoint)

    final_headers = {
        "accept": "application/x-gedcomx-v1+json",
        "content-type": "application/x-gedcomx-v1+json",
    }
    if headers:
        final_headers.update(headers)

    post = getattr(session, "post", None)
    if callable(post):
        try:
            return post(endpoint, json=payload, headers=final_headers)
        except TypeError:
            try:
                return post(endpoint, payload, final_headers)
            except Exception:
                pass
        except Exception:
            pass

    post_url = getattr(session, "post_url", None)
    if callable(post_url):
        try:
            return post_url(endpoint, json=payload, headers=final_headers)
        except Exception:
            pass

    try:
        import requests  # type: ignore
    except Exception:
        return None

    base = (
        getattr(session, "api_url", "")
        or getattr(session, "API_URL", "")
        or "https://api.familysearch.org"
    )
    token = getattr(session, "access_token", "") or ""
    url = str(base).rstrip("/") + endpoint

    if token and "Authorization" not in final_headers:
        final_headers["Authorization"] = f"Bearer {token}"

    try:
        return requests.post(url, json=payload, headers=final_headers, timeout=30)
    except Exception:
        return None


def _head_person(session: Any, fsid: str) -> Tuple[str, Any]:
    fsid = (fsid or "").strip()
    if not fsid:
        return fsid, None

    head = getattr(session, "head_url", None)
    if not callable(head):
        return fsid, None

    path = f"/platform/tree/persons/{fsid}"
    try:
        resp = head(path)
    except Exception:
        return fsid, None

    while (
        resp is not None
        and _response_status(resp) == 301
        and "X-Entity-Forwarded-Id" in _response_headers(resp)
    ):
        new_id = str(_response_headers(resp).get("X-Entity-Forwarded-Id") or "").strip()
        if not new_id:
            break

        fsid = new_id
        path = f"/platform/tree/persons/{fsid}"
        try:
            resp = head(path)
        except Exception:
            break

    return fsid, resp


def _err_text(resp: Any) -> str:
    if resp is None:
        return ""

    try:
        data = resp.json()
        if (
            isinstance(data, dict)
            and isinstance(data.get("errors"), list)
            and data["errors"]
        ):
            first = data["errors"][0]
            message = (first.get("message") or "").strip()
            code = (first.get("code") or "").strip()
            if code and message:
                return f"{code}: {message}"
            return message or code
    except Exception:
        pass

    text = str(getattr(resp, "text", "") or "").strip()
    if not text:
        return ""
    return text[:600] + ("..." if len(text) > 600 else "")


# ----------------------------
# model builders
# ----------------------------


def _make_overview_model() -> ListModel:
    treeview = Gtk.TreeView()
    titles = [
        (_(""), 1, 18, COLOR),
        (_("Property"), 2, 180),
        (_("Gramps date"), 3, 115),
        (_("Gramps value"), 4, 420),
        (_("FS date"), 5, 115),
        (_("FamilySearch value"), 6, 420),
        (" ", NOSORT, 1),
        ("x", 8, 5, TOGGLE, True, lambda *_a, **_k: None),
        (_("xType"), NOSORT, 0),
        (_("xGr"), NOSORT, 0),
        (_("xFs"), NOSORT, 0),
        (_("xGr2"), NOSORT, 0),
        (_("xFs2"), NOSORT, 0),
    ]
    return ListModel(treeview, titles, list_mode="tree")


# ----------------------------
# collect push items
# ----------------------------


def _resolve_active_person(session: Any, fsid: str) -> Tuple[str, str]:
    # Returns (resolved_id, state)
    # state: ok / merged / deleted / missing / unknown
    original = (fsid or "").strip()
    if not original:
        return "", "missing"

    resolved, resp = _head_person(session, original)
    status = _response_status(resp)

    if status == 410:
        return original, "deleted"
    if status == 404:
        return original, "missing"
    if status in (200, 204):
        if resolved and resolved != original:
            return resolved, "merged"
        return resolved or original, "ok"
    if status == 301 and resolved and resolved != original:
        return resolved, "merged"

    data = _session_get_json(session, f"/platform/tree/persons/{original}")
    if isinstance(data, dict) and data:
        return original, "ok"

    return resolved or original, "unknown"


def _fact_type_from_label(label: str) -> str:
    key = re.sub(r"\s+", " ", (label or "").strip().lower())

    mapping = {
        "birth": "http://gedcomx.org/Birth",
        "death": "http://gedcomx.org/Death",
        "burial": "http://gedcomx.org/Burial",
        "cremation": "http://gedcomx.org/Cremation",
        "baptism": "http://gedcomx.org/Baptism",
        "christening": "http://gedcomx.org/Christening",
        "residence": "http://gedcomx.org/Residence",
        "occupation": "http://gedcomx.org/Occupation",
        "marriage": "http://gedcomx.org/Marriage",
        "divorce": "http://gedcomx.org/Divorce",
        "annulment": "http://gedcomx.org/Annulment",
        "immigration": "http://gedcomx.org/Immigration",
        "emigration": "http://gedcomx.org/Emigration",
        "naturalization": "http://gedcomx.org/Naturalization",
        "stillbirth": "http://gedcomx.org/Stillbirth",
    }

    if key in mapping:
        return mapping[key]

    for prefix, uri in mapping.items():
        if key.startswith(prefix):
            return uri

    return ""


def _collect_overview_push_items(model: Any) -> List[Dict[str, Any]]:
    raw = _unwrap_tree_model(model)
    if raw is None:
        return []

    items: List[Dict[str, Any]] = []

    def norm(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    for row_iter in _walk(raw):
        try:
            x_type_raw = str(raw.get_value(row_iter, COL_XTYPE) or "").strip()
        except Exception:
            x_type_raw = ""

        x_type = x_type_raw.lower()
        label = str(raw.get_value(row_iter, COL_PROP) or "")
        label_norm = norm(label)

        gr_date = str(raw.get_value(row_iter, COL_GR_DATE) or "")
        gr_val = str(raw.get_value(row_iter, COL_GR_VAL) or "")
        fs_date = str(raw.get_value(row_iter, COL_FS_DATE) or "")
        fs_val = str(raw.get_value(row_iter, COL_FS_VAL) or "")
        xfs_id = str(raw.get_value(row_iter, COL_XFS_ID) or "").strip()

        is_name_row = x_type in (
            "primary_name",
            "name",
            "preferred_name",
            "primary name",
        ) or label_norm in ("name", "preferred name", "primary name")

        fact_type = _fact_type_from_label(label)
        is_fact_row = x_type in ("fact", "event", "events") or bool(fact_type)

        if is_name_row:
            if not gr_val.strip():
                continue
            if gr_val.strip() == fs_val.strip():
                continue

            items.append(
                {
                    "kind": "primary_name",
                    "label": label or _("Primary Name"),
                    "fs_id": xfs_id,
                    "gr_val": gr_val,
                    "fs_val": fs_val,
                    # compare_names() stores surname in xGr2 and given in xFs2
                    "gr_surname": str(raw.get_value(row_iter, COL_XGR2) or "").strip(),
                    "gr_given": str(raw.get_value(row_iter, COL_XFS2) or "").strip(),
                    "create": not bool(xfs_id),
                }
            )
            continue

        if is_fact_row:
            if not (gr_date.strip() or gr_val.strip()):
                continue
            if gr_date.strip() == fs_date.strip() and gr_val.strip() == fs_val.strip():
                continue

            items.append(
                {
                    "kind": "fact",
                    "label": label,
                    "fs_id": xfs_id,
                    "gr_date": gr_date,
                    "gr_val": gr_val,
                    "fs_date": fs_date,
                    "fs_val": fs_val,
                    "fact_type": fact_type,
                    "create": not bool(xfs_id),
                }
            )

    return items


def _extract_gramps_note_fsid(gr_note_obj: Any) -> str:
    text_obj = getattr(gr_note_obj, "text", None)
    if text_obj is None or not hasattr(text_obj, "get_tags"):
        return ""

    tags = text_obj.get_tags()
    if not tags:
        return ""

    for tag in tags:
        try:
            name = getattr(getattr(tag, "name", None), "name", None)
            value = getattr(tag, "value", "") or ""
        except Exception:
            continue

        if name == "LINK" and isinstance(value, str) and value.startswith("_fsftid="):
            return value[8:].strip()

    return ""


def _gramps_note_title(gr_note_obj: Any) -> str:
    note_type = getattr(gr_note_obj, "type", None)
    if note_type is not None and hasattr(note_type, "xml_str"):
        try:
            return _(note_type.xml_str())
        except Exception:
            pass

    value = getattr(gr_note_obj, "type", "Note")
    return str(value) if value is not None else _("Note")


def _normalize_note_text(text: str) -> str:
    cleaned = text or ""
    if cleaned.startswith("\ufeff"):
        cleaned = cleaned[1:]
    return cleaned.strip()


def _load_fs_person_notes(session: Any, fsid: str) -> List[Any]:
    data = _session_get_json(session, f"/platform/tree/persons/{fsid}/notes")
    if not data:
        return []

    gx = deserialize.Gedcomx()
    try:
        gx.deserialize_json(data)
    except Exception:
        try:
            deserialize.deserialize_json(gx, data)
        except Exception:
            return []

    for person_obj in list(getattr(gx, "persons", []) or []):
        if getattr(person_obj, "id", None) == fsid:
            return list(getattr(person_obj, "notes", []) or [])

    notes = getattr(gx, "notes", None)
    if notes:
        return list(notes) if isinstance(notes, (list, set, tuple)) else [notes]

    return []


def _collect_note_push_items(
    dbstate: Any, person: Any, session: Any, fsid: str
) -> List[Dict[str, Any]]:
    _prime_person_cache(session, fsid)

    fs_notes = _load_fs_person_notes(session, fsid)
    remaining = list(fs_notes)

    def take_matching(note_id: str, subject: str) -> Optional[Any]:
        if note_id:
            for note in remaining:
                if getattr(note, "id", None) == note_id:
                    remaining.remove(note)
                    return note

        if subject:
            for note in remaining:
                if (getattr(note, "subject", None) or "") == subject:
                    remaining.remove(note)
                    return note

        return None

    items: List[Dict[str, Any]] = []

    note_handles = list(person.get_note_list() or [])
    for handle in note_handles:
        gr_note = dbstate.db.get_note_from_handle(handle)
        if not gr_note:
            continue

        gr_subject = _gramps_note_title(gr_note)
        gr_text = _normalize_note_text(getattr(gr_note, "get", lambda: "")() or "")
        note_id = _extract_gramps_note_fsid(gr_note)

        fs_match = take_matching(note_id, gr_subject)
        if fs_match is None:
            if not (gr_subject.strip() or gr_text.strip()):
                continue

            items.append(
                {
                    "kind": "note_create",
                    "label": _("Note: {title}").format(
                        title=gr_subject or _("(untitled)")
                    ),
                    "fs_id": "",
                    "fs_val": _("(missing)"),
                    "gr_val": gr_text or _("(empty)"),
                    "gr_subject": gr_subject,
                    "gr_text": gr_text,
                }
            )
            continue

        fs_subject = getattr(fs_match, "subject", "") or ""
        fs_text = _normalize_note_text(getattr(fs_match, "text", "") or "")
        fs_id = (getattr(fs_match, "id", "") or "").strip()

        if (
            _normalize_note_text(gr_text) == _normalize_note_text(fs_text)
            and gr_subject == fs_subject
        ):
            continue

        items.append(
            {
                "kind": "note_update",
                "label": _("Note: {title}").format(
                    title=gr_subject or fs_subject or _("(untitled)")
                ),
                "fs_id": fs_id,
                "fs_val": fs_text or _("(empty)"),
                "gr_val": gr_text or _("(empty)"),
                "gr_subject": gr_subject or fs_subject,
                "gr_text": gr_text,
                "fs_subject": fs_subject,
                "fs_text": fs_text,
            }
        )

    return items


def _collect_source_push_items(
    dbstate: Any, person: Any, session: Any, person_fsid: str
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    try:
        import gramps.gui.fs.import_ as fs_import_mod

        fs_import = cast(ModuleType, fs_import_mod)
    except Exception:
        fs_import = None

    attached_person_sdids: Optional[Set[str]] = None
    if session is not None and person_fsid:
        attached_person_sdids = _load_attached_person_source_description_ids(
            session, person_fsid
        )

    citation_handles: Set[str] = set()
    citation_scope: Dict[str, str] = {}

    for handle in list(person.get_citation_list() or []):
        citation_handles.add(handle)
        citation_scope.setdefault(handle, "person")

    for event_ref in list(person.get_event_ref_list() or []):
        event = dbstate.db.get_event_from_handle(event_ref.ref)
        if not event:
            continue
        for handle in list(event.get_citation_list() or []):
            citation_handles.add(handle)
            citation_scope.setdefault(handle, "event")

    for family_handle in list(person.get_family_handle_list() or []):
        family = dbstate.db.get_family_from_handle(family_handle)
        if not family:
            continue

        for handle in list(family.get_citation_list() or []):
            citation_handles.add(handle)
            citation_scope.setdefault(handle, "family")

        for event_ref in list(family.get_event_ref_list() or []):
            event = dbstate.db.get_event_from_handle(event_ref.ref)
            if not event:
                continue
            for handle in list(event.get_citation_list() or []):
                citation_handles.add(handle)
                citation_scope.setdefault(handle, "family_event")

    for citation_handle in sorted(citation_handles):
        citation = dbstate.db.get_citation_from_handle(citation_handle)
        if not citation:
            continue

        scope = citation_scope.get(citation_handle, "person")
        raw_sd_id = (fs_utilities.get_fsftid(citation) or "").strip()

        resolved_sd_id = raw_sd_id
        sd_state = "missing"
        is_attached_to_person: Optional[bool] = None

        if raw_sd_id:
            if session is not None:
                resolved_sd_id, sd_state = _resolve_active_source_description(
                    session, raw_sd_id
                )
            else:
                sd_state = "unknown"

            if scope == "person" and attached_person_sdids is not None:
                is_attached_to_person = bool(
                    (raw_sd_id and raw_sd_id in attached_person_sdids)
                    or (resolved_sd_id and resolved_sd_id in attached_person_sdids)
                )

            if (
                scope == "person"
                and is_attached_to_person is True
                and sd_state == "merged"
                and resolved_sd_id
                and resolved_sd_id != raw_sd_id
            ):
                link_fn = getattr(fs_utilities, "link_gramps_fs_id", None)
                if callable(link_fn):
                    try:
                        link_fn(dbstate.db, citation, resolved_sd_id)
                    except Exception:
                        pass

            if scope == "person":
                if is_attached_to_person is True and sd_state in ("ok", "merged"):
                    continue

                if is_attached_to_person is None and sd_state in (
                    "ok",
                    "merged",
                    "unknown",
                ):
                    continue
            else:
                if sd_state in ("ok", "merged", "unknown"):
                    continue

        title = ""
        note_text = ""
        url = ""
        date = ""

        if fs_import is not None and hasattr(fs_import, "IntermediateSource"):
            try:
                source_model = getattr(fs_import, "IntermediateSource")()
                source_model.from_gramps(dbstate.db, citation)
                title = (getattr(source_model, "citation_title", "") or "").strip()
                note_text = (getattr(source_model, "note_text", "") or "").strip()
                url = (getattr(source_model, "url", "") or "").strip()
                date = str(getattr(source_model, "date", "") or "").strip()
            except Exception:
                pass

        if not title:
            title = (getattr(citation, "page", "") or "").strip()
        if not title:
            title = _("Source from Gramps")

        lines = [title]
        if url:
            lines.append(url)
        if date:
            lines.append(date)
        if note_text:
            lines.append(note_text)

        if raw_sd_id and sd_state == "deleted":
            fs_val = _("(deleted on FamilySearch)")
        elif raw_sd_id and sd_state == "missing":
            fs_val = _("(missing on FamilySearch)")
        elif scope == "person" and raw_sd_id and is_attached_to_person is False:
            fs_val = _("(not attached to this person on FamilySearch)")
        else:
            fs_val = _("(missing)")

        items.append(
            {
                "kind": "source_create",
                "label": _("Source: {title}").format(title=title),
                "fs_val": fs_val,
                "gr_val": "\n".join([entry for entry in lines if entry]),
                "citation_handle": citation_handle,
                "title": title,
                "url": url,
                "note_text": note_text,
                "date": date,
                "old_fs_id": raw_sd_id,
                "fs_state": sd_state,
                "scope": scope,
            }
        )

    return items


# ----------------------------
# prompt UI
# ----------------------------


def _prompt(
    parent: Gtk.Window, items: List[Dict[str, Any]]
) -> Tuple[str, List[Dict[str, Any]]]:
    dialog = Gtk.Dialog(title=_("Sync to FamilySearch"), transient_for=parent, flags=0)
    dialog.set_modal(True)
    dialog.set_default_size(820, 620)
    dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dialog.add_button(_("Apply Selected"), Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)

    box = dialog.get_content_area()
    box.set_border_width(10)
    box.set_spacing(8)

    intro = Gtk.Label()
    intro.set_xalign(0.0)
    intro.set_line_wrap(True)
    intro.set_markup(
        _(
            "<b>Choose what to change on FamilySearch</b>\n"
            "Default is to keep FamilySearch.\n"
            "<i>No deletions are performed.</i>"
        )
    )
    box.pack_start(intro, False, False, 0)

    message_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    message_row.pack_start(Gtk.Label(label=_("Change message:")), False, False, 0)

    change_entry = Gtk.Entry()
    change_entry.set_hexpand(True)
    change_entry.set_text(_("Updated from Gramps"))
    message_row.pack_start(change_entry, True, True, 0)
    box.pack_start(message_row, False, False, 0)

    button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    select_all = Gtk.Button(label=_("Select all"))
    select_none = Gtk.Button(label=_("Select none"))
    button_row.pack_start(select_all, False, False, 0)
    button_row.pack_start(select_none, False, False, 0)
    button_row.set_halign(Gtk.Align.START)
    box.pack_start(button_row, False, False, 0)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    box.pack_start(scroll, True, True, 0)

    grid = Gtk.Grid()
    grid.set_column_spacing(12)
    grid.set_row_spacing(10)
    grid.set_border_width(4)
    scroll.add(grid)

    headings = [
        Gtk.Label(label=_("Field")),
        Gtk.Label(label=_("FamilySearch")),
        Gtk.Label(label=_("Gramps")),
        Gtk.Label(label=_("Action")),
    ]
    for heading in headings:
        heading.set_xalign(0.0)
        try:
            heading.get_style_context().add_class("heading")
        except Exception:
            pass

    grid.attach(headings[0], 0, 0, 1, 1)
    grid.attach(headings[1], 1, 0, 1, 1)
    grid.attach(headings[2], 2, 0, 1, 1)
    grid.attach(headings[3], 3, 0, 1, 1)

    combos: List[Gtk.ComboBoxText] = []
    effective_items: List[Dict[str, Any]] = []

    def fmt(value: str) -> str:
        text = (value or "").strip()
        return text if text else _("(empty)")

    row = 1
    for item in items:
        kind = item.get("kind", "")

        field_label = Gtk.Label(label=str(item.get("label") or ""))
        field_label.set_xalign(0.0)
        field_label.set_line_wrap(True)

        fs_label = Gtk.Label(label=fmt(str(item.get("fs_val") or "")))
        fs_label.set_xalign(0.0)
        fs_label.set_line_wrap(True)

        gr_label = Gtk.Label(label=fmt(str(item.get("gr_val") or "")))
        gr_label.set_xalign(0.0)
        gr_label.set_line_wrap(True)

        combo = Gtk.ComboBoxText()
        combo.append_text(_("Keep FamilySearch"))

        if kind in ("primary_name", "fact", "note_update"):
            combo.append_text(_("Overwrite with Gramps"))
        elif kind in ("note_create", "source_create", "memory_create"):
            combo.append_text(_("Add from Gramps"))
        else:
            combo.append_text(_("Apply from Gramps"))

        combo.set_active(0)

        grid.attach(field_label, 0, row, 1, 1)
        grid.attach(fs_label, 1, row, 1, 1)
        grid.attach(gr_label, 2, row, 1, 1)
        grid.attach(combo, 3, row, 1, 1)

        combos.append(combo)
        effective_items.append(item)
        row += 1

    def set_all(apply_changes: bool) -> None:
        index = 1 if apply_changes else 0
        for combo in combos:
            combo.set_active(index)

    select_all.connect("clicked", lambda *_: set_all(True))
    select_none.connect("clicked", lambda *_: set_all(False))

    dialog.show_all()
    response = dialog.run()

    change_message = (change_entry.get_text() or "").strip()
    chosen: List[Dict[str, Any]] = []

    if response == Gtk.ResponseType.OK:
        for item, combo in zip(effective_items, combos):
            if combo.get_active() == 1:
                chosen.append(item)

    dialog.destroy()
    return change_message, chosen


# ----------------------------
# payload builders
# ----------------------------


def _build_person_payload(
    fsid: str, chosen: List[Dict[str, Any]], change_message: str
) -> Dict[str, Any]:
    message = (change_message or "").strip() or _("Updated from Gramps")

    person_obj: Dict[str, Any] = {"id": fsid}
    names: List[Dict[str, Any]] = []
    facts: List[Dict[str, Any]] = []

    for item in chosen:
        kind = item.get("kind")

        if kind == "primary_name":
            full = str(item.get("gr_val") or "").strip()
            given = str(item.get("gr_given") or "").strip()
            surname = str(item.get("gr_surname") or "").strip()

            if not full and not (given or surname):
                continue

            name_form: Dict[str, Any] = {
                "fullText": full or f"{given} {surname}".strip()
            }

            parts: List[Dict[str, str]] = []
            if given:
                parts.append({"type": "http://gedcomx.org/Given", "value": given})
            if surname:
                parts.append({"type": "http://gedcomx.org/Surname", "value": surname})
            if parts:
                name_form["parts"] = parts

            name_obj: Dict[str, Any] = {
                "preferred": True,
                "nameForms": [name_form],
                "attribution": {"changeMessage": message},
            }

            name_id = str(item.get("fs_id") or "").strip()
            if name_id:
                name_obj["id"] = name_id

            names.append(name_obj)
            continue

        if kind == "fact":
            fact_id = str(item.get("fs_id") or "").strip()
            fact_type = ""

            if fact_id:
                fs_person = deserialize.Person._index.get(fsid)
                if fs_person and getattr(fs_person, "facts", None):
                    for fact_obj in fs_person.facts:
                        if getattr(fact_obj, "id", None) == fact_id and getattr(
                            fact_obj, "type", None
                        ):
                            fact_type = str(fact_obj.type)
                            break

            if not fact_type:
                fact_type = str(item.get("fact_type") or "").strip()

            if not fact_type:
                continue

            fact: Dict[str, Any] = {
                "type": fact_type,
                "attribution": {"changeMessage": message},
            }

            if fact_id:
                fact["id"] = fact_id

            gr_date = str(item.get("gr_date") or "").strip()
            gr_place = str(item.get("gr_val") or "").strip()

            if gr_date:
                fact["date"] = {"formal": gr_date, "original": gr_date}
            if gr_place:
                fact["place"] = {"original": gr_place}

            if "date" not in fact and "place" not in fact:
                continue

            facts.append(fact)

    if names:
        person_obj["names"] = names
    if facts:
        person_obj["facts"] = facts

    return {"persons": [person_obj]}


def _build_notes_payload(
    fsid: str, chosen: List[Dict[str, Any]], change_message: str
) -> Optional[Dict[str, Any]]:
    message = (change_message or "").strip() or _("Updated from Gramps")
    notes: List[Dict[str, Any]] = []

    for item in chosen:
        if item.get("kind") not in ("note_create", "note_update"):
            continue

        subject = str(item.get("gr_subject") or "").strip()
        text = str(item.get("gr_text") or "").strip()

        note: Dict[str, Any] = {
            "subject": subject,
            "text": text,
            "attribution": {"changeMessage": message},
        }

        fs_note_id = str(item.get("fs_id") or "").strip()
        if item.get("kind") == "note_update" and fs_note_id:
            note["id"] = fs_note_id

        notes.append(note)

    if not notes:
        return None

    return {"persons": [{"id": fsid, "notes": notes}]}


def _build_source_description_payload(
    title: str, citation_text: str, about: str, change_message: str
) -> Dict[str, Any]:
    message = (change_message or "").strip() or _("Updated from Gramps")

    sd: Dict[str, Any] = {
        "titles": [{"value": title}],
        "citations": [{"value": citation_text}],
        "attribution": {"changeMessage": message},
    }

    if about:
        sd["about"] = about

    return {"sourceDescriptions": [sd]}


def _extract_id_from_location(location: str) -> str:
    if not location:
        return ""

    match = re.search(r"/descriptions/([^/?#]+)", location)
    if match:
        return match.group(1).strip()

    tail = re.search(r"/([^/?#]+)$", location)
    return tail.group(1).strip() if tail else ""


def _create_source_description(
    session: Any, title: str, citation_text: str, about: str, change_message: str
) -> Optional[str]:
    resp = _session_post_json(
        session,
        "/platform/sources/descriptions",
        _build_source_description_payload(title, citation_text, about, change_message),
    )
    if resp is None or _response_status(resp) not in (200, 201):
        return None

    headers = _response_headers(resp)
    sdid = str(headers.get("X-Entity-Id") or headers.get("X-entity-id") or "").strip()
    if sdid:
        return sdid

    location = str(headers.get("Location") or headers.get("location") or "").strip()
    if location:
        return _extract_id_from_location(location)

    try:
        data = resp.json() if hasattr(resp, "json") else None
    except Exception:
        data = None

    if isinstance(data, dict):
        source_descriptions = (
            data.get("sourceDescriptions") or data.get("sourceDescription") or []
        )
        if isinstance(source_descriptions, list) and source_descriptions:
            sdid = (source_descriptions[0].get("id") or "").strip()
            if sdid:
                return sdid

    return None


def _build_person_sources_payload(
    session: Any, fsid: str, source_refs: List[Dict[str, Any]], change_message: str
) -> Dict[str, Any]:
    message = (change_message or "").strip() or _("Updated from Gramps")
    for source_ref in source_refs:
        source_ref.setdefault("attribution", {"changeMessage": message})
    return {"persons": [{"id": fsid, "sources": source_refs}]}


def _api_base(session: Any) -> str:
    base = (
        getattr(session, "api_url", "")
        or getattr(session, "API_URL", "")
        or "https://api.familysearch.org"
    )
    return str(base).rstrip("/")


def _build_source_ref(
    session: Any, sdid: str, tags: List[str], change_message: str
) -> Dict[str, Any]:
    desc_url = f"{_api_base(session)}/platform/sources/descriptions/{sdid}"
    out: Dict[str, Any] = {
        "description": desc_url,
        "tags": [{"resource": tag} for tag in tags if tag],
        "attribution": {
            "changeMessage": (change_message or "").strip() or _("Updated from Gramps")
        },
    }

    if not out["tags"]:
        out.pop("tags", None)

    return out


# ----------------------------
# main push entry point
# ----------------------------


def sync_to_familysearch(
    dbstate: Any,
    uistate: Any,
    track: Any,
    person: Any,
    session: Any,
    parent: Any,
    editor: Any = None,
) -> None:
    try:
        if not (
            getattr(session, "logged", False)
            or getattr(session, "access_token", None)
            or getattr(session, "connected", False)
        ):
            WarningDialog(_("Not connected to FamilySearch."), parent=parent)
            return

        # Always prefer the DB copy. In-editor objects can be stale & It wont report any pushable changes
        handle = _person_handle(person)
        if handle:
            db_person = dbstate.db.get_person_from_handle(handle)
            if db_person is not None:
                person = db_person

        fsid_raw = fs_utilities.get_fsftid(person)
        if not fsid_raw:
            WarningDialog(
                _("No FamilySearch Person ID is set for this person."), parent=parent
            )
            return

        resolved_fsid, fs_state = _resolve_active_person(session, fsid_raw)

        if fs_state == "deleted":
            WarningDialog(
                _(
                    "This linked FamilySearch person is deleted/inactive.\n\n"
                    "Clear or replace the saved FSID before exporting again, "
                    "or restore that FamilySearch person first."
                ),
                parent=parent,
            )
            return

        if fs_state == "missing":
            WarningDialog(
                _(
                    "This linked FamilySearch person could not be found.\n\n"
                    "Clear or replace the saved FSID before exporting again."
                ),
                parent=parent,
            )
            return

        fsid = resolved_fsid or fsid_raw

        if fs_state == "merged" and fsid and fsid != fsid_raw:
            with DbTxn(_("FamilySearch: Refresh merged ID"), dbstate.db) as txn:
                _get_or_set_person_fsid(dbstate.db, txn, person, fsid)

            if handle:
                db_person = dbstate.db.get_person_from_handle(handle)
                if db_person is not None:
                    person = db_person

        _bind_global_session(session)
        _prime_person_cache(session, fsid, force=True)

        fs_person = deserialize.Person._index.get(fsid)
        if fs_person is None:
            WarningDialog(
                _(
                    "FamilySearch person could not be loaded fresh from the API.\n"
                    "Try 'Sync from FamilySearch' or clear the cache, then try again."
                ),
                parent=parent,
            )
            return

        compare_model = _make_overview_model()
        fs_compare.compare_fs_to_gramps(
            fs_person,
            person,
            dbstate.db,
            model=compare_model,
            dupdoc=True,
        )
        _debug_dump_compare_model(compare_model)
        overview_items = _collect_overview_push_items(compare_model)

        if _debug_enabled():
            print(f"[FS SYNC] overview_items={overview_items!r}")

        note_items = _collect_note_push_items(dbstate, person, session, fsid)
        source_items = _collect_source_push_items(dbstate, person, session, fsid)
        memory_items = _collect_memory_push_items(dbstate, person)

        all_items = overview_items + note_items + source_items + memory_items
        if not all_items:
            WarningDialog(_("No pushable differences found."), parent=parent)
            return

        change_message, chosen = _prompt(parent, all_items)
        if not chosen:
            return

        chosen_overview = [
            item for item in chosen if item.get("kind") in ("primary_name", "fact")
        ]
        chosen_notes = [
            item
            for item in chosen
            if item.get("kind") in ("note_create", "note_update")
        ]
        chosen_sources = [
            item for item in chosen if item.get("kind") == "source_create"
        ]
        chosen_memories = [
            item for item in chosen if item.get("kind") == "memory_create"
        ]

        if chosen_overview:
            payload = _build_person_payload(fsid, chosen_overview, change_message)
            person_payload = (
                (payload.get("persons") or [])[0] if payload.get("persons") else {}
            )

            if not person_payload or (
                len(person_payload.keys()) <= 1 and "id" in person_payload
            ):
                WarningDialog(
                    _(
                        "Differences were found, but none could be converted into a valid "
                        "FamilySearch payload. This usually means the fact type could not be determined."
                    ),
                    parent=parent,
                )
                return

            fsid_head, head_resp = _head_person(session, fsid)
            if fsid_head and fsid_head != fsid:
                fsid = fsid_head
                payload = _build_person_payload(fsid, chosen_overview, change_message)

            person_headers: Dict[str, str] = {}
            if head_resp is not None:
                response_headers = _response_headers(head_resp)
                etag = str(
                    response_headers.get("Etag") or response_headers.get("ETag") or ""
                ).strip()
                last_modified = str(response_headers.get("Last-Modified") or "").strip()

                if etag:
                    person_headers["If-Match"] = etag
                if last_modified:
                    person_headers["If-Unmodified-Since"] = last_modified

            resp = _session_post_json(
                session,
                f"/platform/tree/persons/{fsid}",
                payload,
                headers=person_headers,
            )
            if resp is None or _response_status(resp) not in (200, 201, 204):
                message = _err_text(resp) if resp is not None else ""
                WarningDialog(
                    _("FamilySearch update failed (names/facts).")
                    + (("\n" + message) if message else ""),
                    parent=parent,
                )
                return

            _prime_person_cache(session, fsid, force=True)

        if chosen_notes:
            notes_payload = _build_notes_payload(fsid, chosen_notes, change_message)
            if notes_payload:
                resp = _session_post_json(
                    session, f"/platform/tree/persons/{fsid}/notes", notes_payload
                )
                if resp is None or _response_status(resp) not in (200, 201, 204):
                    message = _err_text(resp) if resp is not None else ""
                    WarningDialog(
                        _("FamilySearch update failed (notes).")
                        + (("\n" + message) if message else ""),
                        parent=parent,
                    )
                    return

        if chosen_sources:
            created_refs: List[Dict[str, Any]] = []
            default_tags = ["http://gedcomx.org/Name"]

            for item in chosen_sources:
                citation_handle = item.get("citation_handle")
                if not citation_handle:
                    continue

                citation = dbstate.db.get_citation_from_handle(citation_handle)
                if not citation:
                    continue

                title = str(item.get("title") or _("Source from Gramps"))
                url = str(item.get("url") or "")
                note_text = str(item.get("note_text") or "")
                date = str(item.get("date") or "")

                citation_text = title
                if date:
                    citation_text += f" ({date})"
                if note_text:
                    citation_text += "\n" + note_text

                sdid = _create_source_description(
                    session, title, citation_text, url, change_message
                )
                if not sdid:
                    WarningDialog(
                        _("FamilySearch source creation failed for: {t}").format(
                            t=title
                        ),
                        parent=parent,
                    )
                    return

                created_refs.append(
                    _build_source_ref(session, sdid, default_tags, change_message)
                )

                link_fn = getattr(fs_utilities, "link_gramps_fs_id", None)
                if callable(link_fn):
                    try:
                        link_fn(dbstate.db, citation, sdid)
                    except Exception:
                        pass

            if created_refs:
                fsid_head, head_resp = _head_person(session, fsid)
                if fsid_head and fsid_head != fsid:
                    fsid = fsid_head

                source_headers: Dict[str, str] = {}
                if head_resp is not None:
                    response_headers = _response_headers(head_resp)
                    etag = str(
                        response_headers.get("Etag")
                        or response_headers.get("ETag")
                        or ""
                    ).strip()
                    last_modified = str(
                        response_headers.get("Last-Modified") or ""
                    ).strip()

                    if etag:
                        source_headers["If-Match"] = etag
                    if last_modified:
                        source_headers["If-Unmodified-Since"] = last_modified

                payload = _build_person_sources_payload(
                    session, fsid, created_refs, change_message
                )
                resp = _session_post_json(
                    session,
                    f"/platform/tree/persons/{fsid}",
                    payload,
                    headers=source_headers,
                )
                if resp is None or _response_status(resp) not in (200, 201, 204):
                    message = _err_text(resp) if resp is not None else ""
                    WarningDialog(
                        _("FamilySearch update failed (sources).")
                        + (("\n" + message) if message else ""),
                        parent=parent,
                    )
                    return

        if chosen_memories:
            try:
                from gramps.gen.display.name import displayer as name_displayer

                person_name = str(name_displayer.display(person) or "").strip()
            except Exception:
                person_name = ""

            uploaded: List[Tuple[str, str]] = []
            memory_errors: List[str] = []

            for item in chosen_memories:
                file_path = str(item.get("file_path") or "").strip()
                media_handle = str(item.get("media_handle") or "").strip()
                if not media_handle or not file_path:
                    continue

                mem_id, _mem_ref_id, err = _upload_person_memory(
                    session,
                    fsid,
                    file_path,
                    title=str(item.get("title") or "").strip(),
                    description=str(item.get("description") or "").strip(),
                    filename=str(item.get("filename") or "").strip(),
                    person_name=person_name,
                    artifact_type=str(item.get("artifact_type") or "").strip(),
                )

                if not mem_id:
                    memory_errors.append(
                        f"{os.path.basename(file_path) or '(file)'}: {err or 'upload failed'}"
                    )
                    continue

                uploaded.append((media_handle, mem_id))

            if uploaded:
                db = dbstate.db

                with DbTxn(_("FamilySearch: Link uploaded memories"), db) as txn:
                    for media_handle, mem_id in uploaded:
                        media_obj = None

                        for getter_name in (
                            "get_media_from_handle",
                            "get_media_object_from_handle",
                        ):
                            getter = getattr(db, getter_name, None)
                            if not callable(getter):
                                continue
                            media_obj = getter(media_handle)
                            if media_obj:
                                break

                        if not media_obj:
                            continue

                        link_fn = getattr(fs_utilities, "link_gramps_fs_id", None)
                        if callable(link_fn):
                            try:
                                link_fn(db, media_obj, mem_id)
                                continue
                            except Exception:
                                pass

                        attrs = list(media_obj.get_attribute_list() or [])

                        kept: List[Any] = []
                        for attr in attrs:
                            try:
                                attr_type = str(attr.get_type())
                            except Exception:
                                attr_type = ""

                            if attr_type in ("_FSFTID", "_FSTID", "FamilySearch ID"):
                                continue
                            kept.append(attr)

                        fs_attr = Attribute()
                        fs_attr.set_type(AttributeType("_FSFTID"))
                        fs_attr.set_value(mem_id)
                        kept.append(fs_attr)

                        media_obj.set_attribute_list(kept)

                        for commit_name in (
                            "commit_media_object",
                            "commit_media",
                            "commit_object",
                        ):
                            commit = getattr(db, commit_name, None)
                            if not callable(commit):
                                continue
                            try:
                                commit(media_obj, txn)
                                break
                            except Exception:
                                continue

            if memory_errors:
                WarningDialog(
                    _("Some memories failed to upload:\n\n%s")
                    % "\n".join(memory_errors[:12]),
                    parent=parent,
                )

        _prime_person_cache(session, fsid, force=True)
        WarningDialog(_("FamilySearch updated successfully."), parent=parent)

    except Exception as err:
        WarningDialog(
            _("FamilySearch sync failed: {e}").format(e=str(err)),
            parent=parent,
        )


# ----------------------------
# export helpers
# ----------------------------


def _get_or_set_person_fsid(db: Any, txn: Any, gr_person: Person, fsid: str) -> None:
    fsid = (fsid or "").strip()
    if not fsid:
        return

    link_fn = getattr(fs_utilities, "link_gramps_fs_id", None)
    if callable(link_fn):
        try:
            link_fn(db, gr_person, fsid)
            return
        except Exception:
            pass

    attrs = list(gr_person.get_attribute_list() or [])

    kept: List[Any] = []
    for attr in attrs:
        try:
            attr_type = str(attr.get_type())
        except Exception:
            attr_type = ""

        if attr_type in ("_FSFTID", "_FSTID", "FamilySearch ID"):
            continue
        kept.append(attr)

    fs_attr = Attribute()
    fs_attr.set_type(AttributeType("_FSFTID"))
    fs_attr.set_value(fsid)
    kept.append(fs_attr)

    gr_person.set_attribute_list(kept)
    db.commit_person(gr_person, txn)


def _gramps_name_parts(gr_person: Person) -> Tuple[str, str]:
    name = gr_person.primary_name
    if name is None:
        return "", ""

    given = (getattr(name, "first_name", "") or "").strip()

    try:
        surname = (name.get_surname() or "").strip()
    except Exception:
        surname = (getattr(name, "surname", "") or "").strip()

    return given, surname


def _gender_uri(gr_person: Person) -> str:
    gender = gr_person.get_gender()
    if gender == Person.MALE:
        return "http://gedcomx.org/Male"
    if gender == Person.FEMALE:
        return "http://gedcomx.org/Female"
    return "http://gedcomx.org/Unknown"


def _event_to_fact(db: Any, event: Any, fact_type_uri: str) -> Optional[Dict[str, Any]]:
    if event is None:
        return None

    try:
        date_formal = fs_utilities.gramps_date_to_formal(event.date) or ""
    except Exception:
        date_formal = ""

    place_text = ""
    if getattr(event, "place", None):
        try:
            from gramps.gen.display.place import displayer as place_displayer

            place = db.get_place_from_handle(event.place)
            place_text = (place_displayer.display(db, place) or "").strip()
        except Exception:
            place_text = ""

    if not date_formal and not place_text:
        return None

    fact: Dict[str, Any] = {"type": fact_type_uri}
    if date_formal:
        fact["date"] = {"formal": date_formal, "original": date_formal}
    if place_text:
        fact["place"] = {"original": place_text}
    return fact


def _birth_death_facts(db: Any, gr_person: Person) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []

    birth_lookup: Any = None
    death_lookup: Any = None

    try:
        from gramps.gen.utils.db import get_birth_or_fallback as birth_lookup
        from gramps.gen.utils.db import get_death_or_fallback as death_lookup
    except Exception:
        pass

    birth_event = birth_lookup(db, gr_person) if callable(birth_lookup) else None
    death_event = death_lookup(db, gr_person) if callable(death_lookup) else None

    birth_fact = _event_to_fact(db, birth_event, "http://gedcomx.org/Birth")
    if birth_fact:
        facts.append(birth_fact)

    death_fact = _event_to_fact(db, death_event, "http://gedcomx.org/Death")
    if death_fact:
        facts.append(death_fact)

    return facts


def _extract_created_person_id(resp: Any) -> str:
    headers = _response_headers(resp)

    person_id = str(
        headers.get("X-Entity-Id") or headers.get("X-entity-id") or ""
    ).strip()
    if person_id:
        return person_id

    location = str(headers.get("Location") or headers.get("location") or "").strip()
    if location:
        match = re.search(r"/persons/([^/?#]+)", location)
        if match:
            return match.group(1).strip()

        tail = re.search(r"/([^/?#]+)$", location)
        if tail:
            return tail.group(1).strip()

    try:
        data = resp.json() if hasattr(resp, "json") else None
    except Exception:
        data = None

    if isinstance(data, dict):
        persons = data.get("persons") or []
        if isinstance(persons, list) and persons:
            person_id = (persons[0].get("id") or "").strip()
            if person_id:
                return person_id

    return ""


def _fs_create_person_basic(
    session: Any, db: Any, gr_person: Person, change_message: str
) -> Optional[str]:
    given, surname = _gramps_name_parts(gr_person)
    full = f"{given} {surname}".strip() if (given or surname) else ""

    if not full:
        return None

    message = (change_message or "").strip() or _("Created from Gramps")

    parts: List[Dict[str, str]] = []
    if given:
        parts.append({"type": "http://gedcomx.org/Given", "value": given})
    if surname:
        parts.append({"type": "http://gedcomx.org/Surname", "value": surname})

    name_form: Dict[str, Any] = {"fullText": full}
    if parts:
        name_form["parts"] = parts

    person_payload: Dict[str, Any] = {
        "names": [
            {
                "preferred": True,
                "nameForms": [name_form],
                "attribution": {"changeMessage": message},
            }
        ],
        "gender": {
            "type": _gender_uri(gr_person),
            "attribution": {"changeMessage": message},
        },
        "attribution": {"changeMessage": message},
    }

    facts = _birth_death_facts(db, gr_person)
    if facts:
        person_payload["facts"] = facts

    payload = {"persons": [person_payload]}

    resp = None
    for endpoint in ("/platform/tree/persons", "/platform/tree/persons/"):
        resp = _session_post_json(session, endpoint, payload)
        if resp is None:
            continue
        if _response_status(resp) in (200, 201):
            break

    if resp is None or _response_status(resp) not in (200, 201):
        return None

    fsid = _extract_created_person_id(resp)
    return fsid.strip() or None


def _ok_or_duplicate(resp: Any) -> bool:
    if resp is None:
        return False

    status = _response_status(resp)
    if status in (200, 201, 204):
        return True

    if status in (400, 409):
        message = _err_text(resp).lower()
        if "already" in message or "exists" in message or "duplicate" in message:
            return True

    return False


def _post_couple_relationship(
    session: Any, fsid1: str, fsid2: str, change_message: str
) -> Tuple[bool, str]:
    fsid1 = (fsid1 or "").strip()
    fsid2 = (fsid2 or "").strip()
    if not fsid1 or not fsid2:
        return False, "Missing spouse FSID(s)"

    message = (change_message or "").strip() or "Created from Gramps"
    base = _api_base(session)

    rel = {
        "type": "http://gedcomx.org/Couple",
        "person1": {
            "resource": f"{base}/platform/tree/persons/{fsid1}",
            "resourceId": fsid1,
        },
        "person2": {
            "resource": f"{base}/platform/tree/persons/{fsid2}",
            "resourceId": fsid2,
        },
        "attribution": {"changeMessage": message},
    }

    payload = {"relationships": [rel]}
    headers = {
        "accept": "application/x-fs-v1+json",
        "content-type": "application/x-fs-v1+json",
    }

    last_resp = None
    for endpoint in ("/platform/tree/relationships", "/platform/tree/relationships/"):
        last_resp = _session_post_json(session, endpoint, payload, headers=headers)
        if _ok_or_duplicate(last_resp):
            return True, ""

    if last_resp is None:
        return False, "No response from FamilySearch"

    return False, f"HTTP {_response_status(last_resp)}: {_err_text(last_resp)}"


def _post_child_and_parents(
    session: Any,
    child_fsid: str,
    parent1_fsid: str,
    parent2_fsid: str,
    change_message: str,
) -> Tuple[bool, str]:
    child_fsid = (child_fsid or "").strip()
    parent1_fsid = (parent1_fsid or "").strip()
    parent2_fsid = (parent2_fsid or "").strip()

    if not child_fsid:
        return False, "Missing child FSID"
    if not parent1_fsid and not parent2_fsid:
        return False, "Missing parent FSID(s)"

    message = (change_message or "").strip() or "Created from Gramps"
    base = _api_base(session)

    capr: Dict[str, Any] = {
        "child": {
            "resource": f"{base}/platform/tree/persons/{child_fsid}",
            "resourceId": child_fsid,
        },
        "attribution": {"changeMessage": message},
    }

    # biological links
    if parent1_fsid:
        capr["parent1"] = {
            "resource": f"{base}/platform/tree/persons/{parent1_fsid}",
            "resourceId": parent1_fsid,
        }
        capr["parent1Facts"] = [
            {
                "id": "C.1",
                "type": "http://gedcomx.org/BiologicalParent",
                "attribution": {"changeMessage": message},
            }
        ]

    if parent2_fsid:
        capr["parent2"] = {
            "resource": f"{base}/platform/tree/persons/{parent2_fsid}",
            "resourceId": parent2_fsid,
        }
        capr["parent2Facts"] = [
            {
                "id": "C.2",
                "type": "http://gedcomx.org/BiologicalParent",
                "attribution": {"changeMessage": message},
            }
        ]

    payload: Dict[str, Any] = {"childAndParentsRelationships": [capr]}
    headers: Dict[str, str] = {
        "accept": "application/x-fs-v1+json",
        "content-type": "application/x-fs-v1+json",
    }

    last_resp = None
    for endpoint in ("/platform/tree/relationships", "/platform/tree/relationships/"):
        last_resp = _session_post_json(session, endpoint, payload, headers=headers)
        if _ok_or_duplicate(last_resp):
            return True, ""

    if last_resp is None:
        return False, "No response from FamilySearch"

    return False, f"HTTP {_response_status(last_resp)}: {_err_text(last_resp)}"


def _family_other_parent_handle(family: Family, me_handle: str) -> Optional[str]:
    father = family.get_father_handle()
    mother = family.get_mother_handle()

    if father == me_handle:
        return mother
    if mother == me_handle:
        return father
    return None


def _collect_relatives(
    db: Any, me: Person
) -> Tuple[List[str], List[str], List[str], List[str]]:
    parent_handles: List[str] = []
    spouse_handles: List[str] = []
    child_handles: List[str] = []
    family_handles: List[str] = []

    parents_family_handle = me.get_main_parents_family_handle()
    if not parents_family_handle:
        parent_families = list(me.get_parent_family_handle_list() or [])
        parents_family_handle = parent_families[0] if parent_families else None

    if parents_family_handle:
        family = db.get_family_from_handle(parents_family_handle)
        if family:
            father = family.get_father_handle()
            mother = family.get_mother_handle()

            if father:
                parent_handles.append(father)
            if mother:
                parent_handles.append(mother)

    family_handles = list(me.get_family_handle_list() or [])

    for family_handle in family_handles:
        family = db.get_family_from_handle(family_handle)
        if not family:
            continue

        spouse_handle = _family_other_parent_handle(family, me.handle)
        if spouse_handle and spouse_handle not in spouse_handles:
            spouse_handles.append(spouse_handle)

        child_refs = list(family.get_child_ref_list() or [])
        for child_ref in child_refs:
            child_handle = getattr(child_ref, "ref", None)
            if child_handle and child_handle not in child_handles:
                child_handles.append(child_handle)

    return parent_handles, spouse_handles, child_handles, family_handles


def _export_picker_dialog(
    parent: Gtk.Window, db: Any, me: Person, session: Any
) -> Optional[Tuple[bool, bool, bool, List[str]]]:
    parents, spouses, children, _families = _collect_relatives(db, me)

    dialog = Gtk.Dialog(
        title=_("Export to FamilySearch (basic)"), transient_for=parent, flags=0
    )
    dialog.set_modal(True)
    dialog.set_default_size(760, 520)
    dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dialog.add_button(_("Export"), Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)

    box = dialog.get_content_area()
    box.set_border_width(10)
    box.set_spacing(8)

    info = Gtk.Label()
    info.set_xalign(0.0)
    info.set_line_wrap(True)
    info.set_markup(
        _(
            "<b>Create missing people on FamilySearch and link relationships.</b>\n"
            "Basic export only: name + birth/death facts.\n"
            "<i>No deletions. Existing active linked people are not overwritten here.</i>"
        )
    )
    box.pack_start(info, False, False, 0)

    chk_parents = Gtk.CheckButton(label=_("Include parents"))
    chk_spouses = Gtk.CheckButton(label=_("Include spouse(s)"))
    chk_children = Gtk.CheckButton(label=_("Include children"))

    chk_parents.set_active(True)
    chk_spouses.set_active(True)
    chk_children.set_active(True)

    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row.pack_start(chk_parents, False, False, 0)
    row.pack_start(chk_spouses, False, False, 0)
    row.pack_start(chk_children, False, False, 0)
    box.pack_start(row, False, False, 0)

    box.pack_start(
        Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
    )

    store = Gtk.ListStore(bool, str, str, bool)

    def display_label(prefix: str, person_obj: Any) -> Tuple[str, bool]:
        try:
            from gramps.gen.display.name import displayer as name_displayer

            name = str(name_displayer.display(person_obj) or "")
        except Exception:
            name = _("(person)")

        raw_fsid = (fs_utilities.get_fsftid(person_obj) or "").strip()
        if not raw_fsid:
            return f"{prefix}{name}", False

        resolved, state = _resolve_active_person(session, raw_fsid)

        if state == "ok":
            return f"{prefix}{name}  [{raw_fsid}]", True
        if state == "merged":
            return f"{prefix}{name}  [{raw_fsid} ? {resolved or raw_fsid}]", True
        if state == "deleted":
            return f"{prefix}{name}  [{raw_fsid}; deleted on FamilySearch]", False
        if state == "missing":
            return f"{prefix}{name}  [{raw_fsid}; not found on FamilySearch]", False

        return f"{prefix}{name}  [{raw_fsid}; status unknown]", True

    me_label, me_has_active = display_label(_("This person: "), me)
    store.append([not me_has_active, me_label, me.handle, me_has_active])

    def add_handles(title: str, handles: List[str]) -> None:
        for handle in handles:
            person_obj = db.get_person_from_handle(handle)
            if not person_obj:
                continue

            label, has_active = display_label(f"{title}: ", person_obj)
            store.append([not has_active, label, handle, has_active])

    add_handles(_("Parent"), parents)
    add_handles(_("Spouse"), spouses)
    add_handles(_("Child"), children)

    treeview = Gtk.TreeView(model=store)
    try:
        treeview.set_headers_visible(True)
        treeview.set_rules_hint(True)
    except Exception:
        pass

    toggle_renderer = Gtk.CellRendererToggle()
    toggle_renderer.connect(
        "toggled",
        lambda _w, path: store[path].__setitem__(0, not store[path][0]),
    )
    treeview.append_column(Gtk.TreeViewColumn(_("Create"), toggle_renderer, active=0))

    text_renderer = Gtk.CellRendererText()
    treeview.append_column(Gtk.TreeViewColumn(_("Person"), text_renderer, text=1))

    button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_missing = Gtk.Button(label=_("Select all missing"))
    btn_none = Gtk.Button(label=_("Select none"))

    def select_missing(_button: Any) -> None:
        for row_data in store:
            row_data[0] = not bool(row_data[3])

    def select_none(_button: Any) -> None:
        for row_data in store:
            row_data[0] = False

    btn_missing.connect("clicked", select_missing)
    btn_none.connect("clicked", select_none)
    button_row.pack_start(btn_missing, False, False, 0)
    button_row.pack_start(btn_none, False, False, 0)
    box.pack_start(button_row, False, False, 0)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    scroll.add(treeview)
    box.pack_start(scroll, True, True, 0)

    dialog.show_all()
    response = dialog.run()

    if response != Gtk.ResponseType.OK:
        dialog.destroy()
        return None

    do_parents = bool(chk_parents.get_active())
    do_spouses = bool(chk_spouses.get_active())
    do_children = bool(chk_children.get_active())

    chosen: List[str] = []
    for row_data in store:
        if bool(row_data[0]) and isinstance(row_data[2], str) and row_data[2]:
            chosen.append(row_data[2])

    dialog.destroy()
    return do_parents, do_spouses, do_children, chosen


def export_basic_people_to_familysearch(
    dbstate: Any,
    uistate: Any,
    track: Any,
    person: Any,
    session: Any,
    parent: Any,
    editor: Any = None,
) -> None:
    try:
        if not (
            getattr(session, "logged", False)
            or getattr(session, "access_token", None)
            or getattr(session, "connected", False)
        ):
            WarningDialog(_("Not connected to FamilySearch."), parent=parent)
            return

        db = dbstate.db
        me_handle = _person_handle(person)

        if not me_handle:
            WarningDialog(_("Please save this person first."), parent=parent)
            return

        me = db.get_person_from_handle(me_handle)
        if not me:
            WarningDialog(
                _("Could not resolve this person from the database."), parent=parent
            )
            return

        _bind_global_session(session)

        picked = _export_picker_dialog(parent, db, me, session)
        if picked is None:
            return

        do_parents, do_spouses, do_children, create_handles = picked

        handle_to_fsid: Dict[str, str] = {}
        parents, spouses, children, family_handles = _collect_relatives(db, me)
        change_message = _("Created from Gramps")

        def seed(handle: str, txn: Any) -> None:
            person_obj = db.get_person_from_handle(handle)
            if not person_obj:
                return

            raw_fsid = (fs_utilities.get_fsftid(person_obj) or "").strip()
            if not raw_fsid:
                return

            resolved_fsid, state = _resolve_active_person(session, raw_fsid)
            if state in ("ok", "merged") and resolved_fsid:
                handle_to_fsid[handle] = resolved_fsid
                if state == "merged" and resolved_fsid != raw_fsid:
                    _get_or_set_person_fsid(db, txn, person_obj, resolved_fsid)

        created_any = False
        attempted_relationships = False

        with DbTxn(_("FamilySearch: Export basic people"), db) as txn:
            seed(me.handle, txn)

            if do_parents:
                for handle in parents:
                    seed(handle, txn)

            if do_spouses:
                for handle in spouses:
                    seed(handle, txn)

            if do_children:
                for handle in children:
                    seed(handle, txn)

            for handle in create_handles:
                person_obj = db.get_person_from_handle(handle)
                if not person_obj:
                    continue

                raw_fsid = (fs_utilities.get_fsftid(person_obj) or "").strip()
                if raw_fsid:
                    resolved_fsid, state = _resolve_active_person(session, raw_fsid)
                    if state in ("ok", "merged") and resolved_fsid:
                        handle_to_fsid[handle] = resolved_fsid
                        if state == "merged" and resolved_fsid != raw_fsid:
                            _get_or_set_person_fsid(db, txn, person_obj, resolved_fsid)
                        continue

                fsid_new = _fs_create_person_basic(
                    session, db, person_obj, change_message
                )
                if not fsid_new:
                    WarningDialog(
                        _(
                            "FamilySearch create failed for a person. Check name and connection."
                        ),
                        parent=parent,
                    )
                    return

                created_any = True
                handle_to_fsid[handle] = fsid_new
                _get_or_set_person_fsid(db, txn, person_obj, fsid_new)

        me = db.get_person_from_handle(me_handle)
        raw_me_fsid = (fs_utilities.get_fsftid(me) or "").strip()

        me_fsid = ""
        if raw_me_fsid:
            resolved_me_fsid, me_state = _resolve_active_person(session, raw_me_fsid)
            if me_state in ("ok", "merged") and resolved_me_fsid:
                me_fsid = resolved_me_fsid

        if not me_fsid and me.handle in handle_to_fsid:
            me_fsid = handle_to_fsid[me.handle]

        rel_errors: List[str] = []

        if me_fsid and do_parents and parents:
            parent1 = ""
            parent2 = ""

            parents_family_handle = me.get_main_parents_family_handle()
            family = (
                db.get_family_from_handle(parents_family_handle)
                if parents_family_handle
                else None
            )

            if family:
                father = family.get_father_handle()
                mother = family.get_mother_handle()

                if father:
                    parent1 = handle_to_fsid.get(father, "")
                if mother:
                    parent2 = handle_to_fsid.get(mother, "")

            if not parent1 and len(parents) > 0:
                parent1 = handle_to_fsid.get(parents[0], "")
            if not parent2 and len(parents) > 1:
                parent2 = handle_to_fsid.get(parents[1], "")

            if parent1 or parent2:
                attempted_relationships = True
                ok, err = _post_child_and_parents(
                    session, me_fsid, parent1, parent2, change_message
                )
                if not ok:
                    rel_errors.append(f"Parent link failed for me={me_fsid}: {err}")

        if me_fsid and (do_spouses or do_children):
            for family_handle in family_handles:
                family = db.get_family_from_handle(family_handle)
                if not family:
                    continue

                spouse_handle = _family_other_parent_handle(family, me.handle)
                spouse_fsid = (
                    handle_to_fsid.get(spouse_handle or "", "") if spouse_handle else ""
                )

                if do_spouses and spouse_fsid:
                    attempted_relationships = True
                    ok, err = _post_couple_relationship(
                        session, me_fsid, spouse_fsid, change_message
                    )
                    if not ok:
                        rel_errors.append(
                            f"Spouse link failed me={me_fsid} spouse={spouse_fsid}: {err}"
                        )

                if do_children:
                    child_refs = list(family.get_child_ref_list() or [])
                    for child_ref in child_refs:
                        child_handle = getattr(child_ref, "ref", None)
                        if not child_handle:
                            continue

                        child_fsid = handle_to_fsid.get(child_handle, "")
                        if not child_fsid:
                            continue

                        attempted_relationships = True
                        ok, err = _post_child_and_parents(
                            session,
                            child_fsid,
                            me_fsid,
                            spouse_fsid,
                            change_message,
                        )
                        if not ok:
                            rel_errors.append(
                                f"Child link failed child={child_fsid} parent={me_fsid} spouse={spouse_fsid}: {err}"
                            )

        if rel_errors:
            WarningDialog(
                "Export completed, but some relationships failed to link on FamilySearch:\n\n%s"
                % "\n".join(rel_errors[:12]),
                parent=parent,
            )

        # Cache refresh is post-work polish. Failure here should not invalidate asuccessful export.
        if me_fsid:
            try:
                _prime_person_cache(session, me_fsid, force=True)
            except Exception:
                pass

        if created_any:
            WarningDialog(
                _(
                    "Export complete: created people and linked relationships on FamilySearch."
                ),
                parent=parent,
            )
        elif attempted_relationships:
            WarningDialog(
                _(
                    "Export complete: no new people were created; relationships were attempted."
                ),
                parent=parent,
            )
        else:
            WarningDialog(_("Nothing was exported."), parent=parent)

    except Exception as err:
        WarningDialog(
            _("FamilySearch export failed: {e}").format(e=str(err)),
            parent=parent,
        )

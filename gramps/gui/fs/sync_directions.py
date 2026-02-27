# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2026  Gabriel Rios
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

from typing import Any, Dict, List, Tuple, Optional

from gi.repository import Gtk

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gui.dialog import WarningDialog
from gramps.gui.listmodel import ListModel, NOSORT, COLOR, TOGGLE

from . import utilities as fs_utilities
from . import compare as fs_compare
from .import_ import deserializer as deserialize

try:
    _trans = glocale.get_addon_translator(__file__)
except Exception:
    _trans = glocale.translation
_ = _trans.gettext


# overview model columns must match fs_compare.compare_fs_to_gramps
COL_PROP = 1
COL_GR_DATE = 2
COL_GR_VAL = 3
COL_FS_DATE = 4
COL_FS_VAL = 5
COL_XTYPE = 8
COL_XFS_ID = 10
COL_XGR2 = 11
COL_XFS2 = 12


def _as_tree_model(maybe_model):
    if maybe_model is None:
        return None

    if hasattr(maybe_model, "get_iter_first") and hasattr(maybe_model, "get_value"):
        return maybe_model

    for attr in ("model", "_model", "store", "_store", "treemodel"):
        inner = getattr(maybe_model, attr, None)
        if (
            inner is not None
            and hasattr(inner, "get_iter_first")
            and hasattr(inner, "get_value")
        ):
            return inner

    return None


def _walk(model):
    tm = _as_tree_model(model)
    if tm is None:
        return

    try:
        it = tm.get_iter_first()
    except Exception:
        return

    stack = [it] if it else []
    while stack:
        cur = stack.pop()
        yield tm, cur

        # children
        try:
            child = tm.iter_children(cur)
        except Exception:
            child = None

        kids = []
        while child:
            kids.append(child)
            try:
                child = tm.iter_next(child)
            except Exception:
                child = None

        for k in reversed(kids):
            stack.append(k)


def _collect_push_items(model) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    for tm, it in _walk(model):
        try:
            x_type = tm.get_value(it, COL_XTYPE)
        except Exception:
            continue

        if x_type not in ("primary_name", "fact"):
            continue

        prop = str(tm.get_value(it, COL_PROP) or "")
        gr_date = str(tm.get_value(it, COL_GR_DATE) or "")
        gr_val = str(tm.get_value(it, COL_GR_VAL) or "")
        fs_date = str(tm.get_value(it, COL_FS_DATE) or "")
        fs_val = str(tm.get_value(it, COL_FS_VAL) or "")

        if x_type == "primary_name":
            if not gr_val.strip():
                continue
            if gr_val.strip() == fs_val.strip():
                continue

            fs_name_id = str(tm.get_value(it, COL_XFS_ID) or "").strip()
            if not fs_name_id:
                continue

            # cols produced by compare_names() in comparators.py
            gr_surname = str(tm.get_value(it, COL_XGR2) or "").strip()
            gr_given = str(tm.get_value(it, COL_XFS2) or "").strip()

            items.append(
                {
                    "kind": "primary_name",
                    "label": prop or _("Primary Name"),
                    "fs_id": fs_name_id,
                    "gr_val": gr_val,
                    "fs_val": fs_val,
                    "gr_given": gr_given,
                    "gr_surname": gr_surname,
                }
            )
            continue

        # facts: overwrite existing FS conclusions
        if not (gr_date.strip() or gr_val.strip()):
            continue
        if gr_date.strip() == fs_date.strip() and gr_val.strip() == fs_val.strip():
            continue

        fs_fact_id = str(tm.get_value(it, COL_XFS_ID) or "").strip()
        if not fs_fact_id:
            continue

        items.append(
            {
                "kind": "fact",
                "label": prop,
                "fs_id": fs_fact_id,
                "gr_date": gr_date,
                "gr_val": gr_val,
                "fs_date": fs_date,
                "fs_val": fs_val,
            }
        )

    return items


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


def _bind_global_session(session) -> None:
    try:
        from . import tree as fs_tree_mod

        setattr(fs_tree_mod, "_fs_session", session)
    except Exception:
        pass


def _find_fs_tree(session) -> Optional[Any]:
    # session.fs_Tree
    try:
        t = getattr(session, "fs_Tree", None)
        if (
            t is not None
            and not isinstance(t, type)
            and (hasattr(t, "add_persons") or hasattr(t, "add_person"))
        ):
            return t
    except Exception:
        pass

    # module-level candidates in gramps.gui.fs.tree
    try:
        from . import tree as fs_tree_mod

        for name in ("fs_Tree", "_fs_tree", "tree", "_tree"):
            try:
                t = getattr(fs_tree_mod, name, None)
                if (
                    t is not None
                    and not isinstance(t, type)
                    and (hasattr(t, "add_persons") or hasattr(t, "add_person"))
                ):
                    return t
            except Exception:
                pass

        try:
            for t in fs_tree_mod.__dict__.values():
                if t is None or isinstance(t, type):
                    continue
                if hasattr(t, "add_persons") or hasattr(t, "add_person"):
                    return t
        except Exception:
            pass

    except Exception:
        pass

    return None


def _prime_cache(session, fsid: str) -> None:
    # verify fsid is loaded into deserialize.Person._index created by the FS tree with _tree set
    _bind_global_session(session)

    fs_tree = _find_fs_tree(session)
    if fs_tree is None:
        return

    if hasattr(fs_tree, "add_persons"):
        try:
            fs_tree.add_persons([fsid], with_relatives=True)
            return
        except TypeError:
            try:
                fs_tree.add_persons([fsid])
                return
            except Exception:
                return
        except Exception:
            return

    if hasattr(fs_tree, "add_person"):
        try:
            fs_tree.add_person(fsid, with_relatives=True)
            return
        except TypeError:
            try:
                fs_tree.add_person(fsid)
                return
            except Exception:
                return
        except Exception:
            return


def _fmt(date_s: str, val_s: str) -> str:
    date_s = (date_s or "").strip()
    val_s = (val_s or "").strip()
    if date_s and val_s:
        return f"{date_s}  {val_s}"
    return date_s or val_s or _("(empty)")


def _prompt(
    parent: Gtk.Window, items: List[Dict[str, Any]]
) -> Tuple[str, List[Dict[str, Any]]]:
    dlg = Gtk.Dialog(title=_("Sync to FamilySearch"), transient_for=parent, flags=0)
    dlg.set_modal(True)
    dlg.set_default_size(780, 560)
    dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dlg.add_button(_("Push Selected"), Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)

    box = dlg.get_content_area()
    box.set_border_width(10)
    box.set_spacing(8)

    lbl = Gtk.Label()
    lbl.set_xalign(0.0)
    lbl.set_line_wrap(True)
    lbl.set_markup(
        _(
            "<b>Overwrite selected FamilySearch fields with values from Gramps.</b>\n"
            "Default is to keep FamilySearch.\n"
            "<i>No deletions are performed.</i>"
        )
    )
    box.pack_start(lbl, False, False, 0)

    cm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    cm_row.pack_start(Gtk.Label(label=_("Change message:")), False, False, 0)
    cm_entry = Gtk.Entry()
    cm_entry.set_hexpand(True)
    cm_entry.set_text(_("Updated from Gramps"))
    cm_row.pack_start(cm_entry, True, True, 0)
    box.pack_start(cm_row, False, False, 0)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_all = Gtk.Button(label=_("Select all"))
    btn_none = Gtk.Button(label=_("Select none"))
    btn_row.pack_start(btn_all, False, False, 0)
    btn_row.pack_start(btn_none, False, False, 0)
    btn_row.set_halign(Gtk.Align.START)
    box.pack_start(btn_row, False, False, 0)

    sc = Gtk.ScrolledWindow()
    sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    sc.set_hexpand(True)
    sc.set_vexpand(True)
    box.pack_start(sc, True, True, 0)

    grid = Gtk.Grid()
    grid.set_column_spacing(10)
    grid.set_row_spacing(8)
    grid.set_border_width(4)
    sc.add(grid)

    h0 = Gtk.Label(label=_("Field"))
    h1 = Gtk.Label(label=_("FamilySearch"))
    h2 = Gtk.Label(label=_("Gramps"))
    h3 = Gtk.Label(label=_("Action"))
    for h in (h0, h1, h2, h3):
        h.set_xalign(0.0)
        try:
            h.get_style_context().add_class("heading")
        except Exception:
            pass
    grid.attach(h0, 0, 0, 1, 1)
    grid.attach(h1, 1, 0, 1, 1)
    grid.attach(h2, 2, 0, 1, 1)
    grid.attach(h3, 3, 0, 1, 1)

    combos: List[Gtk.ComboBoxText] = []
    for i, it in enumerate(items, start=1):
        field = Gtk.Label(label=str(it.get("label") or ""))
        fs = Gtk.Label(label=_fmt(it.get("fs_date", ""), it.get("fs_val", "")))
        gr = Gtk.Label(
            label=(
                _fmt(it.get("gr_date", ""), it.get("gr_val", ""))
                if it.get("kind") == "fact"
                else (it.get("gr_val") or "")
            )
        )
        for w in (field, fs, gr):
            w.set_xalign(0.0)
            w.set_line_wrap(True)

        combo = Gtk.ComboBoxText()
        combo.append_text(_("Keep FamilySearch"))
        combo.append_text(_("Overwrite with Gramps"))
        combo.set_active(0)
        combos.append(combo)

        grid.attach(field, 0, i, 1, 1)
        grid.attach(fs, 1, i, 1, 1)
        grid.attach(gr, 2, i, 1, 1)
        grid.attach(combo, 3, i, 1, 1)

    def _set_all(active_overwrite: bool):
        idx = 1 if active_overwrite else 0
        for c in combos:
            c.set_active(idx)

    btn_all.connect("clicked", lambda *_: _set_all(True))
    btn_none.connect("clicked", lambda *_: _set_all(False))

    dlg.show_all()
    resp = dlg.run()
    cm = (cm_entry.get_text() or "").strip()
    chosen: List[Dict[str, Any]] = []
    if resp == Gtk.ResponseType.OK:
        for it, c in zip(items, combos):
            if c.get_active() == 1:
                chosen.append(it)
    dlg.destroy()
    return cm, chosen


def _err_text(resp) -> str:
    try:
        data = resp.json()
        if (
            isinstance(data, dict)
            and isinstance(data.get("errors"), list)
            and data["errors"]
        ):
            e0 = data["errors"][0]
            msg = e0.get("message") or ""
            code = e0.get("code") or ""
            return (f"{code}: {msg}".strip(": ")) if (code or msg) else ""
    except Exception:
        pass
    try:
        t = (getattr(resp, "text", "") or "").strip()
        return t[:600] + ("..." if len(t) > 600 else "")
    except Exception:
        return ""


def _build_payload(
    fsid: str, chosen: List[Dict[str, Any]], change_message: str
) -> Dict[str, Any]:
    msg = (change_message or "").strip() or _("Updated from Gramps")
    person: Dict[str, Any] = {"id": fsid}
    names = []
    facts = []

    for it in chosen:
        if it.get("kind") == "primary_name":
            name_id = str(it.get("fs_id") or "").strip()
            if not name_id:
                continue

            full = str(it.get("gr_val") or "").strip()
            given = str(it.get("gr_given") or "").strip()
            sur = str(it.get("gr_surname") or "").strip()

            name_form: Dict[str, Any] = {
                "fullText": full or (given + " " + sur).strip()
            }
            parts = []
            if given:
                parts.append({"type": "http://gedcomx.org/Given", "value": given})
            if sur:
                parts.append({"type": "http://gedcomx.org/Surname", "value": sur})
            if parts:
                name_form["parts"] = parts

            names.append(
                {
                    "id": name_id,
                    "preferred": True,
                    "nameForms": [name_form],
                    "attribution": {"changeMessage": msg},
                }
            )
            continue

        if it.get("kind") == "fact":
            fact_id = str(it.get("fs_id") or "").strip()
            if not fact_id:
                continue

            fact: Dict[str, Any] = {
                "id": fact_id,
                "attribution": {"changeMessage": msg},
            }

            try:
                fs_person = deserialize.Person._index.get(fsid)
                if fs_person and getattr(fs_person, "facts", None):
                    for f in fs_person.facts:
                        if getattr(f, "id", None) == fact_id and getattr(
                            f, "type", None
                        ):
                            fact["type"] = str(f.type)
                            break
            except Exception:
                pass

            gr_date = str(it.get("gr_date") or "").strip()
            gr_place = str(it.get("gr_val") or "").strip()

            if gr_date:
                fact["date"] = {"formal": gr_date, "original": gr_date}
            if gr_place:
                fact["place"] = {"original": gr_place}

            if "date" not in fact and "place" not in fact:
                continue

            facts.append(fact)

    if names:
        person["names"] = names
    if facts:
        person["facts"] = facts

    return {"persons": [person]}


def sync_to_familysearch(
    dbstate, uistate, track, person, session, parent, editor=None
) -> None:
    if not (
        getattr(session, "logged", False)
        or getattr(session, "access_token", None)
        or getattr(session, "connected", False)
    ):
        WarningDialog(_("Not connected to FamilySearch."), parent=parent)
        return

    fsid = fs_utilities.get_fsftid(person)
    if not fsid:
        WarningDialog(
            _("No FamilySearch Person ID is set for this person."), parent=parent
        )
        return

    _prime_cache(session, fsid)

    fs_person = deserialize.Person._index.get(fsid)
    if fs_person is None:
        WarningDialog(
            _(
                "FamilySearch person is not cached yet.\nTry 'Sync from FamilySearch' once or re-login, then try again."
            ),
            parent=parent,
        )
        return

    try:
        model = _make_overview_model()
        fs_compare.compare_fs_to_gramps(
            fs_person, person, dbstate.db, model=model, dupdoc=True
        )
    except Exception as e:
        WarningDialog(
            _("Could not prepare compare data: {e}").format(e=str(e)), parent=parent
        )
        return

    items = _collect_push_items(model)
    if not items:
        WarningDialog(_("No pushable differences found."), parent=parent)
        return

    change_message, chosen = _prompt(parent, items)
    if not chosen:
        return

    payload = _build_payload(fsid, chosen, change_message)

    path = f"/platform/tree/persons/{fsid}"

    try:
        r_head = session.head_url(path)
    except Exception as e:
        WarningDialog(
            _("FamilySearch request failed: {e}").format(e=str(e)), parent=parent
        )
        return

    # follow forwarded IDs (301)
    try:
        while (
            r_head is not None
            and getattr(r_head, "status_code", 0) == 301
            and "X-Entity-Forwarded-Id" in r_head.headers
        ):
            new_id = (r_head.headers.get("X-Entity-Forwarded-Id") or "").strip()
            if not new_id:
                break
            fsid = new_id
            payload = _build_payload(fsid, chosen, change_message)
            path = f"/platform/tree/persons/{fsid}"
            r_head = session.head_url(path)
    except Exception:
        pass

    etag = ""
    last_mod = ""
    try:
        etag = (r_head.headers.get("Etag") or r_head.headers.get("ETag") or "").strip()
        last_mod = (r_head.headers.get("Last-Modified") or "").strip()
    except Exception:
        pass

    headers = {
        "accept": "application/x-gedcomx-v1+json",
        "content-type": "application/x-gedcomx-v1+json",
    }
    if etag:
        headers["If-Match"] = etag
    if last_mod:
        headers["If-Unmodified-Since"] = last_mod

    try:
        resp = session.post(path, json=payload, headers=headers)
    except Exception as e:
        WarningDialog(
            _("FamilySearch update failed: {e}").format(e=str(e)), parent=parent
        )
        return

    if resp is None:
        WarningDialog(_("FamilySearch update failed (no response)."), parent=parent)
        return

    if resp.status_code in (200, 201, 204):
        _prime_cache(session, fsid)
        WarningDialog(_("FamilySearch updated successfully."), parent=parent)
        return

    if resp.status_code in (409, 412):
        WarningDialog(
            _("FamilySearch rejected the update (person changed on server)."),
            parent=parent,
        )
        return

    msg = _err_text(resp)
    WarningDialog(
        _("FamilySearch update failed (HTTP {code}).").format(
            code=getattr(resp, "status_code", "?")
        )
        + (("\n" + msg) if msg else ""),
        parent=parent,
    )

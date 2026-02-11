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
from typing import Any, Optional

from gi.repository import Gtk, Gdk  # noqa: F401 (Gdk used in some UI flows)

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.lib import (
    Attribute,
    AttributeType,
    Family,
    Person,
    ChildRef,
    EventRef,
    EventRoleType,
    EventType,
)
from gramps.gen.db import DbTxn
from gramps.gen.errors import HandleError

from .import_ import deserializer as deserialize
from . import ui as fs_ui

logger = logging.getLogger(__name__)

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext

FS_ATTR_CANON = "_FSFTID"
FS_ATTR_OLD = "_FSTID"
FS_ATTR_HUMAN = "FamilySearch ID"


def _dbg(msg: str) -> None:
    if os.environ.get("GRAMPS_FS_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        logger.debug("%s", msg)


def _bind_global_session(session) -> None:
    # many FS code paths still rely on module-global tree._fs_session
    # kept for windows compat
    try:
        from gramps.gui.fs import tree as fs_tree
        fs_tree._fs_session = session
    except Exception:
        pass


def _info(parent, title: str, body: str) -> None:
    try:
        from gramps.gui.dialog import OkDialog  # type: ignore
    except Exception:
        OkDialog = None  # type: ignore

    if OkDialog:
        try:
            OkDialog(title, body, parent=parent)
            return
        except TypeError:
            try:
                OkDialog(title, body, parent)
                return
            except Exception:
                pass
        except Exception:
            pass

    try:
        fs_ui.info_dialog(parent, title, body)
    except Exception:
        dlg = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(body)
        dlg.run()
        dlg.destroy()


def _error(parent, title: str, body: str) -> None:
    try:
        from gramps.gui.dialog import ErrorDialog  # type: ignore
    except Exception:
        ErrorDialog = None  # type: ignore

    if ErrorDialog:
        try:
            ErrorDialog(title, body, parent=parent)
            return
        except TypeError:
            try:
                ErrorDialog(title, body, parent)
                return
            except Exception:
                pass
        except Exception:
            pass

    try:
        fs_ui.error_dialog(parent, title, body)
    except Exception:
        dlg = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(body)
        dlg.run()
        dlg.destroy()


def _get_fs_id(person) -> str:
    if not person:
        return ""
    for a in person.get_attribute_list() or []:
        atype = str(a.get_type())
        if atype in (FS_ATTR_CANON, FS_ATTR_OLD, FS_ATTR_HUMAN):
            v = (a.get_value() or "").strip()
            if v:
                return v
    return ""


def _set_fs_id(person, fsid: str) -> None:
    if not person:
        return
    fsid = (fsid or "").strip()

    attrs = []
    for a in person.get_attribute_list() or []:
        if str(a.get_type()) in (FS_ATTR_CANON, FS_ATTR_OLD, FS_ATTR_HUMAN):
            continue
        attrs.append(a)

    if fsid:
        a = Attribute()
        a.set_type(AttributeType(FS_ATTR_CANON))
        a.set_value(fsid)
        attrs.append(a)

    person.set_attribute_list(attrs)


def _require_ready_person(dbstate, parent, person):
    db = dbstate.db
    h = getattr(person, "get_handle", lambda: None)()
    if not h or not getattr(db, "has_person_handle", lambda _h: True)(h):
        _error(
            parent,
            _("FamilySearch"),
            _("Please save/commit this person to the database first, then try again."),
        )
        return None
    return h


# ---------------
# Link/Compare
# ---------------

def link_familysearch_id(dbstate, uistate, track, person, session, parent, editor=None):
    _bind_global_session(session)

    d = Gtk.Dialog(title=_("Link FamilySearch ID"), transient_for=parent, modal=True)
    fs_ui.set_headerbar(d, _("Link FamilySearch ID"))
    d.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    d.add_button(_("OK"), Gtk.ResponseType.OK)

    box = d.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)

    lbl = Gtk.Label(label=_("Enter FamilySearch Person ID (e.g. GSVF-SGV):"))
    lbl.set_xalign(0.0)
    box.pack_start(lbl, False, False, 0)

    entry = Gtk.Entry()
    entry.set_text(_get_fs_id(person))
    box.pack_start(entry, False, False, 0)

    d.show_all()
    resp = d.run()
    if resp == Gtk.ResponseType.OK:
        _set_fs_id(person, entry.get_text())

        if editor is not None and hasattr(editor, "attr_list"):
            try:
                editor.attr_list.rebuild_callback()
            except Exception:
                pass

    d.destroy()


def compare_person(dbstate, uistate, track, person, session, parent, editor=None):
    _bind_global_session(session)

    fsid = _get_fs_id(person)
    if not fsid:
        _info(parent, _("FamilySearch"), _("No FamilySearch ID linked.\nClick 'Link FamilySearch ID' first."))
        return

    _ensure_status_schema(dbstate.db)

    from gramps.gui.fs.compare.window import CompareWindow

    try:
        CompareWindow(
            dbstate,
            uistate,
            track,
            person,
            fsid=fsid,
            session=session,
            parent=parent,
            editor=editor,
        )
    except TypeError:
        CompareWindow(dbstate, uistate, track, person, fsid, session, parent)


# -------------------------------
# FamilySearch API fetch helper
# -------------------------------

def _platform_json(session, endpoint: str) -> dict:
    """
    Fetch GEDCOM JSON from a /platform/... endpoint using the Session object.
    """
    for name in ("get_jsonurl", "get_json", "get_gedcomx"):
        fn = getattr(session, name, None)
        if callable(fn):
            try:
                data = fn(endpoint)
                return data if isinstance(data, dict) else (data.json() if hasattr(data, "json") else {})
            except Exception:
                pass

    fn = getattr(session, "get_url", None)
    if callable(fn):
        try:
            r = fn(endpoint, {"Accept": "application/x-gedcomx-v1+json"})
            if r and hasattr(r, "json"):
                return r.json() or {}
        except TypeError:
            try:
                r = fn(endpoint, headers={"Accept": "application/x-gedcomx-v1+json"})
                if r and hasattr(r, "json"):
                    return r.json() or {}
            except Exception:
                pass
        except Exception:
            pass

    import requests

    base = getattr(session, "api_url", "") or getattr(session, "API_URL", "") or "https://apibeta.familysearch.org"
    base = str(base).rstrip("/")
    token = getattr(session, "access_token", "") or ""
    url = base + (endpoint if endpoint.startswith("/") else ("/" + endpoint))
    headers = {"Accept": "application/x-gedcomx-v1+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json() or {}


def _fs_display_name(p: dict) -> str:
    disp = (p or {}).get("display") or {}
    return (disp.get("name") or disp.get("fullName") or "").strip()


# --------------------------
# Relatives chooser dialog
# --------------------------

def _pick_fsid_list(parent, title: str, rows: list[tuple[str, str, bool]]) -> list[str]:
    """
    rows: [(fsid, label, exists)]
    returns selected fsids
    """
    dlg = Gtk.Dialog(title=title, transient_for=parent, flags=0)
    dlg.get_style_context().add_class("fs-rel-import-dialog")
    fs_ui.set_headerbar(dlg, title)

    dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    dlg.add_button(_("Import selected"), Gtk.ResponseType.OK)

    box = dlg.get_content_area()
    box.get_style_context().add_class("fs-rel-import-wrap")
    box.set_spacing(10)
    box.set_margin_top(10)
    box.set_margin_bottom(10)
    box.set_margin_start(10)
    box.set_margin_end(10)

    store = Gtk.ListStore(bool, str, str, bool)
    existing_ct = 0
    for fsid, label, exists in rows:
        if exists:
            existing_ct += 1
        store.append([not exists, label, fsid, bool(exists)])

    total = len(rows)
    new_ct = total - existing_ct

    top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    top.get_style_context().add_class("fs-rel-import-panel")

    lbl_counts = Gtk.Label(label=_("{total} found - {new} new - {existing} already in tree").format(
        total=total, new=new_ct, existing=existing_ct
    ))
    lbl_counts.set_xalign(0.0)
    top.pack_start(lbl_counts, True, True, 0)

    btn_none = Gtk.Button(label=_("Select none"))
    btn_new = Gtk.Button(label=_("Select all new"))

    def do_none(_btn):
        for row in store:
            row[0] = False

    def do_new(_btn):
        for row in store:
            row[0] = (not row[3])

    btn_none.connect("clicked", do_none)
    btn_new.connect("clicked", do_new)

    top.pack_end(btn_new, False, False, 0)
    top.pack_end(btn_none, False, False, 0)

    tv = Gtk.TreeView(model=store)
    tv.get_style_context().add_class("fs-rel-import-treeview")
    fs_ui.tune_treeview(tv)

    cr_toggle = Gtk.CellRendererToggle()
    cr_toggle.connect("toggled", lambda _w, path: store[path].__setitem__(0, not store[path][0]))
    col_toggle = Gtk.TreeViewColumn(_("Import"), cr_toggle, active=0)
    tv.append_column(col_toggle)

    cr_text = Gtk.CellRendererText()
    col_label = Gtk.TreeViewColumn(_("Person"), cr_text, text=1)

    def _label_cell_func(_col, cell, model, itr, _data=None):
        try:
            exists = bool(model.get_value(itr, 3))
        except Exception:
            exists = False

        if exists:
            fs_ui.set_cell_bg(cell, "green")
        else:
            fs_ui.clear_cell_bg(cell)

    col_label.set_cell_data_func(cr_text, _label_cell_func)
    tv.append_column(col_label)

    box.add(top)
    box.add(
        fs_ui.build_legend_row(
            [("green", _("Already in tree")), ("gray", _("New (will import)"))],
            hint=_("Tip: uncheck anything you don't want to import."),
            wrap_class="fs-rel-import-legend",
        )
    )
    box.add(fs_ui.wrap_scroller(tv, min_h=320))

    dlg.show_all()
    resp = dlg.run()
    chosen: list[str] = []
    if resp == Gtk.ResponseType.OK:
        chosen = [row[2] for row in store if row[0] and row[2]]
    dlg.destroy()
    return chosen


# ------------------------
# Import pipeline wrapper
# ------------------------

def _import_full_person(dbstate, uistate, fsid: str, verbosity: int = 0) -> None:
    import gramps.gui.fs.import_ as fs_import

    _ensure_status_schema(dbstate.db)

    class _Caller:
        def __init__(self, dbstate, uistate):
            self.dbstate = dbstate
            self.uistate = uistate

    caller = _Caller(dbstate, uistate)

    importer = fs_import.FSToGrampsImporter()
    importer.noreimport = False
    importer.asc = 0
    importer.desc = 0
    importer.include_spouses = False
    importer.include_notes = False
    importer.include_sources = False
    importer.refresh_signals = False
    importer.verbosity = verbosity
    importer.import_cpr = False  # avoid double relationship creation

    importer.import_tree(caller, fsid)


# --------------------------
# Family linking helpers
# --------------------------

def _ensure_child_ref(child_handle: str) -> ChildRef:
    cr = ChildRef()
    if hasattr(cr, "set_reference_handle"):
        cr.set_reference_handle(child_handle)
    else:
        cr.ref = child_handle
    return cr


def _family_other_parent_handle(fam: Family, me_handle: str):
    fh = fam.get_father_handle()
    mh = fam.get_mother_handle()
    if fh == me_handle:
        return mh
    if mh == me_handle:
        return fh
    return None


def _ensure_person_has_family_handle(db, txn, person: Person, fam_handle: str) -> None:
    fams = list(person.get_family_handle_list() or [])
    if fam_handle not in fams:
        fams.append(fam_handle)
        person.set_family_handle_list(fams)
        db.commit_person(person, txn)


def _ensure_person_has_parent_family_handle(db, txn, person: Person, fam_handle: str) -> None:
    fams = list(person.get_parent_family_handle_list() or [])
    if fam_handle not in fams:
        fams.append(fam_handle)
        person.set_parent_family_handle_list(fams)
        db.commit_person(person, txn)


def _ensure_child_in_family(db, fam: Family, child_handle: str) -> bool:
    for cr in fam.get_child_ref_list() or []:
        try:
            if getattr(cr, "ref", None) == child_handle:
                return False
            if hasattr(cr, "get_reference_handle") and cr.get_reference_handle() == child_handle:
                return False
        except Exception:
            pass
    fam.add_child_ref(_ensure_child_ref(child_handle))
    return True


def _place_parent_in_family(db, fam: Family, parent_handle: str) -> None:
    try:
        p = db.get_person_from_handle(parent_handle)
        g = p.get_gender()
    except Exception:
        g = Person.UNKNOWN

    fh = fam.get_father_handle()
    mh = fam.get_mother_handle()

    if g == Person.MALE:
        if not fh:
            fam.set_father_handle(parent_handle)
        elif not mh and fh != parent_handle:
            fam.set_mother_handle(parent_handle)
        return

    if g == Person.FEMALE:
        if not mh:
            fam.set_mother_handle(parent_handle)
        elif not fh and mh != parent_handle:
            fam.set_father_handle(parent_handle)
        return

    if not fh:
        fam.set_father_handle(parent_handle)
    elif not mh:
        fam.set_mother_handle(parent_handle)


def _family_parent_set(fam: Family) -> set[str]:
    return set(filter(None, [fam.get_father_handle(), fam.get_mother_handle()]))


def _find_existing_family_for_parents(db, parent_handles: set[str]) -> Family | None:
    if not parent_handles:
        return None

    for ph in parent_handles:
        try:
            p = db.get_person_from_handle(ph)
        except HandleError:
            continue
        except Exception:
            continue

        if not p:
            continue

        for fh in p.get_family_handle_list() or []:
            if not fh:
                continue
            try:
                fam = db.get_family_from_handle(fh)
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
        import gramps.gui.fs.utilities as fs_utilities
        idx = getattr(fs_utilities, "FS_INDEX_PEOPLE", {})
        h = idx.get(fsid)
        if h:
            try:
                return db.get_person_from_handle(h)
            except HandleError:
                idx.pop(fsid, None)
    except Exception:
        pass

    try:
        import gramps.gui.fs.utilities as fs_utilities
        get_fsftid = getattr(fs_utilities, "get_fsftid", None)
    except Exception:
        get_fsftid = None

    for h in db.get_person_handles():
        p = db.get_person_from_handle(h)
        if not p:
            continue
        try:
            pid = get_fsftid(p) if callable(get_fsftid) else ""
        except Exception:
            pid = ""
        if pid == fsid:
            return p
    return None


# ---------------------------------------
# Import: Parents / Children / Spouses
# ---------------------------------------

def import_parents(dbstate, uistate, track, person, session, parent, editor=None):
    _bind_global_session(session)

    fsid = _get_fs_id(person)
    if not fsid:
        _info(parent, _("FamilySearch"), _("No FamilySearch ID linked. Click 'Link FamilySearch ID' first."))
        return
    me_handle = _require_ready_person(dbstate, parent, person)
    if not me_handle:
        return

    _ensure_status_schema(dbstate.db)

    try:
        data = _platform_json(session, f"/platform/tree/persons/{fsid}/parents")
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Failed to fetch parents: {e}").format(e=e))
        return

    persons = {p.get("id"): p for p in (data.get("persons") or []) if p.get("id")}
    caprs = data.get("childAndParentsRelationships") or []

    parent_ids: list[str] = []
    for rel in caprs:
        child = (rel.get("child") or {}).get("resourceId")
        if child != fsid:
            continue
        p1 = (rel.get("parent1") or {}).get("resourceId")
        p2 = (rel.get("parent2") or {}).get("resourceId")
        for pid in (p1, p2):
            if pid and pid != fsid and pid not in parent_ids:
                parent_ids.append(pid)

    if not parent_ids:
        _info(parent, _("FamilySearch"), _("No parents found on FamilySearch for this person."))
        return

    db = dbstate.db
    rows = []
    for pid in parent_ids:
        nm = _fs_display_name(persons.get(pid, {}) or {}) or pid
        exists = _find_person_by_fsid(db, pid) is not None
        label = f"{nm} [{pid}]" + (_("  - already in tree") if exists else "")
        rows.append((pid, label, exists))

    chosen = _pick_fsid_list(parent, _("Import parents"), rows)
    if not chosen:
        return

    imported_parents: list[Person] = []
    for pid in chosen:
        _import_full_person(dbstate, uistate, pid, verbosity=0)
        pr = _find_person_by_fsid(db, pid)
        if pr:
            imported_parents.append(pr)

    if not imported_parents:
        _error(parent, _("FamilySearch"), _("Parents imported but could not be located in the local database."))
        return

    with DbTxn(_("FamilySearch: Link parents"), db) as txn:
        child = db.get_person_from_handle(me_handle)

        fam = None
        fam_handle = None

        if not (child.get_parent_family_handle_list() or []):
            parent_handles = set(
                p.handle for p in imported_parents
                if p is not None and getattr(p, "handle", None)
            )
            if len(parent_handles) >= 2:
                fam = _find_existing_family_for_parents(db, parent_handles)
                if fam:
                    fam_handle = fam.handle
                    _ensure_person_has_parent_family_handle(db, txn, child, fam_handle)

        if fam is None:
            for fh in child.get_parent_family_handle_list() or []:
                f = db.get_family_from_handle(fh)
                if not f:
                    continue
                if (not f.get_father_handle()) or (not f.get_mother_handle()):
                    fam = f
                    fam_handle = fh
                    break

        if fam is None:
            fam = Family()
            db.add_family(fam, txn)
            fam_handle = fam.get_handle()
            _ensure_person_has_parent_family_handle(db, txn, child, fam_handle)

        if _ensure_child_in_family(db, fam, child.handle):
            db.commit_family(fam, txn)

        for p in imported_parents:
            _place_parent_in_family(db, fam, p.handle)
            _ensure_person_has_family_handle(db, txn, p, fam_handle)

        db.commit_family(fam, txn)

    if editor is not None and hasattr(editor, "_update_families"):
        try:
            editor._update_families()
        except Exception:
            pass

    _info(parent, _("FamilySearch"), _("Parent(s) imported and linked."))


def import_children(dbstate, uistate, track, person, session, parent, editor=None):
    _bind_global_session(session)

    fsid = _get_fs_id(person)
    if not fsid:
        _info(parent, _("FamilySearch"), _("No FamilySearch ID linked. Click 'Link FamilySearch ID' first."))
        return
    me_handle = _require_ready_person(dbstate, parent, person)
    if not me_handle:
        return

    _ensure_status_schema(dbstate.db)

    try:
        data = _platform_json(session, f"/platform/tree/persons/{fsid}/children")
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Failed to fetch children: {e}").format(e=e))
        return

    persons = {p.get("id"): p for p in (data.get("persons") or []) if p.get("id")}
    caprs = data.get("childAndParentsRelationships") or []

    child_map: dict[str, str | None] = {}
    for rel in caprs:
        child = (rel.get("child") or {}).get("resourceId")
        p1 = (rel.get("parent1") or {}).get("resourceId")
        p2 = (rel.get("parent2") or {}).get("resourceId")
        if not child:
            continue
        if p1 == fsid:
            child_map[child] = p2 if p2 and p2 != fsid else None
        elif p2 == fsid:
            child_map[child] = p1 if p1 and p1 != fsid else None

    child_ids = [cid for cid in child_map.keys() if cid and cid != fsid]
    if not child_ids:
        _info(parent, _("FamilySearch"), _("No children found on FamilySearch for this person."))
        return

    db = dbstate.db
    rows = []
    for cid in sorted(set(child_ids)):
        cn = _fs_display_name(persons.get(cid, {}) or {}) or cid
        other = child_map.get(cid)
        other_label = ""
        if other:
            other_name = _fs_display_name(persons.get(other, {}) or {}) or other
            other_label = _(" (other parent: {name})").format(name=other_name)
        exists = _find_person_by_fsid(db, cid) is not None
        label = f"{cn}{other_label} [{cid}]" + (_("  - already in tree") if exists else "")
        rows.append((cid, label, exists))

    chosen = _pick_fsid_list(parent, _("Import children"), rows)
    if not chosen:
        return

    imported_children: list[Person] = []
    for cid in chosen:
        _import_full_person(dbstate, uistate, cid, verbosity=0)
        ch = _find_person_by_fsid(db, cid)
        if ch:
            imported_children.append(ch)

    if not imported_children:
        _error(parent, _("FamilySearch"), _("Children imported but could not be located in the local database."))
        return

    with DbTxn(_("FamilySearch: Link children"), db) as txn:
        me = db.get_person_from_handle(me_handle)

        my_fams: list[Family] = []
        for fh in me.get_family_handle_list() or []:
            f = db.get_family_from_handle(fh)
            if f:
                my_fams.append(f)

        for ch in imported_children:
            other_parent_fsid = None
            for cid in chosen:
                p = _find_person_by_fsid(db, cid)
                if p and p.handle == ch.handle:
                    other_parent_fsid = child_map.get(cid)
                    break

            other_parent = _find_person_by_fsid(db, other_parent_fsid) if other_parent_fsid else None

            fam = None
            if other_parent:
                for f in my_fams:
                    if _family_other_parent_handle(f, me.handle) == other_parent.handle:
                        fam = f
                        break

            if fam is None:
                for f in my_fams:
                    if not _family_other_parent_handle(f, me.handle):
                        fam = f
                        break

            if fam is None:
                fam = Family()
                if me.get_gender() == Person.MALE:
                    fam.set_father_handle(me.handle)
                elif me.get_gender() == Person.FEMALE:
                    fam.set_mother_handle(me.handle)
                else:
                    fam.set_father_handle(me.handle)
                db.add_family(fam, txn)
                db.commit_family(fam, txn)
                my_fams.append(fam)
                _ensure_person_has_family_handle(db, txn, me, fam.handle)

            if other_parent:
                _place_parent_in_family(db, fam, other_parent.handle)
                _ensure_person_has_family_handle(db, txn, other_parent, fam.handle)

            if _ensure_child_in_family(db, fam, ch.handle):
                db.commit_family(fam, txn)
            _ensure_person_has_parent_family_handle(db, txn, ch, fam.handle)

    if editor is not None and hasattr(editor, "_update_families"):
        try:
            editor._update_families()
        except Exception:
            pass

    _info(parent, _("FamilySearch"), _("Child(ren) imported and linked."))


def import_spouse(dbstate, uistate, track, person, session, parent, editor=None):
    return import_spouses(dbstate, uistate, track, person, session, parent, editor=editor)


def import_spouses(dbstate, uistate, track, person, session, parent, editor=None):
    _bind_global_session(session)

    fsid = _get_fs_id(person)
    if not fsid:
        _info(parent, _("FamilySearch"), _("No FamilySearch ID linked. Click 'Link FamilySearch ID' first."))
        return
    me_handle = _require_ready_person(dbstate, parent, person)
    if not me_handle:
        return

    _ensure_status_schema(dbstate.db)

    try:
        data = _platform_json(session, f"/platform/tree/persons/{fsid}/spouses")
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Failed to fetch spouses: {e}").format(e=e))
        return

    persons = {p.get("id"): p for p in (data.get("persons") or []) if p.get("id")}
    rels = data.get("relationships") or []
    caprs = data.get("childAndParentsRelationships") or []

    spouse_ids: set[str] = set()

    for rel in rels:
        p1 = (rel.get("person1") or {}).get("resourceId")
        p2 = (rel.get("person2") or {}).get("resourceId")
        if p1 == fsid and p2:
            spouse_ids.add(p2)
        elif p2 == fsid and p1:
            spouse_ids.add(p1)

    for rel in caprs:
        p1 = (rel.get("parent1") or {}).get("resourceId")
        p2 = (rel.get("parent2") or {}).get("resourceId")
        if p1 == fsid and p2:
            spouse_ids.add(p2)
        elif p2 == fsid and p1:
            spouse_ids.add(p1)

    spouse_ids.discard(fsid)
    if not spouse_ids:
        _info(parent, _("FamilySearch"), _("No spouses found on FamilySearch for this person."))
        return

    db = dbstate.db
    rows = []
    for sid in sorted(spouse_ids):
        nm = _fs_display_name(persons.get(sid, {}) or {}) or sid
        exists = _find_person_by_fsid(db, sid) is not None
        label = f"{nm} [{sid}]" + (_("  - already in tree") if exists else "")
        rows.append((sid, label, exists))

    chosen = _pick_fsid_list(parent, _("Import spouse(s)"), rows)
    if not chosen:
        return

    imported_spouses: list[Person] = []
    for sid in chosen:
        _import_full_person(dbstate, uistate, sid, verbosity=0)
        sp = _find_person_by_fsid(db, sid)
        if sp:
            imported_spouses.append(sp)

    if not imported_spouses:
        _error(parent, _("FamilySearch"), _("Spouses imported but could not be located in the local database."))
        return

    with DbTxn(_("FamilySearch: Link spouses"), db) as txn:
        me = db.get_person_from_handle(me_handle)

        my_fams: list[Family] = []
        for fh in me.get_family_handle_list() or []:
            f = db.get_family_from_handle(fh)
            if f:
                my_fams.append(f)

        for sp in imported_spouses:
            fam = None
            for f in my_fams:
                other = _family_other_parent_handle(f, me.handle)
                if other == sp.handle:
                    fam = f
                    break

            if fam is None:
                for f in my_fams:
                    if not _family_other_parent_handle(f, me.handle):
                        fam = f
                        break

            if fam is None:
                fam = Family()
                if me.get_gender() == Person.MALE:
                    fam.set_father_handle(me.handle)
                elif me.get_gender() == Person.FEMALE:
                    fam.set_mother_handle(me.handle)
                else:
                    fam.set_father_handle(me.handle)
                db.add_family(fam, txn)
                db.commit_family(fam, txn)
                my_fams.append(fam)

            _place_parent_in_family(db, fam, sp.handle)
            db.commit_family(fam, txn)

            _ensure_person_has_family_handle(db, txn, me, fam.handle)
            _ensure_person_has_family_handle(db, txn, sp, fam.handle)

    if editor is not None and hasattr(editor, "_update_families"):
        try:
            editor._update_families()
        except Exception:
            pass

    _info(parent, _("FamilySearch"), _("Spouse(s) imported and linked."))


def import_parent_family(dbstate, uistate, track, person, session, parent, editor=None):
    return import_parents(dbstate, uistate, track, person, session, parent, editor=editor)


def import_family_parents(dbstate, uistate, track, person, session, parent, editor=None):
    return import_parents(dbstate, uistate, track, person, session, parent, editor=editor)


def import_child(dbstate, uistate, track, person, session, parent, editor=None):
    return import_children(dbstate, uistate, track, person, session, parent, editor=editor)


def import_family_children(dbstate, uistate, track, person, session, parent, editor=None):
    return import_children(dbstate, uistate, track, person, session, parent, editor=editor)


def import_partner(dbstate, uistate, track, person, session, parent, editor=None):
    return import_spouses(dbstate, uistate, track, person, session, parent, editor=editor)


# -------------------------------------
# clear json cache, tags, sync Person
# -------------------------------------

def _strip_unknowns_inplace(data: Any) -> None:
    KEY = "PersonInfo:visibleToAllWhenUsingFamilySearchApps"
    if isinstance(data, dict):
        data.pop(KEY, None)
        for v in list(data.values()):
            _strip_unknowns_inplace(v)
    elif isinstance(data, list):
        for v in list(data):
            _strip_unknowns_inplace(v)


def clear_cache(dbstate, uistate, track, person, session, parent, editor=None) -> None:
    _bind_global_session(session)

    cleared_indexes = 0
    cleared_disk = False
    disk_path = ""

    try:
        for _name, obj in list(vars(deserialize).items()):
            idx = getattr(obj, "_index", None)
            if isinstance(idx, dict):
                idx.clear()
                cleared_indexes += 1
    except Exception as e:
        _dbg(f"clear_cache: gedcomx index clear failed: {e}")

    try:
        import gramps.gui.fs.person.fsg_sync as FSG_Sync
        from gramps.gui.fs import tree as fs_tree
        try:
            fs_tree._fs_session = session
        except Exception:
            pass
        FSG_Sync.FSG_Sync.fs_Tree = fs_tree.Tree()
        try:
            FSG_Sync.FSG_Sync.fs_Tree._getsources = False
        except Exception:
            pass
    except Exception as e:
        _dbg(f"clear_cache: fs_Tree reset failed: {e}")

    try:
        import gramps.gui.fs.person.mixins.cache as cache_mod
        cache_dir = os.path.dirname(cache_mod.__file__)
        disk_path = cache_dir
        FsCache = getattr(cache_mod, "_FsCache", None)
        if FsCache:
            c = FsCache(cache_dir)
            for meth in ("clear", "clear_all", "purge", "wipe", "reset"):
                fn = getattr(c, meth, None)
                if callable(fn):
                    fn()
                    cleared_disk = True
                    break
    except Exception as e:
        _dbg(f"clear_cache: disk cache clear failed: {e}")

    msg = []
    msg.append(_("Cleared GEDCOMX in-memory indices: {n}").format(n=cleared_indexes))
    msg.append(_("Reset shared FS Tree cache: yes"))
    if cleared_disk:
        msg.append(_("Cleared on-disk cache: yes ({path})").format(path=disk_path))
    else:
        msg.append(_("Cleared on-disk cache: (not available / not supported by cache helper)"))

    _info(parent, _("FamilySearch"), "\n".join(msg))


def tags_dialog(dbstate, uistate, track, person, session, parent, editor=None) -> None:
    _bind_global_session(session)

    dlg = Gtk.Dialog(title=_("FamilySearch Tags"), transient_for=parent, flags=0)
    fs_ui.set_headerbar(dlg, _("FamilySearch Tags"))
    dlg.add_button(_("Close"), Gtk.ResponseType.CLOSE)
    dlg.add_button(_("Retag all (Linked/NotLinked)"), Gtk.ResponseType.OK)

    box = dlg.get_content_area()
    box.set_margin_top(10)
    box.set_margin_bottom(10)
    box.set_margin_start(10)
    box.set_margin_end(10)
    box.set_spacing(8)

    box.add(Gtk.Label(label=_(
        "This will scan your whole tree and apply FS_Linked / FS_NotLinked tags.\n"
        "It does NOT change any person data."
    )))

    dlg.show_all()
    resp = dlg.run()
    dlg.destroy()

    if resp != Gtk.ResponseType.OK:
        return

    try:
        from gramps.gui.fs import tags as fs_tags
        db = dbstate.db
        total, linked, not_linked, changed = fs_tags.retag_all_link_status(db)
        _info(
            parent,
            _("FamilySearch"),
            _(
                "Retag complete.\n\n"
                "Total persons: {total}\n"
                "Linked: {linked}\n"
                "Not linked: {not_linked}\n"
                "Changed: {changed}"
            ).format(total=total, linked=linked, not_linked=not_linked, changed=changed),
        )
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Retag failed: {e}").format(e=e))


def _resolve_redirected_fsid(session, fsid: str) -> str:
    fsid = (fsid or "").strip()
    if not fsid:
        return fsid

    head = getattr(session, "head_url", None)
    if not callable(head):
        return fsid

    try:
        path = f"/platform/tree/persons/{fsid}"
        r = head(path)
        while r is not None and getattr(r, "status_code", None) == 301:
            hdrs = getattr(r, "headers", {}) or {}
            fwd = hdrs.get("X-Entity-Forwarded-Id") or hdrs.get("x-entity-forwarded-id")
            if not fwd:
                break
            fsid = str(fwd).strip()
            path = f"/platform/tree/persons/{fsid}"
            r = head(path)
    except Exception:
        pass

    return fsid


def _refresh_editor_person_views(editor) -> None:
    if editor is None:
        return

    for name in (
        "_update_events",
        "_update_event_list",
        "_update_notebook",
        "_update_notes",
        "_update_facts",
        "_update_families",
        "update",
        "reload",
    ):
        fn = getattr(editor, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass


def sync_this_person(dbstate, uistate, track, person, session, parent, editor=None) -> None:
    # Sync the *selected existing Gramps person* from FamilySearch - facts/events (dates + place) + notes
    _bind_global_session(session)

    fsid = _get_fs_id(person)
    if not fsid:
        _info(parent, _("FamilySearch"), _("No FamilySearch ID linked. Click 'Link FamilySearch ID' first."))
        return

    me_handle = _require_ready_person(dbstate, parent, person)
    if not me_handle:
        return

    db = dbstate.db
    _ensure_status_schema(db)

    fsid2 = _resolve_redirected_fsid(session, fsid)
    if fsid2 and fsid2 != fsid:
        try:
            with DbTxn(_("FamilySearch: Update FSID after redirect"), db) as txn:
                gr = db.get_person_from_handle(me_handle)
                _set_fs_id(gr, fsid2)
                db.commit_person(gr, txn)
            fsid = fsid2
        except Exception:
            fsid = fsid2

    try:
        from gramps.gui.fs import tree as fs_tree

        tmp = fs_tree.Tree()
        try:
            tmp._getsources = False
        except Exception:
            pass

        tmp.add_persons([fsid])

        notes_json = _platform_json(session, f"/platform/tree/persons/{fsid}/notes")
        _strip_unknowns_inplace(notes_json)
        try:
            deserialize.deserialize_json(tmp, notes_json)
        except Exception:
            pass

        fs_person = None
        try:
            fs_person = getattr(tmp, "_persons", {}).get(fsid)
        except Exception:
            fs_person = None

        if fs_person is None:
            try:
                for p in list(getattr(tmp, "persons", []) or []):
                    if getattr(p, "id", None) == fsid:
                        fs_person = p
                        break
            except Exception:
                pass

        if fs_person is None:
            _error(parent, _("FamilySearch"), _("Could not load this person from FamilySearch (no GEDCOMX person object)."))
            return

    except Exception as e:
        _error(parent, _("FamilySearch"), _("Failed to download from FamilySearch: {e}").format(e=e))
        return

    try:
        from gramps.gui.fs.import_.events import add_event
        from gramps.gui.fs.import_.notes import add_note
        import gramps.gui.fs.compare as fs_compare
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Sync pipeline import helpers missing: {e}").format(e=e))
        return

    try:
        with DbTxn(_("FamilySearch: Sync this person"), db) as txn:
            gr_person = db.get_person_from_handle(me_handle)

            for fs_fact in list(getattr(fs_person, "facts", []) or []):
                ev = add_event(db, txn, fs_fact, gr_person)
                if not ev or not getattr(ev, "handle", None):
                    continue

                already = False
                for _er in list(gr_person.get_event_ref_list() or []):
                    try:
                        if getattr(_er, "ref", None) == ev.handle:
                            already = True
                            link_er = _er
                            break
                    except Exception:
                        continue
                else:
                    link_er = None

                if not already:
                    link_er = EventRef()
                    link_er.set_role(EventRoleType.PRIMARY)
                    link_er.set_reference_handle(ev.get_handle())
                    try:
                        db.commit_event(ev, txn)
                    except Exception:
                        pass
                    gr_person.add_event_ref(link_er)

                try:
                    ev_type = int(ev.type) if hasattr(ev.type, "__int__") else ev.type
                    if ev_type == EventType.BIRTH:
                        gr_person.set_birth_ref(link_er)
                    elif ev_type == EventType.DEATH:
                        gr_person.set_death_ref(link_er)
                except Exception:
                    pass

                db.commit_person(gr_person, txn)

            existing_notes = set(gr_person.get_note_list() or [])
            for fs_note in list(getattr(fs_person, "notes", []) or []):
                note = add_note(db, txn, fs_note, gr_person.note_list)
                if note and getattr(note, "handle", None) and note.handle not in existing_notes:
                    gr_person.add_note(note.handle)
                    existing_notes.add(note.handle)

            try:
                fs_compare.compare_fs_to_gramps(fs_person, gr_person, db, None)
            except Exception:
                pass

            db.commit_person(gr_person, txn)

    except Exception as e:
        _error(parent, _("FamilySearch"), _("Sync failed: {e}").format(e=e))
        return

    _refresh_editor_person_views(editor)
    _info(parent, _("FamilySearch"), _("Sync complete: facts/events, dates/places, and notes updated for this person."))


def sync_person(dbstate, uistate, track, person, session, parent, editor=None):
    return sync_this_person(dbstate, uistate, track, person, session, parent, editor=editor)


def _ensure_status_schema(db) -> None:
    # status is stored on Person now, not in a DB table. kept for compat.
    return

# --------------------------
# sync to FS 
# --------------------------

def sync_from_familysearch(dbstate, uistate, track, person, session, parent, editor=None) -> None:
    return sync_this_person(dbstate, uistate, track, person, session, parent, editor=editor)


def sync_to_familysearch(dbstate, uistate, track, person, session, parent, editor=None) -> None:
    _bind_global_session(session)

    fsid = _get_fs_id(person)
    if not fsid:
        _info(parent, _("FamilySearch"), _("No FamilySearch ID linked. Click 'Link FamilySearch ID' first."))
        return

    # ensure person is saved/committed before we read from DB-backed state
    me_handle = _require_ready_person(dbstate, parent, person)
    if not me_handle:
        return

    try:
        from . import sync_directions
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Sync-to module missing: {e}").format(e=e))
        return

    try:
        sync_directions.sync_to_familysearch(dbstate, uistate, track, person, session, parent, editor=editor)
    except Exception as e:
        _error(parent, _("FamilySearch"), _("Sync to FamilySearch failed: {e}").format(e=e))

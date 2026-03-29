#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2025-2026  Gabriel Rios
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

from gramps.gen.db import DbTxn
from gramps.gen.lib import (
    Person,
    Family,
    ChildRef,
    EventRef,
    EventRoleType,
    EventType,
    Attribute,
)
from gramps.gui.utils import ProgressMeter
from gramps.gen.errors import HandleError

from . import _

from gramps.gen.fs import tree
import gramps.gui.fs.person.fsg_sync as FSG_Sync
from gramps.gen.fs.import_ import deserializer as deserialize

from gramps.gen.fs import utilities as fs_utilities
from gramps.gui.fs.utilities.index import build_fs_index

from gramps.gen.fs.utilities import get_fsftid
from .names import add_names
from .events import add_event
from .notes import add_note
from .sources import add_source, fetch_source_dates
from .places import add_place

import gramps.gui.fs.compare as fs_compare


class FSToGrampsImporter:
    """
    Options:
        noreimport (bool)         — skip existing persons (by FSID)
        asc (int)                 — generations upward
        desc (int)                — generations downward
        include_spouses (bool)    — include spouses (default False)
        include_notes (bool)      — include notes
        include_sources (bool)    — include sources
        verbosity (int 0..3)      — log verbosity
        refresh_signals (bool)    — disable/enable db signals during import
    """

    fs_TreeImp = None
    active_handle = None

    def __init__(self):
        self.noreimport = False
        self.asc = 1
        self.desc = 1
        self.include_spouses = False  # never auto-import spouses
        self.include_notes = False
        self.include_sources = False
        self.verbosity = 0
        self.added_person = False
        self.refresh_signals = True
        self.txn = None
        self.dbstate = None
        self.FS_ID = None
        # used by relative import in actions.py to avoid double-linking
        self.import_cpr = True

    # ---- helpers ----------------------------------------------------------

    def _find_couple_family(self, father_h, mother_h):
        # return an existing Family with exactly these parents if any
        if father_h:
            f = self.dbstate.db.get_person_from_handle(father_h)
            for fh in list(f.get_family_handle_list() or []):
                if not fh:
                    continue
                fam = self.dbstate.db.get_family_from_handle(fh)
                if (
                    fam
                    and fam.get_mother_handle() == mother_h
                    and fam.get_father_handle() == father_h
                ):
                    return fam
        if mother_h:
            m = self.dbstate.db.get_person_from_handle(mother_h)
            for fh in list(m.get_family_handle_list() or []):
                if not fh:
                    continue
                fam = self.dbstate.db.get_family_from_handle(fh)
                if (
                    fam
                    and fam.get_father_handle() == father_h
                    and fam.get_mother_handle() == mother_h
                ):
                    return fam
        return None

    def _strip_unknowns(self, data):
        KEY = "PersonInfo:visibleToAllWhenUsingFamilySearchApps"
        if isinstance(data, dict):
            data.pop(KEY, None)
            for v in list(data.values()):
                self._strip_unknowns(v)
        elif isinstance(data, list):
            for v in list(data):
                self._strip_unknowns(v)

    def _ensure_root_parent_link(self, root_fsid: str):
        # if only one parent is imported, ensure the selected/root child is linked to parent by gen-ing/reusing a one-parent Family
        if not root_fsid:
            return
        child_h = fs_utilities.FS_INDEX_PEOPLE.get(root_fsid)
        if not child_h:
            return

        child = self.dbstate.db.get_person_from_handle(child_h)
        # If already linked to some parent family nothing to do.
        if child.get_parent_family_handle_list():
            return

        father_h = mother_h = None
        # explicitly references the root child.
        for cpr in list(
            getattr(self.fs_TreeImp, "childAndParentsRelationships", []) or []
        ):
            if cpr.child and cpr.child.resourceId == root_fsid:
                father_h = (
                    fs_utilities.FS_INDEX_PEOPLE.get(cpr.parent1.resourceId)
                    if cpr.parent1
                    else None
                )
                mother_h = (
                    fs_utilities.FS_INDEX_PEOPLE.get(cpr.parent2.resourceId)
                    if cpr.parent2
                    else None
                )
                break

        # we can't reliably guess parents
        if not father_h and not mother_h:
            return

        # reuse or create the correct family (one- or two-parent)
        family = self._find_couple_family(father_h, mother_h)
        if not family:
            family = Family()
            family.set_father_handle(father_h)
            family.set_mother_handle(mother_h)
            self.dbstate.db.add_family(family, self.txn)
            self.dbstate.db.commit_family(family, self.txn)
            if father_h:
                f = self.dbstate.db.get_person_from_handle(father_h)
                f.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(f, self.txn)
            if mother_h:
                m = self.dbstate.db.get_person_from_handle(mother_h)
                m.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(m, self.txn)

        # attach the child if not already a member of this family.
        if not any(
            cr.get_reference_handle() == child_h
            for cr in list(family.get_child_ref_list() or [])
        ):
            cr = ChildRef()
            cr.set_reference_handle(child_h)
            family.add_child_ref(cr)
            self.dbstate.db.commit_family(family, self.txn)
            child.add_parent_family_handle(family.get_handle())
            self.dbstate.db.commit_person(child, self.txn)

    # ---- core import steps ------------------------------------------------

    def add_person(self, db, txn, fs_person):
        fsid = fs_person.id

        gr_person = None
        gr_handle = fs_utilities.FS_INDEX_PEOPLE.get(fsid)

        if gr_handle:
            try:
                gr_person = db.get_person_from_handle(gr_handle)
            except HandleError:
                fs_utilities.FS_INDEX_PEOPLE.pop(fsid, None)
                gr_handle = None

        # If the mapping was stale but the person may still exist, try to find by FSID
        if gr_person is None:
            try:
                for h in db.get_person_handles():
                    p = db.get_person_from_handle(h)
                    if get_fsftid(p) == fsid:
                        gr_person = p
                        fs_utilities.FS_INDEX_PEOPLE[fsid] = p.handle
                        gr_handle = p.handle
                        break
            except Exception:
                pass

        # create if still not found
        if gr_person is None:
            gr_person = Person()
            add_names(db, txn, fs_person, gr_person)

            if not fs_person.gender:
                gr_person.set_gender(Person.UNKNOWN)
            elif fs_person.gender.type == "http://gedcomx.org/Male":
                gr_person.set_gender(Person.MALE)
            elif fs_person.gender.type == "http://gedcomx.org/Female":
                gr_person.set_gender(Person.FEMALE)
            else:
                gr_person.set_gender(Person.UNKNOWN)

            db.add_person(gr_person, txn)
            self.added_person = True
            fs_utilities.link_gramps_fs_id(db, gr_person, fsid)
            fs_utilities.FS_INDEX_PEOPLE[fsid] = gr_person.handle
        else:
            if self.noreimport:
                return

        # facts / events ensure correct EventRef linkage + birth/death pointers
        for fs_fact in list(getattr(fs_person, "facts", []) or []):
            ev = add_event(db, txn, fs_fact, gr_person)

            # find existing link ref (if any) - list()
            link_er = None
            for _er in list(gr_person.get_event_ref_list() or []):
                if _er.ref == ev.handle:
                    link_er = _er
                    break

            # create link if missing
            if link_er is None:
                link_er = EventRef()
                link_er.set_role(EventRoleType.PRIMARY)
                link_er.set_reference_handle(ev.get_handle())
                db.commit_event(ev, txn)
                gr_person.add_event_ref(link_er)

            # maintain birth/death pointers
            ev_type = int(ev.type) if hasattr(ev.type, "__int__") else ev.type
            if ev_type == EventType.BIRTH:
                gr_person.set_birth_ref(link_er)
            elif ev_type == EventType.DEATH:
                gr_person.set_death_ref(link_er)

            db.commit_person(gr_person, txn)

        # notes
        for fs_note in list(getattr(fs_person, "notes", []) or []):
            note = add_note(db, txn, fs_note, gr_person.note_list)
            gr_person.add_note(note.handle)

        # sources
        for fs_src in list(getattr(fs_person, "sources", []) or []):
            add_source(
                db, txn, fs_src.descriptionId, gr_person, gr_person.citation_list
            )

        # compare
        fs_compare.compare_fs_to_gramps(fs_person, gr_person, db, None)

        db.commit_person(gr_person, txn)

    def import_tree(self, caller, FSFTID):
        print("import ID :" + FSFTID)
        self.FS_ID = FSFTID
        self.dbstate = caller.dbstate

        # active selection to restore at the end
        active_handle = caller.uistate.get_active("Person")

        progress = ProgressMeter(
            _("FamilySearch Import"), _("Starting"), parent=caller.uistate.window
        )
        caller.uistate.set_busy_cursor(True)
        if self.refresh_signals:
            caller.dbstate.db.disable_signals()

        # ensure FS session
        if not FSG_Sync.FSG_Sync.ensure_session(caller, self.verbosity):
            from gramps.gui.dialog import WarningDialog

            WarningDialog(_("Not connected to FamilySearch"))
            caller.uistate.set_busy_cursor(False)
            progress.close()
            # restore selection anyway
            if active_handle:
                caller.uistate.set_active(active_handle, "Person")
            return

        # build FS-Gramps index if needed
        if not fs_utilities.FS_INDEX_PEOPLE:
            build_fs_index(caller, progress, 11)

        print("download")
        if self.fs_TreeImp:
            del self.fs_TreeImp
        self.fs_TreeImp = tree.Tree()

        # during import we build our own Tree instance
        if getattr(FSG_Sync.FSG_Sync, "fs_Tree", None) is None:
            FSG_Sync.FSG_Sync.fs_Tree = self.fs_TreeImp

        # 3/11 — person
        progress.set_pass(
            _("Downloading persons… (3/11)"), mode=ProgressMeter.MODE_ACTIVITY
        )
        print(_("Downloading person…"))
        if self.FS_ID:
            self.fs_TreeImp.add_persons([self.FS_ID])
        else:
            caller.uistate.set_busy_cursor(False)
            progress.close()
            if active_handle:
                caller.uistate.set_active(active_handle, "Person")
            return

        # 4/11 — ancestors (limit by self.asc)
        progress.set_pass(_("Downloading ancestors… (4/11)"), self.asc)
        todo = set(self.fs_TreeImp._persons.keys())
        done = set()
        for i in range(self.asc):
            progress.step()
            if not todo:
                break
            done |= todo
            print(_("Downloading %d generations of ancestors…") % (i + 1))
            todo = self.fs_TreeImp.add_parents(set(todo)) - done

        # 5/11 — descendants (limit by self.desc)
        progress.set_pass(_("Downloading descendants… (5/11)"), self.desc)
        todo = set(self.fs_TreeImp._persons.keys())
        done = set()
        for i in range(self.desc):
            progress.step()
            if not todo:
                break
            done |= todo
            print(_("Downloading %d generations of descendants…") % (i + 1))
            todo = self.fs_TreeImp.add_children(set(todo)) - done

        # 6/11 — spouses (only if explicitly requested)
        if self.include_spouses:
            progress.set_pass(
                _("Downloading spouses… (6/11)"), mode=ProgressMeter.MODE_ACTIVITY
            )
            print(_("Downloading spouses…"))
            todo = set(self.fs_TreeImp._persons.keys())
            self.fs_TreeImp.add_spouses(set(todo))

        # 7/11 — notes/sources/memories (optional)
        if self.include_notes or self.include_sources:
            progress.set_pass(
                _("Downloading notes… (7/11)"),
                len(self.fs_TreeImp.persons) + len(self.fs_TreeImp.relationships),
            )
            print(_("Downloading notes and sources…"))

            def _fetch_into_tree(url_path: str) -> None:
                # mypy: tree._fs_session is Optional at type-check time
                sess = getattr(tree, "_fs_session", None)
                if sess is None:
                    return

                fn = getattr(sess, "get_jsonurl", None) or getattr(
                    sess, "get_json", None
                )
                if not callable(fn):
                    return

                payload = fn(url_path)
                self._strip_unknowns(payload)
                deserialize.deserialize_json(self.fs_TreeImp, payload)

            def _load_person_extras(person_id: str) -> None:
                _fetch_into_tree(f"/platform/tree/persons/{person_id}/notes")
                _fetch_into_tree(f"/platform/tree/persons/{person_id}/sources")
                _fetch_into_tree(f"/platform/tree/persons/{person_id}/memories")

            def _load_couple_extras(rel_id: str) -> None:
                _fetch_into_tree(f"/platform/tree/couple-relationships/{rel_id}/notes")
                _fetch_into_tree(
                    f"/platform/tree/couple-relationships/{rel_id}/sources"
                )

            for fs_person in list(self.fs_TreeImp.persons or []):
                progress.step()
                _load_person_extras(fs_person.id)

            for fs_fam in list(self.fs_TreeImp.relationships or []):
                progress.step()
                _load_couple_extras(fs_fam.id)

            fetch_source_dates(self.fs_TreeImp)

        if self.verbosity >= 3:
            res = deserialize.to_string(self.fs_TreeImp)
            with open("import.out.json", "w") as f:
                import json

                json.dump(res, f, indent=2)

        print(_("Importing…"))

        self.added_person = False

        existing_txn = caller.dbstate.db.transaction
        if existing_txn is not None:
            intr = True
            self.txn = existing_txn
        else:
            intr = False
            self.txn = DbTxn("FamilySearch import", caller.dbstate.db)
            caller.dbstate.db.transaction_begin(self.txn)

        # 8/11 — places
        progress.set_pass(_("Importing places… (8/11)"), len(self.fs_TreeImp.places))
        print(_("Importing places…"))
        for pl in list(self.fs_TreeImp.places or []):
            progress.step()
            add_place(caller.dbstate.db, self.txn, pl)

        # 9/11 — persons
        progress.set_pass(_("Importing persons… (9/11)"), len(self.fs_TreeImp.persons))
        print(_("Importing persons…"))
        for fs_person in list(self.fs_TreeImp.persons or []):
            progress.step()
            self.add_person(caller.dbstate.db, self.txn, fs_person)

        # 10/11 — families (couple relationships)
        progress.set_pass(
            _("Importing families… (10/11)"), len(self.fs_TreeImp.relationships)
        )
        print(_("Importing families…"))
        for fs_fam in list(self.fs_TreeImp.relationships or []):
            progress.step()
            if fs_fam.type == "http://gedcomx.org/Couple":
                self.add_family(fs_fam)

        # 11/11 — children (CPR)
        if self.import_cpr:
            progress.set_pass(
                _("Importing children… (11/11)"),
                len(getattr(self.fs_TreeImp, "childAndParentsRelationships", []) or []),
            )
            print(_("Importing children…"))
            for fs_cpr in list(
                getattr(self.fs_TreeImp, "childAndParentsRelationships", []) or []
            ):
                progress.step()
                self.add_child(fs_cpr)

            # ensure root (selected) child is linked even with one parent only
            self._ensure_root_parent_link(self.FS_ID)
        else:
            progress.set_pass(_("Importing children… (11/11)"), 0)

        if not intr:
            caller.dbstate.db.transaction_commit(self.txn)
            del self.txn
        self.txn = None

        print("import done.")
        caller.uistate.set_busy_cursor(False)
        progress.close()

        if self.refresh_signals:
            caller.dbstate.db.enable_signals()
            if self.added_person:
                caller.dbstate.db.request_rebuild()

        if active_handle:
            caller.uistate.set_active(active_handle, "Person")

    def add_child(self, fs_cpr):
        if fs_cpr.parent1:
            father_h = fs_utilities.FS_INDEX_PEOPLE.get(fs_cpr.parent1.resourceId)
        else:
            father_h = None
        if fs_cpr.parent2:
            mother_h = fs_utilities.FS_INDEX_PEOPLE.get(fs_cpr.parent2.resourceId)
        else:
            mother_h = None

        # Resolve child and reject self-parenting
        child_h = (
            fs_utilities.FS_INDEX_PEOPLE.get(fs_cpr.child.resourceId)
            if fs_cpr.child
            else None
        )
        if child_h and (child_h == father_h or child_h == mother_h):
            print(_("Skipping invalid relationship: child equals a parent"))
            return

        # at least one known parent locally
        if not (father_h or mother_h):
            print(
                _("Possibly parentless family - Need at least one known parent locally")
            )
            return

        # create the correct family (works with one- or two-parent)
        family = self._find_couple_family(father_h, mother_h)
        if not family:
            family = Family()
            family.set_father_handle(father_h)
            family.set_mother_handle(mother_h)
            self.dbstate.db.add_family(family, self.txn)
            self.dbstate.db.commit_family(family, self.txn)
            if father_h:
                father = self.dbstate.db.get_person_from_handle(father_h)
                father.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(father, self.txn)
            if mother_h:
                mother = self.dbstate.db.get_person_from_handle(mother_h)
                mother.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(mother, self.txn)

        if not child_h:
            return

        # attach child
        if not any(
            cr.get_reference_handle() == child_h
            for cr in list(family.get_child_ref_list() or [])
        ):
            cr = ChildRef()
            cr.set_reference_handle(child_h)
            family.add_child_ref(cr)
            self.dbstate.db.commit_family(family, self.txn)

            child = self.dbstate.db.get_person_from_handle(child_h)
            child.add_parent_family_handle(family.get_handle())
            self.dbstate.db.commit_person(child, self.txn)

    def add_family(self, fs_fam):
        family = None
        father_h = fs_utilities.FS_INDEX_PEOPLE.get(fs_fam.person1.resourceId)
        mother_h = fs_utilities.FS_INDEX_PEOPLE.get(fs_fam.person2.resourceId)

        father = self.dbstate.db.get_person_from_handle(father_h) if father_h else None
        mother = self.dbstate.db.get_person_from_handle(mother_h) if mother_h else None

        # reject impossible couple
        if father_h and mother_h and father_h == mother_h:
            print(_("Skipping invalid couple: same person as both parents"))
            return

        if father_h or mother_h:
            family = self._find_couple_family(father_h, mother_h)

        # FSID match if present
        if not family:
            if father:
                for fh in list(father.get_family_handle_list() or []):
                    if not fh:
                        continue
                    f = self.dbstate.db.get_family_from_handle(fh)
                    if not f:
                        continue
                    if f.get_mother_handle() == mother_h:
                        family = f
                        break
                    fam_fsid = get_fsftid(f)
                    if fam_fsid == fs_fam.id:
                        family = f
                        break
            if not family and mother:
                for fh in list(mother.get_family_handle_list() or []):
                    if not fh:
                        continue
                    f = self.dbstate.db.get_family_from_handle(fh)
                    if not f:
                        continue
                    if f.get_father_handle() == father_h:
                        family = f
                        break
                    fam_fsid = get_fsftid(f)
                    if fam_fsid == fs_fam.id:
                        family = f
                        break

        if not father_h and not mother_h:
            print(_("Possible parentless family?"))
            return

        # Ensure both sides are linked if family already exists
        if family and not family.get_father_handle() and father_h:
            family.set_father_handle(father_h)
            if father:
                father.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(father, self.txn)
        if family and not family.get_mother_handle() and mother_h:
            family.set_mother_handle(mother_h)
            if mother:
                mother.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(mother, self.txn)

        if not family:
            family = Family()
            family.set_father_handle(father_h)
            family.set_mother_handle(mother_h)
            attr = Attribute()
            attr.set_type("_FSFTID")
            attr.set_value(fs_fam.id)
            family.add_attribute(attr)
            self.dbstate.db.add_family(family, self.txn)
            self.dbstate.db.commit_family(family, self.txn)
            if father:
                father.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(father, self.txn)
            if mother:
                mother.add_family_handle(family.get_handle())
                self.dbstate.db.commit_person(mother, self.txn)

        # Family facts
        for fs_fact in list(getattr(fs_fam, "facts", []) or []):
            ev = add_event(self.dbstate.db, self.txn, fs_fact, family)
            if not any(
                er.ref == ev.handle for er in list(family.get_event_ref_list() or [])
            ):
                er = EventRef()
                er.set_role(EventRoleType.FAMILY)
                er.set_reference_handle(ev.get_handle())
                self.dbstate.db.commit_event(ev, self.txn)
                family.add_event_ref(er)

        self.dbstate.db.commit_family(family, self.txn)

        # notes
        for fs_note in list(getattr(fs_fam, "notes", []) or []):
            note = add_note(self.dbstate.db, self.txn, fs_note, family.note_list)
            family.add_note(note.handle)

        # sources
        for fs_src in list(getattr(fs_fam, "sources", []) or []):
            add_source(
                self.dbstate.db,
                self.txn,
                fs_src.descriptionId,
                family,
                family.citation_list,
            )

        self.dbstate.db.commit_family(family, self.txn)

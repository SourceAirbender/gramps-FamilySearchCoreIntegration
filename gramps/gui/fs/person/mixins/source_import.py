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

from typing import List, Tuple
from collections import defaultdict

from gi.repository import GLib

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.db import DbTxn
from gramps.gen.lib import Citation, NoteType, Media, MediaRef
from gramps.gen.mime import get_type
from gramps.gen.utils.file import expand_media_path, relative_path
from gramps.gui.dialog import WarningDialog

import gramps.gui.fs.import_ as fs_import
import gramps.gui.fs.utilities as fs_utilities
from gramps.gui.fs.import_ import deserializer as deserialize

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext


def _is_fs_web_url(url: str) -> bool:
    if not url:
        return False
    u = str(url).strip()
    return u.startswith(("http://", "https://")) and ("familysearch.org" in u)


def _get_sd_about_url(sd) -> str:
    """
    return a usable FamilySearch *web* URL for a SourceDescription
    """
    if not sd:
        return ""

    about = getattr(sd, "about", None)
    if about is None and isinstance(sd, dict):
        about = sd.get("about")

    if isinstance(about, str) and _is_fs_web_url(about):
        return about.strip()

    try:
        isrc = fs_import.IntermediateSource()
        isrc.from_fs(sd, None)
        if isrc.url and _is_fs_web_url(isrc.url):
            return isrc.url.strip()
    except Exception:
        pass

    return ""


def _ensure_citation_has_source_and_link(db, txn, cit, title: str, url: str) -> None:
    """
    Ensure citation is not 'empty':
      - it references a Source
      - it has an Internet Address attribute (if url provided)
      - it has a Page value (only if currently empty)
    """
    if not cit:
        return

    # 1) ensure a Source exists
    src_handle = None
    try:
        if hasattr(cit, "get_reference_handle"):
            src_handle = cit.get_reference_handle()
        elif hasattr(cit, "get_source_handle"):
            src_handle = cit.get_source_handle()
        else:
            src_handle = getattr(cit, "source_handle", None)
    except Exception:
        src_handle = getattr(cit, "source_handle", None)

    if not src_handle:
        from gramps.gen.lib import Source

        src = Source()
        src.set_title(title or "FamilySearch Source")
        if url:
            try:
                src.set_publication_info(url)
            except Exception:
                pass

        db.add_source(src, txn)
        db.commit_source(src, txn)

        try:
            if hasattr(cit, "set_reference_handle"):
                cit.set_reference_handle(src.handle)
            elif hasattr(cit, "set_source_handle"):
                cit.set_source_handle(src.handle)
            else:
                cit.source_handle = src.handle
        except Exception:
            pass

    # 2) make sure citation.page carries URL only if empty
    if url:
        try:
            page = cit.get_page() or ""
        except Exception:
            page = getattr(cit, "page", "") or ""

        if not page:
            try:
                cit.set_page(url)
            except Exception:
                pass

    # 3) make sure Internet Address attribute exists
    if url:
        try:
            if not fs_utilities.get_internet_address(cit):
                from gramps.gen.lib import SrcAttribute

                a = SrcAttribute()
                a.set_type("Internet Address")
                a.set_value(url)
                cit.add_attribute(a)
        except Exception:
            pass


class SourceImportMixin:
    def _normalize_attr_name(self, s: str) -> str:
        return (s or "").strip().lower().replace("_", " ")

    def _set_attr_on_citation(self, cit: Citation, key: str, val: str):
        if not val:
            return
        key_norm = self._normalize_attr_name(key)
        try:
            updated = False
            for a in list(cit.get_attribute_list()):
                try:
                    t = a.get_type()
                    name_forms = []
                    if hasattr(t, "xml_str"):
                        name_forms.append(t.xml_str())
                    if t is not None:
                        name_forms.append(str(t))
                    if any(self._normalize_attr_name(n) == key_norm for n in name_forms if n):
                        a.set_value(val)
                        updated = True
                        break
                except Exception:
                    continue

            if not updated:
                from gramps.gen.lib import SrcAttribute

                a = SrcAttribute()
                a.set_type(key)
                a.set_value(val)
                cit.add_attribute(a)
        except Exception as e:
            print(f"Failed to set citation attribute '{key}': {e}")

    # keep active person from snapping to Home
    def _try_set_active_person(self, handle: str) -> None:
        if not handle:
            return

        uis = getattr(self, "uistate", None)

        if uis is not None and hasattr(uis, "set_active"):
            try:
                uis.set_active(handle, "Person")
                return
            except Exception:
                pass
            try:
                uis.set_active("Person", handle)
                return
            except Exception:
                pass

        if hasattr(self, "set_active"):
            try:
                self.set_active("Person", handle)
                return
            except Exception:
                pass
            try:
                self.set_active(handle, "Person")
                return
            except Exception:
                pass

    def _pin_active_person(self, handle: str, ms: int = 15000, interval_ms: int = 150) -> None:
        if not handle:
            return

        deadline = GLib.get_monotonic_time() + (ms * 1000)  # microseconds

        def _tick():
            cur = None
            if hasattr(self, "get_active"):
                try:
                    cur = self.get_active("Person")
                except Exception:
                    cur = None

            if cur is None:
                uis = getattr(self, "uistate", None)
                if uis is not None and hasattr(uis, "get_active"):
                    try:
                        cur = uis.get_active("Person")
                    except Exception:
                        cur = None

            if cur != handle:
                self._try_set_active_person(handle)

            return GLib.get_monotonic_time() < deadline

        self._try_set_active_person(handle)
        GLib.timeout_add(interval_ms, _tick)

    def _get_restore_person_handle(self, gr) -> str | None:
        # 1) self.get_active('Person')
        if hasattr(self, "get_active"):
            try:
                h = self.get_active("Person")
                if h:
                    return h
            except Exception:
                pass

        # 2) uistate.get_active('Person')
        uis = getattr(self, "uistate", None)
        if uis is not None and hasattr(uis, "get_active"):
            try:
                h = uis.get_active("Person")
                if h:
                    return h
            except Exception:
                pass

        # 3) passed person handle
        h = getattr(gr, "handle", None) or gr
        return h or None

    # ------------------------------------------------------------------
    # Main import
    # ------------------------------------------------------------------

    def _import_fs_sources(self, gr, items: List[Tuple]) -> int:
        """
        Import/attach selected FS SourceDescriptions to the current Gramps person.

        This version DOES call db.request_rebuild(), but pins selection so we
        end on the same person instead of Home.
        """
        db = self.dbstate.db

        target_handle = self._get_restore_person_handle(gr)

        def _cluster_citation_handles(person_obj) -> set[str]:
            cl: set[str] = set(person_obj.get_citation_list() or [])

            for er in person_obj.get_event_ref_list() or []:
                ev = db.get_event_from_handle(er.ref)
                if ev:
                    cl.update(ev.get_citation_list() or [])

            for fam_h in person_obj.get_family_handle_list() or []:
                fam = db.get_family_from_handle(fam_h)
                if not fam:
                    continue
                cl.update(fam.get_citation_list() or [])
                for er in fam.get_event_ref_list() or []:
                    ev = db.get_event_from_handle(er.ref)
                    if ev:
                        cl.update(ev.get_citation_list() or [])

            return cl

        imported = 0
        errors: List[str] = []

        with DbTxn(_("Import FamilySearch sources"), db) as txn:
            person = db.get_person_from_handle(target_handle) if target_handle else None
            if not person:
                return 0

            sdid_to_cits = defaultdict(list)
            for ch in _cluster_citation_handles(person):
                c = db.get_citation_from_handle(ch)
                if not c:
                    continue
                sdid = fs_utilities.get_fsftid(c)
                if sdid:
                    sdid_to_cits[sdid].append(ch)

            for tup in items:
                if len(tup) == 4:
                    sdid, fs_modified, contributor, final_kind = tup
                    image_paths = []
                    add_to_person = False
                elif len(tup) == 5:
                    sdid, fs_modified, contributor, final_kind, image_paths = tup
                    add_to_person = False
                else:
                    sdid, fs_modified, contributor, final_kind, image_paths, add_to_person = tup

                sdid = (sdid or "").strip()
                if not sdid:
                    continue

                try:
                    existing_handles = sdid_to_cits.get(sdid, [])
                    target_citations: List[Citation] = []

                    if existing_handles:
                        for h in existing_handles:
                            c = db.get_citation_from_handle(h)
                            if c:
                                target_citations.append(c)

                        for c in target_citations:
                            if c.handle not in (person.get_citation_list() or []):
                                try:
                                    person.add_citation(c.handle)
                                except Exception:
                                    pass
                    else:
                        cit = fs_import.add_source(db, txn, sdid, person, person.get_citation_list())
                        if not cit:
                            errors.append(f"{sdid}: add_source returned None")
                            continue

                        sd = deserialize.SourceDescription._index.get(sdid)
                        url = _get_sd_about_url(sd)
                        title = ""
                        try:
                            if sd:
                                isrc = fs_import.IntermediateSource()
                                isrc.from_fs(sd, None)
                                title = isrc.citation_title or isrc.source_title or ""
                                if not url:
                                    url = isrc.url or ""
                        except Exception:
                            pass

                        _ensure_citation_has_source_and_link(db, txn, cit, title, url)
                        db.commit_citation(cit, txn)

                        target_citations = [cit]
                        sdid_to_cits[sdid].append(cit.handle)

                    all_created_media = []
                    for cit in target_citations:
                        self._set_attr_on_citation(cit, "FS Modified", fs_modified or "")
                        self._set_attr_on_citation(cit, "FS Contributor", contributor or "")
                        self._set_attr_on_citation(cit, "FS Kind", final_kind or "")

                        if image_paths:
                            created = self._attach_images_to_citation(cit, image_paths, txn)
                            all_created_media.extend(created)

                        db.commit_citation(cit, txn)

                    if add_to_person and all_created_media:
                        self._attach_media_to_person_by_handles(person, all_created_media, txn)

                    imported += 1

                except Exception as e:
                    errors.append(f"{sdid}: {e}")

            db.commit_person(person, txn)

        try:
            db.request_rebuild()
        except Exception:
            pass

        if target_handle:
            self._pin_active_person(target_handle, ms=15000, interval_ms=150)

            def _late_pin():
                try:
                    self._pin_active_person(target_handle, ms=15000, interval_ms=150)
                except Exception:
                    pass
                return False

            GLib.timeout_add(400, _late_pin)

        if errors:
            WarningDialog(
                _("Some sources could not be imported:\n{errs}").format(
                    errs="\n".join(errors[:10]) + (("\n...") if len(errors) > 10 else "")
                )
            )

        return imported

    # --------------------------
    # Media attachment helpers 
    # --------------------------

    def _attach_images_to_citation(self, cit: Citation, image_paths: List[str], txn: DbTxn) -> List[str]:
        created_handles: List[str] = []
        if not image_paths:
            return created_handles

        try:
            base = expand_media_path(self.dbstate.db.get_mediapath(), self.dbstate.db)
        except Exception:
            base = None

        src = None
        try:
            if getattr(cit, "source_handle", None):
                src = self.dbstate.db.get_source_from_handle(cit.source_handle)
        except Exception:
            src = None

        import os

        for p in image_paths:
            if not p:
                continue
            try:
                path_use = p
                if base:
                    try:
                        if os.path.commonprefix([os.path.abspath(p), os.path.abspath(base)]) == os.path.abspath(base):
                            try:
                                path_use = relative_path(p, base)
                            except Exception:
                                path_use = p
                    except Exception:
                        pass

                m = Media()
                m.set_path(path_use)
                try:
                    m.set_mime_type(get_type(p))
                except Exception:
                    pass

                try:
                    title = None
                    for nh in getattr(cit, "note_list", []) or []:
                        n = self.dbstate.db.get_note_from_handle(nh)
                        if n and n.type == NoteType.CITATION:
                            title = (n.get() or "").splitlines()[0][:120] if n.get() else None
                            break
                    if title:
                        m.set_description(title)
                except Exception:
                    pass

                self.dbstate.db.add_media(m, txn)
                self.dbstate.db.commit_media(m, txn)

                mr = MediaRef()
                mr.ref = m.handle

                attached = False
                try:
                    if hasattr(cit, "add_media_reference"):
                        cit.add_media_reference(mr)
                        attached = True
                    elif hasattr(cit, "add_media_ref"):
                        cit.add_media_ref(mr)
                        attached = True
                except Exception:
                    attached = False

                if not attached and src:
                    try:
                        if hasattr(src, "add_media_reference"):
                            src.add_media_reference(mr)
                            self.dbstate.db.commit_source(src, txn)
                            attached = True
                        elif hasattr(src, "add_media_ref"):
                            src.add_media_ref(mr)
                            self.dbstate.db.commit_source(src, txn)
                            attached = True
                    except Exception:
                        pass

                if not attached:
                    print("WARN: Could not attach media to citation or source; leaving Media unattached.")

                created_handles.append(m.handle)

            except Exception as e:
                print(f"Failed to attach media '{p}': {e}")

        return created_handles

    def _attach_media_to_person_by_handles(self, person, media_handles: List[str], txn: DbTxn):
        if not media_handles:
            return
        try:
            existing = {mr.ref for mr in person.get_media_list()}
        except Exception:
            existing = set()

        for mh in media_handles:
            if not mh or mh in existing:
                continue
            mr = MediaRef()
            mr.ref = mh
            try:
                if hasattr(person, "add_media_reference"):
                    person.add_media_reference(mr)
                elif hasattr(person, "add_media_ref"):
                    person.add_media_ref(mr)
            except Exception:
                continue

        self.dbstate.db.commit_person(person, txn)

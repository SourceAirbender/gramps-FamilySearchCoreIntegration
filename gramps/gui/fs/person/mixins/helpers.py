# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2024-2026  Gabriel Rios
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
import time
from typing import Dict, Optional, List
from typing import Any, TYPE_CHECKING

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.lib import Person
from gramps.gui.dialog import OkDialog, WarningDialog, ErrorDialog

from gramps.gen.fs import utilities as fs_utilities


from gramps.gen.fs.import_ import deserializer as deserialize

_ = glocale.translation.gettext

FS_DIRECT_TAGS = {
    "http://gedcomx.org/Birth",
    "http://gedcomx.org/Death",
    "http://gedcomx.org/Baptism",
    "http://gedcomx.org/Christening",
    "http://gedcomx.org/Burial",
    "http://gedcomx.org/Marriage",
    "http://gedcomx.org/Divorce",
}
FS_MENTION_ONLY = {"http://gedcomx.org/Name", "http://gedcomx.org/Gender"}


class HelpersMixin:
    """
    - this mixin is used with CacheMixin (provides _ensure_person_cached/_ensure_sources_cached)
    - and a host object that provides dbstate (Gramps DBState).
    """

    # provided by host at runtime
    dbstate: Any

    if TYPE_CHECKING:
        # provided by CacheMixin at runtime
        def _ensure_sources_cached(self, fsid: str) -> None: ...

        def _ensure_person_cached(
            self, fsid: str, *, with_relatives: bool, force: bool = False
        ) -> Any: ...

    def _pretty_tags(self, tags: List[str]) -> str:
        labs = []
        for t in tags:
            try:
                if t.startswith("http://gedcomx.org/"):
                    labs.append(t.split("/")[-1])
                else:
                    labs.append(t)
            except Exception:
                pass
        order = [
            "Birth",
            "Baptism",
            "Christening",
            "Marriage",
            "Divorce",
            "Death",
            "Burial",
            "Gender",
            "Name",
        ]
        labs = sorted(
            set(labs),
            key=lambda x: (order.index(x) if x in order else 99, x),
        )
        return ", ".join(labs)

    def _classify_simple(self, tags: List[str]) -> str:
        if not tags:
            return "Mention"
        if all(t in FS_MENTION_ONLY for t in tags):
            return "Mention"
        if any(t in FS_DIRECT_TAGS for t in tags):
            return "Direct"
        return "Direct"

    def _gather_sr_meta(self, fsid: str) -> Dict[str, dict]:
        self._ensure_sources_cached(fsid)
        fsP = deserialize.Person._index.get(fsid) or deserialize.Person()

        meta: Dict[str, dict] = {}

        def add(sr):
            sdid = getattr(sr, "descriptionId", None)
            if not sdid:
                return
            entry = meta.setdefault(
                sdid,
                {"tags": set(), "kind": "Mention", "contributor": "", "modified": ""},
            )
            try:
                for t in getattr(sr, "tags", []) or []:
                    val = getattr(t, "resource", None) or str(t)
                    if val:
                        entry["tags"].add(val)
            except Exception:
                pass
            try:
                attr = getattr(sr, "attribution", None)
                rid = (
                    getattr(getattr(attr, "contributor", None), "resourceId", "")
                    if attr
                    else ""
                )
                mod_ms = getattr(attr, "modified", None)
                mod_iso = ""
                if isinstance(mod_ms, (int, float)):
                    mod_iso = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(mod_ms / 1000.0)
                    )

                def _to_ts(s):
                    try:
                        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
                    except Exception:
                        return 0

                if _to_ts(mod_iso) >= _to_ts(entry["modified"]):
                    entry["contributor"] = rid or entry["contributor"]
                    entry["modified"] = mod_iso or entry["modified"]
            except Exception:
                pass

        for sr in getattr(fsP, "sources", []) or []:
            add(sr)
        for rel in getattr(fsP, "_spouses", []) or []:
            for sr in getattr(rel, "sources", []) or []:
                add(sr)

        for sdid, e in meta.items():
            tags = list(e["tags"])
            e["kind"] = self._classify_simple(tags)
            e["tags"] = tags
        return meta

    def _label_for_person_id(self, pid: str) -> str:
        try:
            self._ensure_person_cached(pid, with_relatives=False)
            p = deserialize.Person._index.get(pid)
            if p:
                nm = p.preferred_name()
                return f"{nm.akSurname()}, {nm.akGiven()} [{pid}]"
        except Exception:
            pass
        return f"[{pid}]"

    def _find_person_by_fsid(self, fsid: str) -> Optional[Person]:
        for h in self.dbstate.db.iter_person_handles():
            p = self.dbstate.db.get_person_from_handle(h)
            if fs_utilities.get_fsftid(p) == fsid:
                return p
        return None


class AuthMixin:
    @classmethod
    def ensure_session(cls, caller=None, verbosity=5) -> bool:
        """
        Ensure a shared session exists.
        Returns True if a session exists (logged-in or not).
        """
        try:
            from gramps.gui.fs.manager import get_session

            sess = get_session(
                getattr(caller, "dbstate", None) if caller else None,
                getattr(caller, "uistate", None) if caller else None,
            )
            if sess:
                try:
                    import gramps.gen.fs.tree as tree

                    tree._fs_session = sess
                except Exception:
                    pass
                return True
            return False
        except Exception:
            return False

    def _on_login(self, _btn):
        """
        Interactive login button handler (shows OAuth UI).
        """
        from gramps.gui.fs.manager import get_session

        sess = get_session(
            getattr(self, "dbstate", None), getattr(self, "uistate", None)
        )
        parent = getattr(self, "window", None) or getattr(
            getattr(self, "uistate", None), "window", None
        )

        if not sess:
            ErrorDialog(
                _("FamilySearch"),
                _("FamilySearch is not configured (missing app key / redirect)."),
                parent=parent,
            )
            return

        try:
            import gramps.gen.fs.tree as tree

            tree._fs_session = sess
        except Exception:
            pass

        try:
            if sess.logged:
                sess.probe_api(reason="ui-login")
                OkDialog(_("Already logged in to FamilySearch."))
            else:
                code = sess.authorize()
                sess.get_token(code)
                OkDialog(_("Logged in to FamilySearch."))
        except Exception as e:
            WarningDialog(_("Login failed:\n{err}").format(err=str(e)))

        try:
            self._refresh_status()
        except Exception:
            pass

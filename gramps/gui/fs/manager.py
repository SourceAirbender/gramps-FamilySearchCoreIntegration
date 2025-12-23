# gramps/gui/fs/manager.py
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

import os
import sys

from .session import Session, get_active_session

_SESSION = None


def _scan_for_session():
    candidates = []
    for mod in list(sys.modules.values()):
        if not mod:
            continue
        for name in ("GLOBAL_SESSION", "SESSION", "_SESSION", "session"):
            sess = getattr(mod, name, None)
            if sess and hasattr(sess, "access_token"):
                candidates.append(sess)
        cls = getattr(mod, "Session", None)
        if cls:
            for name in ("_shared", "_last_instance", "_singleton", "_instance"):
                sess = getattr(cls, name, None)
                if sess and hasattr(sess, "access_token"):
                    candidates.append(sess)

    if not candidates:
        return None

    for s in candidates:
        if getattr(s, "connected", False) or getattr(s, "access_token", None):
            return s
    return candidates[0]


def _discover_from_grampsgui():
    try:
        from gramps.gui import grampsgui
        return getattr(grampsgui, "dbstate", None), getattr(grampsgui, "uistate", None)
    except Exception:
        return None, None


def _best_effort_track(uistate):
    if not uistate:
        return None
    return getattr(uistate, "track", None) or getattr(uistate, "_track", None)


def _bind_session_context(sess, dbstate=None, uistate=None):
    if not sess:
        return

    if dbstate is None or uistate is None:
        gd, gu = _discover_from_grampsgui()
        dbstate = dbstate or gd
        uistate = uistate or gu

    fn = getattr(sess, "bind_context", None)
    if not callable(fn):
        return

    try:
        fn(dbstate=dbstate, uistate=uistate, track=_best_effort_track(uistate))
    except Exception:
        pass


def get_session(dbstate=None, uistate=None):
    global _SESSION

    sess = get_active_session() or _scan_for_session()
    if sess is not None:
        _SESSION = sess
        _bind_session_context(sess, dbstate=dbstate, uistate=uistate)
        return sess

    app_key = os.environ.get("GRAMPS_FS_APP_KEY", "").strip()
    redirect = os.environ.get("GRAMPS_FS_REDIRECT", "").strip()
    server = int(os.environ.get("GRAMPS_FS_SERVER", "0") or "0")

    if not app_key or not redirect:
        try:
            from gramps.gui.fs.person.mixins.constants import APP_KEY, REDIRECT
            app_key = app_key or (APP_KEY or "").strip()
            redirect = redirect or (REDIRECT or "").strip()
        except Exception:
            pass

    if not app_key or not redirect:
        return None

    sess = Session(server=server, app_key=app_key, redirect=redirect)
    _SESSION = sess
    _bind_session_context(sess, dbstate=dbstate, uistate=uistate)
    return sess

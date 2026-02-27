# -*- coding: utf-8 -*-
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

import gc
import os
import sys
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, cast

from gi.repository import Gtk, GLib, Gdk, GdkPixbuf

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.display.name import displayer as name_displayer
from gramps.gen.const import IMAGE_DIR as _GRAMPS_IMAGE_DIR

from . import ui as fs_ui
from .tags import build_tag_color_note_widget

try:
    _trans = glocale.get_addon_translator(__file__)
except Exception:
    _trans = glocale.translation
_ = _trans.gettext

from gramps.gui.dialog import ErrorDialog

_SINGLETON: Optional["FamilySearchToolsWindow"] = None
_EDITPERSON_HOOK_INSTALLED = False


def _dbg(msg: str) -> None:
    if os.environ.get("GRAMPS_FS_DEBUG", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        try:
            sys.stderr.write(f"[FS TOOLS] {msg}\n")
            sys.stderr.flush()
        except Exception:
            pass


def _try_error(parent: Gtk.Window, title: str, msg: str) -> None:
    # mypy
    try:
        ErrorDialog(title, msg, parent=parent)
        return
    except Exception:
        pass
    fs_ui.error_dialog(parent, title, msg)


def _try_info(parent: Gtk.Window, title: str, msg: str) -> None:
    fs_ui.info_dialog(parent, title, msg)


def _person_handle(person: Any) -> Optional[str]:
    try:
        h = person.get_handle()
        return h if isinstance(h, str) and h else None
    except Exception:
        pass
    h = getattr(person, "handle", None)
    return h if isinstance(h, str) and h else None


def _person_exists_in_db(dbstate: Any, handle: str) -> bool:
    try:
        db = getattr(dbstate, "db", None)
        if db is None:
            return False
        fn = getattr(db, "has_person_handle", None)
        if callable(fn):
            return bool(fn(handle))
        return db.get_person_from_handle(handle) is not None
    except Exception:
        return False


@dataclass
class _EditorCtx:
    dbstate: Any = None
    uistate: Any = None
    track: Any = None
    person_handle: Optional[str] = None
    person_obj_ref: Optional[weakref.ref] = None
    editor_ref: Optional[weakref.ref] = None

    def person_obj(self) -> Any:
        try:
            return self.person_obj_ref() if self.person_obj_ref else None
        except Exception:
            return None

    def editor_obj(self) -> Any:
        try:
            return self.editor_ref() if self.editor_ref else None
        except Exception:
            return None


_LAST_EDITOR = _EditorCtx()


def notify_from_person_editor(
    dbstate: Any, uistate: Any, track: Any, person: Any, editor: Any = None
) -> None:
    global _LAST_EDITOR

    ph = _person_handle(person)
    if not ph:
        return

    _LAST_EDITOR.dbstate = dbstate
    _LAST_EDITOR.uistate = uistate
    _LAST_EDITOR.track = track
    _LAST_EDITOR.person_handle = ph

    try:
        _LAST_EDITOR.person_obj_ref = weakref.ref(person)
    except Exception:
        _LAST_EDITOR.person_obj_ref = None

    try:
        _LAST_EDITOR.editor_ref = weakref.ref(editor) if editor is not None else None
    except Exception:
        _LAST_EDITOR.editor_ref = None

    if _SINGLETON is not None and _SINGLETON.is_alive():
        GLib.idle_add(_SINGLETON._on_editor_ctx_changed)

    _dbg(f"notify_from_person_editor: handle={ph}")


def _install_editperson_hook() -> None:
    """
    Patch EditPerson._post_init so we can detect which person editor is active.

    - intentionally treat EditPerson as Any because we're patching methods
    """
    global _EDITPERSON_HOOK_INSTALLED
    if _EDITPERSON_HOOK_INSTALLED:
        return

    try:
        from gramps.gui.editors.editperson import EditPerson
    except Exception as e:
        _dbg(f"EditPerson import failed (hook not installed yet): {e}")
        return

    EP = cast(Any, EditPerson)

    if getattr(EP, "_fs_tools_hooked", False):
        _EDITPERSON_HOOK_INSTALLED = True
        return

    orig_post_init_obj = getattr(EP, "_post_init", None)
    if not callable(orig_post_init_obj):
        _dbg("EditPerson._post_init not callable; cannot hook")
        return

    orig_post_init: Callable[..., Any] = cast(Callable[..., Any], orig_post_init_obj)

    def _fs_hook_attach(self: Any) -> None:
        if getattr(self, "_fs_tools_hook_attached", False):
            return
        setattr(self, "_fs_tools_hook_attached", True)

        def _fire() -> bool:
            try:
                notify_from_person_editor(
                    self.dbstate, self.uistate, self.track, self.obj, editor=self
                )
            except Exception as e:
                _dbg(f"notify failed: {e}")
            return False

        GLib.idle_add(_fire)

        win = getattr(self, "window", None)
        if win is None:
            top = getattr(self, "top", None)
            win = getattr(top, "toplevel", None) if top is not None else None

        if win is not None and hasattr(win, "connect"):
            try:
                win.connect("focus-in-event", lambda *_a: _fire())
            except Exception:
                pass
            try:
                win.connect("map-event", lambda *_a: _fire())
            except Exception:
                pass

    def wrapped_post_init(self: Any, *args: Any, **kwargs: Any) -> Any:
        rv = orig_post_init(self, *args, **kwargs)
        try:
            _fs_hook_attach(self)
        except Exception as e:
            _dbg(f"hook attach failed: {e}")
        return rv

    # mypy Cannot assign to a method
    setattr(EP, "_post_init", wrapped_post_init)
    setattr(EP, "_fs_tools_hooked", True)
    _EDITPERSON_HOOK_INSTALLED = True
    _dbg("Installed EditPerson hook for FS Tools")


def _find_open_editperson_instance() -> Any:
    try:
        from gramps.gui.editors.editperson import EditPerson
    except Exception:
        return None

    active_win = None
    try:
        app = Gtk.Application.get_default()
        if app is not None and hasattr(app, "get_active_window"):
            active_win = app.get_active_window()
    except Exception:
        active_win = None

    any_visible = None

    try:
        for obj in gc.get_objects():
            try:
                if not isinstance(obj, EditPerson):
                    continue
            except Exception:
                continue

            win = getattr(obj, "window", None)
            if win is None:
                top = getattr(obj, "top", None)
                win = getattr(top, "toplevel", None) if top is not None else None

            if win is None:
                continue

            try:
                if not win.get_visible():
                    continue
            except Exception:
                pass

            if active_win is not None and win is active_win:
                return obj

            any_visible = obj
    except Exception:
        pass

    return any_visible


def close_tools_window() -> None:
    global _SINGLETON
    if _SINGLETON is None:
        return
    try:
        if _SINGLETON.is_alive():
            _SINGLETON.window.destroy()
    except Exception:
        pass
    _SINGLETON = None


def toggle_tools_window(session: Any, dbstate: Any = None, uistate: Any = None) -> None:
    global _SINGLETON
    _install_editperson_hook()

    if _SINGLETON is not None and _SINGLETON.is_alive():
        close_tools_window()
        return

    _SINGLETON = FamilySearchToolsWindow(session=session)
    _SINGLETON.present()


def present_tools_window(
    session: Any, dbstate: Any = None, uistate: Any = None
) -> None:
    global _SINGLETON
    _install_editperson_hook()

    if _SINGLETON is not None and _SINGLETON.is_alive():
        _SINGLETON.present()
        return

    _SINGLETON = FamilySearchToolsWindow(session=session)
    _SINGLETON.present()


class FamilySearchToolsWindow:
    _BANNER_MAX_HEIGHT = 120  # px
    _BANNER_MIN_HEIGHT = 64
    _BANNER_SIDE_PAD = 10

    def __init__(self, session: Any):
        self.session = session
        self._tick_id: Optional[int] = None

        self._logo_pixbuf_orig: Optional[GdkPixbuf.Pixbuf] = None
        self._logo_last_width: int = 0
        self._logo_image: Optional[Gtk.Image] = None

        self.window = Gtk.Window(title=_("FamilySearch Tools"))
        self.window.set_default_size(820, 430)
        self.window.set_border_width(12)
        self.window.get_style_context().add_class("fs-tools-window")

        self._install_css()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.window.add(outer)

        banner = self._build_banner()
        if banner is not None:
            outer.pack_start(banner, False, False, 0)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_row.get_style_context().add_class("fs-status-row")
        outer.pack_start(status_row, False, False, 0)

        try:
            status_widget = self.session.get_status_widget()
        except Exception:
            status_widget = None

        if status_widget is not None:
            try:
                status_widget.set_halign(Gtk.Align.START)
            except Exception:
                pass
            status_row.pack_start(status_widget, False, False, 0)

        self.active_label = Gtk.Label(label=_("Editor person: (none)"))
        self.active_label.set_xalign(0.0)
        try:
            self.active_label.set_ellipsize(3)
        except Exception:
            pass
        self.active_label.get_style_context().add_class("fs-active-label")
        status_row.pack_start(self.active_label, True, True, 0)

        outer.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
        )

        self._size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.BOTH)

        sec_person, box_person = self._make_section(
            _("Person actions"), "fs-sec-person"
        )
        outer.pack_start(sec_person, False, False, 0)

        self.btn_link = Gtk.Button(label=_("Link FamilySearch ID"))
        self.btn_cmp = Gtk.Button(label=_("Compare"))

        self.btn_sync = Gtk.Button(label=_("Sync from FamilySearch"))
        self.btn_sync.get_style_context().add_class("suggested-action")

        self.btn_sync_to = Gtk.Button(label=_("Sync to FamilySearch..."))
        try:
            self.btn_sync_to.set_tooltip_text(
                _(
                    "Overwrite selected FamilySearch fields with Gramps values (no deletes)."
                )
            )
        except Exception:
            pass

        self._add_btn(box_person, self.btn_link)
        self._add_btn(box_person, self.btn_cmp)
        self._add_btn(box_person, self.btn_sync)
        self._add_btn(box_person, self.btn_sync_to)

        sec_import, box_import = self._make_section(
            _("Import relatives"), "fs-sec-import"
        )
        outer.pack_start(sec_import, False, False, 0)

        self.btn_imp_par = Gtk.Button(label=_("Import Parents"))
        self.btn_imp_spo = Gtk.Button(label=_("Import Spouse"))
        self.btn_imp_chi = Gtk.Button(label=_("Import Children"))

        self._add_btn(box_import, self.btn_imp_par)
        self._add_btn(box_import, self.btn_imp_spo)
        self._add_btn(box_import, self.btn_imp_chi)

        sec_util, box_util = self._make_section(_("Utilities"), "fs-sec-util")
        outer.pack_start(sec_util, False, False, 0)

        self.btn_tags = Gtk.Button(label=_("Tags..."))
        self.btn_clear_cache = Gtk.Button(label=_("Clear Cache"))
        self.btn_clear_cache.get_style_context().add_class("destructive-action")

        self._add_btn(box_util, self.btn_tags)
        self._add_btn(box_util, self.btn_clear_cache)

        try:
            note = build_tag_color_note_widget()
            try:
                note.set_margin_top(6)
            except Exception:
                pass

            util_inner = sec_util.get_child()  # Gtk.EventBox -> Gtk.Box
            if isinstance(util_inner, Gtk.Box):
                util_inner.pack_start(note, False, False, 0)
        except Exception as e:
            _dbg(f"Tag color note add failed: {e}")

        self.btn_link.connect("clicked", self._on_link)
        self.btn_cmp.connect("clicked", self._on_compare)
        self.btn_sync.connect("clicked", self._on_sync)
        self.btn_sync_to.connect("clicked", self._on_sync_to)

        self.btn_imp_par.connect("clicked", self._on_import_parents)
        self.btn_imp_spo.connect("clicked", self._on_import_spouse)
        self.btn_imp_chi.connect("clicked", self._on_import_children)

        self.btn_tags.connect("clicked", self._on_tags)
        self.btn_clear_cache.connect("clicked", self._on_clear_cache)

        self.window.connect("destroy", self._on_destroy)
        self.window.show_all()

        try:
            ep = _find_open_editperson_instance()
            if ep is not None:
                notify_from_person_editor(
                    ep.dbstate, ep.uistate, ep.track, ep.obj, editor=ep
                )
        except Exception:
            pass

        self._tick_id = GLib.timeout_add_seconds(1, self._tick)
        self._tick()

    # ---- Styling / layout helpers --------

    def _install_css(self) -> None:
        css = b"""
        .fs-tools-window { }

        .fs-banner {
            border-radius: 10px;
            border: 1px solid rgba(0,0,0,0.08);
            background-color: rgba(0,0,0,0.03);
        }

        .fs-status-row { padding: 2px; }

        .fs-active-label {
            opacity: 0.92;
            font-weight: 600;
        }

        .fs-section {
            border-radius: 12px;
            border: 1px solid rgba(0,0,0,0.10);
        }

        .fs-section-title {
            font-weight: 700;
            letter-spacing: 0.2px;
        }

        .fs-sec-person {
            background-color: rgba(0, 120, 170, 0.10);
            border-color: rgba(0, 120, 170, 0.22);
        }

        .fs-sec-import {
            background-color: rgba(46, 125, 50, 0.10);
            border-color: rgba(46, 125, 50, 0.22);
        }

        .fs-sec-util {
            background-color: rgba(80, 80, 80, 0.06);
            border-color: rgba(0, 0, 0, 0.16);
        }
        """
        fs_ui.install_css_once("fs.tools_window", css)

    def _make_section(
        self, title: str, css_class: str
    ) -> Tuple[Gtk.Widget, Gtk.FlowBox]:
        wrapper = Gtk.EventBox()
        wrapper.set_visible_window(True)
        sc = wrapper.get_style_context()
        sc.add_class("fs-section")
        sc.add_class(css_class)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_border_width(10)
        wrapper.add(inner)

        lbl = Gtk.Label()
        try:
            esc = GLib.markup_escape_text(title)
        except Exception:
            esc = title
        lbl.set_markup(f"<span size='large'><b>{esc}</b></span>")
        lbl.set_xalign(0.0)
        lbl.get_style_context().add_class("fs-section-title")
        inner.pack_start(lbl, False, False, 0)

        inner.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
        )

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_row_spacing(8)
        flow.set_column_spacing(8)
        flow.set_max_children_per_line(6)
        flow.set_min_children_per_line(2)
        inner.pack_start(flow, False, False, 0)

        return wrapper, flow

    def _add_btn(self, flow: Gtk.FlowBox, btn: Gtk.Button) -> None:
        try:
            btn.set_can_focus(True)
        except Exception:
            pass
        try:
            self._size_group.add_widget(btn)
        except Exception:
            pass

        child = Gtk.FlowBoxChild()
        child.add(btn)
        flow.add(child)

    def _build_banner(self) -> Optional[Gtk.Widget]:
        pix = self._load_logo_pixbuf()
        if pix is None:
            _dbg("FS logo not found; banner disabled")
            return None

        self._logo_pixbuf_orig = pix

        wrap = Gtk.EventBox()
        wrap.set_visible_window(True)
        wrap.get_style_context().add_class("fs-banner")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_border_width(self._BANNER_SIDE_PAD)
        wrap.add(box)

        self._logo_image = Gtk.Image()
        self._logo_image.set_halign(Gtk.Align.FILL)
        self._logo_image.set_valign(Gtk.Align.CENTER)
        box.pack_start(self._logo_image, True, True, 0)

        try:
            w = self.window.get_size()[0]
        except Exception:
            w = 820
        self._set_logo_width(w)

        wrap.connect("size-allocate", self._on_banner_size_allocate)
        return wrap

    def _load_logo_pixbuf(self) -> Optional[GdkPixbuf.Pixbuf]:
        candidates: list[str] = []

        try:
            if _GRAMPS_IMAGE_DIR:
                candidates.append(os.path.join(_GRAMPS_IMAGE_DIR, "fs_logo.png"))
        except Exception:
            pass

        try:
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
            candidates.append(os.path.join(repo_root, "images", "fs_logo.png"))
        except Exception:
            pass

        try:
            candidates.append(os.path.join(os.path.dirname(__file__), "fs_logo.png"))
        except Exception:
            pass

        for path in candidates:
            try:
                if path and os.path.exists(path) and os.path.isfile(path):
                    _dbg(f"Loading FS logo: {path}")
                    return GdkPixbuf.Pixbuf.new_from_file(path)
            except Exception as e:
                _dbg(f"Logo load failed ({path}): {e}")

        return None

    def _on_banner_size_allocate(self, _widget: Any, allocation: Any) -> None:
        try:
            w = int(getattr(allocation, "width", 0))
        except Exception:
            return
        if w <= 0:
            return
        if abs(w - self._logo_last_width) < 12:
            return
        self._logo_last_width = w
        self._set_logo_width(w)

    def _set_logo_width(self, container_width: int) -> None:
        if self._logo_pixbuf_orig is None or self._logo_image is None:
            return
        try:
            avail_w = max(1, int(container_width) - (self._BANNER_SIDE_PAD * 2) - 24)
            ow = self._logo_pixbuf_orig.get_width()
            oh = self._logo_pixbuf_orig.get_height()
            if ow <= 0 or oh <= 0:
                return

            scale = avail_w / float(ow)
            nh = int(oh * scale)
            nw = int(ow * scale)

            if nh > self._BANNER_MAX_HEIGHT:
                nh = self._BANNER_MAX_HEIGHT
                scale = nh / float(oh)
                nw = max(1, int(ow * scale))

            if nh < self._BANNER_MIN_HEIGHT:
                nh = self._BANNER_MIN_HEIGHT
                scale = nh / float(oh)
                nw = max(1, int(ow * scale))

            scaled = self._logo_pixbuf_orig.scale_simple(
                nw, nh, GdkPixbuf.InterpType.BILINEAR
            )
            self._logo_image.set_from_pixbuf(scaled)
        except Exception as e:
            _dbg(f"Logo scale failed: {e}")

    def is_alive(self) -> bool:
        try:
            if self.window is None:
                return False
            _ = self.window.get_visible()
            return True
        except Exception:
            return False

    def present(self) -> None:
        try:
            self.window.present()
        except Exception:
            try:
                self.window.show_all()
            except Exception:
                pass

    def _on_destroy(self, *_args: Any) -> None:
        global _SINGLETON
        _SINGLETON = None
        try:
            if self._tick_id is not None:
                GLib.source_remove(self._tick_id)
                self._tick_id = None
        except Exception:
            pass

    def _fs_connected(self) -> bool:
        try:
            return bool(getattr(self.session, "connected", False)) or bool(
                getattr(self.session, "access_token", None)
            )
        except Exception:
            return False

    def _editor_person_obj(self) -> Any:
        p = _LAST_EDITOR.person_obj()
        if p is not None:
            return p

        if not _LAST_EDITOR.person_handle:
            return None
        dbstate = _LAST_EDITOR.dbstate
        if dbstate is None:
            return None
        db = getattr(dbstate, "db", None)
        if db is None:
            return None
        try:
            return db.get_person_from_handle(_LAST_EDITOR.person_handle)
        except Exception:
            return None

    def _update_label(self) -> None:
        p = self._editor_person_obj()
        if p is None:
            self.active_label.set_text(_("Editor person: (none)"))
            return

        try:
            nm = name_displayer.display(p)
        except Exception:
            nm = "(person)"
        try:
            gid = p.get_gramps_id() or ""
        except Exception:
            gid = ""

        if gid:
            self.active_label.set_text(
                _("Editor person: %(name)s  [%(gid)s]") % {"name": nm, "gid": gid}
            )
        else:
            self.active_label.set_text(_("Editor person: %(name)s") % {"name": nm})

    def _tick(self, *_args: Any) -> bool:
        _install_editperson_hook()

        connected = self._fs_connected()

        ph = _LAST_EDITOR.person_handle
        db_ok = False
        if ph and _LAST_EDITOR.dbstate is not None:
            db_ok = _person_exists_in_db(_LAST_EDITOR.dbstate, ph)

        have_person_ctx = bool(connected and ph and db_ok)

        self._update_label()

        for b in (
            self.btn_link,
            self.btn_cmp,
            self.btn_sync,
            self.btn_sync_to,
            self.btn_imp_par,
            self.btn_imp_spo,
            self.btn_imp_chi,
        ):
            try:
                b.set_sensitive(have_person_ctx)
            except Exception:
                pass

        try:
            self.btn_tags.set_sensitive(
                bool(connected and _LAST_EDITOR.dbstate is not None)
            )
        except Exception:
            pass
        try:
            self.btn_clear_cache.set_sensitive(bool(connected))
        except Exception:
            pass

        return True

    def _on_editor_ctx_changed(self) -> bool:
        self._tick()
        return False

    def _require_ready(self) -> Any:
        if not self._fs_connected():
            _try_info(self.window, "FamilySearch", "Not connected to FamilySearch.")
            return None

        ph = _LAST_EDITOR.person_handle
        if not ph or _LAST_EDITOR.dbstate is None:
            _try_info(
                self.window,
                "FamilySearch",
                "No Edit Person window context yet.\nOpen an Edit Person window first.",
            )
            return None

        if not _person_exists_in_db(_LAST_EDITOR.dbstate, ph):
            _try_info(
                self.window,
                "FamilySearch",
                "This person is not saved in the database yet.\nSave/OK the person first.",
            )
            return None

        p = self._editor_person_obj()
        if p is None:
            _try_info(
                self.window,
                "FamilySearch",
                "Could not resolve the editor person from the database.",
            )
            return None

        return p

    def _ctx(self) -> Optional[dict[str, Any]]:
        p = self._require_ready()
        if p is None:
            return None

        return {
            "dbstate": _LAST_EDITOR.dbstate,
            "uistate": _LAST_EDITOR.uistate,
            "track": _LAST_EDITOR.track,
            "person": p,
            "editor": _LAST_EDITOR.editor_obj(),
            "parent": self.window,
            "session": self.session,
        }

    def _ctx_db_only(self) -> Optional[dict[str, Any]]:
        if not self._fs_connected():
            _try_info(self.window, "FamilySearch", "Not connected to FamilySearch.")
            return None
        if _LAST_EDITOR.dbstate is None or _LAST_EDITOR.uistate is None:
            _try_info(
                self.window,
                "FamilySearch",
                "No UI context yet.\nOpen an Edit Person window first.",
            )
            return None
        return {
            "dbstate": _LAST_EDITOR.dbstate,
            "uistate": _LAST_EDITOR.uistate,
            "track": _LAST_EDITOR.track,
            "person": self._editor_person_obj(),
            "editor": _LAST_EDITOR.editor_obj(),
            "parent": self.window,
            "session": self.session,
        }

    def _on_link(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            actions.link_familysearch_id(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Link failed: {e}")

    def _on_compare(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            actions.compare_person(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Compare failed: {e}")

    def _on_sync(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            fn = getattr(actions, "sync_from_familysearch", None)
            if not callable(fn):
                fn = getattr(actions, "sync_this_person", None)

            if not callable(fn):
                raise AttributeError(
                    "No pull-sync function found in actions.py (expected sync_from_familysearch or sync_this_person)"
                )

            fn(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Sync failed: {e}")

    def _on_sync_to(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            fn = getattr(actions, "sync_to_familysearch", None)

            if callable(fn):
                fn(
                    ctx["dbstate"],
                    ctx["uistate"],
                    ctx["track"],
                    ctx["person"],
                    ctx["session"],
                    ctx["parent"],
                    editor=ctx["editor"],
                )
                return

            from . import sync_directions as fs_syncdir

            fs_syncdir.sync_to_familysearch(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Sync to FamilySearch failed: {e}")

    def _on_import_parents(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            actions.import_parents(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Import parents failed: {e}")

    def _on_import_spouse(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            actions.import_spouse(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Import spouse failed: {e}")

    def _on_import_children(self, *_args: Any) -> None:
        ctx = self._ctx()
        if not ctx:
            return
        try:
            from . import actions

            actions.import_children(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Import children failed: {e}")

    def _on_tags(self, *_args: Any) -> None:
        ctx = self._ctx_db_only()
        if not ctx:
            return
        try:
            from . import actions

            actions.tags_dialog(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Tags failed: {e}")

    def _on_clear_cache(self, *_args: Any) -> None:
        ctx = self._ctx_db_only()
        if not ctx:
            return
        try:
            from . import actions

            actions.clear_cache(
                ctx["dbstate"],
                ctx["uistate"],
                ctx["track"],
                ctx["person"],
                ctx["session"],
                ctx["parent"],
                editor=ctx["editor"],
            )
        except Exception as e:
            _try_error(self.window, "FamilySearch", f"Clear cache failed: {e}")

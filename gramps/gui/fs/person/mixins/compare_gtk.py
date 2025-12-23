#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2023, 2024, 2025  Gabriel Rios
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

# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional

from gi.repository import Gtk, Gdk, GLib

from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gui.dialog import WarningDialog
from gramps.gui.listmodel import ListModel, NOSORT, COLOR, TOGGLE
from gramps.gen.lib import Person

import gramps.gui.fs.utilities as fs_utilities
import gramps.gui.fs.compare as fs_compare
import gramps.gui.fs.import_ as fs_import
from gramps.gui.fs import tags as fs_tags
from gramps.gui.fs.import_ import deserializer as deserialize

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext


class CompareGtkMixin:
    """
    GTK compare window (per-person) - refreshed aesthetics:
      - HeaderBar + legend
      - Scrolled tabs
      - Better table styling (grid lines, padding, ellipsize/wrap)
      - Removes ASCII ruler separators from UI
      - FamilySearch-ish palette via soft tints
    """

    _UI = {
        # semantic -> display background tint
        "green":  "#D8F3DC",  # match (mint)
        "red":    "#FFE3E3",  # critical mismatch
        "orange": "#FFE8CC",  # warning mismatch
        "yellow": "#FFF3BF",  # only in Gramps
        "yellow3":"#D0EBFF",  # only in FamilySearch (blue tint)
        "white":  "#F8F9FA",  # neutral / header
        "gray":   "#E9ECEF",
    }

    _CSS_INSTALLED = False

    def _ui_color(self, semantic: str) -> str:
        return self._UI.get((semantic or "").strip(), semantic or "")

    def _ui_row(self, row):
        if not row:
            return row
        try:
            r = list(row)
            r[0] = self._ui_color(r[0])
            return r
        except Exception:
            return row

    def _install_compare_css(self) -> None:
        if getattr(self.__class__, "_CSS_INSTALLED", False):
            return

        css = b"""
        .fs-compare-window {
            /* local-only class; keep theme-friendly */
        }

        .fs-compare-wrap {
            padding: 10px;
        }

        .fs-compare-legend {
            padding: 6px 8px;
            border-radius: 10px;
            border: 1px solid rgba(0,0,0,0.10);
            background-color: rgba(0,0,0,0.03);
        }

        .fs-legend-pill {
            border-radius: 999px;
            padding: 2px 10px;
            border: 1px solid rgba(0,0,0,0.10);
        }

        .fs-legend-label {
            font-weight: 600;
            opacity: 0.92;
        }

        .fs-compare-notebook {
            border-radius: 12px;
        }

        /* TreeView polish */
        .fs-compare-treeview {
            border-radius: 10px;
        }

        /* Give headers a little presence (theme still wins) */
        .fs-compare-treeview header button {
            font-weight: 700;
        }
        """

        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(css)
            screen = Gdk.Screen.get_default()
            if screen is not None:
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            self.__class__._CSS_INSTALLED = True
        except Exception:
            pass

    def _wrap_scroller(self, child: Gtk.Widget, min_h: int = 420) -> Gtk.Widget:
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        try:
            sw.set_min_content_height(min_h)
        except Exception:
            pass
        sw.add(child)
        return sw

    def _tune_treeview(self, tv: Gtk.TreeView, kind: str) -> None:
        """
        kind in {"overview","notes","sources"}: tune column widths + renderer behavior.
        """
        try:
            tv.set_headers_visible(True)
        except Exception:
            pass

        try:
            tv.set_grid_lines(Gtk.TreeViewGridLines.BOTH)
        except Exception:
            pass

        try:
            tv.set_rules_hint(True)
        except Exception:
            pass

        cols = tv.get_columns() or []
        for i, col in enumerate(cols):
            try:
                col.set_resizable(True)
                col.set_reorderable(True)
            except Exception:
                pass

            try:
                if kind == "overview" and i in (3, 5):
                    col.set_expand(True)
                if kind == "notes" and i in (3, 5):
                    col.set_expand(True)
                if kind == "sources" and i in (3, 4, 6, 7, 8):
                    col.set_expand(True)
            except Exception:
                pass

            # Renderer tuning (ellipsize + optional wrap)
            try:
                renderers = col.get_cells() or []
            except Exception:
                renderers = []

            for r in renderers:
                # Only affects text renderers; ignore toggles/etc
                if not isinstance(r, Gtk.CellRendererText):
                    continue

                try:
                    # 3 == Pango.EllipsizeMode.END
                    r.set_property("ellipsize", 3)
                except Exception:
                    pass

                # Wrap the big text columns a bit to reduce horizontal scrolling
                try:
                    if kind == "overview" and i in (3, 5):
                        # 2 == Pango.WrapMode.WORD_CHAR
                        r.set_property("wrap-mode", 2)
                        r.set_property("wrap-width", 520)
                    elif kind == "notes" and i in (3, 5):
                        r.set_property("wrap-mode", 2)
                        r.set_property("wrap-width", 520)
                    elif kind == "sources" and i in (4, 7):  # URL columns
                        # make URLs look link-ish
                        r.set_property("underline", 1)  # Pango.Underline.SINGLE
                        r.set_property("foreground", "steelblue")
                    elif kind == "sources" and i in (3, 6):  # titles
                        r.set_property("wrap-mode", 2)
                        r.set_property("wrap-width", 420)
                except Exception:
                    pass

    def _looks_like_color(self, v: Any) -> bool:
        try:
            if v is None:
                return False
            if isinstance(v, str):
                s = v.strip()
            else:
                s = str(v).strip()
            if not s:
                return False
            if s.startswith("#") and len(s) in (4, 7, 9):
                return True
            if s in self._UI:
                return True
            return False
        except Exception:
            return False

    def _guess_color_model_col(self, model: Gtk.TreeModel) -> int:
        """
        Tries to locate the model column index that contains the color token.
        We do this once per TreeView so we do not guess wrong (ListModel internals vary).
        """
        try:
            n = model.get_n_columns()
        except Exception:
            return 0

        def scan_iter(it) -> Optional[int]:
            if it is None:
                return None
            for ci in range(n):
                try:
                    v = model.get_value(it, ci)
                except Exception:
                    continue
                if self._looks_like_color(v):
                    return ci
            return None

        try:
            it = model.get_iter_first()
        except Exception:
            it = None

        found = scan_iter(it)
        if found is not None:
            return found

        # If first row is a section header with blank color, try first child
        try:
            if it is not None and model.iter_has_child(it):
                child = model.iter_children(it)
                found = scan_iter(child)
                if found is not None:
                    return found
        except Exception:
            pass

        return 0

    def _install_treeview_row_tints(self, tv: Gtk.TreeView) -> None:
        """
        Force row tinting via cell_data_func so it works even if ListModel COLOR
        is not painting anything by itself.
        """
        try:
            model = tv.get_model()
        except Exception:
            model = None
        if model is None:
            return

        color_col = self._guess_color_model_col(model)

        def make_func(is_indicator_col: bool):
            def _func(column, cell, model2, it, _data):
                try:
                    token = model2.get_value(it, color_col)
                except Exception:
                    token = None

                # Normalize to a color string
                try:
                    if token is None:
                        s = ""
                    elif isinstance(token, str):
                        s = token.strip()
                    else:
                        s = str(token).strip()
                except Exception:
                    s = ""

                if not s:
                    try:
                        cell.set_property("cell-background-set", False)
                    except Exception:
                        pass
                    return

                s2 = self._UI.get(s, s)

                # Try RGBA (best), then fall back to string background.
                rgba = None
                try:
                    rgba = Gdk.RGBA()
                    ok = rgba.parse(s2)
                    if not ok:
                        rgba = None
                except Exception:
                    rgba = None

                painted = False
                if rgba is not None:
                    try:
                        cell.set_property("cell-background-rgba", rgba)
                        cell.set_property("cell-background-set", True)
                        painted = True
                    except Exception:
                        painted = False

                if not painted:
                    try:
                        cell.set_property("cell-background", s2)
                        cell.set_property("cell-background-set", True)
                    except Exception:
                        pass

                # indicator column: keep text empty so it looks like a color bar
                if is_indicator_col and isinstance(cell, Gtk.CellRendererText):
                    try:
                        cell.set_property("text", "")
                    except Exception:
                        pass

            return _func

        cols = tv.get_columns() or []
        for idx, col in enumerate(cols):
            is_indicator = (idx == 0)
            try:
                cells = col.get_cells() or []
            except Exception:
                cells = []
            for cell in cells:
                if not isinstance(cell, Gtk.CellRendererText):
                    continue
                try:
                    col.set_cell_data_func(cell, make_func(is_indicator), None)
                except Exception:
                    pass

        try:
            if cols:
                cols[0].set_sizing(Gtk.TreeViewColumnSizing.FIXED)
                cols[0].set_fixed_width(18)
        except Exception:
            pass

    def _build_legend(self) -> Gtk.Widget:
        wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        wrap.get_style_context().add_class("fs-compare-legend")

        def pill(bg_hex: str, text: str) -> Gtk.Widget:
            eb = Gtk.EventBox()
            eb.set_visible_window(True)
            eb.get_style_context().add_class("fs-legend-pill")

            try:
                rgba = Gdk.RGBA()
                rgba.parse(bg_hex)
                eb.override_background_color(Gtk.StateFlags.NORMAL, rgba)
            except Exception:
                pass

            lbl = Gtk.Label(label=text)
            lbl.get_style_context().add_class("fs-legend-label")
            eb.add(lbl)
            return eb

        wrap.pack_start(pill(self._UI["green"],  _("Match")), False, False, 0)
        wrap.pack_start(pill(self._UI["orange"], _("Different")), False, False, 0)
        wrap.pack_start(pill(self._UI["yellow"], _("Only in Gramps")), False, False, 0)
        wrap.pack_start(pill(self._UI["yellow3"], _("Only in FamilySearch")), False, False, 0)
        wrap.pack_start(pill(self._UI["red"], _("Critical mismatch")), False, False, 0)

        hint = Gtk.Label(
            label=_("Tip: resize columns by dragging headers | scroll inside tabs | refresh to re-check FamilySearch")
        )
        hint.set_xalign(1.0)
        try:
            hint.set_ellipsize(3)
        except Exception:
            pass
        wrap.pack_end(hint, True, True, 0)

        return wrap

    def _on_compare(self, _btn):
        active = self.get_active("Person")
        if not active:
            WarningDialog(_("Select a person first."))
            return

        from gramps.gui.fs import tree

        if not (tree._fs_session and tree._fs_session.logged):
            WarningDialog(_("You must login first."))
            return

        # active may be a handle or a Person instance depending on caller
        gr_handle = getattr(active, "handle", None) or active

        def _get_gr() -> Optional[Person]:
            try:
                return self.dbstate.db.get_person_from_handle(gr_handle)
            except Exception:
                if isinstance(active, Person):
                    return active
                return None

        gr = _get_gr()
        if not gr:
            WarningDialog(_("Could not resolve the selected person in the database."))
            return

        fsid = fs_utilities.get_fsftid(gr)
        if not fsid:
            WarningDialog(_("This Gramps person is not linked to FamilySearch yet. Use 'Link person'."))
            return

        self._ensure_person_cached(fsid, with_relatives=True)

        # ---- Window ----
        self._install_compare_css()

        win = Gtk.Window()
        win.set_title(_("FamilySearch Compare"))
        win.set_transient_for(self.uistate.window)
        win.set_default_size(1180, 740)
        win.get_style_context().add_class("fs-compare-window")

        # headerbar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = _("FamilySearch Compare")
        try:
            gid = gr.get_gramps_id() or ""
        except Exception:
            gid = ""
        subtitle = ("%s  <->  %s" % (gid, fsid)) if gid else fsid
        hb.props.subtitle = subtitle
        win.set_titlebar(hb)

        # Action buttons
        btn_refresh = Gtk.Button()
        btn_refresh.set_tooltip_text(_("Refresh from FamilySearch"))
        btn_refresh.add(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(btn_refresh)

        btn_import_sources = Gtk.Button()
        btn_import_sources.set_tooltip_text(_("Import sources..."))
        btn_import_sources.add(Gtk.Image.new_from_icon_name("document-import-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(btn_import_sources)

        btn_close = Gtk.Button()
        btn_close.set_tooltip_text(_("Close"))
        btn_close.add(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(btn_close)

        # ---- Main layout ---
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.get_style_context().add_class("fs-compare-wrap")
        win.add(outer)

        notebook = Gtk.Notebook()
        notebook.get_style_context().add_class("fs-compare-notebook")
        outer.pack_start(notebook, True, True, 0)

        # overview tab
        tv_overview, model_overview = self._make_overview_tree_model()
        tv_overview.get_style_context().add_class("fs-compare-treeview")
        self._tune_treeview(tv_overview, "overview")
        self._install_treeview_row_tints(tv_overview)
        notebook.append_page(self._wrap_scroller(tv_overview), Gtk.Label(label=_("Overview")))

        # notes tab
        tv_notes, model_notes = self._make_notes_tree()
        tv_notes.get_style_context().add_class("fs-compare-treeview")
        self._tune_treeview(tv_notes, "notes")
        self._install_treeview_row_tints(tv_notes)
        notebook.append_page(self._wrap_scroller(tv_notes), Gtk.Label(label=_("Notes")))

        # sources tab
        tv_sources, model_sources = self._make_sources_tree()
        tv_sources.get_style_context().add_class("fs-compare-treeview")
        try:
            tv_sources.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        except Exception:
            pass
        self._tune_treeview(tv_sources, "sources")
        self._install_treeview_row_tints(tv_sources)
        notebook.append_page(self._wrap_scroller(tv_sources), Gtk.Label(label=_("Sources")))

        # key/legend row
        outer.pack_end(self._build_legend(), False, False, 0)

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_halign(Gtk.Align.END)

        btn_refresh_text = Gtk.Button(label=_("Refresh"))
        btn_import_sources_text = Gtk.Button(label=_("Import sources..."))
        btn_close_text = Gtk.Button(label=_("Close"))

        action_box.pack_end(btn_close_text, False, False, 0)
        action_box.pack_end(btn_import_sources_text, False, False, 0)
        action_box.pack_end(btn_refresh_text, False, False, 0)

        outer.pack_end(action_box, False, False, 0)

        def do_fill_all(force: bool = False):
            if force:
                self._ensure_person_cached(fsid, with_relatives=True, force=True)

            gr_local = _get_gr()
            if not gr_local:
                return

            model_overview.clear()
            self._fill_overview(model_overview, gr_local, fsid)

            model_notes.clear()
            self._fill_notes(model_notes, gr_local, fsid)

            model_sources.clear()
            self._fill_sources(model_sources, gr_local, fsid)

            # auto-tag after each refresh/fill
            try:
                payload = self._build_compare_json(gr_local, fsid)
                is_synced = fs_tags.compute_sync_from_payload(payload)
                fs_tags.set_sync_status_for_person(self.dbstate.db, gr_local, is_synced=is_synced)
            except Exception:
                pass

        def do_import_sources(_btn):
            self._import_sources_dialog(_get_gr(), fsid)
            model_sources.clear()
            self._fill_sources(model_sources, _get_gr(), fsid)

        btn_refresh.connect("clicked", lambda *_: do_fill_all(True))
        btn_refresh_text.connect("clicked", lambda *_: do_fill_all(True))

        btn_import_sources.connect("clicked", do_import_sources)
        btn_import_sources_text.connect("clicked", do_import_sources)

        btn_close.connect("clicked", lambda *_: win.destroy())
        btn_close_text.connect("clicked", lambda *_: win.destroy())

        do_fill_all(False)

        # auto-tag on initial open
        try:
            gr_local = _get_gr()
            if gr_local:
                payload = self._build_compare_json(gr_local, fsid)
                is_synced = fs_tags.compute_sync_from_payload(payload)
                fs_tags.set_sync_status_for_person(self.dbstate.db, gr_local, is_synced=is_synced)
        except Exception:
            pass

        win.show_all()

    def _canon_fs_web(self, url: str) -> str:
        try:
            from gramps.gui.fs import tree
            sess = getattr(tree, "_fs_session", None)
            if sess and hasattr(sess, "canonical_web_url"):
                return sess.canonical_web_url(url)
        except Exception:
            pass
        return (url or "")

    # ------------------ models / columns ------------------

    def _make_overview_tree_model(self):
        titles = [
            (_(""), 1, 18, COLOR),  # color pill column
            (_("Property"), 2, 180),
            (_("Gramps date"), 3, 115),
            (_("Gramps value"), 4, 420),
            (_("FS date"), 5, 115),
            (_("FamilySearch value"), 6, 420),
            (" ", NOSORT, 1),
            ("x", 8, 5, TOGGLE, True, self._toggle_noop),
            (_("xType"), NOSORT, 0),
            (_("xGr"), NOSORT, 0),
            (_("xFs"), NOSORT, 0),
            (_("xGr2"), NOSORT, 0),
            (_("xFs2"), NOSORT, 0),
        ]
        treeview = Gtk.TreeView()
        model = ListModel(treeview, titles, list_mode="tree")
        return treeview, model

    def _make_notes_tree(self):
        treeview = Gtk.TreeView()
        titles = [
            (_(""), 1, 18, COLOR),
            (_("Scope"), 2, 110),
            (_("Title"), 3, 220),
            (_("Gramps"), 4, 460),
            (_("FS Title"), 5, 220),
            (_("FamilySearch"), 6, 460),
        ]
        model = ListModel(treeview, titles, list_mode="tree")
        return treeview, model

    def _make_sources_tree(self):
        treeview = Gtk.TreeView()
        titles = [
            (_(""), 1, 18, COLOR),
            (_("Kind"), 2, 90),
            (_("Gramps date"), 3, 110),
            (_("Gramps title"), 4, 260),
            (_("Gramps URL"), 5, 280),
            (_("FS date"), 6, 110),
            (_("FS title"), 7, 260),
            (_("FS URL"), 8, 280),
            (_("Tags"), 9, 220),
            (_("Contributor"), 10, 130),
            (_("Modified"), 11, 165),
            (_("FS ID"), NOSORT, 0),
        ]
        model = ListModel(treeview, titles, list_mode="tree")
        return treeview, model

    # ------------------ fills ------------------

    def _fill_overview(self, model: ListModel, gr: Person, fsid: str):
        fs_person = deserialize.Person._index.get(fsid) or deserialize.Person()
        try:
            fs_compare.compare_fs_to_gramps(fs_person, gr, self.dbstate.db, model=model, dupdoc=True)
        except Exception as e:
            WarningDialog(_("Compare failed: {e}").format(e=str(e)))

    def _fill_notes(self, model: Any, gr: Person, fsid: str):
        self._ensure_notes_cached(fsid)
        fs_person = deserialize.Person._index.get(fsid) or deserialize.Person()
        if not fs_person:
            return

        em = "_"
        fs_placeholder = em if self.__class__.fs_Tree else _("Not connected to FamilySearch")

        note_handles = gr.get_note_list()
        fs_notes_remaining = fs_person.notes.copy()

        # person notes
        for nh in note_handles:
            n = self.dbstate.db.get_note_from_handle(nh)
            note_text = n.get()
            title = _(n.type.xml_str())
            fs_text = fs_placeholder
            fs_title = ""
            gr_note_id = None
            try:
                for t in n.text.get_tags():
                    if t.name.name == "LINK" and t.value.startswith("_fsftid="):
                        gr_note_id = t.value[8:]
                        break
            except Exception:
                pass

            found = None
            if gr_note_id:
                for x in fs_notes_remaining:
                    if x.id == gr_note_id:
                        found = x
                        break
            if not found:
                for x in fs_notes_remaining:
                    if x.subject == title:
                        found = x
                        break

            if found:
                fs_notes_remaining.remove(found)
                fs_title = found.subject or ""
                fs_text = found.text or ""
                color = "green" if (
                    fs_title == title and (fs_text == note_text or (note_text.startswith("\ufeff") and fs_text == note_text[1:]))
                ) else "orange"
            else:
                color = "yellow"

            model.add(self._ui_row([color, _("Person"), title, note_text, fs_title or em, fs_text or em]))

        # FS-only person notes
        for x in fs_notes_remaining:
            model.add(self._ui_row(["yellow3", _("Person"), _("(missing in Gramps)"), em, x.subject or "", x.text or ""]))

        # family (spouse) notes
        fs_couples_remaining = fs_person._spouses.copy()
        for fam_h in gr.get_family_handle_list():
            fam = self.dbstate.db.get_family_from_handle(fam_h)
            if not fam:
                continue
            spouse_h = fam.mother_handle if fam.mother_handle != gr.handle else fam.father_handle
            spouse = self.dbstate.db.get_person_from_handle(spouse_h) if spouse_h else None
            spouse_fsid = fs_utilities.get_fsftid(spouse) if spouse else ""
            fs_rel = None
            for rel in list(fs_couples_remaining):
                p1 = rel.person1.resourceId if rel.person1 else ""
                p2 = rel.person2.resourceId if rel.person2 else ""
                if spouse_fsid in (p1, p2):
                    fs_rel = rel
                    fs_couples_remaining.remove(rel)
                    break

            rel_notes: set = set()
            if fs_rel:
                rel_notes = fs_rel.notes.copy()

            for nh in fam.get_note_list():
                n = self.dbstate.db.get_note_from_handle(nh)
                note_text = n.get()
                title = _(n.type.xml_str())
                fs_text = fs_placeholder
                fs_title = ""
                gr_note_id = None
                try:
                    for t in n.text.get_tags():
                        if t.name.name == "LINK" and t.value.startswith("_fsftid="):
                            gr_note_id = t.value[8:]
                            break
                except Exception:
                    pass

                found = None
                if gr_note_id:
                    for x in rel_notes:
                        if x.id == gr_note_id:
                            found = x
                            break
                if not found:
                    for x in rel_notes:
                        if x.subject == title:
                            found = x
                            break

                if found:
                    rel_notes.remove(found)
                    fs_title = found.subject or ""
                    fs_text = found.text or ""
                    color = "green" if (
                        fs_title == title and (fs_text == note_text or (note_text.startswith("\ufeff") and fs_text == note_text[1:]))
                    ) else "orange"
                else:
                    color = "yellow"

                model.add(self._ui_row([color, _("Family"), title, note_text, fs_title or em, fs_text or em]))

            for x in rel_notes:
                model.add(self._ui_row(["yellow3", _("Family"), _("(missing in Gramps)"), em, x.subject or "", x.text or ""]))

        for rel in fs_couples_remaining:
            for x in rel.notes:
                model.add(self._ui_row(["yellow3", _("Family"), _("(missing in Gramps)"), em, x.subject or "", x.text or ""]))

    def _fill_sources(self, model: Any, gr: Person, fsid: str):
        self._ensure_sources_cached(fsid)

        fs_person = deserialize.Person._index.get(fsid) or deserialize.Person()
        em = "_"
        fs_placeholder = em if self.__class__.fs_Tree else _("Not connected to FamilySearch")

        source_meta = self._gather_sr_meta(fsid)

        fs_source_ids: dict[str, None] = {}
        for sr in getattr(fs_person, "sources", []) or []:
            fs_source_ids[getattr(sr, "descriptionId", "")] = None
        for rel in getattr(fs_person, "_spouses", []) or []:
            for sr in getattr(rel, "sources", []) or []:
                fs_source_ids[getattr(sr, "descriptionId", "")] = None

        for sdid in list(fs_source_ids.keys()):
            if not sdid:
                continue
            if sdid not in deserialize.SourceDescription._index:
                sd = deserialize.SourceDescription()
                sd.id = sdid
                deserialize.SourceDescription._index[sdid] = sd
                self.__class__.fs_Tree.sourceDescriptions.add(sd)

        fs_import.fetch_source_dates(self.__class__.fs_Tree)

        citation_handles: set[str] = set(gr.get_citation_list())
        for er in gr.get_event_ref_list():
            ev = self.dbstate.db.get_event_from_handle(er.ref)
            citation_handles.update(ev.get_citation_list())
        for fam_h in gr.get_family_handle_list():
            fam = self.dbstate.db.get_family_from_handle(fam_h)
            citation_handles.update(fam.get_citation_list())
            for er in fam.get_event_ref_list():
                ev = self.dbstate.db.get_event_from_handle(er.ref)
                citation_handles.update(ev.get_citation_list())

        for ch in citation_handles:
            c = self.dbstate.db.get_citation_from_handle(ch)
            src_gr = fs_import.IntermediateSource()
            src_gr.from_gramps(self.dbstate.db, c)
            title = src_gr.citation_title
            note_text = (src_gr.note_text or "").strip()
            gr_url = self._canon_fs_web(src_gr.url)
            date = fs_utilities.gramps_date_to_formal(c.date)
            sd_id = fs_utilities.get_fsftid(c)

            color = "yellow"
            fs_title = fs_date = fs_url = ""
            fs_text = fs_placeholder
            kind = ""
            tags_disp = ""
            contributor = ""
            modified = ""

            if sd_id and sd_id in deserialize.SourceDescription._index:
                sd = deserialize.SourceDescription._index[sd_id]
                src_fs = fs_import.IntermediateSource()
                src_fs.from_fs(sd, None)
                fs_title = src_fs.citation_title
                fs_text = src_fs.note_text
                fs_date = str(src_fs.date)
                fs_url = self._canon_fs_web(src_fs.url)
                meta = source_meta.get(sd_id, {})
                kind = meta.get("kind", "")
                tags_disp = self._pretty_tags(meta.get("tags", []))
                contributor = meta.get("contributor", "")
                modified = meta.get("modified", "")
                color = "orange"
                if (fs_date == date and fs_title == title and fs_url == gr_url and (fs_text or "").strip() == note_text):
                    color = "green"
                fs_source_ids.pop(sd_id, None)
            else:
                fs_date = fs_title = fs_url = em

            model.add(self._ui_row([color, kind, date, title, gr_url, fs_date, fs_title or em, fs_url or em, tags_disp, contributor, modified, sd_id or ""]))

        for sdid in list(fs_source_ids.keys()):
            if not sdid:
                continue
            sd = deserialize.SourceDescription._index.get(sdid)
            fs_title = ""
            if sd and getattr(sd, "titles", None):
                for t in sd.titles:
                    fs_title += t.value
            fs_date = getattr(sd, "_date", "") or ""
            fs_url = self._canon_fs_web(getattr(sd, "about", "") or "")
            meta = source_meta.get(sdid, {})
            kind = meta.get("kind", _("Mention"))
            tags_disp = self._pretty_tags(meta.get("tags", []))
            contributor = meta.get("contributor", "")
            modified = meta.get("modified", "")
            model.add(self._ui_row(["yellow3", kind, em, em, em, str(fs_date) or em, fs_title or em, fs_url or em, tags_disp, contributor, modified, sdid]))

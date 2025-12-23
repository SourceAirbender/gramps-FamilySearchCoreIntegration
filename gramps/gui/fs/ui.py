# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2025  Gabriel Rios
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

"""
Shared GTK3 UI helpers for the FamilySearch integration:
- Palette / color normalization
- CSS install-once registry
- HeaderBar helpers
- TreeView tuning
- ScrolledWindow wrapper
- Legend row builder (pills)
- CellRenderer background set/clear (prevents sticky backgrounds)
- Icon+label button builder
- Simple message dialogs (info/error/warn)
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from gi.repository import Gtk, Gdk, GLib

try:
    from gi.repository import Pango 
except Exception:
    Pango = None


PALETTE = {
    "green":  "#D8F3DC",
    "red":    "#FFE3E3",
    "orange": "#FFE8CC",
    "yellow": "#FFF3BF",
    "yellow3": "#D0EBFF",
    "white":  "#F8F9FA",
    "gray":   "#E9ECEF",
}


_CSS_KEYS: set[str] = set()


def install_css_once(key: str, css: bytes) -> bool:
    global _CSS_KEYS
    k = (key or "fs.css").strip() or "fs.css"
    if k in _CSS_KEYS:
        return True

    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        _CSS_KEYS.add(k)
        return True
    except Exception:
        return False


def ensure_base_css() -> None:
    """
    Optional shared atoms CSS. Modules can still install their own wrappers.
    """
    css = b"""
    .fs-legend-pill {
        border-radius: 999px;
        padding: 2px 10px;
        border: 1px solid rgba(0,0,0,0.10);
    }

    .fs-legend-label {
        font-weight: 600;
        opacity: 0.92;
    }

    .fs-muted {
        opacity: 0.92;
    }
    """
    install_css_once("fs.base_atoms", css)



def ui_color(token: str) -> str:
    """
    Maps semantic tokens like 'green' -> '#D8F3DC'.
    If token is already a color string, returns it unchanged.
    """
    s = (token or "").strip()
    if not s:
        return s
    return PALETTE.get(s, s)


def set_headerbar(widget: object, title: str, subtitle: str = "") -> None:
    """
    HeaderBar for Gtk.Window or Gtk.Dialog.
    """
    try:
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = title or ""
        if subtitle:
            hb.props.subtitle = subtitle
        if hasattr(widget, "set_titlebar"):
            widget.set_titlebar(hb)
    except Exception:
        pass


def wrap_scroller(child: Gtk.Widget, min_h: int = 360) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    try:
        sw.set_min_content_height(min_h)
    except Exception:
        pass
    sw.add(child)
    return sw


def tune_treeview(tv: Gtk.TreeView, *, headers: bool = True, grid: bool = True, rules: bool = True) -> None:
    """
    General TreeView polish used across multiple dialogs/windows
    """
    try:
        tv.set_headers_visible(bool(headers))
    except Exception:
        pass
    if grid:
        try:
            tv.set_grid_lines(Gtk.TreeViewGridLines.BOTH)
        except Exception:
            pass
    if rules:
        try:
            tv.set_rules_hint(True)
        except Exception:
            pass

    cols = tv.get_columns() or []
    for col in cols:
        try:
            col.set_resizable(True)
            col.set_reorderable(True)
        except Exception:
            pass

        try:
            for r in col.get_cells() or []:
                if isinstance(r, Gtk.CellRendererText):
                    try:
                        r.set_property("ellipsize", 3)  # Pango.EllipsizeMode.END
                    except Exception:
                        pass
        except Exception:
            pass


def set_cell_bg(cell: Gtk.CellRenderer, color_token: str) -> None:
    s = ui_color(color_token)
    if not s:
        clear_cell_bg(cell)
        return

    painted = False
    try:
        rgba = Gdk.RGBA()
        if rgba.parse(s):
            cell.set_property("cell-background-rgba", rgba)
            cell.set_property("cell-background-set", True)
            painted = True
    except Exception:
        painted = False

    if not painted:
        try:
            cell.set_property("cell-background", s)
            cell.set_property("cell-background-set", True)
        except Exception:
            pass


def clear_cell_bg(cell: Gtk.CellRenderer) -> None:
    try:
        cell.set_property("cell-background-set", False)
        return
    except Exception:
        pass
    try:
        cell.set_property("cell-background", "")
        cell.set_property("cell-background-set", False)
    except Exception:
        pass


def build_legend_row(
    items: Sequence[Tuple[str, str]],
    *,
    hint: str = "",
    wrap_class: str = "",
) -> Gtk.Widget:
    """
    items: [(color_token, label), ...]
    Returns a Gtk.Box with colored pill indicators and an optional hint label.
    """
    ensure_base_css()

    wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    if wrap_class:
        try:
            wrap.get_style_context().add_class(wrap_class)
        except Exception:
            pass

    def pill(color_token: str, text: str) -> Gtk.Widget:
        eb = Gtk.EventBox()
        eb.set_visible_window(True)
        eb.get_style_context().add_class("fs-legend-pill")

        try:
            rgba = Gdk.RGBA()
            rgba.parse(ui_color(color_token))
            eb.override_background_color(Gtk.StateFlags.NORMAL, rgba)
        except Exception:
            pass

        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class("fs-legend-label")
        eb.add(lbl)
        return eb

    for c, t in items:
        wrap.pack_start(pill(c, t), False, False, 0)

    if hint:
        h = Gtk.Label(label=hint)
        h.set_xalign(1.0)
        try:
            h.set_ellipsize(3)  # END
        except Exception:
            pass
        wrap.pack_end(h, True, True, 0)

    return wrap


def set_button_icon_and_label(btn: Gtk.Button, icon_name: str, label: str) -> None:
    try:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        box.pack_start(img, False, False, 0)
        box.pack_start(Gtk.Label(label=label), False, False, 0)
        btn.add(box)
        return
    except Exception:
        pass

    try:
        btn.set_label(label)
    except Exception:
        pass


# ---------------------
# Message dialogs
# --------------------

def message_dialog(
    parent: Optional[Gtk.Window],
    title: str,
    body: str,
    *,
    message_type: Gtk.MessageType = Gtk.MessageType.INFO,
    buttons: Gtk.ButtonsType = Gtk.ButtonsType.OK,
) -> None:
    try:
        d = Gtk.MessageDialog(
            transient_for=parent,
            modal=True,
            message_type=message_type,
            buttons=buttons,
            text=title or "",
        )
        if body:
            d.format_secondary_text(body)
        d.run()
        d.destroy()
    except Exception:
        pass


def info_dialog(parent: Optional[Gtk.Window], title: str, body: str) -> None:
    message_dialog(parent, title, body, message_type=Gtk.MessageType.INFO)


def error_dialog(parent: Optional[Gtk.Window], title: str, body: str) -> None:
    message_dialog(parent, title, body, message_type=Gtk.MessageType.ERROR)


def warn_dialog(parent: Optional[Gtk.Window], title: str, body: str) -> None:
    message_dialog(parent, title, body, message_type=Gtk.MessageType.WARNING)

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

from gramps.gen.const import GRAMPS_LOCALE as glocale

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext

from . import deserializer as deserialize

from .tool import FSImportTool
from .options import FSImportOptions
from .importer import FSToGrampsImporter
from .places import create_place, add_place, get_place_by_id
from .notes import add_note
from .events import add_event, update_event
from .names import add_name, add_names
from .sources import fetch_source_dates, add_source, IntermediateSource

# Classes
FSImportTool
FSImportOptions
FSToGrampsImporter
IntermediateSource

# Functions
create_place
add_place
get_place_by_id
add_note
add_event
update_event
add_name
add_names
fetch_source_dates
add_source

__all__ = [
    "FSImportTool",
    "FSImportOptions",
    "FSToGrampsImporter",
    "create_place",
    "add_place",
    "get_place_by_id",
    "add_note",
    "add_event",
    "update_event",
    "add_name",
    "add_names",
    "fetch_source_dates",
    "add_source",
    "IntermediateSource",
]

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

from gramps.gen.const import GRAMPS_LOCALE as glocale

# In-core Gramps module: use the core translator (not addon translator)
_ = glocale.translation.gettext

from gramps.gen.fs.import_.events import add_event, update_event
from .importer import FSToGrampsImporter
from gramps.gen.fs.import_.names import add_name, add_names
from gramps.gen.fs.import_.notes import add_note
from .options import FSImportOptions
from gramps.gen.fs.import_.places import create_place, add_place, get_place_by_id
from gramps.gen.fs.import_.sources import (
    fetch_source_dates,
    add_source,
    IntermediateSource,
)
from .tool import FSImportTool

__all__ = [
    "FSImportTool",
    "FSImportOptions",
    "FSToGrampsImporter",
    "IntermediateSource",
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
]

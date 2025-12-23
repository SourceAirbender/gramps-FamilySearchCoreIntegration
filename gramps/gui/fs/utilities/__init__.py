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

from .index import (
    build_fs_index,
    FS_INDEX_PEOPLE,
    FS_INDEX_PLACES,
)
from .dates import (
    fs_date_to_gramps_date,
    gramps_date_to_formal,
)
from .attributes import (
    get_fsftid,
    get_internet_address,
)
from .events import (
    get_fs_fact,
    get_gramps_event,
)
from .linking import (
    link_gramps_fs_id,
)

get_url = get_internet_address

__all__ = [
    "build_fs_index",
    "fs_date_to_gramps_date",
    "gramps_date_to_formal",
    "get_fsftid",
    "get_internet_address",
    "get_fs_fact",
    "get_gramps_event",
    "link_gramps_fs_id",
    "FS_INDEX_PEOPLE",
    "FS_INDEX_PLACES",
    "get_url",
]

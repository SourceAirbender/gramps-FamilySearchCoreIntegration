#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2026  Doug Blank
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

"""Tests for comparator helpers in gramps/gen/fs/compare/comparators.py."""

# python3 -m unittest gramps.gen.fs.compare.test.comparators_test -v

import os
import shutil
import tempfile
import types
import unittest

ROOT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)


def _ensure_test_resources():
    """Ensure GRAMPS_RESOURCES points to a directory with required files."""
    resource_path = os.environ.get("GRAMPS_RESOURCES")
    if resource_path and os.path.exists(
        os.path.join(resource_path, "gramps", "authors.xml")
    ):
        return resource_path

    build_share = os.path.join(ROOT_DIR, "build", "share")
    if os.path.exists(os.path.join(build_share, "gramps", "authors.xml")):
        return build_share

    resource_path = tempfile.mkdtemp(prefix="gramps-resources-")
    os.makedirs(os.path.join(resource_path, "gramps", "images"), exist_ok=True)
    os.makedirs(os.path.join(resource_path, "doc", "gramps"), exist_ok=True)
    os.makedirs(os.path.join(resource_path, "locale"), exist_ok=True)

    shutil.copyfile(
        os.path.join(ROOT_DIR, "data", "authors.xml"),
        os.path.join(resource_path, "gramps", "authors.xml"),
    )
    shutil.copyfile(
        os.path.join(ROOT_DIR, "images", "gramps.png"),
        os.path.join(resource_path, "gramps", "images", "gramps.png"),
    )
    shutil.copyfile(
        os.path.join(ROOT_DIR, "COPYING"),
        os.path.join(resource_path, "doc", "gramps", "COPYING"),
    )
    return resource_path


os.environ["GRAMPS_RESOURCES"] = _ensure_test_resources()
os.environ["HOME"] = os.environ.get("HOME") or tempfile.mkdtemp(prefix="gramps-home-")

from gramps.gen.fs.compare.comparators import (
    _child_id_for_rel,
    _child_rel_matches_family,
    _other_parent_id_for_child_rel,
)


def _make_rel(parent1_id, parent2_id, child_id):
    """
    Build a minimal child-and-parents relationship using SimpleNamespace.

    :param parent1_id: resourceId for parent1, or None to omit parent1.
    :param parent2_id: resourceId for parent2, or None to omit parent2.
    :param child_id: resourceId for the child reference.
    """
    parent1 = (
        types.SimpleNamespace(resourceId=parent1_id) if parent1_id is not None else None
    )
    parent2 = (
        types.SimpleNamespace(resourceId=parent2_id) if parent2_id is not None else None
    )
    child = types.SimpleNamespace(resourceId=child_id)
    return types.SimpleNamespace(parent1=parent1, parent2=parent2, child=child)


# -------------------------------------------------------------------------
#
# TestOtherParentIdForChildRel
#
# -------------------------------------------------------------------------
class TestOtherParentIdForChildRel(unittest.TestCase):
    """Tests for _other_parent_id_for_child_rel."""

    def test_fsid_is_parent1_returns_parent2(self):
        """When fsid matches parent1, return parent2.resourceId."""
        rel = _make_rel("A", "B", "X")
        self.assertEqual(_other_parent_id_for_child_rel("A", rel), "B")

    def test_fsid_is_parent2_returns_parent1(self):
        """When fsid matches parent2, return parent1.resourceId."""
        rel = _make_rel("A", "B", "X")
        self.assertEqual(_other_parent_id_for_child_rel("B", rel), "A")

    def test_fsid_is_neither_returns_none(self):
        """When fsid matches neither parent, return None."""
        rel = _make_rel("A", "B", "X")
        self.assertIsNone(_other_parent_id_for_child_rel("Z", rel))

    def test_fsid_is_parent1_parent2_is_none_returns_empty_string(self):
        """When fsid is parent1 and parent2 is absent, return ''."""
        rel = _make_rel("A", None, "X")
        self.assertEqual(_other_parent_id_for_child_rel("A", rel), "")

    def test_fsid_is_parent1_parent2_has_empty_resource_id(self):
        """When fsid is parent1 and parent2.resourceId is '', return ''."""
        rel = _make_rel("A", "", "X")
        self.assertEqual(_other_parent_id_for_child_rel("A", rel), "")


# -------------------------------------------------------------------------
#
# TestChildIdForRel
#
# -------------------------------------------------------------------------
class TestChildIdForRel(unittest.TestCase):
    """Tests for _child_id_for_rel."""

    def test_returns_child_resource_id(self):
        """Return the child's resourceId when it is present."""
        rel = _make_rel("A", "B", "X")
        self.assertEqual(_child_id_for_rel(rel), "X")

    def test_no_child_attribute_returns_empty_string(self):
        """Return '' when the rel has no child attribute."""
        rel = types.SimpleNamespace(parent1=None, parent2=None)
        self.assertEqual(_child_id_for_rel(rel), "")

    def test_child_resource_id_is_none_returns_empty_string(self):
        """Return '' when child.resourceId is None."""
        child = types.SimpleNamespace(resourceId=None)
        rel = types.SimpleNamespace(parent1=None, parent2=None, child=child)
        self.assertEqual(_child_id_for_rel(rel), "")


# -------------------------------------------------------------------------
#
# TestChildRelMatchesFamily
#
# -------------------------------------------------------------------------
class TestChildRelMatchesFamily(unittest.TestCase):
    """Tests for _child_rel_matches_family."""

    def test_child_and_spouse_both_match_returns_true(self):
        """Return True when both child and other-parent match the family."""
        rel = _make_rel("A", "B", "X")
        self.assertTrue(_child_rel_matches_family("A", rel, "X", "B"))

    def test_child_matches_but_spouse_does_not_returns_false(self):
        """Return False when child matches but the other parent differs."""
        rel = _make_rel("A", "C", "X")
        self.assertFalse(_child_rel_matches_family("A", rel, "X", "B"))

    def test_child_does_not_match_returns_false(self):
        """Return False when the child id does not match."""
        rel = _make_rel("A", "B", "Y")
        self.assertFalse(_child_rel_matches_family("A", rel, "X", "B"))

    def test_single_parent_family_no_other_parent_returns_true(self):
        """Return True for a single-parent family where the rel has no other parent."""
        rel = _make_rel("A", None, "X")
        self.assertTrue(_child_rel_matches_family("A", rel, "X", ""))

    def test_single_parent_family_rel_has_other_parent_returns_false(self):
        """Return False when the local family has no spouse but the rel does."""
        rel = _make_rel("A", "C", "X")
        self.assertFalse(_child_rel_matches_family("A", rel, "X", ""))

    def test_empty_child_fsid_returns_false(self):
        """Return False when child_fsid is empty (guard condition)."""
        rel = _make_rel("A", "B", "X")
        self.assertFalse(_child_rel_matches_family("A", rel, "", "B"))


# -------------------------------------------------------------------------
#
# TestChildTripleConsumptionFix
#
# -------------------------------------------------------------------------
class TestChildTripleConsumptionFix(unittest.TestCase):
    """
    Issue 1: verify that a triple belonging to family A+C is not consumed
    while iterating over family A+B.
    """

    def test_triple_for_family_ac_not_matched_by_family_ab(self):
        """
        Triple2 (A+C, child=X) must NOT match when checking family A+B.
        """
        triple1 = _make_rel("A", "B", "X")
        triple2 = _make_rel("A", "C", "X")
        fs_children = [triple1, triple2]

        # Simulate the inner loop for family A+B (fs_spouse_id='B')
        matched_for_ab = [
            t for t in fs_children if _child_rel_matches_family("A", t, "X", "B")
        ]
        self.assertEqual(matched_for_ab, [triple1])

        # Simulate consumption: remove the matched triple
        fs_children.remove(triple1)

        # Only triple2 remains for family A+C
        matched_for_ac = [
            t for t in fs_children if _child_rel_matches_family("A", t, "X", "C")
        ]
        self.assertEqual(matched_for_ac, [triple2])

    def test_triple_for_family_ab_not_matched_by_family_ac(self):
        """
        Triple1 (A+B, child=X) must NOT match when checking family A+C.
        """
        triple1 = _make_rel("A", "B", "X")
        self.assertFalse(_child_rel_matches_family("A", triple1, "X", "C"))


# -------------------------------------------------------------------------
#
# TestPostLoopUnknownParentGuard
#
# -------------------------------------------------------------------------
class TestPostLoopUnknownParentGuard(unittest.TestCase):
    """
    Issue 2: when fs_spouse_id == '' (no linked spouse) and the triple has
    no other parent, _child_rel_matches_family returns True but the post-loop
    guard 'if fs_spouse_id and (...)' must prevent false positives.
    """

    def test_single_parent_triple_matches_single_parent_family(self):
        """_child_rel_matches_family returns True for a matching single-parent case."""
        triple = _make_rel("A", None, "X")
        self.assertTrue(_child_rel_matches_family("A", triple, "X", ""))

    def test_post_loop_guard_is_false_when_fs_spouse_id_empty(self):
        """
        The post-loop condition 'if fs_spouse_id and (...)' is False when
        fs_spouse_id is '' even if _other_parent_id_for_child_rel would match.
        """
        fs_spouse_id = ""
        triple = _make_rel("A", None, "X")
        other_parent_id = _other_parent_id_for_child_rel("A", triple)

        # The guard used in compare_spouses post-loop
        guard_triggers = bool(fs_spouse_id and (other_parent_id == fs_spouse_id))
        self.assertFalse(guard_triggers)

    def test_post_loop_guard_triggers_for_known_spouse(self):
        """
        The post-loop guard fires when fs_spouse_id is non-empty and matches.
        """
        fs_spouse_id = "B"
        triple = _make_rel("A", "B", "X")
        other_parent_id = _other_parent_id_for_child_rel("A", triple)

        guard_triggers = bool(fs_spouse_id and (other_parent_id == fs_spouse_id))
        self.assertTrue(guard_triggers)


if __name__ == "__main__":
    unittest.main()

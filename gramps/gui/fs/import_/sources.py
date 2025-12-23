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

from gramps.gen.lib import (
    Citation,
    Note,
    NoteType,
    StyledTextTag,
    StyledTextTagType,
    StyledText,
    Source,
    SrcAttribute,
    Repository,
    RepoRef,
    RepositoryType,
    Url,
    UrlType,
    SourceMediaType,
)

from . import _

import gramps.gui.fs.utilities as fs_utilities
from gramps.gui.fs import tree
from . import deserializer as deserialize

import re
from urllib.parse import urlparse, urlunparse, urljoin

_URL_RE = re.compile(r"https?://[^\s)\]\">]+")


def _canon_fs_web(url: str) -> str:
    """
    convert any FamilySearch host (beta/api/ident/etc) to www.familysearch.org
    for the image pull function (no images on beta)
    """
    if not url:
        return ""
    u = str(url).strip()
    if not u.startswith(("http://", "https://")):
        return u
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        if host.endswith("familysearch.org"):
            return urlunparse((
                p.scheme or "https",
                "www.familysearch.org",
                p.path or "",
                p.params or "",
                p.query or "",
                p.fragment or "",
            ))
    except Exception:
        pass
    return u


def _looks_like_record_page(u: str) -> bool:
    if not u:
        return False
    try:
        p = urlparse(u)
        if not (p.netloc or "").lower().endswith("familysearch.org"):
            return False
        path = p.path or ""
        if "/ark:/" in path or "/ark:" in path:
            return True
    except Exception:
        return False
    return False


def _extract_fs_record_url(sd) -> str:
    """
    try to pull a usable record/source page URL from a GEDCOMX SourceDescription prefers ark/Persistent/Primary identifiers, then URLs in citations, then sd.about. 
    returns www.familysearch.org URL when possible
    """
    if not sd:
        return ""

    candidates: list[str] = []

    # identifiers
    ids = getattr(sd, "identifiers", None)
    if isinstance(ids, dict):
        preferred_keys = (
            "http://gedcomx.org/Persistent",
            "http://gedcomx.org/Primary",
            "https://gedcomx.org/Persistent",
            "https://gedcomx.org/Primary",
        )
        for k in preferred_keys:
            vals = ids.get(k)
            if isinstance(vals, (list, tuple)):
                for v in vals:
                    if isinstance(v, str) and v.startswith(("http://", "https://")):
                        candidates.append(v)
            elif isinstance(vals, str) and vals.startswith(("http://", "https://")):
                candidates.append(vals)

        for vals in ids.values():
            if isinstance(vals, (list, tuple)):
                for v in vals:
                    if isinstance(v, str) and v.startswith(("http://", "https://")):
                        candidates.append(v)
            elif isinstance(vals, str) and vals.startswith(("http://", "https://")):
                candidates.append(vals)

    citations = getattr(sd, "citations", None) or []
    for c in citations:
        txt = getattr(c, "value", "") or ""
        for m in _URL_RE.finditer(txt):
            u = m.group(0)
            if "familysearch.org" in u:
                candidates.append(u)

    about = getattr(sd, "about", "") or ""
    if about and "familysearch.org" in about:
        candidates.append(about)

    best = ""
    for u in candidates:
        uu = _canon_fs_web(u)
        if _looks_like_record_page(uu):
            return uu
        if not best and "familysearch.org" in uu:
            best = uu

    return best


def _safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def _get_fs_web_base() -> str:
    s = getattr(tree, "_fs_session", None)
    base = getattr(s, "fs_url", "") if s else ""
    base = (base or "").strip()
    if base.startswith("http"):
        return base.rstrip("/") + "/"
    return "https://www.familysearch.org/"


def _hydrate_source_description(fs_tree, sdid: str) -> None:
    if not sdid:
        return
    sess = getattr(tree, "_fs_session", None)
    if not sess:
        return

    try:
        r = sess.get_url(
            f"/platform/sources/descriptions/{sdid}",
            headers={"Accept": "application/x-gedcomx-v1+json"},
        )
    except TypeError:
        r = sess.get_url(
            f"/platform/sources/descriptions/{sdid}",
            {"Accept": "application/x-gedcomx-v1+json"},
        )

    if not r or getattr(r, "status_code", None) != 200:
        return

    data = _safe_json(r)
    if not isinstance(data, dict):
        return

    try:
        deserialize.deserialize_json(fs_tree, data)
    except Exception:
        return


def fetch_source_dates(fs_tree):
    """
    Populate SourceDescriptions in fs_tree
    """
    sess = getattr(tree, "_fs_session", None)
    if not sess:
        return

    web_base = _get_fs_web_base()

    for sd in getattr(fs_tree, "sourceDescriptions", []) or []:
        if not getattr(sd, "id", ""):
            continue

        if sd.id[:2] == "SD":
            continue

        if not hasattr(sd, "_date"):
            sd._date = None
        if not hasattr(sd, "_collectionUri"):
            sd._collectionUri = None
        if not hasattr(sd, "_collection"):
            sd._collection = None

        if not hasattr(sd, "_fs_links_fetched"):
            sd._fs_links_fetched = False
        if not hasattr(sd, "_fs_api_hydrated"):
            sd._fs_api_hydrated = False

        # 1) Try /service/tree/links/source/{id} only once
        r = None
        if not sd._fs_links_fetched:
            try:
                links_url = urljoin(web_base, f"service/tree/links/source/{sd.id}")
                r = sess.get_url(links_url, headers={"Accept": "application/json"})
            except TypeError:
                r = sess.get_url(links_url, {"Accept": "application/json"})
            except Exception:
                r = None
            finally:
                sd._fs_links_fetched = True

        if r and getattr(r, "status_code", None) == 200:
            data = _safe_json(r)
            if isinstance(data, dict):
                e = data.get("event")
                if isinstance(e, dict):
                    str_formal = e.get("eventDate")
                    if str_formal:
                        try:
                            d = deserialize.Date()
                            d.original = str_formal
                            d.formal = deserialize.DateFormal(str_formal)
                            sd._date = d
                        except Exception:
                            pass

                sd._collectionUri = data.get("fsCollectionUri")
                if sd._collectionUri and isinstance(sd._collectionUri, str):
                    try:
                        sd._collection = sd._collectionUri.removeprefix(
                            "https://www.familysearch.org/platform/records/collections/"
                        )
                    except Exception:
                        prefix = "https://www.familysearch.org/platform/records/collections/"
                        sd._collection = (
                            sd._collectionUri[len(prefix):]
                            if sd._collectionUri.startswith(prefix)
                            else None
                        )

                t = data.get("title")
                if t and isinstance(t, str):
                    try:
                        if len(sd.titles):
                            next(iter(sd.titles)).value = t
                        else:
                            tv = deserialize.TextValue()
                            tv.value = t
                            sd.titles.add(tv)
                    except Exception:
                        pass

                uri_val = (
                    data.get("uri")
                    or data.get("recordUri")
                    or data.get("recordUrl")
                    or data.get("url")
                )
                cand = ""
                if isinstance(uri_val, dict):
                    cand = uri_val.get("uri") or uri_val.get("url") or uri_val.get("href") or ""
                elif isinstance(uri_val, str):
                    cand = uri_val

                cand = _canon_fs_web(cand)
                if cand:
                    try:
                        sd.about = cand
                    except Exception:
                        pass

                n = data.get("notes")
                note_txt = ""
                if isinstance(n, list):
                    note_txt = "\n".join([str(x) for x in n if x])
                elif isinstance(n, str):
                    note_txt = n
                if note_txt:
                    try:
                        if len(sd.notes):
                            next(iter(sd.notes)).text = note_txt
                        else:
                            fn = deserialize.Note()
                            fn.text = note_txt
                            sd.notes.add(fn)
                    except Exception:
                        pass

        if (not getattr(sd, "about", "")) and (not sd._fs_api_hydrated):
            _hydrate_source_description(fs_tree, sd.id)
            sd._fs_api_hydrated = True

        try:
            sd_full = deserialize.SourceDescription._index.get(sd.id) or sd
        except Exception:
            sd_full = sd

        best_url = _extract_fs_record_url(sd_full)
        if best_url:
            try:
                if not getattr(sd_full, "about", ""):
                    sd_full.about = best_url
                if not getattr(sd, "about", ""):
                    sd.about = best_url
            except Exception:
                pass



class IntermediateSource:
    # Helper DTO bridging FS SourceDescription/SourceReference and Gramps Citation/Source.
    id: str | None = None
    repository_name: str | None = None
    source_title: str | None = None
    citation_title: str | None = None
    page_or_position: str | None = None
    confidence_label: str | None = None
    url: str | None = None
    date: any = None
    note_text: str | None = None
    collection: str | None = None
    collection_url: str | None = None

    def from_fs(self, fs_sd, fs_sr):
        self.id = fs_sd.id
        self.repository_name = "FamilySearch"
        self.source_title = "FamilySearch"
        self.citation_title = None
        self.page_or_position = None
        self.confidence_label = None

        extracted = _extract_fs_record_url(fs_sd)
        self.url = extracted or _canon_fs_web(getattr(fs_sd, "about", "") or "") or None

        self.date = None
        self.note_text = "\n"
        self.collection = getattr(fs_sd, "_collection", None)
        self.collection_url = (
            "https://www.familysearch.org/search/collection/" + self.collection
            if self.collection
            else None
        )

        if len(fs_sd.titles):
            self.citation_title = next(iter(fs_sd.titles)).value
        fs_citation_value = None
        if len(fs_sd.citations):
            fs_citation_value = next(iter(fs_sd.citations)).value

        if fs_sd.resourceType not in ("FSREADONLY", "LEGACY", "DEFAULT", "IGI"):
            print("Unknown resourceType !!! :", str(fs_sd.resourceType))
        if fs_sd.resourceType == "LEGACY":
            self.source_title = "Legacy NFS Sources"

        if fs_sd.resourceType != "DEFAULT":
            self.repository_name = "FamilySearch"
            if fs_citation_value:
                parts = fs_citation_value.split('"')
                if len(parts) >= 3:
                    self.source_title = parts[1]
                    self.note_text = self.note_text + "\n".join(parts[2:])

        if len(fs_sd.notes):
            self.note_text = next(iter(fs_sd.notes)).text or ""

        if fs_citation_value and fs_sd.resourceType == "DEFAULT":
            lines = fs_citation_value.split("\n")
            for line in lines:
                if line.startswith(_("Repository")):
                    self.repository_name = line.removeprefix(
                        _("Repository") + " :"
                    ).strip()
                elif line.startswith(_("Source:")):
                    self.source_title = line.removeprefix(_("Source:")).strip()
                elif line.startswith(_("Volume/Page:")):
                    self.page_or_position = line.removeprefix(
                        _("Volume/Page:")
                    ).strip()
                elif line.startswith(_("Confidence:")):
                    self.confidence_label = line.removeprefix(
                        _("Confidence:")
                    ).strip()
            if not self.source_title and len(lines) >= 1:
                self.source_title = lines[0]

        if hasattr(fs_sd, "_date") and fs_sd._date:
            self.date = fs_sd._date

    def from_gramps(self, db, citation):
        self.id = fs_utilities.get_fsftid(citation)
        self.repository_name = None
        self.source_title = None
        self.citation_title = None
        self.page_or_position = citation.page
        self.confidence_label = None
        self.url = fs_utilities.get_internet_address(citation)
        str_formal = fs_utilities.gramps_date_to_formal(citation.date)
        self.date = deserialize.DateFormal(str_formal)
        self.note_text = ""
        self.collection = None
        self.collection_url = None

        if citation.source_handle:
            s = db.get_source_from_handle(citation.source_handle)
            if s:
                self.source_title = s.title
                if len(s.reporef_list) > 0:
                    dh = s.reporef_list[0].ref
                    d = db.get_repository_from_handle(dh)
                    if d:
                        self.repository_name = d.name

        conf = citation.get_confidence_level()
        if conf == Citation.CONF_VERY_HIGH:
            self.confidence_label = _("Very High")
        elif conf == Citation.CONF_HIGH:
            self.confidence_label = _("High")
        elif conf == Citation.CONF_NORMAL:
            self.confidence_label = _("Normal")
        elif conf == Citation.CONF_LOW:
            self.confidence_label = _("Low")
        elif conf == Citation.CONF_VERY_LOW:
            self.confidence_label = _("Very Low")

        title_note_handle = None
        for nh in citation.note_list:
            n = db.get_note_from_handle(nh)
            if n.type == NoteType.CITATION:
                title_note_handle = nh
                text = n.get()
                pos = text.find("\n")
                if pos > 0:
                    self.note_text = text[pos + 1 :].strip(" \n")
                    self.citation_title = text[:pos]
                else:
                    self.citation_title = text
                break

        for nh in citation.note_list:
            if nh == title_note_handle:
                continue
            n = db.get_note_from_handle(nh)
            self.note_text += n.get()

    def to_gramps(self, db, txn, obj):
        repo_handle = None
        if self.repository_name:
            db.dbapi.execute(
                "select handle from repository where name=?", [self.repository_name]
            )
            row = db.dbapi.fetchone()
            if row and row[0]:
                repo_handle = row[0]
            else:
                r = Repository()
                r.set_name(self.repository_name)
                rtype = RepositoryType()
                rtype.set(RepositoryType.WEBSITE)
                r.set_type(rtype)
                if self.repository_name == "FamilySearch":
                    url = Url()
                    url.path = "https://www.familysearch.org/"
                    url.set_type(UrlType.WEB_HOME)
                    r.add_url(url)
                db.add_repository(r, txn)
                db.commit_repository(r, txn)
                repo_handle = r.handle

        src = None
        if self.source_title and not src and self.collection:
            src = db.get_source_from_gramps_id("FS_coll_" + self.collection)
        if not src and self.source_title:
            db.dbapi.execute(
                "select handle from source where title=?", [self.source_title]
            )
            row = db.dbapi.fetchone()
            if row and row[0]:
                src = db.get_source_from_handle(row[0])

        if not src and self.source_title:
            src = Source()
            if self.collection:
                src.gramps_id = "FS_coll_" + self.collection
            src.set_title(self.source_title)
            if self.collection:
                attr = SrcAttribute()
                attr.set_type(_("Internet Address"))
                attr.set_value(self.collection_url)
                src.add_attribute(attr)
            db.add_source(src, txn)
            db.commit_source(src, txn)
            if repo_handle:
                rr = RepoRef()
                rr.ref = repo_handle
                rr.set_media_type(SourceMediaType.ELECTRONIC)
                src.add_repo_reference(rr)
            db.commit_source(src, txn)

        found = False
        citation = None
        for ch in db.get_citation_handles():
            c = db.get_citation_from_handle(ch)
            for attr in c.get_attribute_list():
                if attr.get_type() == "_FSFTID" and attr.get_value() == self.id:
                    found = True
                    citation = c
                    print(" citation found _FSFTID=" + self.id)
                    break
            if found:
                break

        if not citation:
            print(" citation not found _FSFTID=" + self.id)
            citation = Citation()
            attr = SrcAttribute()
            attr.set_type("_FSFTID")
            attr.set_value(self.id)
            citation.add_attribute(attr)
            db.add_citation(citation, txn)

        if self.page_or_position:
            citation.set_page(self.page_or_position)

        if self.confidence_label:
            if self.confidence_label == _("Very High"):
                citation.set_confidence_level(Citation.CONF_VERY_HIGH)
            elif self.confidence_label == _("High"):
                citation.set_confidence_level(Citation.CONF_HIGH)
            elif self.confidence_label == _("Normal"):
                citation.set_confidence_level(Citation.CONF_NORMAL)
            elif self.confidence_label == _("Low"):
                citation.set_confidence_level(Citation.CONF_LOW)
            elif self.confidence_label == _("Very Low"):
                citation.set_confidence_level(Citation.CONF_VERY_LOW)

        if self.date:
            from gramps.gui.fs.utilities import fs_date_to_gramps_date
            citation.date = fs_date_to_gramps_date(self.date)

        if src:
            citation.set_reference_handle(src.get_handle())

        if self.url:
            u0 = fs_utilities.get_internet_address(citation)
            if not u0:
                attr = SrcAttribute()
                attr.set_type(_("Internet Address"))
                attr.set_value(self.url)
                citation.add_attribute(attr)

        db.commit_citation(citation, txn)

        if self.citation_title or len(self.note_text or ""):
            note = None
            for nh in citation.note_list:
                n = db.get_note_from_handle(nh)
                if n.type == NoteType.CITATION:
                    note = n
            if not note:
                n = Note()
                tags = []
                n.set_type(NoteType(NoteType.CITATION))
                n.append(self.citation_title or "")
                n.append("\n\n")
                tags.append(
                    StyledTextTag(
                        StyledTextTagType.BOLD,
                        True,
                        [(0, len(self.citation_title or ""))],
                    )
                )
                n.append(self.note_text or "")
                n.text.set_tags(tags)
                db.add_note(n, txn)
                db.commit_note(n, txn)
                citation.add_note(n.handle)

        db.commit_citation(citation, txn)

        for ch in obj.citation_list:
            if ch == citation.handle:
                return citation
        obj.add_citation(citation.handle)
        return citation


def add_source(db, txn, sd_id, obj, existing_citation_handles):
    # Given a FS SourceDescription id, create/attach the matching Gramps Citation/Source.
    fs_sd = deserialize.SourceDescription._index.get(sd_id)
    if not fs_sd:
        return
    isrc = IntermediateSource()
    isrc.from_fs(fs_sd, None)
    citation = isrc.to_gramps(db, txn, obj)
    return citation

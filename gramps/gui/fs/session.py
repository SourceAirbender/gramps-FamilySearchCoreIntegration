# -*- coding: utf-8 -*-
#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2025       Nick Hall
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
"""
FamilySearch session/auth helper.

Environment variables
- GRAMPS_FS_DEBUG: enable debug logging (1/true/yes/on)
- GRAMPS_FS_SHOW_STATUS: show connection status window on startup
- GRAMPS_FS_SHOW_TOOLS: auto-open tools window after successful probe (default on)
- GRAMPS_FS_ENV: beta|prod
- GRAMPS_FS_AUTH_METHOD: auto|webkit|loopback|manual
- GRAMPS_FS_OAUTH_SCOPE: override OAuth scope
- GRAMPS_FS_LISTENER_TIMEOUT: seconds for loopback listener / webkit capture timeout
- GRAMPS_FS_BETA_APP_KEY / GRAMPS_FS_PROD_APP_KEY: override app keys
- GRAMPS_FS_BETA_REDIRECT / GRAMPS_FS_PROD_REDIRECT: override redirects
"""

from __future__ import annotations

import logging
import math
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import certifi
import requests

from gramps.gen.config import config
from gramps.gen.constfunc import lin, win
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402

sys.modules.setdefault("gramps.gui.fs.session", sys.modules[__name__])
LOG = logging.getLogger(__name__)


def _debug_enabled() -> bool:
    val = os.environ.get("GRAMPS_FS_DEBUG", "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    try:
        return bool(config.get("familysearch.debug"))
    except Exception:
        return False


if _debug_enabled():
    LOG.setLevel(logging.DEBUG)
    if not LOG.handlers and not logging.getLogger().handlers:
        _h = logging.StreamHandler(stream=sys.stderr)
        _h.setLevel(logging.DEBUG)
        _h.setFormatter(
            logging.Formatter("[FS DEBUG %(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        LOG.addHandler(_h)
        LOG.propagate = False


GLOBAL_SESSION: "Session | None" = None
SESSION: "Session | None" = None

DEFAULT_LOOPBACK_REDIRECT = "http://127.0.0.1:57938/familysearch-auth"


class FSException(Exception):
    pass


class FSPermission(Exception):
    pass


def _dbg(msg: str) -> None:
    LOG.debug(msg)


def _safe(s: str | None, max_len: int = 900) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= max_len else (s[:max_len] + "...(truncated)...")


def _mask(s: str | None, keep: int = 6) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + ("*" * (len(s) - keep))


def _cookie_summary(jar) -> str:
    try:
        items: list[str] = []
        for c in jar:
            items.append(f"{c.name}@{c.domain}")
        items = sorted(set(items))
        if not items:
            return "(none)"
        if len(items) > 25:
            return ", ".join(items[:25]) + f", ...(+{len(items) - 25})"
        return ", ".join(items)
    except Exception:
        return "(unavailable)"


def _cfg_get(key: str, default: Any = None) -> Any:
    try:
        return config.get(key)
    except Exception:
        return default


def _cfg_set(key: str, value: Any) -> None:
    try:
        config.set(key, value)
    except Exception:
        return


def _try_import_webkit():
    if not lin():
        return False, None
    try:
        import gi as _gi

        _gi.require_version("WebKit2", "4.0")  # hard requirement
        from gi.repository import WebKit2  # type: ignore

        return True, WebKit2
    except Exception:
        return False, None


def _is_loopback_redirect(uri: str) -> bool:
    try:
        p = urlparse(uri)
        return p.hostname in ("127.0.0.1", "localhost")
    except Exception:
        return False


def _extract_code_from_text(text: str) -> str:
    # Accept raw code OR full redirect URL containing ?code=...
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        try:
            qs = parse_qs(urlparse(text).query, keep_blank_values=True)
            return (qs.get("code", [""])[0] or "").strip()
        except Exception:
            return ""
    return text


class FamilySearchSession:
    def __init__(self, *args, **kwargs):
        self._dbstate = None
        self._uistate = None
        self._track = None
        self._last_person_handle = None

    def bind_context(self, dbstate=None, uistate=None, track=None, person_handle=None):
        if dbstate is not None:
            self._dbstate = dbstate
        if uistate is not None:
            self._uistate = uistate
        if track is not None:
            self._track = track
        elif uistate is not None:
            self._track = getattr(uistate, "track", None) or getattr(
                uistate, "_track", None
            )
        if person_handle:
            self._last_person_handle = person_handle


# GUI status indicator
class FSStatusIndicator:
    # Keeps state
    DOT_SIZE = 12

    def __init__(self):
        self._state = "DISCONNECTED"
        self._detail = ""
        self._http = None
        self._views: list[dict] = []
        self._window: Gtk.Window | None = None

    def create_widget(self) -> Gtk.Widget:
        dot = Gtk.DrawingArea()
        dot.set_size_request(self.DOT_SIZE, self.DOT_SIZE)
        dot.connect("draw", self._on_draw)

        label = Gtk.Label(label="FS: disconnected")
        label.set_xalign(0.0)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_border_width(6)
        box.pack_start(dot, False, False, 0)
        box.pack_start(label, True, True, 0)

        view = {"box": box, "dot": dot, "label": label}
        self._views.append(view)

        def _cleanup(*_args):
            try:
                self._views.remove(view)
            except Exception:
                pass

        box.connect("destroy", _cleanup)
        self._apply_to_view(view)
        return box

    def show_window(self, parent: Gtk.Window | None = None):
        if self._window is not None:
            self._window.present()
            return

        win_ = Gtk.Window(title="FamilySearch Connection")
        win_.set_default_size(360, 70)
        win_.set_resizable(False)
        win_.add(self.create_widget())
        try:
            win_.set_keep_above(True)
        except Exception:
            pass
        if parent is not None:
            try:
                win_.set_transient_for(parent)
            except Exception:
                pass
        win_.connect("destroy", lambda *_: setattr(self, "_window", None))
        win_.show_all()
        self._window = win_

    def set(self, state: str, detail: str = "", http: int | None = None):
        self._state = state
        self._detail = detail or ""
        self._http = http
        for view in list(self._views):
            self._apply_to_view(view)

    def _apply_to_view(self, view: dict):
        label: Gtk.Label = view["label"]
        dot: Gtk.DrawingArea = view["dot"]
        box: Gtk.Box = view["box"]

        extra = ""
        if self._http is not None:
            extra = f" (HTTP {self._http})"
        elif self._detail:
            extra = f" ({self._detail})"

        pretty = (self._state or "").lower().replace("_", " ")
        label.set_text(f"FS: {pretty}{extra}")

        tip = self._detail
        if self._http is not None and self._detail:
            tip = f"HTTP {self._http}: {self._detail}"
        if tip:
            box.set_tooltip_text(tip)

        dot.queue_draw()

    def _state_color(self):
        s = (self._state or "").upper()
        if s in ("CONNECTED", "API_OK"):
            return (0.20, 0.70, 0.25)  # green
        if s in (
            "PROBING",
            "LOGGING_IN",
            "AUTHORIZING",
            "EXCHANGING_TOKEN",
            "AWAITING_BROWSER",
        ):
            return (0.95, 0.70, 0.15)  # amber
        if s in ("TOKEN_ACQUIRED", "AUTH_CODE_RECEIVED"):
            return (0.20, 0.55, 0.90)  # blue
        if s in ("ERROR", "API_FAIL"):
            return (0.85, 0.20, 0.20)  # red
        return (0.55, 0.55, 0.55)  # grey

    def _on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        r = min(w, h) / 2.0 - 1.0
        cx = w / 2.0
        cy = h / 2.0

        rr, gg, bb = self._state_color()
        cr.set_source_rgb(rr, gg, bb)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.set_line_width(1.0)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        return False


class Listener(threading.Thread):
    def __init__(self, host: str, port: int, expected_path: str, timeout_s: int = 300):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.expected_path = expected_path or "/"
        self.timeout_s = timeout_s
        self.result: dict[str, str] = {}
        self.error: str | None = None

    def run(self):
        try:
            _dbg(
                f"Listener starting; bind {self.host}:{self.port} "
                f"expected_path={self.expected_path!r} timeout={self.timeout_s}s"
            )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.host, self.port))
                s.listen(5)
                s.settimeout(self.timeout_s)

                try:
                    conn, addr = s.accept()
                except socket.timeout:
                    raise TimeoutError(
                        f"Callback not received within {self.timeout_s}s"
                    )

                _dbg(f"Listener accepted connection from {addr}")
                with conn:
                    raw = conn.recv(8192)
                    text = raw.decode("utf-8", errors="replace")
                    first_line = text.splitlines()[0] if text else ""
                    _dbg(f"Listener first line: {_safe(first_line, 350)}")

                    parts = first_line.split(" ")
                    req_path = parts[1] if len(parts) >= 2 else ""
                    path_only = req_path.split("?", 1)[0] if req_path else ""
                    query = req_path.split("?", 1)[1] if "?" in req_path else ""

                    if (
                        self.expected_path
                        and path_only
                        and path_only != self.expected_path
                    ):
                        _dbg(
                            f"Listener WARNING: unexpected path {path_only!r} "
                            f"expected {self.expected_path!r}"
                        )

                    qs = parse_qs(query, keep_blank_values=True)
                    self.result = {k: (v[0] if v else "") for k, v in qs.items()}
                    _dbg(f"Listener parsed params: {self.result}")

                    if "error" in self.result or "error_description" in self.result:
                        msg = (
                            "Access denied or error returned. You can close this tab.\n"
                        )
                    else:
                        msg = "Success! You can now return to Gramps.\n"

                    conn.sendall(b"HTTP/1.1 200 OK\r\n")
                    conn.sendall(f"Content-Length: {len(msg)}\r\n".encode("utf-8"))
                    conn.sendall(b"Content-Type: text/plain\r\n\r\n")
                    conn.sendall(msg.encode("utf-8"))
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            _dbg(f"Listener exception: {self.error}")


AUTH_AUTO = "auto"
AUTH_WEBKIT = "webkit"  # embedded WebKit (Linux only)
AUTH_LOOPBACK = "loopback"  # listener + loopback redirect
AUTH_MANUAL = "manual"  # system browser + paste code/URL

ENV_BETA = "beta"
ENV_PROD = "prod"


@dataclass(frozen=True)
class EnvProfile:
    env: str
    fs_url: str
    ident_url: str
    api_url: str
    web_url: str
    app_key: str
    redirect: str


class Session(requests.Session):
    # Requests session with FamilySearch auth helpers.

    # mypy declare dynamically-assigned class attributes
    _shared: ClassVar["Session | None"] = None
    _last_instance: ClassVar["Session | None"] = None

    def __init__(self, server: int = 0, app_key: str = "", redirect: str = ""):
        super().__init__()

        self.verify = certifi.where()
        try:
            self.max_redirects = 10
        except Exception:
            pass

        self._legacy_server = int(server or 0)
        self._legacy_app_key = (app_key or "").strip()
        self._legacy_redirect = (redirect or "").strip()

        # session state
        self.access_token: str | None = None
        self.connected: bool = False
        self.last_probe_http: int | None = None
        self.last_probe_detail: str = ""
        self.username: str = ""
        self._oauth_code: str = ""
        self._oauth_error: str = ""

        self.oauth_scope: str = (
            os.environ.get("GRAMPS_FS_OAUTH_SCOPE", "").strip()
            or str(_cfg_get("familysearch.scope", "") or "").strip()
            or "profile email qualifies_for_affiliate_account country"
        )
        self.listen_timeout: int = int(
            os.environ.get("GRAMPS_FS_LISTENER_TIMEOUT", "300") or "300"
        )

        self.status_indicator = FSStatusIndicator()
        self._set_status("DISCONNECTED", "No token yet")
        self._tools_window_shown = False

        env = (
            os.environ.get("GRAMPS_FS_ENV", "").strip().lower()
            or str(_cfg_get("familysearch.env", "") or "").strip().lower()
        )
        if env not in (ENV_BETA, ENV_PROD):
            legacy_server = _cfg_get("familysearch.server", None)
            if legacy_server is None:
                legacy_server = self._legacy_server
            env = ENV_BETA if int(legacy_server or 0) == 0 else ENV_PROD

        self._auth_method_chosen: str = (
            os.environ.get("GRAMPS_FS_AUTH_METHOD", "").strip().lower()
            or str(_cfg_get("familysearch.auth_method", AUTH_AUTO) or AUTH_AUTO)
            .strip()
            .lower()
        )
        if self._auth_method_chosen not in (
            AUTH_AUTO,
            AUTH_WEBKIT,
            AUTH_LOOPBACK,
            AUTH_MANUAL,
        ):
            self._auth_method_chosen = AUTH_AUTO

        self._beta_profile = self._build_profile(ENV_BETA)
        self._prod_profile = self._build_profile(ENV_PROD)
        self._profile: EnvProfile = (
            self._beta_profile if env == ENV_BETA else self._prod_profile
        )
        self._apply_profile(self._profile, clear_state=False)

        # Listener plumbing
        self.listen_host = "127.0.0.1"
        self.listen_port = 57938
        self.listen_path = "/familysearch-auth"
        self.listener: Listener | None = None
        self._recompute_listener()

        global GLOBAL_SESSION, SESSION
        GLOBAL_SESSION = self
        SESSION = self
        try:
            Session._shared = self
            Session._last_instance = self
        except Exception:
            pass

        _dbg(
            "Session initialized: "
            f"env={self._profile.env} fs_url={self.fs_url} ident_url={self.ident_url} api_url={self.api_url} "
            f"app_key={_mask(self.app_key)} redirect={self.redirect} scope={self.oauth_scope!r}"
        )

        if os.environ.get("GRAMPS_FS_SHOW_STATUS", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            try:
                self.status_indicator.show_window(parent=self._get_parent_window())
            except Exception as e:
                _dbg(f"status window: failed to show: {type(e).__name__}: {e}")

    @classmethod
    def from_config(cls) -> "Session":
        legacy_server = _cfg_get("familysearch.server", 0)
        legacy_app = _cfg_get("familysearch.app-key", "") or ""
        legacy_redirect = (
            _cfg_get("familysearch.redirect", "") or DEFAULT_LOOPBACK_REDIRECT
        )
        return cls(int(legacy_server or 0), str(legacy_app), str(legacy_redirect))

    def _build_profile(self, env: str) -> EnvProfile:
        env = (env or "").strip().lower()
        if env not in (ENV_BETA, ENV_PROD):
            env = ENV_PROD

        if env == ENV_BETA:
            fs_url = "https://beta.familysearch.org/"
            ident_url = "https://identbeta.familysearch.org/"
            api_url = "https://apibeta.familysearch.org/"
            web_url = "https://beta.familysearch.org/"

            app_key = (
                os.environ.get("GRAMPS_FS_BETA_APP_KEY", "").strip()
                or str(_cfg_get("familysearch.beta.app_key", "") or "").strip()
                or str(_cfg_get("familysearch.app-key", "") or "").strip()
                or (self._legacy_app_key if int(self._legacy_server or 0) == 0 else "")
            ).strip()

            redirect = (
                os.environ.get("GRAMPS_FS_BETA_REDIRECT", "").strip()
                or str(_cfg_get("familysearch.beta.redirect", "") or "").strip()
                or str(_cfg_get("familysearch.redirect", "") or "").strip()
                or (self._legacy_redirect or "")
                or DEFAULT_LOOPBACK_REDIRECT
            ).strip()

            return EnvProfile(
                env=ENV_BETA,
                fs_url=fs_url,
                ident_url=ident_url,
                api_url=api_url,
                web_url=web_url,
                app_key=app_key,
                redirect=redirect,
            )

        # prod
        fs_url = "https://www.familysearch.org/"
        ident_url = "https://ident.familysearch.org/"
        api_url = "https://api.familysearch.org/"
        web_url = "https://www.familysearch.org/"

        app_key = (
            os.environ.get("GRAMPS_FS_PROD_APP_KEY", "").strip()
            or str(_cfg_get("familysearch.prod.app_key", "") or "").strip()
            or str(_cfg_get("familysearch.app-key", "") or "").strip()
            or (self._legacy_app_key if int(self._legacy_server or 0) != 0 else "")
        ).strip()

        redirect = (
            os.environ.get("GRAMPS_FS_PROD_REDIRECT", "").strip()
            or str(_cfg_get("familysearch.prod.redirect", "") or "").strip()
            or str(_cfg_get("familysearch.redirect", "") or "").strip()
            or (self._legacy_redirect or "")
            or DEFAULT_LOOPBACK_REDIRECT
        ).strip()

        return EnvProfile(
            env=ENV_PROD,
            fs_url=fs_url,
            ident_url=ident_url,
            api_url=api_url,
            web_url=web_url,
            app_key=app_key,
            redirect=redirect,
        )

    def _apply_profile(self, prof: EnvProfile, clear_state: bool = True) -> None:
        self._profile = prof
        self.fs_url = prof.fs_url
        self.ident_url = prof.ident_url
        self.api_url = prof.api_url
        self.web_url = prof.web_url
        self.app_key = (prof.app_key or "").strip()
        self.redirect = (prof.redirect or "").strip()

        if clear_state:
            try:
                self.cookies.clear()
            except Exception:
                pass
            self.access_token = None
            self.connected = False
            self.last_probe_http = None
            self.last_probe_detail = ""
            self._oauth_code = ""
            self._oauth_error = ""
            self._set_status("DISCONNECTED", "Switched environment; no token")

        self._recompute_listener()

    @staticmethod
    def _response_is_empty_json(r: requests.Response) -> bool:
        try:
            if r.status_code == 204:
                return True
            clen = r.headers.get("Content-Length")
            if clen is not None and clen.strip() == "0":
                return True
            if not r.content:
                return True
            if hasattr(r, "text") and (r.text is None or r.text.strip() == ""):
                return True
        except Exception:
            pass
        return False

    def _get_parent_window(self) -> Gtk.Window | None:
        try:
            wins = Gtk.Window.list_toplevels() or []
            for w in wins:
                try:
                    if w.get_visible():
                        return w
                except Exception:
                    continue
            return wins[0] if wins else None
        except Exception:
            return None

    def get_status_widget(self) -> Gtk.Widget:
        return self.status_indicator.create_widget()

    def _set_status(self, state: str, detail: str = "", http: int | None = None):
        _dbg(f"status: {state} detail={_safe(detail, 220)} http={http}")
        try:
            if threading.current_thread() is threading.main_thread():
                self.status_indicator.set(state, detail=detail, http=http)
            else:
                GLib.idle_add(self.status_indicator.set, state, detail, http)
        except Exception:
            self.status_indicator.set(state, detail=detail, http=http)

    def _fs_join(self, base: str, path: str) -> str:
        return base.rstrip("/") + "/" + str(path).lstrip("/")

    def _ensure_api_headers(self, kwargs):
        headers = kwargs.setdefault("headers", {})
        if not any(k.lower() == "accept" for k in headers.keys()):
            headers["accept"] = "application/x-fs-v1+json"
        if self.access_token and not any(
            k.lower() == "authorization" for k in headers.keys()
        ):
            headers["authorization"] = "Bearer " + self.access_token

    def get(self, url, **kwargs):
        if not str(url).startswith("http"):
            url = self._fs_join(self.api_url, str(url))
            self._ensure_api_headers(kwargs)
        _dbg(f"GET {url}")
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        if not str(url).startswith("http"):
            url = self._fs_join(self.api_url, str(url))
            self._ensure_api_headers(kwargs)
        _dbg(f"POST {url}")
        return super().post(url, **kwargs)

    def head(self, url, **kwargs):
        if not str(url).startswith("http"):
            url = self._fs_join(self.api_url, str(url))
            self._ensure_api_headers(kwargs)
        _dbg(f"HEAD {url}")
        return super().head(url, **kwargs)

    def write_log(self, text: str) -> None:
        _dbg(str(text))

    def get_url(self, url, headers=None, params=None):
        return self.get(url, headers=headers or {}, params=params)

    def post_url(self, url, data=None, headers=None):
        return self.post(url, data=data, headers=headers or {})

    def head_url(self, url, headers=None):
        return self.head(url, headers=headers or {})

    @property
    def logged(self) -> bool:
        return bool(self.access_token)

    @logged.setter
    def logged(self, _val: bool) -> None:
        return

    @property
    def client_id(self) -> str:
        return self.app_key

    @client_id.setter
    def client_id(self, v: str) -> None:
        self.app_key = (v or "").strip()

    # JSON helper
    def get_jsonurl(self, url: str, headers: dict | None = None):
        """
        Retrieve JSON from a FamilySearch URL.
        Returns:
            dict on success,
            {}   on 204/empty body,
            None on errors/non-JSON/HTTP failure,
            'error' for 403 case
        """
        try:
            r = self.get_url(url, headers=headers)
        except requests.exceptions.RequestException as e:
            self.write_log(
                f"WARNING: request failed for {url}: {type(e).__name__}: {e}"
            )
            return None

        if r is None or r == "error":
            return r

        if isinstance(r, requests.Response) and self._response_is_empty_json(r):
            return {}

        if not isinstance(r, requests.Response):
            return None

        if r.status_code == 401:
            self.write_log(f"WARNING: 401 from {url}")
            return None

        if r.status_code == 403:
            try:
                msg = r.json().get("errors", [{}])[0].get("message")
            except Exception:
                msg = None
            if msg == "Unable to get ordinances.":
                return "error"

        if r.status_code >= 400:
            self.write_log(
                f"WARNING: HTTP {r.status_code} from {url}: {_safe(getattr(r, 'text', ''), 200)}"
            )
            return None

        try:
            return r.json()
        except Exception as e:
            body_preview = ""
            try:
                body_preview = _safe(getattr(r, "text", ""), 220)
            except Exception:
                pass
            self.write_log(
                f"WARNING: JSON decode failed from {url}: {type(e).__name__}: {e}"
                + (f"; body preview: {body_preview}" if body_preview else "")
            )
            return None

    # listener plumbing
    def _recompute_listener(self) -> None:
        r = urlparse(self.redirect or DEFAULT_LOOPBACK_REDIRECT)
        self.listen_timeout = int(
            os.environ.get("GRAMPS_FS_LISTENER_TIMEOUT", str(self.listen_timeout))
            or str(self.listen_timeout)
        )
        if r.hostname in ("127.0.0.1", "localhost"):
            self.listen_host = r.hostname or "127.0.0.1"
            self.listen_port = r.port or 57938
            self.listen_path = r.path or "/familysearch-auth"
        else:
            # not used unless loopback redirect
            self.listen_host = "127.0.0.1"
            self.listen_port = 57938
            self.listen_path = "/familysearch-auth"

    def listen(self) -> str:
        while self.listener and self.listener.is_alive():
            time.sleep(0.1)
            while Gtk.events_pending():
                Gtk.main_iteration()

        _dbg(
            f"listen(): finished. error={getattr(self.listener, 'error', None)!r} "
            f"result={getattr(self.listener, 'result', None)!r}"
        )

        if self.listener and self.listener.error:
            self._set_status("ERROR", self.listener.error)
            return ""

        if self.listener and (
            "error_description" in self.listener.result
            or "error" in self.listener.result
        ):
            err = self.listener.result.get("error", "")
            desc = self.listener.result.get("error_description", "")
            self._set_status("ERROR", f"{err} {desc}".strip())
            return ""

        return self.listener.result.get("code", "") if self.listener else ""

    def canonical_web_url(self, url: str) -> str:
        """
        For PROD only: rewrite beta.familysearch.org links to www.familysearch.org.
        For BETA: do not rewrite.
        """
        if not url:
            return ""
        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            return url
        if getattr(self._profile, "env", ENV_PROD) == ENV_BETA:
            return url
        try:
            p = urlparse(url)
            host = (p.netloc or "").lower()
            if host in (
                "beta.familysearch.org",
                "familysearch.org",
                "www.familysearch.org",
            ):
                return urlunparse(
                    (
                        p.scheme or "https",
                        "www.familysearch.org",
                        p.path or "",
                        p.params or "",
                        p.query or "",
                        p.fragment or "",
                    )
                )
            return url
        except Exception:
            return url

    # tools window auto-open
    def _maybe_show_tools_window(self) -> None:
        try:
            val = os.environ.get("GRAMPS_FS_SHOW_TOOLS", "").strip().lower()
            if val in ("0", "false", "no", "off"):
                return
        except Exception:
            pass
        if getattr(self, "_tools_window_shown", False):
            return
        self._tools_window_shown = True

        def _open():
            try:
                from .tools_window import present_tools_window

                present_tools_window(self)
            except Exception as e:
                _dbg(f"tools window: failed to open: {type(e).__name__}: {e}")
            return False

        try:
            GLib.idle_add(_open)
        except Exception:
            _open()

    # ---- API probe ----
    def probe_api(self, reason: str = "") -> bool:
        if not self.access_token:
            self.connected = False
            self.last_probe_http = None
            self.last_probe_detail = "No access token"
            self._set_status(
                "DISCONNECTED", f"{reason}: no token" if reason else "No token"
            )
            return False

        self._set_status(
            "PROBING",
            (
                f"{reason}: GET platform/users/current"
                if reason
                else "GET platform/users/current"
            ),
        )
        try:
            r = self.get("platform/users/current")
            http = r.status_code
            self.last_probe_http = http
            self.last_probe_detail = (r.reason or "") or "OK"
            if http == 200:
                self.connected = True
                self._set_status("CONNECTED", "API probe OK", http=http)
                self._maybe_show_tools_window()
                return True
            self.connected = False
            self._set_status(
                "API_FAIL",
                _safe(r.text, 200) or (r.reason or "API probe failed"),
                http=http,
            )
            return False
        except Exception as e:
            self.connected = False
            self.last_probe_http = None
            self.last_probe_detail = f"{type(e).__name__}: {e}"
            self._set_status("ERROR", self.last_probe_detail)
            return False

    def get_current_person(self) -> str:
        if not self.access_token:
            raise FSException("Not connected (no access token).")
        r = self.get("platform/tree/current-person")
        return r.text

    def logout(self) -> None:
        self.access_token = None
        try:
            self.cookies.clear()
        except Exception:
            pass
        self.connected = False
        self.last_probe_http = None
        self.last_probe_detail = "Logged out"
        self._set_status("DISCONNECTED", "Logged out")

    # =============================================================================
    #  Auth method selection + entrypoints
    # =============================================================================
    def _available_methods(self) -> list[str]:
        methods = [AUTH_AUTO, AUTH_LOOPBACK, AUTH_MANUAL]
        has_webkit, _ = _try_import_webkit()
        if (not win()) and has_webkit:
            methods.insert(1, AUTH_WEBKIT)
        return methods

    def _choose_effective_method(self, requested: str) -> str:
        requested = (requested or AUTH_AUTO).strip().lower()
        avail = self._available_methods()
        if requested != AUTH_AUTO and requested in avail:
            return requested

        has_webkit, _ = _try_import_webkit()
        if (not win()) and has_webkit:
            return AUTH_WEBKIT

        # windows/no-webkit:
        if _is_loopback_redirect(self.redirect or ""):
            return AUTH_LOOPBACK
        return AUTH_MANUAL

    def login(self, username: str = "", password: str = "") -> bool:
        """
        do NOT do cookie-login by default (prevents double sign-in attempts)
        """
        self.username = username or self.username or ""
        return True

    def authorize(self, username: str | None = None, *args, **kwargs) -> str:
        """
        Legacy authorization entrypoint
        """
        if username:
            self.username = username

        forced_env = os.environ.get("GRAMPS_FS_ENV", "").strip().lower()
        env = forced_env or str(_cfg_get("familysearch.env", "") or "").strip().lower()
        if env not in (ENV_BETA, ENV_PROD):
            legacy_server = _cfg_get("familysearch.server", self._legacy_server)
            env = ENV_BETA if int(legacy_server or 0) == 0 else ENV_PROD

        prof = self._beta_profile if env == ENV_BETA else self._prod_profile
        self._apply_profile(prof, clear_state=True)

        if not (self.app_key or "").strip():
            self._set_status(
                "ERROR",
                "No app key (client_id) configured (Integrations ? FamilySearch ? App key)",
            )
            return ""
        if not (self.redirect or "").strip():
            self._set_status(
                "ERROR",
                "No redirect configured (Integrations ? FamilySearch ? Redirect URL)",
            )
            return ""

        forced_method = os.environ.get("GRAMPS_FS_AUTH_METHOD", "").strip().lower()
        req_method = forced_method or self._auth_method_chosen or AUTH_AUTO
        effective = self._choose_effective_method(req_method)
        return self._oauth_authorize_only(effective) or ""

    def get_token(self, auth_code: str) -> bool:
        if not auth_code:
            self._set_status("ERROR", "No auth code")
            return False
        try:
            self._set_status("EXCHANGING_TOKEN", "Posting code to token endpoint")
            url = urljoin(self.ident_url, "cis-web/oauth2/v3/token")
            payload = {
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect,
                "client_id": self.app_key,
                "code": auth_code,
            }
            headers = {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            }
            _dbg(f"POST {url}")
            r = super().post(url, data=payload, headers=headers, verify=self.verify)

            try:
                data = r.json()
            except Exception as e:
                self._set_status(
                    "ERROR", "Token endpoint returned non-JSON", http=r.status_code
                )
                _dbg(
                    f"get_token(): non-JSON: {type(e).__name__}: {e} body={_safe(getattr(r, 'text', ''), 900)}"
                )
                return False

            if isinstance(data, dict) and data.get("access_token"):
                self.access_token = data["access_token"]
                self._set_status("TOKEN_ACQUIRED", "Access token acquired")
                try:
                    self.probe_api(reason="post-token")
                except Exception as e:
                    _dbg(f"get_token(): probe_api failed: {type(e).__name__}: {e}")
                return True

            err = data.get("error") if isinstance(data, dict) else "token_error"
            desc = (
                (data.get("error_description") or data.get("message"))
                if isinstance(data, dict)
                else ""
            )
            self._set_status("ERROR", f"{err} {desc}".strip(), http=r.status_code)
            _dbg(
                f"get_token(): failed HTTP {r.status_code}: error={err!r} desc={desc!r}"
            )
            return False
        except Exception as e:
            _dbg(f"get_token(): exception: {type(e).__name__}: {e}")
            self._set_status("ERROR", f"token exchange failed: {type(e).__name__}")
            return False

    # ========================
    #  OAuth attempt
    # ========================
    def _auth_url(self, state: str) -> str:
        base = urljoin(self.ident_url, "cis-web/oauth2/v3/authorization")
        params = {
            "client_id": self.app_key,
            "redirect_uri": self.redirect,
            "response_type": "code",
            "scope": self.oauth_scope,
            "state": state,
        }
        # prefill username if provided
        if self.username:
            params["username"] = self.username
        return base + "?" + urlencode(params)

    def _oauth_authorize_only(self, method: str) -> str:
        method = (method or AUTH_AUTO).strip().lower()

        # - Windows: loopback uses system browser
        # - Linux: loopback requires WebKit
        if method == AUTH_LOOPBACK and not _is_loopback_redirect(self.redirect):
            self._set_status(
                "ERROR", "Loopback method requires a localhost redirect URL"
            )
            return ""

        state = secrets.token_urlsafe(18)
        auth_url = self._auth_url(state)
        self._oauth_code = ""
        self._oauth_error = ""

        if _is_loopback_redirect(self.redirect):
            return self._authorize_via_loopback(auth_url)
        if method == AUTH_WEBKIT and (not win()):
            return self._authorize_via_webkit_capture(auth_url, expected_state=state)
        return self._authorize_via_manual(auth_url)

    def _authorize_via_loopback(self, auth_url: str) -> str:
        self._set_status("AUTHORIZING", "Preparing loopback listener")
        self._recompute_listener()
        self.listener = Listener(
            self.listen_host, self.listen_port, self.listen_path, self.listen_timeout
        )
        self.listener.start()

        if win():
            self._set_status(
                "AUTHORIZING", "Opening system browser (loopback callback)"
            )
            webbrowser.open(auth_url, new=1, autoraise=True)
            code = self.listen().strip()
            if code:
                self._set_status("AUTH_CODE_RECEIVED", "Auth code received")
            return code

        # Linux: require WebKit so loopback does not force system browser
        has_webkit, _ = _try_import_webkit()
        if not has_webkit:
            self._set_status(
                "ERROR",
                "Loopback on Linux requires WebKit2GTK. Use manual method or install WebKit2GTK.",
            )
            return ""

        ok = self._open_auth_ui(auth_url, capture_code=False, expected_state="")
        if not ok:
            self._set_status(
                "ERROR",
                "Loopback on Linux requires WebKit2GTK. Use manual method or install WebKit2GTK.",
            )
            return ""

        code = self.listen().strip()
        if code:
            self._set_status("AUTH_CODE_RECEIVED", "Auth code received")
        self._close_auth_window()
        return code

    def _authorize_via_webkit_capture(self, auth_url: str, expected_state: str) -> str:
        if win():
            self._set_status("ERROR", "WebKit method is not available on Windows")
            return ""
        ok = self._open_auth_ui(
            auth_url, capture_code=True, expected_state=expected_state
        )
        if not ok:
            self._set_status(
                "ERROR", "Embedded WebKit not available; use manual method"
            )
            return ""

        self._set_status("AUTHORIZING", "Waiting for redirect (capturing code)")
        deadline = time.time() + self.listen_timeout
        while time.time() < deadline and not self._oauth_code and not self._oauth_error:
            time.sleep(0.1)
            while Gtk.events_pending():
                Gtk.main_iteration()

        if self._oauth_error:
            self._set_status("ERROR", self._oauth_error)
            self._close_auth_window()
            return ""

        if not self._oauth_code:
            self._set_status("ERROR", "Timed out waiting for redirect")
            self._close_auth_window()
            return ""

        self._set_status("AUTH_CODE_RECEIVED", "Auth code received")
        self._close_auth_window()
        return self._oauth_code

    def _authorize_via_manual(self, auth_url: str) -> str:
        self._set_status("AUTHORIZING", "Opening system browser (manual code entry)")
        webbrowser.open(auth_url, new=1, autoraise=True)

        dlg = Gtk.Dialog(
            title="FamilySearch: Paste authorization code",
            transient_for=self._get_parent_window(),
            flags=0,
        )
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Continue", Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_border_width(10)
        box.set_spacing(10)

        msg = (
            "After approving access in your browser, paste the authorization code here.\n"
            "You may paste either:\n"
            "  - the CODE itself, or\n"
            "  - the full redirect URL containing ?code=...\n"
        )
        lab = Gtk.Label(label=msg)
        lab.set_xalign(0.0)
        lab.set_line_wrap(True)

        entry = Gtk.Entry()
        entry.set_activates_default(True)
        dlg.set_default_response(Gtk.ResponseType.OK)

        box.pack_start(lab, False, False, 0)
        box.pack_start(entry, False, False, 0)
        dlg.show_all()

        resp = dlg.run()
        text = entry.get_text()
        dlg.destroy()

        if resp != Gtk.ResponseType.OK:
            self._set_status("ERROR", "Manual login cancelled")
            return ""

        code = _extract_code_from_text(text)
        if not code:
            self._set_status("ERROR", "No code provided")
            return ""

        self._set_status("AUTH_CODE_RECEIVED", "Auth code received")
        return code

    # =======================================================
    #  Embedded WebKit UI (used for capture + Linux loopback)
    # =======================================================
    def _close_auth_window(self) -> None:
        win_ = getattr(self, "_auth_win", None)
        if win_ is not None:
            try:
                win_.destroy()
            except Exception:
                pass
        setattr(self, "_auth_win", None)

    def _open_auth_ui(
        self, auth_url: str, capture_code: bool = False, expected_state: str = ""
    ) -> bool:
        has_webkit, WebKit2 = _try_import_webkit()
        if not has_webkit or WebKit2 is None:
            return False
        if win():
            return False

        win_ = Gtk.Window(title="FamilySearch Login")
        win_.set_default_size(920, 920)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.pack_start(self.get_status_widget(), False, False, 0)

        web = WebKit2.WebView()
        sc = Gtk.ScrolledWindow()
        sc.add(web)
        outer.pack_start(sc, True, True, 0)
        win_.add(outer)

        handler_ids = {"decide": None, "load": None}

        def _shutdown_now():
            try:
                web.stop_loading()
            except Exception:
                pass
            try:
                if handler_ids["decide"] is not None:
                    web.disconnect(handler_ids["decide"])
                    handler_ids["decide"] = None
            except Exception:
                pass
            try:
                if handler_ids["load"] is not None:
                    web.disconnect(handler_ids["load"])
                    handler_ids["load"] = None
            except Exception:
                pass
            try:
                win_.destroy()
            except Exception:
                pass
            return False

        def _capture_from_uri(uri: str) -> None:
            try:
                q = urlparse(uri).query
                qs = parse_qs(q, keep_blank_values=True)
                got_state = qs.get("state", [""])[0] or ""
                if expected_state and got_state and got_state != expected_state:
                    self._oauth_error = (
                        "State mismatch (possible stale/duplicate redirect)"
                    )
                    self._oauth_code = ""
                    _dbg(
                        f"auth ui: state mismatch expected={expected_state!r} got={got_state!r}"
                    )
                    return
                if "error" in qs or "error_description" in qs:
                    self._oauth_error = (
                        qs.get("error_description", [""])[0] or qs.get("error", [""])[0]
                    )
                    self._oauth_code = ""
                else:
                    self._oauth_code = qs.get("code", [""])[0] or ""
                    self._oauth_error = ""
                _dbg(
                    f"auth ui: captured code_present={bool(self._oauth_code)} "
                    f"error={_safe(self._oauth_error, 200)}"
                )
            except Exception as e:
                self._oauth_error = f"{type(e).__name__}: {e}"
                self._oauth_code = ""

        def on_decide_policy(view, decision, decision_type):
            try:
                if decision_type not in (
                    WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
                ):
                    return False
                nav = decision.get_navigation_action()
                req = nav.get_request()
                uri = req.get_uri() or ""
                _dbg(f"auth ui: policy uri={_safe(uri, 500)}")
                if capture_code and self.redirect and uri.startswith(self.redirect):
                    _dbg(
                        "auth ui: policy hit redirect uri (capturing code; ignoring navigation)"
                    )
                    _capture_from_uri(uri)
                    try:
                        decision.ignore()
                    except Exception:
                        pass
                    GLib.idle_add(_shutdown_now)
                    return True
                try:
                    decision.use()
                    return True
                except Exception:
                    return False
            except Exception:
                return False

        def on_load_changed(view, load_event):
            try:
                if load_event == WebKit2.LoadEvent.COMMITTED:
                    uri = view.get_uri() or ""
                    _dbg(f"auth ui: committed uri={_safe(uri, 500)}")
            except Exception:
                pass

        handler_ids["decide"] = web.connect("decide-policy", on_decide_policy)
        handler_ids["load"] = web.connect("load-changed", on_load_changed)

        def _on_destroy(*_a):
            try:
                if getattr(self, "_auth_win", None) is win_:
                    setattr(self, "_auth_win", None)
            except Exception:
                pass

        win_.connect("destroy", _on_destroy)
        setattr(self, "_auth_win", win_)
        win_.show_all()
        web.load_uri(auth_url)
        return True


def get_active_session():
    return (
        GLOBAL_SESSION
        or SESSION
        or getattr(Session, "_shared", None)
        or getattr(Session, "_last_instance", None)
    )

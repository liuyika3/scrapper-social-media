# Local web UI for Apify TikTok creator screener.
# Run from repo root: python tools/creator_screener_server.py
# Open http://127.0.0.1:5180/

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import smtplib
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "creator_screener_index.html"
PORT = int(os.environ.get("PORT", "5180"))
MAX_HANDLES = 60
DATA_DIR = ROOT.parent
GOOGLE_ACCOUNTS_PATH = DATA_DIR / ".google-accounts.json"
FAVORITES_PATH = DATA_DIR / ".favorites.json"
_state_lock = threading.Lock()


# ---------- Persistence helpers ----------
def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_google_accounts() -> dict:
    return _load_json(GOOGLE_ACCOUNTS_PATH, {"activeAccount": None, "accounts": {}})


def _save_google_accounts(data: dict) -> None:
    _save_json(GOOGLE_ACCOUNTS_PATH, data)


def _load_favorites() -> dict:
    return _load_json(FAVORITES_PATH, {})


def _save_favorites(data: dict) -> None:
    _save_json(FAVORITES_PATH, data)


# ---------- Gmail OAuth ----------
def _exchange_code(code: str, redirect_uri: str, client_id: str, client_secret: str) -> dict:
    payload = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("token exchange failed: %s" % e.read().decode("utf-8", errors="replace"))


def _refresh_token(refresh_token: str, client_id: str, client_secret: str) -> dict:
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("token refresh failed: %s" % e.read().decode("utf-8", errors="replace"))


def _get_valid_access_token():
    """Returns (access_token, email) or (None, email_or_None) if not authenticated."""
    with _state_lock:
        data = _load_google_accounts()
        email = data.get("activeAccount")
        if not email:
            return None, None
        acct = data.get("accounts", {}).get(email) or {}
        tok = acct.get("token") or {}
        if not tok.get("access_token"):
            return None, email
        now = int(time.time())
        if tok.get("expires_at") and now < tok["expires_at"] - 120:
            return tok["access_token"], email
        if not tok.get("refresh_token"):
            return None, email
        try:
            new_tok = _refresh_token(tok["refresh_token"], acct.get("clientId", ""), acct.get("clientSecret", ""))
        except Exception:
            # refresh broke (invalid_grant etc) — clear access_token, keep client config
            acct["token"] = {**tok, "access_token": ""}
            _save_google_accounts(data)
            return None, email
        merged = {**tok, **new_tok, "expires_at": now + int(new_tok.get("expires_in", 0))}
        acct["token"] = merged
        _save_google_accounts(data)
        return merged["access_token"], email


def _gmail_api_send(to_addr: str, subject: str, body_html: str, *, plain: bool = False, sender_name: str = "") -> str:
    access_token, email = _get_valid_access_token()
    if not access_token:
        raise RuntimeError("AUTH_REQUIRED: Gmail token 失效或未授权，请到 UI 顶部点 🔑 用 Gmail 授权 重新登录")
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, email)) if sender_name else email
    msg["To"] = to_addr
    msg["Subject"] = subject
    if plain:
        msg.set_content(body_html)
    else:
        msg.set_content("This email is HTML — please view in an HTML-capable client.")
        msg.add_alternative(body_html, subtype="html")
    raw = base64.urlsafe_b64encode(bytes(msg)).decode("ascii").rstrip("=")
    payload = json.dumps({"raw": raw}).encode("utf-8")
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=payload,
        headers={"Authorization": "Bearer " + access_token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            return r.get("id", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Gmail API send failed: %s" % e.read().decode("utf-8", errors="replace"))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apify_creator_screener as _aps  # noqa: E402
from apify_creator_screener import (  # noqa: E402
    _parse_handles,
    ai_recommend_for_creator,
    apify_run_actor,
    fetch_comments,
    screen_creators,
)

# Startup guard: refuse to launch if the obsolete (404-returning) wait-for-finish
# URL pattern leaked back into apify_creator_screener.py. Inspect the actual
# function source so we don't false-positive on comments elsewhere.
_APS_SOURCE = inspect.getsource(apify_run_actor)
if "/wait-for-finish?" in _APS_SOURCE or '"/wait-for-finish?' in _APS_SOURCE:
    sys.stderr.write(
        "ERROR: tools/apify_creator_screener.py is an OLD copy (calls removed Apify URL).\n"
        "Save the latest file from this project, or delete any duplicate folder you run from.\n"
        "Loaded from: %s\n" % (Path(apify_run_actor.__code__.co_filename).resolve(),)
    )
    raise SystemExit(2)
if "waitForFinish" not in _APS_SOURCE:
    sys.stderr.write("ERROR: apify_run_actor missing waitForFinish polling.\n")
    raise SystemExit(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, code: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, code: int, ctype: str, raw: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not INDEX.exists():
                return self._send_json(
                    500,
                    {"ok": False, "error": "missing tools/creator_screener_index.html"},
                )
            return self._send_bytes(200, "text/html; charset=utf-8", INDEX.read_bytes())
        if path.startswith("/api/health"):
            return self._send_json(200, {"ok": True})
        if path.startswith("/api/auth/status"):
            return self._handle_auth_status()
        if path.startswith("/api/favorites"):
            return self._handle_favorites_list()
        if path.startswith("/oauth/callback"):
            return self._handle_oauth_callback()
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/comments"):
            return self._handle_comments()
        if self.path.startswith("/api/ai-recommend"):
            return self._handle_ai_recommend()
        if self.path.startswith("/api/send-mail"):
            return self._handle_send_mail()
        if self.path.startswith("/api/auth/config"):
            return self._handle_auth_config()
        if self.path.startswith("/api/auth/logout"):
            return self._handle_auth_logout()
        if self.path.startswith("/api/favorites/upsert"):
            return self._handle_fav_upsert()
        if self.path.startswith("/api/favorites/remove"):
            return self._handle_fav_remove()
        if self.path.startswith("/api/favorites/status"):
            return self._handle_fav_status()
        if self.path.startswith("/api/favorites/save-ai"):
            return self._handle_fav_save_ai()
        if not self.path.startswith("/api/screen"):
            return self._send_json(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})

        token = (payload.get("apifyToken") or "").strip()
        raw_handles = (payload.get("handles") or "").strip()
        keywords = (payload.get("keywords") or "").strip()
        if not token:
            return self._send_json(400, {"ok": False, "error": "apifyToken required"})

        handles: list = []
        if raw_handles:
            lines = [ln for ln in raw_handles.splitlines() if ln.strip()]
            handles = _parse_handles(None, lines)
            if not handles:
                return self._send_json(400, {"ok": False, "error": "no valid handles"})
            if len(handles) > MAX_HANDLES:
                return self._send_json(
                    400,
                    {"ok": False, "error": "too many handles (max %s)" % MAX_HANDLES},
                )
        else:
            if not keywords:
                return self._send_json(
                    400,
                    {"ok": False, "error": "either handles or keywords (for discovery) required"},
                )

        threshold = payload.get("threshold") or "tiktok"
        if threshold not in ("tiktok", "other"):
            return self._send_json(400, {"ok": False, "error": "threshold must be tiktok or other"})

        try:
            result = screen_creators(
                token,
                handles,
                actor=(payload.get("actor") or "").strip() or None,
                search_actor=(payload.get("searchActor") or "").strip() or None,
                results_per_page=int(payload.get("resultsPerPage") or 80),
                days=int(payload.get("days") or 30),
                min_followers=int(payload.get("minFollowers") or 1000),
                threshold=threshold,
                chunk_size=int(payload.get("chunkSize") or 25),
                wait_secs=int(payload.get("waitSecs") or 900),
                keywords=keywords or None,
                discover_limit=int(payload.get("discoverLimit") or 30),
                discover_results_per_page=int(payload.get("discoverResultsPerPage") or 60),
                download_top_video=bool(payload.get("downloadTopVideo")),
                video_actor=(payload.get("videoActor") or "").strip() or None,
                platform=(payload.get("platform") or "tiktok").strip(),
                fetch_top_comments=payload.get("fetchTopComments", True),
                comments_per_post=int(payload.get("commentsPerPost") or 20),
            )
        except ValueError as e:
            return self._send_json(400, {"ok": False, "error": str(e)})
        except SystemExit as e:
            return self._send_json(502, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send_json(
                502,
                {"ok": False, "error": str(e), "trace": traceback.format_exc()},
            )

        rows = result.get("rows") or []
        return self._send_json(
            200,
            {
                "ok": True,
                "rows": rows,
                "count": len(rows),
                "discovery": result.get("discovery"),
            },
        )


    def _handle_comments(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        token = (payload.get("apifyToken") or "").strip()
        urls = payload.get("videoUrls") or []
        if not token:
            return self._send_json(400, {"ok": False, "error": "apifyToken required"})
        if not isinstance(urls, list) or not urls:
            return self._send_json(400, {"ok": False, "error": "videoUrls list required"})
        urls = [u for u in urls if isinstance(u, str) and u.strip()][:20]
        try:
            data = fetch_comments(
                token,
                urls,
                comments_actor=(payload.get("commentsActor") or "").strip() or None,
                per_post=int(payload.get("perPost") or 20),
                wait_secs=int(payload.get("waitSecs") or 300),
            )
        except SystemExit as e:
            return self._send_json(502, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send_json(502, {"ok": False, "error": str(e), "trace": traceback.format_exc()})
        return self._send_json(200, {"ok": True, "commentsByUrl": data})


    def _handle_ai_recommend(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        api_key = (payload.get("geminiKey") or "").strip()
        creator = payload.get("creator") or {}
        if not api_key:
            return self._send_json(400, {"ok": False, "error": "geminiKey required"})
        if not creator or not creator.get("username"):
            return self._send_json(400, {"ok": False, "error": "creator data required"})
        model = (payload.get("model") or "").strip() or None
        user_context = (payload.get("userContext") or "").strip()
        prompt_template = payload.get("promptTemplate") or None
        try:
            data = ai_recommend_for_creator(
                api_key, creator,
                model=model,
                user_context=user_context,
                prompt_template=prompt_template,
            )
        except ValueError as e:
            return self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send_json(502, {"ok": False, "error": str(e)})
        return self._send_json(200, {"ok": True, "advice": data})


    # ---------- OAuth Gmail ----------
    def _oauth_redirect_uri(self):
        host_header = self.headers.get("Host") or ("127.0.0.1:%d" % PORT)
        # Always force 127.0.0.1 to match Cloud Console redirect URI
        host_only = host_header.split(":")[0]
        port_part = host_header.split(":")[1] if ":" in host_header else str(PORT)
        if host_only not in ("localhost", "127.0.0.1"):
            host_only = "127.0.0.1"
        return "http://%s:%s/oauth/callback" % (host_only, port_part)

    def _handle_auth_status(self):
        with _state_lock:
            data = _load_google_accounts()
            email = data.get("activeAccount")
            if not email or email not in data.get("accounts", {}):
                return self._send_json(200, {"authenticated": False})
            tok = data["accounts"][email].get("token") or {}
            has_access = bool(tok.get("access_token"))
            has_refresh = bool(tok.get("refresh_token"))
            return self._send_json(200, {
                "authenticated": has_access or has_refresh,
                "activeAccount": email,
                "hasRefreshToken": has_refresh,
            })

    def _handle_auth_config(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        email = (payload.get("email") or "").strip()
        client_id = (payload.get("clientId") or "").strip()
        client_secret = (payload.get("clientSecret") or "").strip()
        if not email or "@" not in email:
            return self._send_json(400, {"ok": False, "error": "email required"})
        if not client_id or not client_secret:
            return self._send_json(400, {"ok": False, "error": "clientId + clientSecret required"})
        with _state_lock:
            data = _load_google_accounts()
            data.setdefault("accounts", {})
            acct = data["accounts"].get(email) or {}
            acct["clientId"] = client_id
            acct["clientSecret"] = client_secret
            # When user re-configures, drop any stale token so fresh OAuth runs
            acct["token"] = None
            data["accounts"][email] = acct
            data["activeAccount"] = email
            _save_google_accounts(data)
        redirect_uri = self._oauth_redirect_uri()
        params = urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "access_type": "offline",
            # Force the account chooser AND consent screen, even if signed in elsewhere
            "prompt": "select_account consent",
            "scope": "https://www.googleapis.com/auth/gmail.send",
            "state": email,
            # Pre-suggest the target account so Google highlights/filters to it
            "login_hint": email,
        })
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + params
        return self._send_json(200, {"ok": True, "authUrl": auth_url, "redirectUri": redirect_uri})

    def _handle_auth_logout(self):
        with _state_lock:
            data = _load_google_accounts()
            email = data.get("activeAccount")
            if email and email in data.get("accounts", {}):
                data["accounts"][email]["token"] = None
                _save_google_accounts(data)
        return self._send_json(200, {"ok": True})

    def _handle_oauth_callback(self):
        qs = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(qs))
        code = params.get("code")
        email = params.get("state")
        err = params.get("error")
        if err:
            return self._send_html(400, "<h2>授权失败</h2><pre>%s</pre>" % err)
        if not code or not email:
            return self._send_html(400, "<h2>授权失败</h2><p>缺少 code 或 state</p>")
        with _state_lock:
            data = _load_google_accounts()
            acct = data.get("accounts", {}).get(email)
            if not acct or not acct.get("clientId"):
                return self._send_html(400, "<h2>找不到此邮箱配置</h2>")
            try:
                tok = _exchange_code(code, self._oauth_redirect_uri(), acct["clientId"], acct["clientSecret"])
            except Exception as e:
                return self._send_html(500, "<h2>token 交换失败</h2><pre>%s</pre>" % str(e))
            now = int(time.time())
            acct["token"] = {**tok, "expires_at": now + int(tok.get("expires_in", 0)), "obtained_at": now}
            data["activeAccount"] = email
            data["accounts"][email] = acct
            _save_google_accounts(data)
        html = (
            "<!DOCTYPE html><meta charset='utf-8'>"
            "<title>授权成功</title>"
            "<body style='font-family:system-ui;background:#0a0e16;color:#eef2f8;padding:40px'>"
            "<h2>✅ Gmail 授权成功</h2>"
            "<p>已绑定 <b>%s</b>。本窗口将自动关闭，回到 screener 即可发送邮件。</p>"
            "<script>setTimeout(function(){window.close();},1500)</script></body>"
        ) % email
        return self._send_html(200, html)

    def _send_html(self, code: int, html: str):
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---------- Favorites (backend persistence) ----------
    def _handle_favorites_list(self):
        with _state_lock:
            return self._send_json(200, {"ok": True, "favorites": _load_favorites()})

    def _handle_fav_upsert(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        creator = payload.get("creator") or {}
        username = (creator.get("username") or "").strip()
        if not username:
            return self._send_json(400, {"ok": False, "error": "creator.username required"})
        with _state_lock:
            data = _load_favorites()
            key = username.lower()
            existing = data.get(key) or {}
            now = int(time.time() * 1000)
            data[key] = {
                "creator": creator,
                "status": existing.get("status") or "uncontacted",
                "addedAt": existing.get("addedAt") or now,
                "lastContactedAt": existing.get("lastContactedAt"),
                "aiAdvice": existing.get("aiAdvice"),
                "aiSavedAt": existing.get("aiSavedAt"),
            }
            _save_favorites(data)
        return self._send_json(200, {"ok": True, "favorite": data[key]})

    def _handle_fav_remove(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        username = (payload.get("username") or "").strip()
        if not username:
            return self._send_json(400, {"ok": False, "error": "username required"})
        with _state_lock:
            data = _load_favorites()
            data.pop(username.lower(), None)
            _save_favorites(data)
        return self._send_json(200, {"ok": True})

    def _handle_fav_status(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        username = (payload.get("username") or "").strip()
        status = payload.get("status")
        if not username or status not in ("contacted", "uncontacted"):
            return self._send_json(400, {"ok": False, "error": "username + status (contacted/uncontacted) required"})
        with _state_lock:
            data = _load_favorites()
            key = username.lower()
            if key not in data:
                return self._send_json(404, {"ok": False, "error": "not in favorites"})
            data[key]["status"] = status
            if status == "contacted":
                data[key]["lastContactedAt"] = int(time.time() * 1000)
            _save_favorites(data)
        return self._send_json(200, {"ok": True, "favorite": data[key]})

    def _handle_fav_save_ai(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})
        username = (payload.get("username") or "").strip()
        advice = payload.get("advice")
        creator = payload.get("creator") or {}
        if not username or advice is None:
            return self._send_json(400, {"ok": False, "error": "username + advice required"})
        with _state_lock:
            data = _load_favorites()
            key = username.lower()
            now = int(time.time() * 1000)
            if key not in data:
                # auto-add to favorites if not present
                data[key] = {
                    "creator": creator,
                    "status": "uncontacted",
                    "addedAt": now,
                    "lastContactedAt": None,
                }
            data[key]["aiAdvice"] = advice
            data[key]["aiSavedAt"] = now
            _save_favorites(data)
        return self._send_json(200, {"ok": True, "favorite": data[key]})

    def _handle_send_mail(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})

        recipients_raw = (payload.get("recipients") or "").strip()
        subject = payload.get("subject") or ""
        html_body = payload.get("body") or ""
        plain = bool(payload.get("plainText"))
        sender_name = (payload.get("senderName") or "").strip()
        force_smtp = bool(payload.get("forceSmtp"))
        sender = (payload.get("senderEmail") or "").strip()
        app_pw = (payload.get("appPassword") or "").strip()

        if not recipients_raw:
            return self._send_json(400, {"ok": False, "error": "recipients required"})

        # parse recipients: "Name <email>" or "email", separated by , ; or newline
        addrs: list = []
        for token in re.split(r"[,;\n]+", recipients_raw):
            t = token.strip()
            if not t:
                continue
            name, email = parseaddr(t)
            if not email or "@" not in email:
                continue
            addrs.append((name.strip(), email.strip()))
        if not addrs:
            return self._send_json(400, {"ok": False, "error": "no valid email addresses parsed"})

        # Path A — OAuth Gmail API (preferred, matches GMAIL配置全流程.md)
        if not force_smtp:
            access_token, oauth_email = _get_valid_access_token()
            if access_token:
                results: list = []
                for (nm, em) in addrs:
                    try:
                        msg_id = _gmail_api_send(em, subject, html_body, plain=plain, sender_name=sender_name)
                        results.append({
                            "email": em, "name": nm, "success": True,
                            "message_id": msg_id, "deliveryStatusText": "Gmail API 已受理",
                        })
                    except Exception as e:
                        msg = str(e)
                        if "AUTH_REQUIRED" in msg:
                            return self._send_json(401, {"ok": False, "error": msg})
                        results.append({"email": em, "name": nm, "success": False, "error": msg})
                sent = sum(1 for r in results if r.get("success"))
                return self._send_json(200, {
                    "ok": True, "results": results, "sent": sent, "failed": len(results) - sent,
                    "via": "gmail-api", "from": oauth_email,
                })
            # OAuth not configured — fall through to SMTP if creds provided

        # Path B — SMTP App Password fallback
        if not sender or "@" not in sender:
            return self._send_json(400, {"ok": False, "error": "未授权 Gmail OAuth，且没有提供 SMTP 凭据。请到 UI 顶部点 🔑 用 Gmail 授权 完成 OAuth；或在高级里填发件 Gmail + App Password。"})
        if not app_pw or len(app_pw.replace(" ", "")) < 12:
            return self._send_json(400, {"ok": False, "error": "appPassword 缺失或太短（16 位 Gmail App Password）"})

        # determine SMTP server (default gmail; user can override)
        host = (payload.get("smtpHost") or "smtp.gmail.com").strip()
        try:
            port = int(payload.get("smtpPort") or 465)
        except Exception:
            port = 465

        from_header = formataddr((sender_name or "", sender)) if sender_name else sender
        clean_pw = app_pw.replace(" ", "")

        results: list = []
        smtp = None
        try:
            try:
                smtp = smtplib.SMTP_SSL(host, port, timeout=30)
                smtp.login(sender, clean_pw)
            except smtplib.SMTPAuthenticationError as e:
                return self._send_json(401, {
                    "ok": False,
                    "error": "Gmail rejected login: %s. 检查：1) 是否开启了两步验证 2) 用的是 App Password 不是常用密码 3) Less-secure-apps 已不再支持。" % str(e),
                })
            except Exception as e:
                return self._send_json(502, {"ok": False, "error": "SMTP connect/login failed: %s" % e})

            for (name, email) in addrs:
                try:
                    msg = EmailMessage()
                    msg["From"] = from_header
                    msg["To"] = formataddr((name, email)) if name else email
                    msg["Subject"] = subject
                    if plain:
                        msg.set_content(html_body)
                    else:
                        msg.set_content("This email is HTML — please view in an HTML-capable client.")
                        msg.add_alternative(html_body, subtype="html")
                    smtp.send_message(msg)
                    results.append({
                        "email": email, "name": name, "success": True,
                        "message_id": msg["Message-ID"] or "",
                        "deliveryStatusText": "已交 SMTP",
                    })
                except Exception as e:
                    results.append({"email": email, "name": name, "success": False, "error": str(e)})
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass

        sent = sum(1 for r in results if r.get("success"))
        return self._send_json(
            200,
            {"ok": True, "results": results, "sent": sent, "failed": len(results) - sent},
        )


def _maybe_open_browser(url: str) -> None:
    if os.environ.get("NO_BROWSER", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        threading.Timer(0.6, lambda: webbrowser.open_new_tab(url)).start()
    except Exception as e:
        sys.stderr.write("browser open skipped: %s\n" % e)


def main() -> None:
    try:
        httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        sys.stderr.write(
            "无法绑定端口 %s（可能已有本工具在运行）。关掉旧窗口或执行: set PORT=8088\n%s\n"
            % (PORT, e)
        )
        raise SystemExit(1) from e

    url = "http://127.0.0.1:%s/" % PORT
    print("")
    print("  Creator screener 已启动")
    print("  地址: %s" % url)
    print("  停止: 在本窗口按 Ctrl+C")
    print("  换端口: set PORT=8080 后重新运行")
    print("")

    _maybe_open_browser(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

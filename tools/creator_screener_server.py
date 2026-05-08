# Local web UI for Apify TikTok creator screener.
# Run from repo root: python tools/creator_screener_server.py
# Open http://127.0.0.1:5180/

from __future__ import annotations

import inspect
import json
import os
import re
import smtplib
import sys
import threading
import traceback
import webbrowser
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "creator_screener_index.html"
PORT = int(os.environ.get("PORT", "5180"))
MAX_HANDLES = 60

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
        return self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/comments"):
            return self._handle_comments()
        if self.path.startswith("/api/ai-recommend"):
            return self._handle_ai_recommend()
        if self.path.startswith("/api/send-mail"):
            return self._handle_send_mail()
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


    def _handle_send_mail(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": "invalid json: %s" % e})

        sender = (payload.get("senderEmail") or "").strip()
        app_pw = (payload.get("appPassword") or "").strip()
        recipients_raw = (payload.get("recipients") or "").strip()
        subject = payload.get("subject") or ""
        html_body = payload.get("body") or ""
        plain = bool(payload.get("plainText"))
        sender_name = (payload.get("senderName") or "").strip()

        if not sender or "@" not in sender:
            return self._send_json(400, {"ok": False, "error": "senderEmail required (e.g. you@gmail.com)"})
        if not app_pw or len(app_pw.replace(" ", "")) < 12:
            return self._send_json(400, {"ok": False, "error": "appPassword required (16-char Gmail app password — generate at myaccount.google.com/apppasswords)"})
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

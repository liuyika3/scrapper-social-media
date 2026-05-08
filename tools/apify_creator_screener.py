# Apify TikTok creator screener (stdlib).
# CLI: APIFY_TOKEN=... python tools/apify_creator_screener.py --keywords "fitness, gym"
#  or: APIFY_TOKEN=... python tools/apify_creator_screener.py --handles handles.txt
# Default profile actor: clockworks/tiktok-profile-scraper
# Default search  actor: clockworks/tiktok-scraper

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_PROFILE_ACTOR = "clockworks/tiktok-profile-scraper"
DEFAULT_SEARCH_ACTOR = "clockworks/tiktok-scraper"
DEFAULT_COMMENTS_ACTOR = "clockworks/tiktok-comments-scraper"
DEFAULT_VIDEO_ACTOR = "clockworks/tiktok-scraper"

DEFAULT_IG_PROFILE_ACTOR = "apify/instagram-profile-scraper"
DEFAULT_IG_SEARCH_ACTOR = "apify/instagram-scraper"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _must_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit("Missing environment variable: %s" % name)
    return v


def _http_json(method: str, url: str, payload: Optional[dict] = None, timeout: int = 120) -> Any:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit("HTTP %s %s\n%s" % (e.code, url, body)) from e


def apify_run_actor(token: str, actor_id: str, actor_input: dict, wait_secs: int = 900) -> list:
    q = urllib.parse.urlencode({"token": token})
    run_url = (
        "https://api.apify.com/v2/acts/"
        + urllib.parse.quote(actor_id, safe="")
        + "/runs?"
        + q
    )
    run_json = _http_json("POST", run_url, actor_input or {})
    run_id = run_json.get("data", {}).get("id")
    if not run_id:
        raise SystemExit("Apify: missing run id in response")

    # Poll GET /v2/actor-runs/:id with the waitForFinish query param (max 60s
    # per request). The legacy /wait-for-finish path returns HTTP 404.
    deadline = time.time() + max(30, wait_secs)
    data: dict = {}
    while True:
        if time.time() > deadline:
            raise SystemExit("Apify wait timeout after %ss (run id %s)" % (wait_secs, run_id))
        wf = min(60, max(1, int(deadline - time.time())))
        get_run_url = (
            "https://api.apify.com/v2/actor-runs/"
            + urllib.parse.quote(run_id, safe="")
            + "?"
            + urllib.parse.urlencode({"token": token, "waitForFinish": wf})
        )
        wait_json = _http_json("GET", get_run_url, timeout=wf + 45)
        data = wait_json.get("data", {}) or {}
        status = data.get("status")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    status = data.get("status")
    if status != "SUCCEEDED":
        raise SystemExit("Apify run status=%r run_id=%s" % (status, run_id))

    dataset_id = data.get("defaultDatasetId")
    if not dataset_id:
        raise SystemExit("Apify: missing defaultDatasetId")

    items_url = (
        "https://api.apify.com/v2/datasets/"
        + urllib.parse.quote(dataset_id, safe="")
        + "/items?"
        + urllib.parse.urlencode({"token": token, "clean": "true", "format": "json"})
    )
    out = _http_json("GET", items_url)
    return out if isinstance(out, list) else []


def _parse_handles(path: Optional[Path], inline: list) -> list:
    raw = []
    if path:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            raw.append(s)
    for h in inline:
        s = str(h).strip().lstrip("@")
        if s:
            raw.append(s)
    out = []
    seen = set()
    for h in raw:
        key = h.lstrip("@").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(h.lstrip("@").strip())
    return out


def _num(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _video_ts(item: dict) -> Optional[float]:
    t = item.get("createTimeISO") or item.get("createTime")
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            s = t
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


def _video_cover(item: dict) -> str:
    covers = item.get("covers")
    if isinstance(covers, list) and covers:
        first = covers[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or ""
    if isinstance(covers, str):
        return covers
    vm = item.get("videoMeta") or {}
    if isinstance(vm, dict):
        return vm.get("coverUrl") or vm.get("originalCoverUrl") or vm.get("cover") or ""
    return ""


_CONTACT_RE_EMAIL = None


def _extract_contacts(bio: str) -> dict:
    import re

    global _CONTACT_RE_EMAIL
    if _CONTACT_RE_EMAIL is None:
        _CONTACT_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    text = bio or ""
    emails = list(dict.fromkeys(_CONTACT_RE_EMAIL.findall(text)))
    lower = text.lower()

    handles_ig = re.findall(r"(?:^|\s|\W)(?:ig|insta|instagram)\s*[:：@]?\s*@?([A-Za-z0-9_.]{3,30})", text, re.IGNORECASE)
    handles_yt = re.findall(r"(?:^|\s|\W)(?:yt|youtube)\s*[:：@]?\s*@?([A-Za-z0-9_.\-]{3,30})", text, re.IGNORECASE)
    if "youtube.com" in lower or "youtu.be" in lower:
        handles_yt.append("(youtube link in bio)")

    return {
        "emails": emails,
        "instagram": list(dict.fromkeys(handles_ig)),
        "youtube": list(dict.fromkeys(handles_yt)),
    }


def analyze_profile(
    username: str,
    items: list,
    window_start: float,
    threshold_mode: str,
    min_followers: int,
) -> dict:
    fans = None
    bio = ""
    avatar = ""
    nickname = ""
    verified = False
    region = ""
    total_hearts = 0
    total_videos = 0
    following = 0

    for it in items:
        am = it.get("authorMeta") or {}
        if isinstance(am, dict):
            if fans is None:
                fans = _num(am.get("fans"))
            if not bio:
                bio = (am.get("signature") or am.get("nickName") or "") or ""
            if not avatar:
                avatar = am.get("avatar") or am.get("avatarMedium") or am.get("avatarLarger") or ""
            if not nickname:
                nickname = am.get("nickName") or am.get("name") or ""
            if not verified:
                verified = bool(am.get("verified"))
            if not region:
                region = am.get("region") or ""
            if not total_hearts:
                total_hearts = _num(am.get("heart")) or _num(am.get("heartCount")) or 0
            if not total_videos:
                total_videos = _num(am.get("video")) or _num(am.get("videoCount")) or 0
            if not following:
                following = _num(am.get("following")) or 0

    plays_in_window = []
    texts_recent = []
    last_ts = None
    in_window_videos = []

    for it in items:
        ts = _video_ts(it)
        if ts is None:
            continue
        last_ts = ts if last_ts is None else max(last_ts, ts)
        if ts >= window_start:
            pc = _num(it.get("playCount"))
            if pc is not None:
                plays_in_window.append(pc)
            cap = (it.get("text") or "")[:500]
            if cap:
                texts_recent.append(cap)
            in_window_videos.append({
                "cover": _video_cover(it),
                "plays": pc or 0,
                "likes": _num(it.get("diggCount")) or 0,
                "comments": _num(it.get("commentCount")) or 0,
                "shares": _num(it.get("shareCount")) or 0,
                "url": it.get("webVideoUrl") or "",
                "caption": (it.get("text") or "")[:140],
                "ts_iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            })

    in_window_videos.sort(key=lambda r: r["plays"], reverse=True)
    top_videos = in_window_videos[:4]

    avg_play = (
        sum(plays_in_window) / len(plays_in_window) if plays_in_window else None
    )
    followers = fans or 0
    ratio_needed = 1.0 if threshold_mode == "tiktok" else 0.5
    threshold_views = followers * ratio_needed

    passes_followers = followers > min_followers
    passes_views = (
        avg_play is not None
        and followers > 0
        and avg_play > threshold_views
        and len(plays_in_window) > 0
    )

    last_post_iso = None
    if last_ts is not None:
        last_post_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()

    contacts = _extract_contacts(bio)
    profile_url = "https://www.tiktok.com/@%s" % username.lstrip("@")

    return {
        "username": username.lstrip("@"),
        "nickname": nickname,
        "verified": verified,
        "region": region,
        "avatar": avatar,
        "profile_url": profile_url,
        "followers": followers,
        "following": following,
        "total_hearts": total_hearts,
        "total_videos_lifetime": total_videos,
        "bio": bio.replace("\n", " ")[:300],
        "posts_in_window": len(plays_in_window),
        "avg_play_in_window": round(avg_play, 2) if avg_play is not None else None,
        "threshold_mode": threshold_mode,
        "required_avg_for_rule": round(threshold_views, 2),
        "last_post_at": last_post_iso,
        "pass_min_followers": passes_followers,
        "pass_view_rule": passes_views,
        "pass_all": bool(passes_followers and passes_views),
        "recent_captions_sample": " | ".join(texts_recent[:3])[:400],
        "recent_videos_top": top_videos,
        "contacts": contacts,
    }


def match_keywords(bio: str, captions_blob: str, kws: list) -> bool:
    hay = ("%s\n%s" % (bio, captions_blob)).lower()
    return any(k.strip().lower() in hay for k in kws if k.strip())


def discover_handles(
    token: str,
    keywords: list,
    *,
    search_actor: Optional[str] = None,
    results_per_page: int = 60,
    wait_secs: int = 600,
) -> list:
    """Search TikTok by keywords; return unique handles found in the results."""
    actor_id = (
        search_actor
        or os.environ.get("APIFY_ACTOR_TIKTOK_SEARCH")
        or DEFAULT_SEARCH_ACTOR
    )
    queries = [k.strip() for k in keywords if k.strip()]
    if not queries:
        return []
    inp = {
        "searchQueries": queries,
        "resultsPerPage": max(1, min(int(results_per_page), 200)),
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadVideos": False,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    items = apify_run_actor(token, actor_id, inp, wait_secs=wait_secs)
    handles: list = []
    seen: set = set()
    for it in items:
        am = it.get("authorMeta") or {}
        name = None
        if isinstance(am, dict):
            name = am.get("name") or am.get("uniqueId")
        if not name:
            name = it.get("username") or it.get("uniqueId")
        if isinstance(name, str):
            key = name.lstrip("@").strip().lower()
            if key and key not in seen:
                seen.add(key)
                handles.append(name.lstrip("@").strip())
    return handles


def fetch_comments(
    token: str,
    video_urls: list,
    *,
    comments_actor: Optional[str] = None,
    per_post: int = 20,
    wait_secs: int = 300,
) -> dict:
    """Fetch top comments for each video URL. Returns {url: [{text,likes,user,user_nick,ts}]} sorted by likes desc."""
    if not video_urls:
        return {}
    actor_id = (
        comments_actor
        or os.environ.get("APIFY_ACTOR_TIKTOK_COMMENTS")
        or DEFAULT_COMMENTS_ACTOR
    )
    inp = {
        "postURLs": list(video_urls),
        "commentsPerPost": max(1, min(int(per_post), 100)),
        "proxyConfiguration": {"useApifyProxy": True},
    }
    items = apify_run_actor(token, actor_id, inp, wait_secs=wait_secs)
    by_url: dict = defaultdict(list)
    for it in items:
        url = (
            it.get("videoWebUrl")
            or it.get("postUrl")
            or it.get("postURL")
            or it.get("submittedVideoUrl")
            or ""
        )
        if not url:
            continue
        user = it.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        text = it.get("text") or it.get("comment") or ""
        by_url[url].append({
            "text": str(text).replace("\n", " ")[:500],
            "likes": _num(it.get("diggCount") or it.get("likeCount")) or 0,
            "user": user.get("uniqueId") or it.get("uniqueId") or it.get("authorName") or "",
            "user_nick": user.get("nickname") or it.get("nickname") or "",
            "ts": it.get("createTimeISO") or it.get("createTime") or "",
        })
    out: dict = {}
    for url, comments in by_url.items():
        comments.sort(key=lambda c: c["likes"], reverse=True)
        out[url] = comments[:5]
    return out


def discover_handles_ig(
    token: str,
    keywords: list,
    *,
    search_actor: Optional[str] = None,
    results_limit: int = 60,
    wait_secs: int = 600,
) -> list:
    """Discover IG handles via apify/instagram-scraper.

    Apify's actor accepts either `directUrls` (most reliable) or `searchTerm` + `searchType`.
    We feed both for max compatibility across actor versions.
    """
    actor_id = search_actor or os.environ.get("APIFY_ACTOR_IG_SEARCH") or DEFAULT_IG_SEARCH_ACTOR
    queries = [k.strip().lstrip("#") for k in keywords if k.strip()]
    if not queries:
        return []
    rl = max(10, min(int(results_limit), 200))
    direct_urls = [
        "https://www.instagram.com/explore/tags/" + urllib.parse.quote(q) + "/"
        for q in queries[:3]
    ]
    inp = {
        "directUrls": direct_urls,
        "resultsType": "posts",
        "resultsLimit": rl,
        "addParentData": False,
        # Older / alternate field names — actor ignores unknown keys, so include all
        "searchType": "hashtag",
        "searchTerm": queries[0],
        "search": queries[0],
        "searchLimit": rl,
        "hashtags": queries,
    }
    items = apify_run_actor(token, actor_id, inp, wait_secs=wait_secs)
    handles: list = []
    seen: set = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        u = (
            it.get("ownerUsername")
            or it.get("username")
            or (it.get("owner") or {}).get("username")
            or (it.get("user") or {}).get("username")
            or it.get("uniqueId")
        )
        if isinstance(u, str):
            k = u.strip().lstrip("@").lower()
            if k and k not in seen:
                seen.add(k)
                handles.append(u.strip().lstrip("@"))
    return handles


def analyze_profile_ig(profile: dict, window_start: float, threshold_mode: str, min_followers: int) -> dict:
    username = profile.get("username") or ""
    bio = profile.get("biography") or ""
    avatar = profile.get("profilePicUrlHD") or profile.get("profilePicUrl") or ""
    nickname = profile.get("fullName") or username
    verified = bool(profile.get("isVerified"))
    biz_email = profile.get("businessEmail") or profile.get("publicEmail") or ""
    followers = _num(profile.get("followersCount")) or 0
    following = _num(profile.get("followsCount")) or 0
    posts_count = _num(profile.get("postsCount")) or 0
    external = profile.get("externalUrl") or ""

    posts = profile.get("latestPosts") or []
    plays_in_window: list = []
    in_window_videos: list = []
    last_ts: Optional[float] = None
    texts_recent: list = []

    for p in posts:
        ts_raw = p.get("timestamp") or p.get("takenAtTimestamp")
        ts: Optional[float] = None
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        elif isinstance(ts_raw, (int, float)):
            ts = float(ts_raw)
        if ts is None:
            continue
        last_ts = ts if last_ts is None else max(last_ts, ts)
        if ts >= window_start:
            views = _num(p.get("videoViewCount") or p.get("videoPlayCount"))
            likes = _num(p.get("likesCount")) or 0
            comments = _num(p.get("commentsCount")) or 0
            metric = views if views else (likes * 10)
            if metric:
                plays_in_window.append(metric)
            cap = (p.get("caption") or "")[:500]
            if cap:
                texts_recent.append(cap)
            short = p.get("shortCode") or p.get("code") or ""
            in_window_videos.append({
                "cover": p.get("displayUrl") or p.get("thumbnailUrl") or "",
                "plays": metric,
                "likes": likes,
                "comments": comments,
                "shares": 0,
                "url": ("https://www.instagram.com/p/" + short) if short else "",
                "caption": cap[:140],
                "ts_iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            })

    in_window_videos.sort(key=lambda r: r["plays"], reverse=True)
    top_videos = in_window_videos[:4]
    avg_play = sum(plays_in_window) / len(plays_in_window) if plays_in_window else None
    ratio_needed = 1.0 if threshold_mode == "tiktok" else 0.5
    threshold_views = followers * ratio_needed
    pf = followers > min_followers
    pv = (avg_play is not None and followers > 0 and avg_play > threshold_views and len(plays_in_window) > 0)
    last_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat() if last_ts is not None else None

    contacts = _extract_contacts(bio)
    if biz_email and biz_email not in contacts.get("emails", []):
        contacts["emails"].insert(0, biz_email)
    if external:
        contacts.setdefault("links", []).append(external)

    return {
        "platform": "instagram",
        "username": username,
        "nickname": nickname,
        "verified": verified,
        "region": "",
        "avatar": avatar,
        "profile_url": ("https://www.instagram.com/" + username + "/") if username else "",
        "followers": followers,
        "following": following,
        "total_hearts": 0,
        "total_videos_lifetime": posts_count,
        "bio": bio.replace("\n", " ")[:300],
        "posts_in_window": len(plays_in_window),
        "avg_play_in_window": round(avg_play, 2) if avg_play is not None else None,
        "threshold_mode": threshold_mode,
        "required_avg_for_rule": round(threshold_views, 2),
        "last_post_at": last_iso,
        "pass_min_followers": pf,
        "pass_view_rule": pv,
        "pass_all": bool(pf and pv),
        "recent_captions_sample": " | ".join(texts_recent[:3])[:400],
        "recent_videos_top": top_videos,
        "contacts": contacts,
    }


def _screen_ig(
    token: str,
    handles: list,
    *,
    kws_for_match: list,
    days: int,
    min_followers: int,
    threshold: str,
    wait_secs: int,
    profile_actor: Optional[str],
    search_actor: Optional[str],
    discover_limit: int,
    discover_results_per_page: int,
) -> dict:
    actor_id = profile_actor or os.environ.get("APIFY_ACTOR_IG_PROFILE") or DEFAULT_IG_PROFILE_ACTOR
    discovery_info: Optional[dict] = None
    if not handles:
        if not kws_for_match:
            raise ValueError("must supply handles, or keywords for discovery")
        discovered = discover_handles_ig(token, kws_for_match, search_actor=search_actor, results_limit=discover_results_per_page, wait_secs=wait_secs)
        kept = discovered[: max(1, int(discover_limit))]
        discovery_info = {
            "keywords": kws_for_match, "raw_count": len(discovered), "kept": len(kept),
            "search_actor": search_actor or os.environ.get("APIFY_ACTOR_IG_SEARCH") or DEFAULT_IG_SEARCH_ACTOR,
        }
        handles = kept
        if not handles:
            return {"rows": [], "discovery": discovery_info}

    window_start = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    inp = {"usernames": list(handles)}
    t0 = time.perf_counter()
    items = apify_run_actor(token, actor_id, inp, wait_secs=wait_secs)
    elapsed = time.perf_counter() - t0
    by_user: dict = {}
    for it in items:
        u = it.get("username")
        if isinstance(u, str):
            by_user[u.lower()] = it
    all_rows: list = []
    for h in handles:
        prof = by_user.get(h.lower()) or {"username": h, "latestPosts": []}
        row = analyze_profile_ig(prof, window_start, threshold, min_followers)
        row["keyword_matched"] = (
            match_keywords(row["bio"], row["recent_captions_sample"], kws_for_match) if kws_for_match else None
        )
        row["apify_chunk_sec"] = round(elapsed, 2)
        row["actor"] = actor_id
        all_rows.append(row)
    return {"rows": all_rows, "discovery": discovery_info}


def call_gemini(api_key: str, prompt: str, *, model: Optional[str] = None, timeout: int = 60) -> str:
    """Call Gemini generateContent. Returns the model's text output."""
    if not api_key or not str(api_key).strip():
        raise ValueError("missing Gemini API key")
    m = model or DEFAULT_GEMINI_MODEL
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + urllib.parse.quote(m) + ":generateContent?key=" + urllib.parse.quote(api_key))
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("Gemini HTTP %s: %s" % (e.code, body[:500]))
    cand = (data.get("candidates") or [None])[0]
    if not cand:
        raise RuntimeError("Gemini: no candidates: %s" % json.dumps(data)[:300])
    parts = (cand.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def ai_recommend_for_creator(api_key: str, creator: dict, *, model: Optional[str] = None) -> dict:
    summary = {k: creator.get(k) for k in (
        "platform", "username", "nickname", "followers", "total_hearts",
        "total_videos_lifetime", "avg_play_in_window", "posts_in_window",
        "last_post_at", "verified", "region", "bio", "recent_captions_sample",
        "contacts", "keyword_matched",
    )}
    prompt = (
        "You evaluate a TikTok or Instagram influencer for a paid creator-marketing partnership "
        "by Jovida (a wellness/personal-coaching brand).\n\n"
        "Decide:\n"
        "1) Should we reach out? (yes/maybe/no)\n"
        "2) Suggested USD price ranges: a 60s sponsored post, a story (or 3-day TikTok pin), a co-created video.\n"
        "3) 2-3 talking points for the cold outreach.\n"
        "4) Warning flags (audience mismatch, low engagement-to-follower ratio, dormant account, etc).\n\n"
        "Return STRICT JSON only matching this schema, no prose, no markdown:\n"
        "{\n"
        '  "recommend_outreach": "yes|maybe|no",\n'
        '  "recommend_reason": "...",\n'
        '  "suggested_price_usd": {"post": 0, "story": 0, "video_collab": 0},\n'
        '  "price_reason": "...",\n'
        '  "talking_points": ["...", "..."],\n'
        '  "warnings": ["..."]\n'
        "}\n\n"
        "Creator data:\n" + json.dumps(summary, ensure_ascii=False, indent=2)
    )
    text = call_gemini(api_key, prompt, model=model)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError("Gemini returned non-JSON: %s" % text[:300])


def fetch_top_video_downloads(
    token: str,
    video_urls: list,
    *,
    video_actor: Optional[str] = None,
    wait_secs: int = 600,
) -> dict:
    """Download mp4s for given TikTok video URLs. Returns {webVideoUrl: mp4_url}."""
    if not video_urls:
        return {}
    actor_id = (
        video_actor
        or os.environ.get("APIFY_ACTOR_TIKTOK_VIDEO")
        or DEFAULT_VIDEO_ACTOR
    )
    inp = {
        "videos": list(video_urls),
        "shouldDownloadVideos": True,
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadSubtitles": False,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    items = apify_run_actor(token, actor_id, inp, wait_secs=wait_secs)
    out: dict = {}
    for it in items:
        url = it.get("webVideoUrl") or it.get("videoWebUrl") or ""
        if not url:
            continue
        media = it.get("mediaUrls") or []
        mp4 = ""
        if isinstance(media, list) and media:
            first = media[0]
            mp4 = first if isinstance(first, str) else (first.get("url") if isinstance(first, dict) else "")
        if not mp4:
            mp4 = it.get("videoUrl") or it.get("videoUrlNoWaterMark") or ""
        if mp4:
            out[url] = mp4
    return out


def screen_creators(
    token: str,
    handles: list,
    *,
    actor: Optional[str] = None,
    results_per_page: int = 80,
    days: int = 30,
    min_followers: int = 1000,
    threshold: str = "tiktok",
    chunk_size: int = 25,
    wait_secs: int = 900,
    keywords: Optional[str] = None,
    search_actor: Optional[str] = None,
    discover_limit: int = 30,
    discover_results_per_page: int = 60,
    download_top_video: bool = False,
    video_actor: Optional[str] = None,
    platform: str = "tiktok",
) -> dict:
    if not token or not str(token).strip():
        raise ValueError("missing Apify token")
    kws_for_match = [x for x in (keywords or "").split(",") if x.strip()]
    if platform == "instagram":
        return _screen_ig(
            token, list(handles or []),
            kws_for_match=kws_for_match, days=days, min_followers=min_followers,
            threshold=threshold, wait_secs=wait_secs,
            profile_actor=actor, search_actor=search_actor,
            discover_limit=discover_limit, discover_results_per_page=discover_results_per_page,
        )
    actor_id = actor or os.environ.get("APIFY_ACTOR_TIKTOK") or DEFAULT_PROFILE_ACTOR

    discovery_info: Optional[dict] = None
    if not handles:
        if not kws_for_match:
            raise ValueError("must supply handles, or keywords for discovery")
        discovered = discover_handles(
            token,
            kws_for_match,
            search_actor=search_actor,
            results_per_page=discover_results_per_page,
            wait_secs=wait_secs,
        )
        kept = discovered[: max(1, int(discover_limit))]
        discovery_info = {
            "keywords": kws_for_match,
            "raw_count": len(discovered),
            "kept": len(kept),
            "search_actor": (
                search_actor
                or os.environ.get("APIFY_ACTOR_TIKTOK_SEARCH")
                or DEFAULT_SEARCH_ACTOR
            ),
        }
        handles = kept
        if not handles:
            return {"rows": [], "discovery": discovery_info}

    window_start = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    actor_input_base = {
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadVideos": False,
        "resultsPerPage": max(1, min(int(results_per_page), 200)),
    }
    all_rows: list = []
    for i in range(0, len(handles), chunk_size):
        chunk = handles[i : i + chunk_size]
        inp = dict(actor_input_base)
        inp["profiles"] = chunk
        t0 = time.perf_counter()
        items = apify_run_actor(token, actor_id, inp, wait_secs=wait_secs)
        elapsed = time.perf_counter() - t0
        by_user = defaultdict(list)
        for it in items:
            am = it.get("authorMeta") or {}
            u = None
            if isinstance(am, dict):
                u = am.get("name")
            if not u:
                u = it.get("input")
            if isinstance(u, str):
                by_user[u.lstrip("@").lower()].append(it)
        for h in chunk:
            key = h.lstrip("@").lower()
            prof_items = by_user.get(key, [])
            row = analyze_profile(
                h,
                prof_items,
                window_start,
                threshold,
                min_followers,
            )
            row["keyword_matched"] = (
                match_keywords(row["bio"], row["recent_captions_sample"], kws_for_match)
                if kws_for_match
                else None
            )
            row["apify_chunk_sec"] = round(elapsed, 2)
            row["actor"] = actor_id
            all_rows.append(row)

    if download_top_video and all_rows:
        top_urls = []
        for row in all_rows:
            rv = row.get("recent_videos_top") or []
            if rv and rv[0].get("url"):
                top_urls.append(rv[0]["url"])
        if top_urls:
            try:
                downloads = fetch_top_video_downloads(
                    token, top_urls, video_actor=video_actor, wait_secs=wait_secs
                )
            except SystemExit:
                downloads = {}
            for row in all_rows:
                rv = row.get("recent_videos_top") or []
                if rv and rv[0].get("url"):
                    mp4 = downloads.get(rv[0]["url"])
                    if mp4:
                        row["top_video_mp4"] = mp4

    return {"rows": all_rows, "discovery": discovery_info}


_CSV_FIELDS = (
    "username", "nickname", "verified", "region",
    "followers", "total_hearts", "total_videos_lifetime",
    "posts_in_window", "avg_play_in_window", "required_avg_for_rule",
    "pass_min_followers", "pass_view_rule", "pass_all",
    "last_post_at", "keyword_matched",
    "profile_url", "avatar", "bio",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Screen creators via Apify TikTok actors")
    parser.add_argument("--handles", type=Path, help="One handle per line")
    parser.add_argument("extra_handles", nargs="*", help="Usernames")
    parser.add_argument("--actor", default=os.environ.get("APIFY_ACTOR_TIKTOK", DEFAULT_PROFILE_ACTOR))
    parser.add_argument("--search-actor", default=os.environ.get("APIFY_ACTOR_TIKTOK_SEARCH", DEFAULT_SEARCH_ACTOR))
    parser.add_argument("--results-per-page", type=int, default=80)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--min-followers", type=int, default=1000)
    parser.add_argument("--threshold", choices=("tiktok", "other"), default="tiktok")
    parser.add_argument("--chunk-size", type=int, default=25)
    parser.add_argument("--wait-secs", type=int, default=900)
    parser.add_argument("--keywords", help="Comma-separated; required for discovery if no handles")
    parser.add_argument("--discover-limit", type=int, default=30)
    parser.add_argument("--discover-results-per-page", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    handles = _parse_handles(args.handles, list(args.extra_handles))
    if not handles and not (args.keywords or "").strip():
        raise SystemExit("Provide --handles or --keywords for discovery")

    token = _must_env("APIFY_TOKEN")
    result = screen_creators(
        token,
        handles,
        actor=args.actor,
        search_actor=args.search_actor,
        results_per_page=args.results_per_page,
        days=args.days,
        min_followers=args.min_followers,
        threshold=args.threshold,
        chunk_size=args.chunk_size,
        wait_secs=args.wait_secs,
        keywords=args.keywords,
        discover_limit=args.discover_limit,
        discover_results_per_page=args.discover_results_per_page,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    rows = result.get("rows") or []
    if not rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=list(_CSV_FIELDS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


if __name__ == "__main__":
    main()

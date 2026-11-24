"""Request store shared by `job` and `serve`.

A request is the factory's input: factory/requests/<n>.json. It replaces a
labeled GitHub issue. `messages` is the thread between the requester and the
factory (intake questions, answers, rejections), replacing issue comments.

Writes are atomic (tmp file + rename) and serialized by an flock on
factory/requests/.lock, so the server and the job script can both write.
"""

import contextlib
import fcntl
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

FACTORY = Path(__file__).resolve().parents[1]
REQUESTS = FACTORY / "requests"
CONFIG = json.loads((FACTORY / "config.json").read_text())
LIMITS = CONFIG.get("limits", {})


class BadRequest(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def locked():
    REQUESTS.mkdir(parents=True, exist_ok=True)
    with open(REQUESTS / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _path(n):
    return REQUESTS / f"{int(n)}.json"


def load(n):
    p = _path(n)
    if not p.exists():
        raise BadRequest(f"no request {n}", 404)
    return json.loads(p.read_text())


def _save(r):
    p = _path(r["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, indent=2) + "\n")
    tmp.replace(p)


def save(r):
    with locked():
        _save(r)


def all_requests():
    if not REQUESTS.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(REQUESTS.glob("*.json"), key=lambda p: int(p.stem))]


def valid_callback(url):
    u = urlparse(url)
    return u.scheme in {"http", "https"} and bool(u.netloc)


def create(title, body, submitter=None, callback_url=None):
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        raise BadRequest("title and body are required")
    if len(title) > LIMITS.get("max_title_chars", 200):
        raise BadRequest("title too long", 413)
    if len(body.encode()) > LIMITS.get("max_body_bytes", 65536):
        raise BadRequest("body too large", 413)
    if callback_url and not valid_callback(callback_url):
        raise BadRequest("callback_url must be http(s)")
    with locked():
        reqs = all_requests()
        if sum(r["status"] == "open" for r in reqs) >= LIMITS.get("max_open_requests", 200):
            raise BadRequest("too many open requests", 429)
        n = max((r["id"] for r in reqs), default=0) + 1
        r = {"id": n, "title": title, "body": body, "submitter": submitter or None,
             "callback_url": callback_url or None, "created": now(), "status": "open",
             "messages": []}
        _save(r)
    return r


def notify(r, event, **data):
    """Fire-and-forget webhook. Failures never block a stage change."""
    url = r.get("callback_url")
    if not url or not valid_callback(url):
        return
    payload = json.dumps({"request": r["id"], "event": event, "at": now(), **data}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:
        pass


def post_message(n, who, body, event="message"):
    """Append to the thread (who: factory | requester | operator) and notify."""
    with locked():
        r = load(n)
        r["messages"].append({"from": who, "at": now(), "body": body})
        _save(r)
    notify(r, event, **{"from": who, "body": body})
    return r


def close(n):
    with locked():
        r = load(n)
        r["status"] = "closed"
        _save(r)
    return r


def replied_since_question(r):
    """True when the requester or operator wrote after the factory's last message."""
    replied = False
    seen_factory = False
    for m in r["messages"]:
        if m["from"] == "factory":
            seen_factory, replied = True, False
        elif seen_factory:
            replied = True
    return replied

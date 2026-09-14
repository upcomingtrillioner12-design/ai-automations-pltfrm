"""
Insta-Automate — minimal Instagram automation (official Graph API only).
Triggers: comment -> reply, keyword -> DM.
Auth: Meta OAuth (Facebook Login) — user clicks "Continue with Instagram",
grants permissions, and this app exchanges the code for tokens automatically.
"""
import os, json, time, hmac, hashlib, threading, urllib.parse
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, abort, redirect
import requests
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))  # <-- .env was never loaded before; FB_APP_ID/APP_SECRET were always empty

FB_API = "https://graph.facebook.com/v21.0"
FB_OAUTH = "https://www.facebook.com/v21.0/dialog/oauth"
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

AUTOMATIONS_FILE = os.path.join(DATA, "automations.json")
CONFIG_FILE = os.path.join(DATA, "config.json")

# Scopes needed: manage comments, send DMs, read page metadata (IG account link).
# NOTE: pages_show_list and instagram_basic are required to list the user's Pages
# and read their connected Instagram Business account — without them the
# me/accounts lookup in oauth_callback() silently returns nothing.
SCOPES = ",".join([
    "business_management",
    "pages_show_list",
    "pages_manage_metadata",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_manage_comments",
    "instagram_manage_messages",
])

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---------------- config / env ----------------
def env(k, d=""):
    v = os.getenv(k, "").strip()
    return v if v else d

FB_APP_ID = env("FB_APP_ID")
FB_APP_SECRET = env("APP_SECRET")
VERIFY_TOKEN = env("WEBHOOK_VERIFY_TOKEN", "dev-verify-token")
REDIRECT_URI = env("OAUTH_REDIRECT_URI")  # e.g. https://your-url/api/oauth/callback
BASE_URL = env("WEBHOOK_BASE_URL", "http://localhost:5000")

# ---------------- storage ----------------
def _load(path, default):
    try: return json.load(open(path))
    except Exception: return default

def _save(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, path)

get_config = lambda: _load(CONFIG_FILE, {})
save_config = lambda cfg: _save(CONFIG_FILE, cfg)
get_automations = lambda: _load(AUTOMATIONS_FILE, [])
save_autos = lambda a: _save(AUTOMATIONS_FILE, a)

# ---------------- Instagram API helpers ----------------
def ig_token():
    return get_config().get("access_token") or ""

def ig_user_id():
    return get_config().get("ig_user_id") or ""

def ig_get(path, params=None, token=None):
    p = dict(params or {}); p["access_token"] = token or ig_token()
    return requests.get(f"{FB_API}/{path}", params=p, timeout=20)

def ig_post(path, data=None, token=None):
    p = dict(data or {}); p["access_token"] = token or ig_token()
    return requests.post(f"{FB_API}/{path}", data=p, timeout=20)

def ig_post_json(path, payload=None, token=None):
    """Some Graph API endpoints (e.g. /messages) require a nested JSON body,
    not flat form fields. Access token goes in the query string instead."""
    return requests.post(f"{FB_API}/{path}",
                          params={"access_token": token or ig_token()},
                          json=payload or {}, timeout=20)

def validate_token():
    uid, tok = ig_user_id(), ig_token()
    if not uid or not tok:
        return {"ok": False, "connected": False, "error": "No credentials saved"}
    r = requests.get(f"{FB_API}/{uid}", params={"fields": "id,username,name", "access_token": tok}, timeout=15)
    if r.status_code == 200:
        info = r.json(); info["auth_method"] = get_config().get("auth_method", "manual")
        return {"ok": True, "connected": True, "account": info}
    return {"ok": False, "connected": False, "error": r.json().get("error", {}).get("message", "Token invalid")}

# ---------------- OAuth ----------------
def oauth_redirect_uri():
    return REDIRECT_URI or f"{BASE_URL}/api/oauth/callback"

@app.route("/api/oauth/url")
def oauth_url():
    if not FB_APP_ID:
        return jsonify({"ok": False, "error": "Server not configured: set FB_APP_ID and APP_SECRET in .env"}), 500
    url = (f"{FB_OAUTH}?client_id={FB_APP_ID}"
           f"&redirect_uri={urllib.parse.quote(oauth_redirect_uri(), safe='')}"
           f"&scope={SCOPES}&response_type=code")
    return jsonify({"ok": True, "url": url})

@app.route("/api/oauth/callback")
def oauth_callback():
    err = request.args.get("error_description") or request.args.get("error")
    code = request.args.get("code")
    if err or not code:
        return redirect(f"/?oauth_error={urllib.parse.quote(err or 'authorization denied')}")
    try:
        # 1. code -> short-lived user token
        r = requests.get(f"{FB_API}/oauth/access_token", params={
            "client_id": FB_APP_ID, "client_secret": FB_APP_SECRET,
            "redirect_uri": oauth_redirect_uri(), "code": code}, timeout=20)
        j = r.json()
        if "access_token" not in j:
            return redirect(f"/?oauth_error={urllib.parse.quote(j.get('error', {}).get('message', 'token exchange failed'))}")
        short = j["access_token"]
        # 2. short-lived -> long-lived user token (60 days)
        r = requests.get(f"{FB_API}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET, "fb_exchange_token": short}, timeout=20)
        user_token = r.json().get("access_token", short)
        # 3. find the Facebook Page connected to the Instagram Business account
        r = ig_get("me/accounts", {"fields": "id,name,access_token"}, token=user_token)
        pages = (r.json() or {}).get("data", [])
        chosen = None
        for p in pages:
            r2 = ig_get(p["id"], {"fields": "instagram_business_account{id,username,name}"}, token=user_token)
            ig = (r2.json() or {}).get("instagram_business_account")
            if ig:
                chosen = {"page": p, "ig": ig}; break
        if not chosen:
            return redirect("/?oauth_error=" + urllib.parse.quote(
                "No Facebook Page with a connected Instagram Business account found. Connect one in your Facebook Page settings first."))
        # 4. store page token (long-lived user token => page token does not expire)
        save_config({
            "access_token": chosen["page"]["access_token"],
            "ig_user_id": chosen["ig"]["id"],
            "page_id": chosen["page"]["id"],
            "page_name": chosen["page"]["name"],
            "account": {"id": chosen["ig"]["id"], "username": chosen["ig"].get("username"),
                        "name": chosen["ig"].get("name")},
            "auth_method": "oauth",
            "consented_at": datetime.now(timezone.utc).isoformat(),
        })
        return redirect("/?connected=1")
    except Exception as e:
        return redirect(f"/?oauth_error={urllib.parse.quote(str(e))}")

# ---------------- automation engine ----------------
def mark_run(aid, success=True, note=""):
    autos = get_automations()
    for a in autos:
        if a["id"] == aid:
            a["stats"]["runs"] = a["stats"].get("runs", 0) + 1
            a["stats"]["last_run"] = datetime.now(timezone.utc).isoformat()
            a["stats"]["last_error"] = None if success else note
    save_autos(autos)

PROCESSED_FILE = os.path.join(DATA, "processed_comments.json")
_processed_lock = threading.Lock()

def already_processed(comment_id):
    """Meta redelivers webhook events on retry/timeout, and our own auto-replies
    land back on the same webhook as new comments. Without dedup + self-filtering
    the bot would reply to its own replies forever. Keep a rolling window of the
    last N processed comment ids on disk."""
    if not comment_id:
        return True
    with _processed_lock:
        seen = _load(PROCESSED_FILE, [])
        if comment_id in seen:
            return True
        seen.append(comment_id)
        if len(seen) > 2000:
            seen = seen[-2000:]
        _save(PROCESSED_FILE, seen)
        return False

def handle_comment(commenter_id, commenter_username, text, comment_id):
    # Never react to comments made by the connected account itself
    # (e.g. its own auto-reply) — that would create an infinite reply loop.
    if commenter_id and commenter_id == ig_user_id():
        return
    if already_processed(comment_id):
        return
    for a in get_automations():
        if not a.get("on"):
            continue
        if a["trigger"] == "comment" and a["action"] == "reply":
            r = ig_post(f"{comment_id}/replies", {"message": a["message"]})
            mark_run(a["id"], r.status_code == 200,
                     r.json().get("error", {}).get("message", "") if r.status_code != 200 else "")
        elif a["trigger"] == "keyword" and a["action"] == "dm":
            kw = (a.get("keyword") or "").strip().lower()
            if kw and kw in (text or "").lower():
                r = ig_post_json(f"{ig_user_id()}/messages",
                                  {"recipient": {"id": commenter_id}, "message": {"text": a["message"]}})
                mark_run(a["id"], r.status_code == 200,
                         r.json().get("error", {}).get("message", "") if r.status_code != 200 else "")

# ---------------- webhook ----------------
@app.route("/webhook", methods=["GET"])
def webhook_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    abort(403)

def _signature_ok():
    if not FB_APP_SECRET:
        return True  # dev mode
    sig = request.headers.get("X-Hub-Signature-256", "")
    mac = hmac.new(FB_APP_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, "sha256=" + mac)

@app.route("/webhook", methods=["POST"])
def webhook_receive():
    if not _signature_ok():
        abort(403)
    body = request.get_json(force=True, silent=True) or {}
    def process():
        try:
            for entry in body.get("entry", []):
                for ch in entry.get("changes", []):
                    if ch.get("field") != "comments": continue
                    v = ch.get("value", {})
                    if v.get("verb") not in ("add", None): continue
                    handle_comment(
                        (v.get("from") or {}).get("id"),
                        (v.get("from") or {}).get("username", ""),
                        v.get("text", ""),
                        v.get("comment_id") or v.get("id"))
        except Exception as e:
            print("webhook process error:", e)
    threading.Thread(target=process, daemon=True).start()
    return "ok", 200

# ---------------- API ----------------
@app.route("/api/status")
def api_status():
    st = validate_token()
    st["oauth_configured"] = bool(FB_APP_ID and FB_APP_SECRET)
    return jsonify(st)

@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Manual consent-based fallback (owner enters their own credentials)."""
    d = request.get_json(force=True)
    token, uid = (d.get("access_token") or "").strip(), (d.get("ig_user_id") or "").strip()
    if d.get("consent") is not True:
        return jsonify({"ok": False, "error": "You must accept the disclaimer to continue."}), 400
    if not token or not uid:
        return jsonify({"ok": False, "error": "Access token and Instagram User ID are required."}), 400
    r = requests.get(f"{FB_API}/{uid}", params={"fields": "id,username,name", "access_token": token}, timeout=15)
    if r.status_code != 200:
        return jsonify({"ok": False, "error": r.json().get("error", {}).get("message", "Invalid credentials")}), 400
    save_config({"access_token": token, "ig_user_id": uid, "auth_method": "manual",
                 "account": r.json(), "consented_at": datetime.now(timezone.utc).isoformat()})
    return jsonify({"ok": True, "account": r.json()})

@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    save_config({})
    return jsonify({"ok": True})

@app.route("/api/automations", methods=["GET"])
def api_list():
    return jsonify(get_automations())

@app.route("/api/automations", methods=["POST"])
def api_create():
    d = request.get_json(force=True)
    if d.get("trigger") not in ("comment", "keyword"):
        return jsonify({"ok": False, "error": "Unsupported trigger."}), 400
    if d.get("action") not in ("reply", "dm"):
        return jsonify({"ok": False, "error": "Unsupported action."}), 400
    if not (d.get("message") or "").strip():
        return jsonify({"ok": False, "error": "Message is required."}), 400
    if d.get("trigger") == "keyword" and not (d.get("keyword") or "").strip():
        return jsonify({"ok": False, "error": "Keyword is required."}), 400
    a = {"id": "a" + str(int(time.time() * 1000)),
         "trigger": d["trigger"], "action": d["action"],
         "keyword": (d.get("keyword") or "").strip().lower(),
         "message": d["message"].strip(), "on": bool(d.get("on", True)),
         "stats": {"runs": 0, "last_run": None, "last_error": None},
         "created_at": datetime.now(timezone.utc).isoformat()}
    autos = get_automations(); autos.append(a); save_autos(autos)
    return jsonify(a), 201

@app.route("/api/automations/<aid>", methods=["PUT"])
def api_update(aid):
    d = request.get_json(force=True)
    autos = get_automations()
    for a in autos:
        if a["id"] == aid:
            if "keyword" in d: a["keyword"] = (d.get("keyword") or "").strip().lower()
            if "message" in d:
                if not (d.get("message") or "").strip():
                    return jsonify({"ok": False, "error": "Message is required."}), 400
                a["message"] = d["message"].strip()
            save_autos(autos)
            return jsonify(a)
    abort(404)

@app.route("/api/automations/<aid>/toggle", methods=["PATCH"])
def api_toggle(aid):
    autos = get_automations()
    for a in autos:
        if a["id"] == aid:
            a["on"] = not a.get("on", False); save_autos(autos); return jsonify(a)
    abort(404)

@app.route("/api/automations/<aid>", methods=["DELETE"])
def api_delete(aid):
    save_autos([a for a in get_automations() if a["id"] != aid])
    return jsonify({"ok": True})

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/healthz")
def healthz():
    # Simple liveness endpoint for hosting platforms (Render/Railway/etc.)
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")), threaded=True)

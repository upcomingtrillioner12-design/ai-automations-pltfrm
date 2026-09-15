"""
Insta-Automate — minimal Instagram automation (official Graph API only).
Triggers: comment -> reply, keyword -> DM.
Auth: Instagram API with Instagram Login ("Instagram business login") —
user clicks "Continue with Instagram", grants permissions, and this app
exchanges the code for tokens automatically. No Facebook Page required.
"""
import os, json, time, hmac, hashlib, threading, urllib.parse
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, abort, redirect
import requests
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

IG_OAUTH = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com/v21.0"
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

AUTOMATIONS_FILE = os.path.join(DATA, "automations.json")
CONFIG_FILE = os.path.join(DATA, "config.json")

SCOPES = ",".join([
    "instagram_business_basic",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_content_publish",
])

app = Flask(__name__, static_folder="static", static_url_path="/static")

def env(k, d=""):
    v = os.getenv(k, "").strip()
    return v if v else d

# Accept either naming: IG_APP_ID / INSTAGRAM_APP_ID (and same for secret).
IG_APP_ID = env("IG_APP_ID") or env("INSTAGRAM_APP_ID") or env("FB_APP_ID")
IG_APP_SECRET = (env("IG_APP_SECRET") or env("INSTAGRAM_APP_SECRET")
                 or env("APP_SECRET"))
VERIFY_TOKEN = env("WEBHOOK_VERIFY_TOKEN", "dev-verify-token")
REDIRECT_URI = env("OAUTH_REDIRECT_URI")
BASE_URL = env("WEBHOOK_BASE_URL", "http://localhost:5000")

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

def ig_token():
    return get_config().get("access_token") or ""

def ig_user_id():
    return get_config().get("ig_user_id") or ""

def ig_get(path, params=None, token=None):
    p = dict(params or {}); p["access_token"] = token or ig_token()
    return requests.get(f"{IG_GRAPH}/{path}", params=p, timeout=20)

def ig_post(path, data=None, token=None):
    p = dict(data or {}); p["access_token"] = token or ig_token()
    return requests.post(f"{IG_GRAPH}/{path}", data=p, timeout=20)

def ig_post_json(path, payload=None, token=None):
    return requests.post(f"{IG_GRAPH}/{path}",
                          params={"access_token": token or ig_token()},
                          json=payload or {}, timeout=20)

def validate_token():
    uid, tok = ig_user_id(), ig_token()
    if not uid or not tok:
        return {"ok": False, "connected": False, "error": "No credentials saved"}
    r = requests.get(f"{IG_GRAPH}/{uid}",
                     params={"fields": "id,username,name", "access_token": tok},
                     timeout=15)
    if r.status_code == 200:
        info = r.json(); info["auth_method"] = get_config().get("auth_method", "manual")
        return {"ok": True, "connected": True, "account": info}
    return {"ok": False, "connected": False,
            "error": r.json().get("error", {}).get("message", "Token invalid")}

def oauth_redirect_uri():
    return REDIRECT_URI or f"{BASE_URL}/api/oauth/callback"

@app.route("/api/oauth/url")
def oauth_url():
    if not IG_APP_ID:
        return jsonify({"ok": False, "error": "Server not configured: set IG_APP_ID and IG_APP_SECRET in .env"}), 500
    url = (f"{IG_OAUTH}?client_id={IG_APP_ID}"
           f"&redirect_uri={urllib.parse.quote(oauth_redirect_uri(), safe='')}"
           f"&response_type=code&scope={SCOPES}")
    return jsonify({"ok": True, "url": url})

@app.route("/api/oauth/callback")
def oauth_callback():
    err = request.args.get("error_description") or request.args.get("error")
    code = request.args.get("code")
    if err or not code:
        return redirect(f"/?oauth_error={urllib.parse.quote(err or 'authorization denied')}")
    try:
        # 1. code -> short-lived user token (~1 hour). Instagram business login
        # requires this as a form-encoded POST, not a GET like classic FB OAuth.
        r = requests.post(IG_TOKEN_URL, data={
            "client_id": IG_APP_ID, "client_secret": IG_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": oauth_redirect_uri(), "code": code}, timeout=20)
        j = r.json()
        if "access_token" not in j:
            return redirect(f"/?oauth_error={urllib.parse.quote(j.get('error_message', j.get('error', 'token exchange failed')))}")
        short_token = j["access_token"]
        # Instagram sometimes nests user_id inside "data"; handle both shapes.
        ig_uid = j.get("user_id") or (j.get("data") or {}).get("user_id")
        # 2. short-lived -> long-lived token (60 days, refreshable)
        r = requests.get(f"{IG_GRAPH}/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": IG_APP_SECRET, "access_token": short_token}, timeout=20)
        lj = r.json()
        long_token = lj.get("access_token", short_token)
        # 3. fetch account profile directly — no Facebook Page lookup needed
        r = ig_get("me", {"fields": "id,username,name,account_type"}, token=long_token)
        profile = r.json() if r.status_code == 200 else {"id": ig_uid}
        save_config({
            "access_token": long_token,
            "ig_user_id": profile.get("id", ig_uid),
            "account": profile,
            "auth_method": "oauth_ig_business_login",
            "consented_at": datetime.now(timezone.utc).isoformat(),
        })
        return redirect("/?connected=1")
    except Exception as e:
        return redirect(f"/?oauth_error={urllib.parse.quote(str(e))}")

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

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    abort(403)

def _signature_ok():
    if not IG_APP_SECRET:
        return True
    sig = request.headers.get("X-Hub-Signature-256", "")
    mac = hmac.new(IG_APP_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
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

@app.route("/api/status")
def api_status():
    st = validate_token()
    st["oauth_configured"] = bool(IG_APP_ID and IG_APP_SECRET)
    return jsonify(st)

@app.route("/api/connect", methods=["POST"])
def api_connect():
    d = request.get_json(force=True)
    token, uid = (d.get("access_token") or "").strip(), (d.get("ig_user_id") or "").strip()
    if d.get("consent") is not True:
        return jsonify({"ok": False, "error": "You must accept the disclaimer to continue."}), 400
    if not token or not uid:
        return jsonify({"ok": False, "error": "Access token and Instagram User ID are required."}), 400
    r = requests.get(f"{IG_GRAPH}/{uid}",
                     params={"fields": "id,username,name", "access_token": token},
                     timeout=15)
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
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "5000")), threaded=True)

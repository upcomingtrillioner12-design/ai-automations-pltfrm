# Insta-Automate

Minimal Instagram automation, official API only. **When → Do → Turn on.**

> **Fixed in this build:** `.env` values weren't actually being loaded (OAuth
> looked "configured" but silently used blank credentials), the OAuth scope
> list was missing two permissions required to find your Page's Instagram
> account, DMs were sent with the wrong payload shape and would have been
> rejected by the Graph API, the bot could get stuck auto-replying to its own
> replies forever, retried webhook deliveries could double-send, and the
> dashboard had no way to edit or delete an automation even though the backend
> supported it. A `Procfile` and `.gitignore` were added for deployment. See
> "What was fixed" at the bottom for the full list.

- Comment → auto-reply
- Keyword → auto-DM
- **One-click "Continue with Instagram" login** (Meta OAuth — no manual token hunting for users)
- Consent + disclaimer built into the connect flow
- No databases, no node graphs — one folder of JSON state

---

## 1. One-time setup (server owner only, ~10 min)

1. Go to https://developers.facebook.com → **My Apps → Create App** → "Other" → "Business".
2. Add the **Messenger** product. Under **Instagram settings**, make sure your Facebook Page (linked to your Instagram Business/Creator account) is connected.
3. In **App settings → Basic**, copy your **App ID** and **App Secret** into `.env` as `FB_APP_ID` and `APP_SECRET`.
4. In **App settings → Advanced → Security → "Valid OAuth Redirect URIs"**, add:
   `https://your-public-url/api/oauth/callback`
   (for local testing: `http://localhost:5000/api/oauth/callback` won't work — use the ngrok URL instead, see step 3 below).

Your users never see developers.facebook.com. They click the button, log in to Facebook, grant permissions, and are connected.

## 2. Install & run

```bash
cp .env.example .env      # fill in FB_APP_ID + APP_SECRET
pip install -r requirements.txt
python app.py             # → http://localhost:5000
```

## 3. Go public (webhooks + OAuth both need a public HTTPS URL)

```bash
ngrok http 5000
# or deploy to any VPS / Render / Railway with HTTPS
```

Put that URL in `.env` as `WEBHOOK_BASE_URL`, restart, then in your Meta App:

- **Webhooks → Instagram** → Callback URL `https://<url>/webhook`, verify token = your `WEBHOOK_VERIFY_TOKEN`, subscribe to field **`comments`**.
- Make sure the OAuth redirect URI from step 1 matches your public URL.

## 4. How a user connects

1. Checks the consent & disclaimer box (required).
2. Clicks **Continue with Instagram** → Meta's OAuth screen → grants permissions.
3. This server automatically: exchanges the code → long-lived token → finds the Facebook Page connected to the IG Business account → stores the Page access token (which doesn't expire). Connected. ✅

A manual token entry remains available as a fallback (for the owner or edge cases).

## 5. What runs

| When | Then | API call |
|---|---|---|
| Any comment on your posts | Replies | `POST /{comment-id}/replies` |
| Comment contains your keyword | DMs the commenter | `POST /{ig-user-id}/messages` |

Permissions requested at login: `business_management, pages_manage_metadata, pages_read_engagement, instagram_manage_comments, instagram_manage_messages`.

## 6. Disclaimer & consent

- Users connect **their own** account through Meta's official OAuth; tokens are stored only on this server and deleted on disconnect.
- Automation must comply with Instagram/Meta Platform Terms — no spam, no bulk unsolicited messaging.
- **"New follower" automation is intentionally not included**: Meta's official API does not expose follower events; tools offering it use private APIs that violate Instagram's terms and risk permanent account bans.

## 7. What was fixed / completed

| Issue | Fix |
|---|---|
| `.env` was never loaded into the process — `FB_APP_ID`/`APP_SECRET` were always empty even when set | `app.py` now calls `load_dotenv()` on startup |
| OAuth scope list was missing `pages_show_list` and `instagram_basic` | Added both — without them, listing the user's Pages / reading the linked IG account fails silently |
| Auto-DM sent `recipient`/`message` as flat form fields | The Graph Messages API needs nested JSON (`{"recipient":{"id":...},"message":{"text":...}}`) — added `ig_post_json()` and fixed the call |
| Comment→Reply could loop forever (bot replies to its own reply, which fires the webhook again) | `handle_comment()` now skips events where the commenter is the connected account itself |
| Meta can redeliver the same webhook event on retry/timeout, causing duplicate replies/DMs | Added a rolling on-disk dedup list of processed comment IDs |
| No way to edit or delete an automation from the UI (delete API existed but was never wired up) | Added `PUT /api/automations/<id>` + Edit/Delete buttons on each automation card |
| Deployment: `gunicorn` was a dependency but nothing used it; no `.gitignore` | Added `Procfile` (`gunicorn app:app`) and `.gitignore` (`.env`, `data/`) |
| No health-check route for hosts like Render/Railway | Added `GET /healthz` |
| `datetime.utcnow()` is deprecated | Switched to timezone-aware `datetime.now(timezone.utc)` |

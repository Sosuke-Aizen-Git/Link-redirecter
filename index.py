import os
import requests
from flask import Flask, request, jsonify
from pymongo import MongoClient

app = Flask(__name__)

# ── Config (set these as Environment Variables in Vercel) ──────────────────
MONGO_URL = os.environ["MONGO_URL"]
MAIN_BOT_TOKEN = os.environ["MAIN_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
BASE_URL = os.environ["BASE_URL"].rstrip("/")  # e.g. https://your-app.vercel.app

# ── DB (reused across warm invocations) ─────────────────────────────────────
_client = MongoClient(MONGO_URL)
db = _client["deeplink_bot"]
bots_col = db["bots"]


def tg_call(token: str, method: str, payload: dict):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}


def send_message(token, chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call(token, "sendMessage", payload)


def get_bot_doc(token):
    return bots_col.find_one({"_id": token})


def ensure_main_bot():
    """Make sure the main bot has a DB row. Runs cheaply (indexed _id lookup)."""
    if not get_bot_doc(MAIN_BOT_TOKEN):
        bots_col.update_one(
            {"_id": MAIN_BOT_TOKEN},
            {
                "$setOnInsert": {
                    "owner_id": OWNER_ID,
                    "is_main": True,
                    "target_username": None,
                    "parent_token": None,
                }
            },
            upsert=True,
        )


@app.route("/", methods=["GET"])
def health():
    return "Bot is alive."


@app.route("/webhook/<token>", methods=["POST"])
def webhook(token):
    ensure_main_bot()
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message")
    if not message or "text" not in message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    from_id = message["from"]["id"]
    text = message["text"].strip()

    bot_doc = get_bot_doc(token)
    if not bot_doc:
        # Unknown token hitting our webhook — ignore silently.
        return jsonify(ok=True)

    is_owner = from_id == bot_doc.get("owner_id")
    is_main_owner = from_id == OWNER_ID
    is_main = bot_doc.get("is_main", False)

    # ── /start [payload] — rewrite deep link ────────────────────────────────
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1] if len(parts) > 1 else None
        target = bot_doc.get("target_username")

        if not target:
            send_message(
                token, chat_id,
                "This bot isn't configured yet. Ask the owner to set a target "
                "with /username <bot_username>."
            )
            return jsonify(ok=True)

        link = f"https://t.me/{target}" + (f"?start={payload}" if payload else "")
        reply_markup = {"inline_keyboard": [[{"text": "Here's your link", "url": link}]]}
        send_message(token, chat_id, "Tap below to continue:", reply_markup)
        return jsonify(ok=True)

    # ── /username <name> — set this bot's deep-link redirect target ────────
    if text.startswith("/username"):
        if not (is_owner or is_main_owner):
            send_message(token, chat_id, "Only the bot owner can set this.")
            return jsonify(ok=True)

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(token, chat_id, "Usage: /username <bot_username>\nExample: /username Guts_Store_Bot")
            return jsonify(ok=True)

        uname = parts[1].strip().lstrip("@")
        bots_col.update_one({"_id": token}, {"$set": {"target_username": uname}})
        send_message(token, chat_id, f"Deep-link target set to @{uname}")
        return jsonify(ok=True)

    # ── /clone <bot_token> — only on the main bot ───────────────────────────
    if text.startswith("/clone") and is_main:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(token, chat_id, "Usage: /clone <bot_token>")
            return jsonify(ok=True)

        new_token = parts[1].strip()
        me = tg_call(new_token, "getMe", {})
        if not me.get("ok"):
            send_message(token, chat_id, "That doesn't look like a valid bot token.")
            return jsonify(ok=True)

        set_wh = tg_call(new_token, "setWebhook", {"url": f"{BASE_URL}/webhook/{new_token}"})
        if not set_wh.get("ok"):
            send_message(token, chat_id, f"Could not set webhook: {set_wh.get('description', 'unknown error')}")
            return jsonify(ok=True)

        bots_col.update_one(
            {"_id": new_token},
            {"$set": {
                "owner_id": from_id,
                "is_main": False,
                "target_username": None,
                "parent_token": MAIN_BOT_TOKEN,
            }},
            upsert=True,
        )

        uname = me["result"]["username"]
        send_message(
            token, chat_id,
            f"Cloned successfully: @{uname}\n"
            f"Now message @{uname} directly with /username <target_bot_username> to configure it."
        )
        return jsonify(ok=True)

    # ── /setusername <clone_token> <name> — main owner overrides a clone ───
    if text.startswith("/setusername") and is_main and is_main_owner:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_message(token, chat_id, "Usage: /setusername <clone_token> <bot_username>")
            return jsonify(ok=True)

        clone_token, uname = parts[1], parts[2].strip().lstrip("@")
        result = bots_col.update_one(
            {"_id": clone_token, "parent_token": MAIN_BOT_TOKEN},
            {"$set": {"target_username": uname}},
        )
        send_message(token, chat_id, "Updated." if result.matched_count else "Clone not found.")
        return jsonify(ok=True)

    # ── /clones — list all clones (main owner only) ─────────────────────────
    if text.startswith("/clones") and is_main and is_main_owner:
        clones = list(bots_col.find({"parent_token": MAIN_BOT_TOKEN}))
        if not clones:
            send_message(token, chat_id, "No clones yet.")
        else:
            lines = [
                f"• {c['_id'][:10]}… → target: @{c.get('target_username') or 'not set'} "
                f"(owner: {c.get('owner_id')})"
                for c in clones
            ]
            send_message(token, chat_id, "\n".join(lines))
        return jsonify(ok=True)

    # ── /delclone <clone_token> — remove a clone + its webhook (main owner) ─
    if text.startswith("/delclone") and is_main and is_main_owner:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(token, chat_id, "Usage: /delclone <clone_token>")
            return jsonify(ok=True)
        clone_token = parts[1].strip()
        tg_call(clone_token, "deleteWebhook", {})
        result = bots_col.delete_one({"_id": clone_token, "parent_token": MAIN_BOT_TOKEN})
        send_message(token, chat_id, "Removed." if result.deleted_count else "Clone not found.")
        return jsonify(ok=True)

    return jsonify(ok=True)

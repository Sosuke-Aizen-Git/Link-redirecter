import os
import math
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from pymongo import MongoClient

app = Flask(__name__)

# ── Config (set these as Environment Variables in Vercel) ──────────────────
MONGO_URL = os.environ["MONGO_URL"]
MAIN_BOT_TOKEN = os.environ["MAIN_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
BASE_URL = os.environ["BASE_URL"].rstrip("/")  # e.g. https://your-app.vercel.app

PAGE_SIZE = 5          # clones shown per page in /clones
REFRESH_BATCH = 25     # users checked per /refresh call (stays well under Vercel's 10s cap)

# ── Bot command menus (shown in Telegram's "/" menu) ────────────────────────
MAIN_BOT_COMMANDS = [
    {"command": "start", "description": "Get your redirect link"},
    {"command": "username", "description": "Set this bot's redirect target"},
    {"command": "clone", "description": "Register a new clone bot"},
    {"command": "clones", "description": "Manage all clone bots"},
    {"command": "mybots", "description": "Manage your own clone bots"},
    {"command": "users", "description": "View this bot's user stats"},
    {"command": "refresh", "description": "Sweep out dead or blocked users"},
]

CLONE_BOT_COMMANDS = [
    {"command": "start", "description": "Get your redirect link"},
    {"command": "username", "description": "Set this bot's redirect target"},
    {"command": "users", "description": "View this bot's user stats"},
    {"command": "refresh", "description": "Sweep out dead or blocked users"},
    {"command": "unclone", "description": "Remove this bot"},
]

# ── DB (reused across warm invocations) ─────────────────────────────────────
_client = MongoClient(MONGO_URL)
db = _client["deeplink_bot"]
bots_col = db["bots"]
meta_col = db["meta"]

bots_col.create_index("bot_id")
bots_col.create_index("parent_token")
bots_col.create_index("owner_id")


def get_users_collection(bot_id: str):
    """Each bot — main and every clone — gets its own isolated users collection,
    so blocking/removing a user on one bot never touches another bot's data."""
    return db[f"users_{bot_id}"]


# ── Small-caps unicode styling ───────────────────────────────────────────────
_SMALL_CAPS = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
}


def sc(text: str) -> str:
    """Render text in small-caps unicode for headers/labels."""
    return "".join(_SMALL_CAPS.get(ch.lower(), ch) for ch in text)


# ── Telegram helpers ─────────────────────────────────────────────────────────
def tg_call(token: str, method: str, payload: dict):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=8)
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "description": str(e)}


def send_message(token, chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call(token, "sendMessage", payload)


def edit_message(token, chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call(token, "editMessageText", payload)


def answer_callback(token, callback_id, text=None, alert=False):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    return tg_call(token, "answerCallbackQuery", payload)


# ── DB helpers ────────────────────────────────────────────────────────────────
def get_bot_doc(token):
    return bots_col.find_one({"_id": token})


def get_bot_by_id(bot_id):
    return bots_col.find_one({"bot_id": bot_id})


def bot_id_of(token, bot_doc):
    """Prefer the stored bot_id; fall back to deriving it from the token
    (covers bots registered before this field existed)."""
    return (bot_doc or {}).get("bot_id") or token.split(":")[0]


def set_bot_commands(token, commands):
    tg_call(token, "setMyCommands", {"commands": commands})


def ensure_main_bot():
    doc = get_bot_doc(MAIN_BOT_TOKEN)
    if not doc:
        me = tg_call(MAIN_BOT_TOKEN, "getMe", {})
        username = me.get("result", {}).get("username") if me.get("ok") else None
        bots_col.update_one(
            {"_id": MAIN_BOT_TOKEN},
            {"$setOnInsert": {
                "owner_id": OWNER_ID,
                "is_main": True,
                "target_username": None,
                "parent_token": None,
                "bot_id": MAIN_BOT_TOKEN.split(":")[0],
                "username": username,
                "name": me.get("result", {}).get("first_name") if me.get("ok") else None,
                "banned": False,
                "commands_set": True,
            }},
            upsert=True,
        )
        set_bot_commands(MAIN_BOT_TOKEN, MAIN_BOT_COMMANDS)
    elif not doc.get("commands_set"):
        # Backfill for main bots registered before command menus existed.
        set_bot_commands(MAIN_BOT_TOKEN, MAIN_BOT_COMMANDS)
        bots_col.update_one({"_id": MAIN_BOT_TOKEN}, {"$set": {"commands_set": True}})


def track_user(bot_id, user_id, first_name=None, username=None):
    now = datetime.now(timezone.utc)
    col = get_users_collection(bot_id)
    set_fields = {"last_seen": now, "blocked": False}
    if first_name is not None:
        set_fields["first_name"] = first_name
    if username is not None:
        set_fields["username"] = username
    col.update_one(
        {"_id": user_id},
        {
            "$set": set_fields,
            "$setOnInsert": {"first_seen": now},
        },
        upsert=True,
    )


def ensure_bot_name(clone):
    """Prefer the stored display name; backfill it via getMe for clones
    registered before this field existed."""
    if clone.get("name"):
        return clone["name"]
    me = tg_call(clone["_id"], "getMe", {})
    name = me.get("result", {}).get("first_name") if me.get("ok") else None
    if name:
        bots_col.update_one({"_id": clone["_id"]}, {"$set": {"name": name}})
        return name
    return clone.get("username") or bot_id_of(clone["_id"], clone)


def get_owner_label(owner_id):
    """Look up the owner's name/username from the main bot's own users
    collection (they must have messaged the main bot to own a clone)."""
    main_doc = get_bot_doc(MAIN_BOT_TOKEN)
    main_id = bot_id_of(MAIN_BOT_TOKEN, main_doc)
    user = get_users_collection(main_id).find_one({"_id": owner_id})
    name = (user or {}).get("first_name")
    uname = (user or {}).get("username")
    label = name or "?"
    if uname:
        label += f" (@{uname})"
    return f"{label} — {owner_id}"


def build_bot_detail_text(clone, bid):
    name = ensure_bot_name(clone)
    uname = clone.get("username")
    uname_disp = f"@{uname}" if uname else "?"
    token = clone.get("_id", "?")
    owner_label = get_owner_label(clone.get("owner_id"))
    return (
        f"{sc('bot name')}: {name}\n"
        f"{sc('username')}: {uname_disp}\n"
        f"{sc('api token')}: {token}\n"
        f"{sc('owner')}: {owner_label}"
    )


# ── View builders ────────────────────────────────────────────────────────────
def build_clones_keyboard(page=0):
    clones = list(bots_col.find({"parent_token": MAIN_BOT_TOKEN}).sort("_id", 1))
    total = len(clones)

    if total == 0:
        return f"{sc('clones overview')}\n\n{sc('no clones yet')}", None, 0

    start = page * PAGE_SIZE
    page_items = clones[start:start + PAGE_SIZE]

    rows = []
    for c in page_items:
        bid = bot_id_of(c["_id"], c)
        if not c.get("bot_id"):
            bots_col.update_one({"_id": c["_id"]}, {"$set": {"bot_id": bid}})
        uname = c.get("username") or bid
        status_icon = "🚫" if c.get("banned") else "✅"
        rows.append([{"text": f"{status_icon} @{uname}", "callback_data": f"cl:view:{bid}:{page}"}])

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"cl:page:{page - 1}"})
    nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "noop"})
    if start + PAGE_SIZE < total:
        nav.append({"text": "➡️", "callback_data": f"cl:page:{page + 1}"})
    rows.append(nav)

    text = f"{sc('clones overview')}\n\n{sc('total')}: {total}"
    return text, {"inline_keyboard": rows}, total


def build_mybots_view(user_id):
    clones = list(
        bots_col.find({"parent_token": MAIN_BOT_TOKEN, "owner_id": user_id}).sort("_id", 1)
    )
    if not clones:
        return f"{sc('your clones')}\n\n{sc('you have none yet')}", None

    rows = []
    for c in clones:
        bid = bot_id_of(c["_id"], c)
        if not c.get("bot_id"):
            bots_col.update_one({"_id": c["_id"]}, {"$set": {"bot_id": bid}})
        rows.append(
            [{"text": f"🤖 @{c.get('username') or bid}", "callback_data": f"mb:view:{bid}"}]
        )
    text = f"{sc('your clones')}\n\n{sc('total')}: {len(clones)}"
    return text, {"inline_keyboard": rows}


# ── Callback (button tap) handling ──────────────────────────────────────────
def handle_callback(token, bot_doc, callback):
    data = callback.get("data", "")
    from_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    cb_id = callback["id"]
    is_main_owner = from_id == OWNER_ID
    is_main = bot_doc.get("is_main", False)

    if data == "noop":
        answer_callback(token, cb_id)
        return

    # ── /clones admin actions (main bot, main owner only) ──────────────────
    if data.startswith("cl:"):
        if not (is_main and is_main_owner):
            answer_callback(token, cb_id, sc("owner only"), alert=True)
            return

        parts = data.split(":")
        action = parts[1]

        if action == "page":
            page = int(parts[2])
            view_text, markup, _ = build_clones_keyboard(page)
            edit_message(token, chat_id, message_id, view_text, markup)
            answer_callback(token, cb_id)
            return

        if action == "back":
            page = int(parts[2])
            view_text, markup, _ = build_clones_keyboard(page)
            edit_message(token, chat_id, message_id, view_text, markup)
            answer_callback(token, cb_id)
            return

        bot_id, page = parts[2], int(parts[3])
        clone = get_bot_by_id(bot_id)
        if not clone:
            answer_callback(token, cb_id, sc("not found"), alert=True)
            view_text, markup, _ = build_clones_keyboard(page)
            edit_message(token, chat_id, message_id, view_text, markup)
            return

        def _clone_detail_kb(c):
            ban_action = "unban" if c.get("banned") else "ban"
            ban_label = ("✅ " + sc("unban")) if c.get("banned") else ("🚫 " + sc("ban"))
            return {"inline_keyboard": [
                [{"text": ban_label, "callback_data": f"cl:{ban_action}:{bot_id}:{page}"}],
                [{"text": "🗑 " + sc("delete"), "callback_data": f"cl:rm:{bot_id}:{page}"}],
                [{"text": "⬅️ " + sc("back"), "callback_data": f"cl:back:{page}"}],
            ]}

        if action == "view":
            edit_message(token, chat_id, message_id, build_bot_detail_text(clone, bot_id), _clone_detail_kb(clone))
            answer_callback(token, cb_id)
            return

        if action == "ban":
            tg_call(clone["_id"], "deleteWebhook", {})
            bots_col.update_one({"_id": clone["_id"]}, {"$set": {"banned": True}})
            answer_callback(token, cb_id, sc("banned"))
            clone["banned"] = True
            edit_message(token, chat_id, message_id, build_bot_detail_text(clone, bot_id), _clone_detail_kb(clone))
            return

        if action == "unban":
            tg_call(clone["_id"], "setWebhook", {"url": f"{BASE_URL}/webhook/{clone['_id']}"})
            bots_col.update_one({"_id": clone["_id"]}, {"$set": {"banned": False}})
            answer_callback(token, cb_id, sc("unbanned"))
            clone["banned"] = False
            edit_message(token, chat_id, message_id, build_bot_detail_text(clone, bot_id), _clone_detail_kb(clone))
            return

        if action == "rm":
            uname = clone.get("username") or bot_id
            confirm_kb = {"inline_keyboard": [[
                {"text": "✅ " + sc("confirm"), "callback_data": f"cl:rmyes:{bot_id}:{page}"},
                {"text": "✖️ " + sc("cancel"), "callback_data": f"cl:view:{bot_id}:{page}"},
            ]]}
            edit_message(token, chat_id, message_id, f"{sc('remove')} @{uname}?", confirm_kb)
            answer_callback(token, cb_id)
            return

        if action == "rmyes":
            tg_call(clone["_id"], "deleteWebhook", {})
            bots_col.delete_one({"_id": clone["_id"]})
            answer_callback(token, cb_id, sc("removed"))
            view_text, markup, _ = build_clones_keyboard(page)
            edit_message(token, chat_id, message_id, view_text, markup)
            return

    # ── /mybots actions (any user, own clones only) ────────────────────────
    if data.startswith("mb:"):
        parts = data.split(":")
        action = parts[1]

        if action in ("cancel", "back"):
            view_text, markup = build_mybots_view(from_id)
            edit_message(token, chat_id, message_id, view_text, markup)
            answer_callback(token, cb_id)
            return

        bot_id = parts[2]
        clone = get_bot_by_id(bot_id)
        if not clone or clone.get("owner_id") != from_id:
            answer_callback(token, cb_id, sc("not yours"), alert=True)
            return

        if action == "view":
            detail_kb = {"inline_keyboard": [
                [{"text": "🗑 " + sc("delete"), "callback_data": f"mb:rm:{bot_id}"}],
                [{"text": "⬅️ " + sc("back"), "callback_data": "mb:back"}],
            ]}
            edit_message(token, chat_id, message_id, build_bot_detail_text(clone, bot_id), detail_kb)
            answer_callback(token, cb_id)
            return

        if action == "rm":
            uname = clone.get("username") or bot_id
            confirm_kb = {"inline_keyboard": [[
                {"text": "✅ " + sc("confirm"), "callback_data": f"mb:rmyes:{bot_id}"},
                {"text": "✖️ " + sc("cancel"), "callback_data": f"mb:view:{bot_id}"},
            ]]}
            edit_message(token, chat_id, message_id, f"{sc('remove your clone')} @{uname}?", confirm_kb)
            answer_callback(token, cb_id)
            return

        if action == "rmyes":
            tg_call(clone["_id"], "deleteWebhook", {})
            bots_col.delete_one({"_id": clone["_id"]})
            answer_callback(token, cb_id, sc("removed"))
            view_text, markup = build_mybots_view(from_id)
            edit_message(token, chat_id, message_id, view_text, markup)
            return


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "Bot is alive."


@app.route("/webhook/<token>", methods=["POST"])
def webhook(token):
    ensure_main_bot()
    update = request.get_json(force=True, silent=True) or {}

    bot_doc = get_bot_doc(token)
    if not bot_doc or bot_doc.get("banned"):
        return jsonify(ok=True)

    # ── Button taps ──────────────────────────────────────────────────────────
    callback = update.get("callback_query")
    if callback:
        handle_callback(token, bot_doc, callback)
        return jsonify(ok=True)

    # ── Regular messages ─────────────────────────────────────────────────────
    message = update.get("message")
    if not message or "text" not in message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    from_id = message["from"]["id"]
    text = message["text"].strip()
    bot_id = bot_id_of(token, bot_doc)

    # Exact command token (not startswith) — "/clones" must never match a
    # "/clone" check just because it happens to start with the same letters.
    cmd = text.split(maxsplit=1)[0].split("@")[0] if text else ""

    track_user(bot_id, from_id, message["from"].get("first_name"), message["from"].get("username"))

    is_owner = from_id == bot_doc.get("owner_id")
    is_main_owner = from_id == OWNER_ID
    is_main = bot_doc.get("is_main", False)

    if not bot_doc.get("commands_set"):
        # Backfill for bots registered before command menus existed.
        set_bot_commands(token, MAIN_BOT_COMMANDS if is_main else CLONE_BOT_COMMANDS)
        bots_col.update_one({"_id": token}, {"$set": {"commands_set": True}})

    # ── /start [payload] — rewrite deep link ────────────────────────────────
    if cmd == "/start":
        parts = text.split(maxsplit=1)
        payload = parts[1] if len(parts) > 1 else None
        target = bot_doc.get("target_username")

        if not target:
            send_message(token, chat_id, "⚠️ " + sc("this bot isn't configured yet"))
            return jsonify(ok=True)

        link = f"https://t.me/{target}" + (f"?start={payload}" if payload else "")
        reply_markup = {"inline_keyboard": [[{"text": "🔗 " + sc("here's your link"), "url": link}]]}
        send_message(token, chat_id, sc("tap below to continue") + " 👇", reply_markup)
        return jsonify(ok=True)

    # ── /username <name> — set this bot's deep-link redirect target ────────
    if cmd == "/username":
        if not (is_owner or is_main_owner):
            send_message(token, chat_id, "⚠️ " + sc("only the bot owner can set this"))
            return jsonify(ok=True)

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(token, chat_id, "Usage: /username <bot_username>\nExample: /username Guts_Store_Bot")
            return jsonify(ok=True)

        uname = parts[1].strip().lstrip("@")
        bots_col.update_one({"_id": token}, {"$set": {"target_username": uname}})
        send_message(token, chat_id, f"✅ {sc('target set to')} @{uname}")
        return jsonify(ok=True)

    # ── /clone <bot_token> — register a new clone (main bot only) ──────────
    if cmd == "/clone" and is_main:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(token, chat_id, "Usage: /clone <bot_token>")
            return jsonify(ok=True)

        new_token = parts[1].strip()
        me = tg_call(new_token, "getMe", {})
        if not me.get("ok"):
            send_message(token, chat_id, "❌ " + sc("that doesn't look like a valid bot token"))
            return jsonify(ok=True)

        set_wh = tg_call(new_token, "setWebhook", {"url": f"{BASE_URL}/webhook/{new_token}"})
        if not set_wh.get("ok"):
            send_message(token, chat_id, "❌ " + sc("could not set webhook") + f": {set_wh.get('description', '?')}")
            return jsonify(ok=True)

        set_bot_commands(new_token, CLONE_BOT_COMMANDS)

        bot_result = me["result"]
        bots_col.update_one(
            {"_id": new_token},
            {"$set": {
                "owner_id": from_id,
                "is_main": False,
                "target_username": None,
                "parent_token": MAIN_BOT_TOKEN,
                "bot_id": str(bot_result["id"]),
                "username": bot_result.get("username"),
                "name": bot_result.get("first_name"),
                "banned": False,
                "commands_set": True,
            }},
            upsert=True,
        )

        send_message(
            token, chat_id,
            f"✅ {sc('clone created')}: @{bot_result.get('username')}\n\n"
            f"{sc('message it directly with')} /username <target> {sc('to configure it')}"
        )
        return jsonify(ok=True)

    # ── /clones — total + toggle-button management (main owner only) ───────
    if cmd == "/clones" and is_main and is_main_owner:
        view_text, markup, _ = build_clones_keyboard(0)
        send_message(token, chat_id, view_text, markup)
        return jsonify(ok=True)

    # ── /mybots — any user removes their own clone(s) (main bot) ───────────
    if cmd == "/mybots" and is_main:
        view_text, markup = build_mybots_view(from_id)
        send_message(token, chat_id, view_text, markup)
        return jsonify(ok=True)

    # ── /unclone — a clone owner removes THIS bot directly, no need to
    #   go back to the main bot's /mybots ─────────────────────────────────
    if cmd == "/unclone" and not is_main and (is_owner or is_main_owner):
        send_message(token, chat_id, "✅ " + sc("this clone has been removed"))
        tg_call(token, "deleteWebhook", {})
        bots_col.delete_one({"_id": token})
        return jsonify(ok=True)

    # ── /users — this bot's own user stats (owner or main owner) ───────────
    if cmd == "/users" and (is_owner or is_main_owner):
        col = get_users_collection(bot_id)
        total = col.count_documents({})
        blocked = col.count_documents({"blocked": True})
        active = total - blocked
        msg = (
            f"{sc('users overview')}\n\n"
            f"{sc('total')}: {total}\n"
            f"{sc('active')}: {active}\n"
            f"{sc('blocked')}: {blocked}"
        )
        send_message(token, chat_id, msg)
        return jsonify(ok=True)

    # ── /refresh — sweep + remove dead/blocked users for THIS bot ──────────
    if cmd == "/refresh" and (is_owner or is_main_owner):
        col = get_users_collection(bot_id)
        cursor_key = f"refresh_cursor_{bot_id}"
        cursor_doc = meta_col.find_one({"_id": cursor_key}) or {}
        last_id = cursor_doc.get("last_id", 0)

        query = {"_id": {"$gt": last_id}} if last_id else {}
        batch = list(col.find(query).sort("_id", 1).limit(REFRESH_BATCH))

        if not batch:
            meta_col.update_one({"_id": cursor_key}, {"$set": {"last_id": 0}}, upsert=True)
            send_message(token, chat_id, "✅ " + sc("sweep complete — starting over next time"))
            return jsonify(ok=True)

        checked = 0
        removed = 0
        last_checked_id = last_id
        for u in batch:
            checked += 1
            last_checked_id = u["_id"]
            result = tg_call(token, "sendChatAction", {"chat_id": u["_id"], "action": "typing"})
            if not result.get("ok"):
                desc = str(result.get("description", "")).lower()
                if "blocked" in desc or "deactivated" in desc or "not found" in desc:
                    col.delete_one({"_id": u["_id"]})
                    removed += 1

        meta_col.update_one({"_id": cursor_key}, {"$set": {"last_id": last_checked_id}}, upsert=True)
        msg = (
            f"{sc('refresh batch complete')}\n\n"
            f"{sc('checked')}: {checked}\n"
            f"{sc('removed')}: {removed}\n\n"
            f"{sc('run again to continue the sweep')}"
        )
        send_message(token, chat_id, msg)
        return jsonify(ok=True)

    # ── /setusername <clone_token> <name> — main owner overrides a clone ───
    if cmd == "/setusername" and is_main and is_main_owner:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_message(token, chat_id, "Usage: /setusername <clone_token> <bot_username>")
            return jsonify(ok=True)

        clone_token, uname = parts[1], parts[2].strip().lstrip("@")
        result = bots_col.update_one(
            {"_id": clone_token, "parent_token": MAIN_BOT_TOKEN},
            {"$set": {"target_username": uname}},
        )
        send_message(token, chat_id, "✅ " + sc("updated") if result.matched_count else "❌ " + sc("clone not found"))
        return jsonify(ok=True)

    # ── /delclone <clone_token> — fallback text command (main owner) ───────
    if cmd == "/delclone" and is_main and is_main_owner:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(token, chat_id, "Usage: /delclone <clone_token>")
            return jsonify(ok=True)
        clone_token = parts[1].strip()
        tg_call(clone_token, "deleteWebhook", {})
        result = bots_col.delete_one({"_id": clone_token, "parent_token": MAIN_BOT_TOKEN})
        send_message(token, chat_id, "✅ " + sc("removed") if result.deleted_count else "❌ " + sc("clone not found"))
        return jsonify(ok=True)

    return jsonify(ok=True)

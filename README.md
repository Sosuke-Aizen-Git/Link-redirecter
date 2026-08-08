# Deep-Link Username Rewriter Bot

Rewrites `/start <payload>` deep links to a different bot username, configured
per-bot. Supports unlimited clones, each independently configurable, all
controllable by the main bot owner. Runs entirely on Vercel's free serverless
tier — no polling, pure webhooks.

## How it works

- One Vercel deployment serves **every** bot (main bot + all clones) through
  a single dynamic route: `POST /webhook/<bot_token>`.
- Telegram pushes each bot's updates to its own webhook URL, which is just
  the app URL + that bot's token. No background process needed.
- MongoDB stores one document per bot: owner, whether it's the main bot,
  its configured target username, and (for clones) which main bot spawned it.

## 1. Prerequisites

- A Telegram bot token from [@BotFather](https://t.me/BotFather) → this becomes `MAIN_BOT_TOKEN`
- A free MongoDB Atlas cluster → connection string becomes `MONGO_URL`
- A [Vercel](https://vercel.com) account (free tier is enough)
- Your numeric Telegram user ID → `OWNER_ID` (get it from [@userinfobot](https://t.me/userinfobot))

## 2. Deploy

1. Push this folder to a GitHub repo.
2. In Vercel: **New Project → Import** that repo.
3. Add Environment Variables in the Vercel project settings:
   - `MONGO_URL` — your MongoDB Atlas connection string
   - `MAIN_BOT_TOKEN` — your main bot's token
   - `OWNER_ID` — your Telegram numeric user ID
   - `BASE_URL` — your Vercel deployment URL, e.g. `https://your-app.vercel.app`
     (you'll know this after the first deploy — redeploy once you have it, or
     set it via a custom domain up front)
4. Deploy.

## 3. Register the main bot's webhook (one-time, manual)

Open this URL in a browser (replace the placeholders):

```
https://api.telegram.org/bot<MAIN_BOT_TOKEN>/setWebhook?url=<BASE_URL>/webhook/<MAIN_BOT_TOKEN>
```

You should see `"ok": true`. That's it — the main bot is live.

## 4. Commands

**On the main bot:**
| Command | Who | Description |
|---|---|---|
| `/username <name>` | owner | Set the main bot's own deep-link redirect target |
| `/clone <bot_token>` | anyone | Register a new bot as a clone (auto-sets its webhook) |
| `/clones` | main owner only | Total count + inline **Ban/Unban/Remove** buttons per clone, paginated |
| `/mybots` | anyone | Lists *your own* clones with a **Remove** button — no need to bother the main owner |
| `/users` | main owner | Total / active / blocked counts for the main bot's *own* users |
| `/refresh` | main owner | Sweeps the main bot's own users in batches of 25, removes anyone blocked/deactivated |
| `/setusername <clone_token> <name>` | main owner only | Text-command fallback to override any clone's target |
| `/delclone <clone_token>` | main owner only | Text-command fallback to remove a clone by token |

**On any cloned bot:**
| Command | Who | Description |
|---|---|---|
| `/username <name>` | that clone's owner | Set this clone's own deep-link redirect target |
| `/unclone` | that clone's owner (or main owner) | Removes this clone immediately — no need to go through `/mybots` on the main bot |
| `/users` | that clone's owner (or main owner) | Total / active / blocked counts for **this clone's own** users |
| `/refresh` | that clone's owner (or main owner) | Sweeps **this clone's own** users, removes anyone blocked/deactivated |

`/users` and `/refresh` work identically on every bot — each one only ever
sees and manages its own users, never another bot's.

**On any bot (main or clone), for end users:**
- `https://t.me/YourBot?start=abc123` → bot replies with an inline button
  linking to `https://t.me/<configured_target>?start=abc123`

## How the button UI works

- **`/clones`** shows every clone (5 per page) with a status line (✅ active /
  🚫 banned) and two buttons: toggle **Ban/Unban** and **Remove**. Banning
  deletes that bot's Telegram webhook immediately (it stops responding to
  anyone); unbanning re-registers it. Remove asks for confirmation, then
  deletes the clone's webhook and its database entry permanently.
- **`/mybots`** is the same idea scoped to whoever sent the command — each
  user only sees and can remove clones where they're the recorded owner.
  Ownership is checked server-side on every button tap, not just when the
  list is built.

## Data model notes

- Any clones registered **before** this update won't have the `bot_id` /
  `username` / `banned` fields the new UI relies on — they'll need to be
  re-added via `/clone` to show up correctly in `/clones` or `/mybots`.
- **Users are isolated per bot.** Instead of one shared collection, every
  bot — main and each clone — gets its own MongoDB collection named
  `users_<bot_id>` (e.g. `users_6334890925`), created automatically the
  first time someone messages that bot. A user who's blocked or removed
  on one bot is untouched on every other bot. `/refresh` pings users using
  that specific bot's own token, so blocking is detected per-bot too — a
  user blocking your clone doesn't affect their status on the main bot.
- If you had the earlier single shared `users` collection from a previous
  version of this bot, it's no longer written to or read from — old data
  there is harmless but unused; delete it in Atlas if you want to tidy up.

## Notes / limits

- Vercel's free (Hobby) tier caps serverless function execution at 10s.
  Everything here fits comfortably except `/refresh`, which is deliberately
  batched (25 users per run) to stay well under that limit — just send it
  again to keep sweeping a large user base.
- Bot tokens are stored in MongoDB. Treat your `MONGO_URL` as a secret and
  restrict database access (IP allowlist / strong password) in Atlas.
- If a cloned bot's owner deletes their bot via @BotFather, calls to it will
  simply start failing — `/refresh` doesn't touch clones, only users, so
  prune dead clones manually via `/clones` if this happens.
- No persistent connections are used anywhere, so this scales to as many
  clones and users as your MongoDB free tier and Vercel invocation limits
  allow.

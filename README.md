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
| `/setusername <clone_token> <name>` | main owner only | Override any clone's target |
| `/clones` | main owner only | List all clones and their configured targets |
| `/delclone <clone_token>` | main owner only | Remove a clone and delete its webhook |

**On any cloned bot:**
| Command | Who | Description |
|---|---|---|
| `/username <name>` | that clone's owner | Set this clone's own deep-link redirect target |

**On any bot (main or clone), for end users:**
- `https://t.me/YourBot?start=abc123` → bot replies with an inline button
  linking to `https://t.me/<configured_target>?start=abc123`

## Notes / limits

- Vercel's free (Hobby) tier caps serverless function execution at 10s —
  plenty for this, since each request is just a couple of HTTP calls.
- Bot tokens are stored in MongoDB. Treat your `MONGO_URL` as a secret and
  restrict database access (IP allowlist / strong password) in Atlas.
- If a cloned bot's owner deletes their bot via @BotFather, calls to it will
  simply start failing — consider periodically pruning dead clones.
- No persistent connections are used anywhere, so this scales to as many
  clones as your MongoDB free tier and Vercel invocation limits allow.

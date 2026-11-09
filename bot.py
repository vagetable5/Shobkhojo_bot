"""
ShobKhojo video-delivery bot (made for Render) - token-protected version.

The website sends visitors to:  https://t.me/Shobkhojo0_bot?start=t_<one-time-token>
The token is created by the website after the timer, works once, and expires in 10 minutes.
Plain links like ?start=video_18 now work for the ADMIN only.

Environment variables (same as before):
  BOT_TOKEN, CHANNEL_ID, ADMIN_TELEGRAM_ID, SUPABASE_URL, SUPABASE_SERVICE_KEY
  optional: SITE_URL, PROTECT_CONTENT (default 1), AUTO_DELETE_MINUTES (default 1440 = 1 day)
"""
import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
_raw_channel = os.environ["CHANNEL_ID"].strip()
CHANNEL_ID = int(_raw_channel) if re.fullmatch(r"-?\d+", _raw_channel) else _raw_channel
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
SITE = os.environ.get("SITE_URL", "https://shobkhojo.netlify.app").rstrip("/")
PROTECT = os.environ.get("PROTECT_CONTENT", "1") == "1"
AUTO_DELETE = int(os.environ.get("AUTO_DELETE_MINUTES", "1440"))
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
MAX_RANGE = 50
TOKEN_MIN = 10  # minutes a website token stays valid

if not (SB_URL and SB_KEY):
    raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required (token check + auto-delete).")

HEAD = {"apikey": SB_KEY}
if SB_KEY.startswith("eyJ"):
    HEAD["Authorization"] = f"Bearer {SB_KEY}"

log = logging.getLogger("shobkhojo-bot")


# ---------- one-time website tokens ----------
async def consume_token(tok):
    """Deletes the token and returns (first_id, last_id), or None if used/expired."""
    cut = (datetime.now(timezone.utc) - timedelta(minutes=TOKEN_MIN)).isoformat()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.delete(f"{SB_URL}/rest/v1/dl_tokens", headers={**HEAD, "Prefer": "return=representation"},
                           params={"token": f"eq.{tok}", "created_at": f"gt.{cut}"})
        r.raise_for_status()
        rows = r.json()
    if not rows:
        return None
    first = int(rows[0]["msg_id"])
    return first, int(rows[0].get("msg_last") or first)


async def in_maintenance():
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{SB_URL}/rest/v1/settings", headers=HEAD,
                            params={"key": "eq.maintenance", "select": "value"})
            rows = r.json()
        return bool(rows) and rows[0].get("value") == "true"
    except Exception:
        return False


# ---------- delete-later queue (stored in Supabase table bot_deletions) ----------
async def queue_deletions(chat_id, message_ids):
    when = (datetime.now(timezone.utc) + timedelta(minutes=AUTO_DELETE)).isoformat()
    rows = [{"chat_id": chat_id, "message_id": m, "delete_at": when} for m in message_ids]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{SB_URL}/rest/v1/bot_deletions", json=rows, headers=HEAD)
        r.raise_for_status()


async def run_deletions(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.delete(f"{SB_URL}/rest/v1/dl_tokens", headers=HEAD,
                           params={"created_at": f"lt.{(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()}"})
            r = await c.get(f"{SB_URL}/rest/v1/bot_deletions", headers=HEAD,
                            params={"select": "id,chat_id,message_id", "delete_at": f"lte.{now}", "limit": "100"})
            r.raise_for_status()
            rows = r.json()
            for row in rows:
                try:
                    await ctx.bot.delete_message(row["chat_id"], row["message_id"])
                except Exception:
                    pass
            if rows:
                ids = ",".join(str(x["id"]) for x in rows)
                await c.delete(f"{SB_URL}/rest/v1/bot_deletions", headers=HEAD, params={"id": f"in.({ids})"})
    except Exception:
        log.exception("deletion job failed")


# ---------- handlers ----------
async def start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await u.message.reply_text(f"ShobKhojo-তে স্বাগতম! 🎬\nভিডিও পেতে সাইট থেকে লিংকে চাপুন:\n{SITE}")
    arg = ctx.args[0]
    is_admin = u.effective_user.id == ADMIN_ID

    if arg.startswith("t_"):
        tok = arg[2:]
        if not tok.isalnum() or len(tok) > 64:
            return await u.message.reply_text("❌ লিংকটি সঠিক নয়। সাইট থেকে আবার চেষ্টা করুন।")
        if not is_admin and await in_maintenance():
            return await u.message.reply_text("🛠️ রক্ষণাবেক্ষণের কাজ চলছে। কিছুক্ষণ পর আবার চেষ্টা করুন।")
        try:
            rng = await consume_token(tok)
        except Exception:
            log.exception("token check failed")
            return await u.message.reply_text("⚠️ একটু সমস্যা হয়েছে, কিছুক্ষণ পর আবার চেষ্টা করুন।")
        if not rng:
            return await u.message.reply_text(f"⌛ এই লিংকের মেয়াদ শেষ অথবা আগেই ব্যবহার হয়ে গেছে। সাইট থেকে আবার খুলুন:\n{SITE}")
        first, last = rng
    else:
        m = re.fullmatch(r"(?:video_)?(\d{1,12})(?:_(\d{1,12}))?", arg)
        if not m or not is_admin:
            return await u.message.reply_text(f"🔗 ভিডিও পেতে সাইটে গিয়ে বাটনে চাপুন:\n{SITE}")
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2) else first

    if last < first or last - first + 1 > MAX_RANGE:
        return await u.message.reply_text("❌ লিংকটি সঠিক নয়।")

    chat_id = u.effective_chat.id
    sent, failed = [], 0
    for mid in range(first, last + 1):
        try:
            res = await ctx.bot.copy_message(chat_id=chat_id, from_chat_id=CHANNEL_ID,
                                             message_id=mid, protect_content=PROTECT)
            sent.append(res.message_id)
        except Exception as e:
            failed += 1
            log.warning("copy %s failed: %s", mid, e)
        await asyncio.sleep(0.3)

    if not sent:
        return await u.message.reply_text("❌ ভিডিওটি পাওয়া যায়নি। সম্ভবত মুছে ফেলা হয়েছে।")
    if failed:
        await u.message.reply_text(f"⚠️ {failed}টি ফাইল পাঠানো যায়নি।")
    if AUTO_DELETE:
        hours = AUTO_DELETE / 60
        label = f"{hours:g} ঘণ্টা" if AUTO_DELETE % 60 == 0 else f"{AUTO_DELETE} মিনিট"
        note = await u.message.reply_text(
            f"⏳ ভিডিওটি {label} পরে মুছে যাবে। আবার দেখতে সাইট থেকে আসুন।" + ("\n🔒 ডাউনলোড বা ফরোয়ার্ড করা যাবে না।" if PROTECT else ""))
        try:
            await queue_deletions(chat_id, sent + [note.message_id])
        except Exception:
            log.exception("could not queue deletion")


async def on_channel_post(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """New file in the channel -> DM the admin the post ID to paste into the website."""
    m = u.channel_post
    media = m.video or m.document or m.audio
    name = m.caption or (getattr(media, "file_name", None) if media else None) or "Untitled"
    try:
        await ctx.bot.send_message(ADMIN_ID, f"✅ নতুন ফাইল সেভ হয়েছে\n📝 {name[:80]}\n🆔 Post ID: {m.message_id}\n(ওয়েবসাইটের Telegram post ID ঘরে এটি বসান)")
    except Exception:
        log.exception("Could not DM admin. Open the bot and press /start first.")


async def series(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        a, b = int(ctx.args[0]), int(ctx.args[1])
        assert 0 < a <= b and b - a + 1 <= MAX_RANGE
    except Exception:
        return await u.message.reply_text(f"ব্যবহার: /series প্রথম_আইডি শেষ_আইডি (সর্বোচ্চ {MAX_RANGE}টি)")
    await u.message.reply_text(f"🆔 ওয়েবসাইটের Telegram post ID ঘরে বসান: {a}-{b}")


class Health(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *a):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), Health).serve_forever, daemon=True).start()


def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("series", series, filters=filters.User(user_id=ADMIN_ID)))
    chat_filter = filters.Chat(chat_id=CHANNEL_ID) if isinstance(CHANNEL_ID, int) else filters.Chat(username=CHANNEL_ID.lstrip("@"))
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & chat_filter
        & (filters.VIDEO | filters.Document.ALL | filters.AUDIO), on_channel_post))
    app.job_queue.run_repeating(run_deletions, interval=60, first=30)
    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

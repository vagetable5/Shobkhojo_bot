"""
ShobKhojo video-delivery bot (made for Render).

Link format for the website's "Destination link":
    https://t.me/Shobkhojo0_bot?start=video_18        (18 = the post number in your channel)
    https://t.me/Shobkhojo0_bot?start=video_18_25     (series: posts 18 to 25, max 50)

Videos are sent with forwarding/saving blocked and deleted from the user's chat after
AUTO_DELETE_MINUTES (default 300 = 5 hours). Deletion times are stored in Supabase, so they
survive Render restarts.

Environment variables (set them in Render > Environment):
  BOT_TOKEN, CHANNEL_ID, ADMIN_TELEGRAM_ID, SUPABASE_URL, SUPABASE_SERVICE_KEY
  optional: SITE_URL, PROTECT_CONTENT (default 1), AUTO_DELETE_MINUTES (default 300)
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
CHANNEL_ID = os.environ["CHANNEL_ID"]
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
SITE = os.environ.get("SITE_URL", "https://shobkhojo.netlify.app").rstrip("/")
PROTECT = os.environ.get("PROTECT_CONTENT", "1") == "1"
AUTO_DELETE = int(os.environ.get("AUTO_DELETE_MINUTES", "300"))
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
MAX_RANGE = 50

if AUTO_DELETE and not (SB_URL and SB_KEY):
    raise SystemExit("AUTO_DELETE_MINUTES is on, so SUPABASE_URL and SUPABASE_SERVICE_KEY are required.")

HEAD = {"apikey": SB_KEY}
if SB_KEY.startswith("eyJ"):
    HEAD["Authorization"] = f"Bearer {SB_KEY}"

log = logging.getLogger("shobkhojo-bot")


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
            r = await c.get(f"{SB_URL}/rest/v1/bot_deletions", headers=HEAD,
                            params={"select": "id,chat_id,message_id", "delete_at": f"lte.{now}", "limit": "100"})
            r.raise_for_status()
            rows = r.json()
            for row in rows:
                try:
                    await ctx.bot.delete_message(row["chat_id"], row["message_id"])
                except Exception:
                    pass  # already deleted by the user, or too old
            if rows:
                ids = ",".join(str(x["id"]) for x in rows)
                await c.delete(f"{SB_URL}/rest/v1/bot_deletions", headers=HEAD, params={"id": f"in.({ids})"})
    except Exception:
        log.exception("deletion job failed")


# ---------- handlers ----------
async def start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await u.message.reply_text(f"ShobKhojo-তে স্বাগতম! 🎬\nভিডিও পেতে সাইট থেকে লিংকে চাপুন:\n{SITE}")
    m = re.fullmatch(r"(?:video_)?(\d{1,12})(?:_(\d{1,12}))?", ctx.args[0])
    if not m:
        return await u.message.reply_text("❌ লিংকটি সঠিক নয়। সাইট থেকে আবার চেষ্টা করুন।")
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
        await asyncio.sleep(0.3)  # stay under Telegram flood limits

    if not sent:
        return await u.message.reply_text("❌ ভিডিওটি পাওয়া যায়নি। সম্ভবত মুছে ফেলা হয়েছে।")
    if failed:
        await u.message.reply_text(f"⚠️ {failed}টি ফাইল পাঠানো যায়নি।")
    if AUTO_DELETE:
        hours = AUTO_DELETE / 60
        label = f"{hours:g} ঘণ্টা" if AUTO_DELETE % 60 == 0 else f"{AUTO_DELETE} মিনিট"
        note = await u.message.reply_text(
            f"⏳ ভিডিওটি {label} পরে মুছে যাবে।" + ("\n🔒 ডাউনলোড বা ফরোয়ার্ড করা যাবে না।" if PROTECT else ""))
        try:
            await queue_deletions(chat_id, sent + [note.message_id])
        except Exception:
            log.exception("could not queue deletion")


async def on_channel_post(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """New file in the channel -> DM the admin the website link."""
    m = u.channel_post
    media = m.video or m.document or m.audio
    name = m.caption or (getattr(media, "file_name", None) if media else None) or "Untitled"
    link = f"https://t.me/{ctx.bot.username}?start=video_{m.message_id}"
    try:
        await ctx.bot.send_message(ADMIN_ID, f"✅ নতুন ফাইল সেভ হয়েছে\n📝 {name[:80]}\n🔗 {link}")
    except Exception:
        log.exception("Could not DM admin. Open the bot and press /start first.")


async def series(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        a, b = int(ctx.args[0]), int(ctx.args[1])
        assert 0 < a <= b and b - a + 1 <= MAX_RANGE
    except Exception:
        return await u.message.reply_text(f"ব্যবহার: /series প্রথম_আইডি শেষ_আইডি (সর্বোচ্চ {MAX_RANGE}টি)\nউদাহরণ: /series 120 130")
    await u.message.reply_text(f"🔗 https://t.me/{ctx.bot.username}?start=video_{a}_{b}")


# ---------- tiny web server so Render sees an open port (and UptimeRobot can ping it) ----------
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
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & filters.Chat(CHANNEL_ID)
        & (filters.VIDEO | filters.Document.ALL | filters.AUDIO), on_channel_post))
    if AUTO_DELETE:
        app.job_queue.run_repeating(run_deletions, interval=60, first=30)
    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import os, asyncio, aiohttp, logging
from datetime import datetime, timedelta, timezone
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

logging.basicConfig(level=logging.INFO)

E = os.environ
SB = E["SUPABASE_URL"] + "/rest/v1"
H = {"apikey": E["SERVICE_KEY"], "Content-Type": "application/json"}
LOG = E["LOG_CHANNEL"]
ADMIN = int(E["ADMIN_ID"])
HOURS = 3
app = None

def ts(dt): return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

async def get(path):
    async with aiohttp.ClientSession() as s:
        async with s.get(SB + path, headers=H) as r:
            return await r.json()

async def schedule(chat, *ids):
    t = ts(datetime.now(timezone.utc) + timedelta(hours=HOURS))
    async with aiohttp.ClientSession() as s:
        await s.post(SB + "/bot_deletes", headers=H, json=[{"chat_id": chat, "message_id": i, "delete_at": t} for i in ids])

async def start(c, m):
    print("START PELAM:", m.text, flush=True)
    try:
        if len(m.command) < 2 or not m.command[1].startswith("video_"):
            return await m.reply("🔥 Viral Video বটে স্বাগতম! ওয়েবসাইট থেকে ভিডিও লিংকে ক্লিক করুন।")
        mid = int(m.command[1][6:])
        r = await get(f"/post_links?tg_msg_id=eq.{mid}&select=posts(type)")
        print("DB REPLY:", r, flush=True)
        if not r or not isinstance(r, list):
            return await m.reply("❌ ভিডিও পাওয়া যায়নি।")
        if r[0]["posts"]["type"] == "premium":
            u = m.from_user.username or "-"
            ok = await get(f"/profiles?tg_username=ilike.{u}&status=eq.approved&select=id")
            if not ok or not isinstance(ok, list):
                return await m.reply("🔒 প্রিমিয়াম! ওয়েবসাইটে রেজিস্ট্রেশনের সময় এই Telegram username দিন এবং অ্যাডমিনের অনুমোদন নিন।")
        v = await c.copy_message(m.chat.id, LOG, mid, protect_content=True)
        n = await m.reply(f"⏳ এই ভিডিও {HOURS} ঘণ্টা পর অটো ডিলিট হয়ে যাবে। আবার দেখতে হলে ওয়েবসাইট থেকে আসুন।")
        await schedule(m.chat.id, v.id, n.id, m.id)
    except Exception as e:
        print("START ERROR:", repr(e), flush=True)
        try: await m.reply("⚠️ এরর: " + str(e)[:200])
        except Exception as e2: print("REPLY ERROR:", repr(e2), flush=True)

async def anymsg(c, m):
    print("MESSAGE ESECHE:", m.text, flush=True)

async def sweeper():
    while True:
        try:
            now = ts(datetime.now(timezone.utc))
            for d in await get(f"/bot_deletes?delete_at=lt.{now}&select=id,chat_id,message_id"):
                try: await app.delete_messages(d["chat_id"], d["message_id"])
                except Exception as e: print(e)
                async with aiohttp.ClientSession() as s:
                    await s.delete(SB + f"/bot_deletes?id=eq.{d['id']}", headers=H)
        except Exception as e: print(e)
        await asyncio.sleep(60)

async def notify():
    while True:
        try:
            for u in await get("/profiles?notified=eq.false&select=id,email,tg_username"):
                await app.send_message(ADMIN, f"🆕 নতুন ইউজার: {u['email']} @{u['tg_username']}")
                async with aiohttp.ClientSession() as s:
                    await s.patch(SB + f"/profiles?id=eq.{u['id']}", headers=H, json={"notified": True})
        except Exception as e: print(e)
        await asyncio.sleep(30)

async def main():
    global app
    app = Client("bot", api_id=int(E["API_ID"]), api_hash=E["API_HASH"], bot_token=E["BOT_TOKEN"], in_memory=True)
    app.add_handler(MessageHandler(start, filters.command("start") & filters.private), group=0)
    app.add_handler(MessageHandler(anymsg, filters.private), group=1)
    await app.start()
    me = await app.get_me()
    print("BOT CHALU:", me.username, flush=True)
    asyncio.create_task(notify()); asyncio.create_task(sweeper())
    w = web.Application(); w.router.add_get("/", lambda r: web.Response(text="ok"))
    rn = web.AppRunner(w); await rn.setup()
    await web.TCPSite(rn, "0.0.0.0", int(E.get("PORT", 10000))).start()
    await asyncio.Event().wait()

asyncio.run(main())

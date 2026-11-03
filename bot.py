import os, asyncio, aiohttp, logging, time
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)  # নতুন পাইথনে pyrogram import এর আগে লুপ লাগে
from datetime import datetime, timedelta, timezone
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

logging.basicConfig(level=logging.INFO)

E = os.environ
LOG = E["LOG_CHANNEL"]
LOG = int(LOG) if LOG.lstrip("-").isdigit() else LOG
HOURS = float(E.get("HOURS", "3"))

# Supabase ঐচ্ছিক: না দিলে অটো-ডিলিট মেমোরিতে চলবে (বট রিস্টার্ট হলে হারাবে)
SBU = E.get("SUPABASE_URL")
SBK = E.get("SERVICE_KEY")
SB = SBU + "/rest/v1" if SBU else None
H = {"apikey": SBK, "Content-Type": "application/json"} if SBK else None
USE_DB = bool(SB and H)
MEM = []
app = None

def ts(dt): return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

async def get(path):
    async with aiohttp.ClientSession() as s:
        async with s.get(SB + path, headers=H) as r:
            return await r.json()

async def schedule(chat, *ids):
    when = datetime.now(timezone.utc) + timedelta(hours=HOURS)
    if not USE_DB:
        MEM.extend((when.timestamp(), chat, i) for i in ids)
        return
    async with aiohttp.ClientSession() as s:
        await s.post(SB + "/bot_deletes", headers=H,
                     json=[{"chat_id": chat, "message_id": i, "delete_at": ts(when)} for i in ids])

async def start(c, m):
    print("START PELAM:", m.text, flush=True)
    try:
        if len(m.command) < 2:
            return await m.reply("🔥 স্বাগতম! ওয়েবসাইট থেকে ভিডিও লিংকে ক্লিক করুন।")
        p = m.command[1]
        p = p[6:] if p.startswith("video_") else p   # start=45 বা start=video_45 দুটোই চলবে
        if not p.isdigit():
            return await m.reply("❌ ভিডিও পাওয়া যায়নি।")
        v = await c.copy_message(m.chat.id, LOG, int(p), protect_content=True)
        n = await m.reply(f"⏳ এই ভিডিও {HOURS:g} ঘণ্টা পর অটো ডিলিট হয়ে যাবে। আবার দেখতে হলে ওয়েবসাইট থেকে আসুন।")
        await schedule(m.chat.id, v.id, n.id, m.id)
    except Exception as e:
        print("START ERROR:", repr(e), flush=True)
        try: await m.reply("⚠️ ভিডিওটি পাওয়া যায়নি বা সরানো হয়েছে।")
        except Exception as e2: print("REPLY ERROR:", repr(e2), flush=True)

async def anymsg(c, m):
    print("MESSAGE ESECHE:", m.text, flush=True)

async def sweeper():
    while True:
        try:
            if USE_DB:
                now = ts(datetime.now(timezone.utc))
                for d in await get(f"/bot_deletes?delete_at=lt.{now}&select=id,chat_id,message_id"):
                    try: await app.delete_messages(d["chat_id"], d["message_id"])
                    except Exception as e: print(e)
                    async with aiohttp.ClientSession() as s:
                        await s.delete(SB + f"/bot_deletes?id=eq.{d['id']}", headers=H)
            else:
                now = time.time()
                due = [x for x in MEM if x[0] < now]
                for x in due:
                    MEM.remove(x)
                    try: await app.delete_messages(x[1], x[2])
                    except Exception as e: print(e)
        except Exception as e: print(e)
        await asyncio.sleep(60)

async def main():
    global app
    app = Client("bot", api_id=int(E["API_ID"]), api_hash=E["API_HASH"], bot_token=E["BOT_TOKEN"], in_memory=True)
    app.add_handler(MessageHandler(start, filters.command("start") & filters.private), group=0)
    app.add_handler(MessageHandler(anymsg, filters.private), group=1)
    await app.start()
    me = await app.get_me()
    print("BOT CHALU:", me.username, flush=True)
    asyncio.create_task(sweeper())
    w = web.Application(); w.router.add_get("/", lambda r: web.Response(text="ok"))
    rn = web.AppRunner(w); await rn.setup()
    await web.TCPSite(rn, "0.0.0.0", int(E.get("PORT", 10000))).start()
    await asyncio.Event().wait()

loop.run_until_complete(main())

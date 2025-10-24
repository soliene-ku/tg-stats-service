# app.py
import os, asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import pytz

from flask import Flask, request, jsonify
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

# ── Налаштування ────────────────────────────────────────────────────────────────
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]

TZ = pytz.timezone("Europe/Lisbon")  # щоб години збігались із тим, що бачиш в інтерфейсі
POSITIVE_EMOJIS = {
    "👍","❤️","🔥","👏","😁","😊","🥳","😻","✨","💯","🙌","😍","😎"
}

app = Flask(__name__)

# ── Допоміжне ──────────────────────────────────────────────────────────────────
def ts_to_lisbon(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)

async def get_client():
    return TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

# ── /hourly: гістограма за останні 7 днів (24 бін-и "hour of day") ────────────
@app.get("/hourly")
def hourly_sync():
    channel = request.args.get("channel")
    if not channel:
        return jsonify({"error": "Missing ?channel=@your_channel"}), 400
    return asyncio.run(hourly_async(channel))

async def hourly_async(channel: str):
    async with await get_client() as client:
        entity = await client.get_entity(channel)
        stats = await client(functions.stats.GetBroadcastStats(channel=entity))
        graph = getattr(stats, "top_hours_graph", None) or getattr(stats, "views_graph", None)

        points = []
        if isinstance(graph, types.StatsGraphAsync):
            loaded = await client(functions.stats.LoadAsyncGraph(token=graph.token, x=0))
            points = getattr(loaded, "points", [])
        elif isinstance(graph, types.StatsGraph):
            points = getattr(graph, "points", [])

        # Агрегація "година → сумарні перегляди за останній тиждень"
        by_hour = defaultdict(int)
        for p in points or []:
            ts = ts_to_lisbon(p.x)
            by_hour[ts.hour] += int(p.y or 0)

        # Діапазон тижня для маркування рядків
        today = datetime.now(TZ).date()
        week_end = today
        week_start = today - timedelta(days=6)
        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")

        out = []
        for h in range(24):
            out.append({
                "week_start": ws,
                "week_end": we,
                "hour": h,
                "views": by_hour.get(h, 0)
            })
        return jsonify(out)

# ── /daily: підсумки за дату (views/shares/reactions: positive/other/total) ───
@app.get("/daily")
def daily_sync():
    channel = request.args.get("channel")
    date_str = request.args.get("date")  # YYYY-MM-DD
    if not channel or not date_str:
        return jsonify({"error": "Missing ?channel=@your_channel&date=YYYY-MM-DD"}), 400
    return asyncio.run(daily_async(channel, date_str))

async def daily_async(channel: str, date_str: str):
    # Межі доби в Lisbon TZ
    day = TZ.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = day.replace(hour=23, minute=59, second=59, microsecond=0)

    total_views = 0
    total_forwards = 0
    reactions_pos = 0
    reactions_other = 0

    async with await get_client() as client:
        entity = await client.get_entity(channel)
        # Ідемо повідомленнями за день (у зростаючому порядку в межах offset_date)
        async for msg in client.iter_messages(entity, offset_date=end, reverse=True):
            if not msg.date:
                continue
            dt = msg.date.astimezone(TZ)
            if dt < start:
                continue
            if dt > end:
                break

            total_views += msg.views or 0
            total_forwards += msg.forwards or 0

            if msg.reactions and msg.reactions.results:
                for r in msg.reactions.results:
                    emoji = getattr(r.reaction, "emoticon", None)
                    if isinstance(emoji, str) and emoji in POSITIVE_EMOJIS:
                        reactions_pos += r.count
                    else:
                        reactions_other += r.count

    return jsonify({
        "date": date_str,
        "views_total": total_views,
        "shares_total": total_forwards,
        "reactions_positive": reactions_pos,
        "reactions_other": reactions_other,
        "reactions_total": reactions_pos + reactions_other
    })

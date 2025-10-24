# app.py
import os, asyncio
from datetime import datetime, timezone
from typing import List, Dict
import pytz

from flask import Flask, request, jsonify
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

# --- Налаштування ---
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]
TZ = pytz.timezone("Europe/Lisbon")

POSITIVE_EMOJIS = {
    "👍","❤️","🔥","👏","😁","😊","🥳","😻","✨","💯","🙌","😍","😎"
}

app = Flask(__name__)

def ts_to_lisbon(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)

async def get_client():
    # окрема сесія збережена у STRING_SESSION; повторний логін не потрібен
    return TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

# --------- /hourly ----------
@app.get("/hourly")
def hourly_sync():
    channel = request.args.get("channel")
    col1 = request.args.get("c1")
    col2 = request.args.get("c2")
    if not channel:
        return jsonify({"error": "Missing ?channel=@your_channel"}), 400
    return asyncio.run(hourly_async(channel, col1, col2))

async def hourly_async(channel: str, col1: str | None, col2: str | None):
    async with await get_client() as client:
        entity = await client.get_entity(channel)
        # Отримуємо статистику трансляцій
        stats = await client(functions.stats.GetBroadcastStats(channel=entity))
        # Пробуємо знайти погодинний граф
        graph = getattr(stats, "top_hours_graph", None) or getattr(stats, "views_graph", None)

        points = []
        if isinstance(graph, types.StatsGraphAsync):
            loaded = await client(functions.stats.LoadAsyncGraph(token=graph.token, x=0))
            points = getattr(loaded, "points", [])
        elif isinstance(graph, types.StatsGraph):
            points = getattr(graph, "points", [])

        out: List[Dict] = []
        for p in points or []:
            # p.x — unix timestamp у секундах, p.y — число переглядів
            ts = ts_to_lisbon(p.x)
            out.append({
                "date": ts.strftime("%Y-%m-%d"),
                "col1": col1,
                "col2": col2,
                "hour": ts.hour,
                "views": p.y,
                "channel": channel
            })
        return jsonify(out)

# --------- /daily ----------
@app.get("/daily")
def daily_sync():
    channel = request.args.get("channel")
    date_str = request.args.get("date")  # YYYY-MM-DD
    col1 = request.args.get("c1")
    col2 = request.args.get("c2")
    if not channel or not date_str:
        return jsonify({"error": "Missing ?channel=@your_channel&date=YYYY-MM-DD"}), 400
    return asyncio.run(daily_async(channel, date_str, col1, col2))

async def daily_async(channel: str, date_str: str, col1: str | None, col2: str | None):
    # межі дня в LISBON TZ
    day = TZ.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = day.replace(hour=23, minute=59, second=59, microsecond=0)

    total_views = 0
    total_forwards = 0
    reactions_pos = 0
    reactions_other = 0

    async with await get_client() as client:
        entity = await client.get_entity(channel)
        # Йдемо повідомленнями в зростаючому порядку в межах дня
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
                    # r.reaction може бути емодзі або кастомний документ (стікер)
                    emoji = getattr(r.reaction, "emoticon", None)
                    if isinstance(emoji, str) and emoji in POSITIVE_EMOJIS:
                        reactions_pos += r.count
                    else:
                        reactions_other += r.count

    return jsonify({
        "date": date_str,
        "col1": col1,
        "col2": col2,
        "views_total": total_views,
        "shares_total": total_forwards,
        "reactions_positive": reactions_pos,
        "reactions_other": reactions_other,
        "reactions_total": reactions_pos + reactions_other,
        "channel": channel
    })

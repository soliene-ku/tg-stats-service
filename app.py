# app.py
import os
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytz
from flask import Flask, jsonify, request
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import (
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
)

# ─── ENV ───────────────────────────────────────────────────────────────────────
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]

TZ = pytz.timezone("Europe/Lisbon")
POSITIVE_EMOJIS = {
    "👍", "❤️", "🔥", "👏", "😁", "😊", "🥳", "😻", "✨", "💯", "🙌", "😍", "😎"
}

app = Flask(__name__)


# ─── Helpers ───────────────────────────────────────────────────────────────────
def ts_to_lisbon(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)


async def get_client():
    # окрема користувацька сесія; повторна авторизація не потрібна
    return TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)


async def load_stats_graph_points(client, channel):
    """
    Безпечно вантажимо граф статистики.
    Повертаємо список points або пустий список, якщо граф недоступний.
    """
    try:
        entity = await client.get_entity(channel)
        stats = await client(functions.stats.GetBroadcastStats(channel=entity))
    except (UsernameInvalidError, UsernameNotOccupiedError):
        raise ValueError("Channel username not found")
    except ChannelPrivateError:
        raise PermissionError("Channel is private or not accessible")
    except ChatAdminRequiredError:
        raise PermissionError("Your account must be an admin of this channel")
    except FloodWaitError as e:
        # Краще повернути явну помилку з очікуванням
        raise RuntimeError(f"Flood wait: retry after {e.seconds} seconds")
    except Exception as e:
        # Невідоме — прокинемо вище
        raise RuntimeError(f"Failed to fetch broadcast stats: {e}")

    graph = getattr(stats, "top_hours_graph", None) or getattr(stats, "views_graph", None)
    if not graph:
        return []  # немає погодинного графа (малий/приватний канал без Statistics)

    try:
        if isinstance(graph, types.StatsGraphAsync):
            loaded = await client(functions.stats.LoadAsyncGraph(token=graph.token, x=0))
            return getattr(loaded, "points", []) or []
        elif isinstance(graph, types.StatsGraph):
            return getattr(graph, "points", []) or []
        else:
            return []
    except Exception:
        # Якщо завалився load async — віддамо порожньо, але без 500
        return []


# ─── Healthcheck ───────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return jsonify({"ok": True})


# ─── /hourly: 24 біни "hour of day" за останні 7 днів ─────────────────────────
@app.get("/hourly")
def hourly_sync():
    channel = request.args.get("channel", "").strip()
    if not channel:
        return jsonify({"error": "Missing ?channel=@your_channel"}), 400
    return asyncio.run(hourly_async(channel))


async def hourly_async(channel: str):
    async with await get_client() as client:
        try:
            points = await load_stats_graph_points(client, channel)
        except ValueError as e:
            return jsonify({"error": str(e), "channel": channel}), 400
        except PermissionError as e:
            return jsonify({"error": str(e), "channel": channel}), 403
        except RuntimeError as e:
            return jsonify({"error": str(e), "channel": channel}), 503

        # агрегуємо "hour -> sum(views) за останні 7 днів"
        by_hour = defaultdict(int)
        for p in points:
            try:
                ts = ts_to_lisbon(int(p.x))
                by_hour[ts.hour] += int(p.y or 0)
            except Exception:
                continue

        today = datetime.now(TZ).date()
        week_end = today
        week_start = today - timedelta(days=6)

        ws = week_start.strftime("%Y-%m-%d")
        we = week_end.strftime("%Y-%m-%d")

        out = [{"week_start": ws, "week_end": we, "hour": h, "views": by_hour.get(h, 0)} for h in range(24)]
        return jsonify(out)


# ─── /daily: підсумки за дату ──────────────────────────────────────────────────
@app.get("/daily")
def daily_sync():
    channel = request.args.get("channel", "").strip()
    date_str = request.args.get("date", "").strip()  # YYYY-MM-DD
    if not channel or not date_str:
        return jsonify({"error": "Missing ?channel=@your_channel&date=YYYY-MM-DD"}), 400
    return asyncio.run(daily_async(channel, date_str))


async def daily_async(channel: str, date_str: str):
    try:
        day = TZ.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    except Exception:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=0)

    total_views = 0
    total_forwards = 0
    reactions_pos = 0
    reactions_other = 0

    async with await get_client() as client:
        try:
            entity = await client.get_entity(channel)
        except (UsernameInvalidError, UsernameNotOccupiedError):
            return jsonify({"error": "Channel username not found", "channel": channel}), 400
        except ChannelPrivateError:
            return jsonify({"error": "Channel is private or not accessible", "channel": channel}), 403
        except ChatAdminRequiredError:
            return jsonify({"error": "Your account must be an admin of this channel", "channel": channel}), 403
        except Exception as e:
            return jsonify({"error": f"Failed to access channel: {e}", "channel": channel}), 503

        try:
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
        except FloodWaitError as e:
            return jsonify({"error": f"Flood wait: retry after {e.seconds} seconds"}), 503
        except Exception as e:
            return jsonify({"error": f"Failed while iterating messages: {e}"}), 503

    return jsonify({
        "date": date_str,
        "views_total": total_views,
        "shares_total": total_forwards,
        "reactions_positive": reactions_pos,
        "reactions_other": reactions_other,
        "reactions_total": reactions_pos + reactions_other,
    })

# app.py — final
import os
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytz
import telethon
from flask import Flask, jsonify, request
from telethon import TelegramClient, types
from telethon.sessions import StringSession
from telethon.errors import (
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
)
from telethon.tl.functions import stats as stats_fns  # для GetBroadcastStats / LoadAsyncGraph

# ── Settings ───────────────────────────────────────────────────────────────────
TZ = pytz.timezone("Europe/Lisbon")
POSITIVE_EMOJIS = {
    "👍", "❤️", "🔥", "👏", "😁", "😊", "🥳", "😻", "✨", "💯", "🙌", "😍", "😎"
}

app = Flask(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────
def env_ok() -> bool:
    return all(os.getenv(k) for k in ("TG_API_ID", "TG_API_HASH", "TG_STRING_SESSION"))

def ts_to_lisbon(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)

def create_client() -> TelegramClient:
    """Ліниве читання env + створення клієнта."""
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    string_session = os.getenv("TG_STRING_SESSION")
    if not api_id or not api_hash or not string_session:
        raise RuntimeError("Missing TG_API_ID / TG_API_HASH / TG_STRING_SESSION in environment")
    try:
        api_id = int(api_id)
    except Exception:
        raise RuntimeError("TG_API_ID must be an integer")
    return TelegramClient(StringSession(string_session), api_id, api_hash)

async def fetch_stats_points(client: TelegramClient, channel: str):
    """
    Акуратно тягнемо broadcast stats і повертаємо список points (або []).
    Працює з різними версіями Telethon (GetBroadcastStats / GetBroadcastStatsRequest).
    """
    # 1) канал + статистика
    entity = await client.get_entity(channel)

    if hasattr(stats_fns, "GetBroadcastStats"):
        req_stats = stats_fns.GetBroadcastStats(channel=entity)
    elif hasattr(stats_fns, "GetBroadcastStatsRequest"):
        req_stats = stats_fns.GetBroadcastStatsRequest(channel=entity)
    else:
        raise RuntimeError("Telethon has no GetBroadcastStats* in this build")

    stats = await client(req_stats)

    # 2) дістаємо граф (погодинний)
    graph = getattr(stats, "top_hours_graph", None) or getattr(stats, "views_graph", None)
    if not graph:
        return []  # у каналу немає погодинного графа

    # 3) якщо граф асинхронний — догружаємо точки
    try:
        if isinstance(graph, types.StatsGraphAsync):
            if hasattr(stats_fns, "LoadAsyncGraph"):
                req_load = stats_fns.LoadAsyncGraph(token=graph.token, x=0)
            elif hasattr(stats_fns, "LoadAsyncGraphRequest"):
                req_load = stats_fns.LoadAsyncGraphRequest(token=graph.token, x=0)
            else:
                return []
            loaded = await client(req_load)
            return getattr(loaded, "points", []) or []
        elif isinstance(graph, types.StatsGraph):
            return getattr(graph, "points", []) or []
    except Exception:
        return []

    return []

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "env_ok": env_ok(),
        "telethon": getattr(telethon, "__version__", "unknown"),
    })

# ── /hourly: 24 біни "hour of day" за останні 7 днів ──────────────────────────
@app.get("/hourly")
def hourly_sync():
    channel = request.args.get("channel", "").strip()
    if not channel:
        return jsonify({"error": "Missing ?channel=@your_channel"}), 400
    try:
        return asyncio.run(hourly_async(channel))
    except Exception as e:
        return jsonify({"error": "Unhandled error in /hourly", "detail": str(e)}), 500

async def hourly_async(channel: str):
    async with create_client() as client:
        try:
            points = await fetch_stats_points(client, channel)
        except (UsernameInvalidError, UsernameNotOccupiedError):
            return jsonify({"error": "Channel username not found", "channel": channel}), 400
        except ChannelPrivateError:
            return jsonify({"error": "Channel is private or not accessible", "channel": channel}), 403
        except ChatAdminRequiredError:
            return jsonify({"error": "Your account must be an admin of this channel", "channel": channel}), 403
        except FloodWaitError as e:
            return jsonify({"error": f"Flood wait: retry after {e.seconds} seconds"}), 503
        except RPCError as e:
            return jsonify({"error": f"Telegram RPC error: {e.__class__.__name__}", "detail": str(e)}), 409
        except Exception as e:
            return jsonify({"error": f"Failed to fetch broadcast stats: {e}"}), 503

        # агрегуємо: hour -> sum(views) за останні 7 днів
        by_hour = defaultdict(int)
        for p in points:
            try:
                ts = ts_to_lisbon(int(p.x))
                by_hour[ts.hour] += int(p.y or 0)
            except Exception:
                continue

        today = datetime.now(TZ).date()
        ws = (today - timedelta(days=6)).strftime("%Y-%m-%d")
        we = today.strftime("%Y-%m-%d")

        out = [{"week_start": ws, "week_end": we, "hour": h, "views": by_hour.get(h, 0)} for h in range(24)]
        return jsonify(out)

# ── /daily: підсумки за дату ───────────────────────────────────────────────────
@app.get("/daily")
def daily_sync():
    channel = request.args.get("channel", "").strip()
    date_str = request.args.get("date", "").strip()  # YYYY-MM-DD
    if not channel or not date_str:
        return jsonify({"error": "Missing ?channel=@your_channel&date=YYYY-MM-DD"}), 400
    try:
        return asyncio.run(daily_async(channel, date_str))
    except Exception as e:
        return jsonify({"error": "Unhandled error in /daily", "detail": str(e)}), 500

async def daily_async(channel: str, date_str: str):
    # межі доби (Лісабон)
    try:
        day = TZ.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    except Exception:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = day.replace(hour=23, minute=59, second=59, microsecond=0)

    totals = {"views": 0, "forwards": 0, "pos": 0, "other": 0}

    async with create_client() as client:
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

                totals["views"] += msg.views or 0
                totals["forwards"] += msg.forwards or 0

                if msg.reactions and msg.reactions.results:
                    for r in msg.reactions.results:
                        emoji = getattr(r.reaction, "emoticon", None)
                        if isinstance(emoji, str) and emoji in POSITIVE_EMOJIS:
                            totals["pos"] += r.count
                        else:
                            totals["other"] += r.count
        except FloodWaitError as e:
            return jsonify({"error": f"Flood wait: retry after {e.seconds} seconds"}), 503
        except RPCError as e:
            return jsonify({"error": f"Telegram RPC error: {e.__class__.__name__}", "detail": str(e)}), 409
        except Exception as e:
            return jsonify({"error": f"Failed while iterating messages: {e}"}), 503

    return jsonify({
        "date": date_str,
        "views_total": totals["views"],
        "shares_total": totals["forwards"],
        "reactions_positive": totals["pos"],
        "reactions_other": totals["other"],
        "reactions_total": totals["pos"] + totals["other"],
    })

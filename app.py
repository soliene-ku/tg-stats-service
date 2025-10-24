# app.py
import os, asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytz
from flask import Flask, jsonify, request
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import RPCError, UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError, ChatAdminRequiredError, FloodWaitError

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]

TZ = pytz.timezone("Europe/Lisbon")
POSITIVE_EMOJIS = {"👍","❤️","🔥","👏","😁","😊","🥳","😻","✨","💯","🙌","😍","😎"}

app = Flask(__name__)

def ts_to_lisbon(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)

async def get_client():
    return TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

@app.get("/")
def health():
    return jsonify({"ok": True})

# ── /hourly ────────────────────────────────────────────────────────────────────
@app.get("/hourly")
def hourly_sync():
    channel = request.args.get("channel", "").strip()
    if not channel:
        return jsonify({"error": "Missing ?channel=@your_channel"}), 400
    try:
        return asyncio.run(hourly_async(channel))
    except Exception as e:
        # підстраховка, щоб ніколи не віддавати 500 без пояснення
        return jsonify({"error": "Unhandled error in /hourly", "detail": str(e)}), 500

async def hourly_async(channel: str):
    async with await get_client() as client:
        try:
            entity = await client.get_entity(channel)
            stats = await client(functions.stats.GetBroadcastStats(channel=entity))
        except (UsernameInvalidError, UsernameNotOccupiedError):
            return jsonify({"error": "Channel username not found", "channel": channel}), 400
        except ChannelPrivateError:
            return jsonify({"error": "Channel is private or not accessible", "channel": channel}), 403
        except ChatAdminRequiredError:
            return jsonify({"error": "Your account must be an admin of this channel", "channel": channel}), 403
        except FloodWaitError as e:
            return jsonify({"error": f"Flood wait: retry after {e.seconds} seconds"}), 503
        except RPCError as e:
            # типові: STATS_MIGRATE, STATS_GRAPH_INVALID, STATS_NOT_AVAILABLE тощо
            return jsonify({"error": f"Telegram RPC error: {e.__class__.__name__}", "detail": str(e)}), 409
        except Exception as e:
            return jsonify({"error": f"Failed to fetch broadcast stats: {e}"}), 503

        graph = getattr(stats, "top_hours_graph", None) or getattr(stats, "views_graph", None)
        if not graph:
            return jsonify([])

        # завантажуємо точки графіка
        try:
            if isinstance(graph, types.StatsGraphAsync):
                loaded = await client(functions.stats.LoadAsyncGraph(token=graph.token, x=0))
                points = getattr(loaded, "points", []) or []
            elif isinstance(graph, types.StatsGraph):
                points = getattr(graph, "points", []) or []
            else:
                points = []
        except RPCError:
            # буває, коли граф недоступний — краще віддати порожньо, ніж 500
            points = []
        except Exception as e:
            return jsonify({"error": f"Failed to load stats graph: {e}"}), 503

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

# ── /daily ─────────────────────────────────────────────────────────────────────
@app.get("/daily")
def daily_sync():
    channel = request.args.get("channel", "").strip()
    date_str = request.args.get("date", "").strip()
    if not channel or not date_str:
        return jsonify({"error": "Missing ?channel=@your_channel&date=YYYY-MM-DD"}), 400
    try:
        return asyncio.run(daily_async(channel, date_str))
    except Exception as e:
        return jsonify({"error": "Unhandled error in /daily", "detail": str(e)}), 500

async def daily_async(channel: str, date_str: str):
    try:
        day = TZ.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    except Exception:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = day.replace(hour=23, minute=59, second=59, microsecond=0)

    totals = {"views": 0, "forwards": 0, "pos": 0, "other": 0}

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

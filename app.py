# app.py — final assembled
import os
import json
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
from telethon.tl.functions import stats as stats_fns

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
    """Ліниво читаємо ENV та створюємо клієнт."""
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

@app.get("/hourly_debug")
def hourly_debug_sync():
    channel = request.args.get("channel", "").strip()
    if not channel:
        return jsonify({"error": "Missing ?channel=@your_channel"}), 400
    try:
        return asyncio.run(hourly_debug_async(channel))
    except Exception as e:
        return jsonify({"error": "debug fail", "detail": str(e)}), 500

async def hourly_debug_async(channel: str):
    async with create_client() as client:
        entity = await client.get_entity(channel)

        # який саме метод є у твоїй Telethon
        use_stats = (
            stats_fns.GetBroadcastStats(channel=entity)
            if hasattr(stats_fns, "GetBroadcastStats")
            else stats_fns.GetBroadcastStatsRequest(channel=entity)
        )
        stats = await client(use_stats)

        # подивимося, які поля в stats
        attrs = [a for a in dir(stats) if "graph" in a or "hour" in a or "view" in a]

        # візьмемо перший з можливих графів
        graph = (
            getattr(stats, "top_hours_graph", None)
            or getattr(stats, "hours_graph", None)
            or getattr(stats, "views_graph", None)
        )

        info = {
            "attrs_in_stats": attrs,
            "graph_present": graph is not None,
            "graph_class": type(graph).__name__ if graph else None,
            "has_points_attr": bool(getattr(graph, "points", None)) if graph else False,
            "has_json_attr": bool(getattr(graph, "json", None)) if graph else False,
        }

        # якщо асинхр. граф — спробуймо догрузити
        if isinstance(graph, types.StatsGraphAsync):
            loader = (stats_fns.LoadAsyncGraph if hasattr(stats_fns, "LoadAsyncGraph")
                      else stats_fns.LoadAsyncGraphRequest)
            loaded = await client(loader(token=graph.token, x=0))
            info["loaded_class"] = type(loaded).__name__
            info["loaded_has_points"] = bool(getattr(loaded, "points", None))
            j = getattr(loaded, "json", None)
            info["loaded_has_json"] = bool(j)
            info["loaded_json_preview"] = (j or "")[:500]
            return jsonify(info)

        # якщо одразу JSON — дай прев’ю
        if graph is not None:
            j = getattr(graph, "json", None)
            info["json_preview"] = (j or "")[:500]
        return jsonify(info)


async def fetch_stats_points(client: TelegramClient, channel: str):
    """
    Повертає список (x, y), де x = unix timestamp, y = значення переглядів.
    Підтримує:
      - StatsGraphAsync (через LoadAsyncGraph/LoadAsyncGraphRequest)
      - StatsGraph із JSON (типовий кейс)
    """
    # 1) Канал + статистика (сумісно з різними версіями Telethon)
    entity = await client.get_entity(channel)
    if hasattr(stats_fns, "GetBroadcastStats"):
        req_stats = stats_fns.GetBroadcastStats(channel=entity)
    elif hasattr(stats_fns, "GetBroadcastStatsRequest"):
        req_stats = stats_fns.GetBroadcastStatsRequest(channel=entity)
    else:
        raise RuntimeError("Telethon has no GetBroadcastStats* in this build")
    stats = await client(req_stats)

    # 2) Дістаємо граф погодинних переглядів
    graph = (
        getattr(stats, "top_hours_graph", None)
        or getattr(stats, "views_graph", None)
        or getattr(stats, "hours_graph", None)
    )
    if not graph:
        return []

    # 3) Якщо граф асинхронний — догружаємо
    if isinstance(graph, types.StatsGraphAsync):
        if hasattr(stats_fns, "LoadAsyncGraph"):
            req_load = stats_fns.LoadAsyncGraph(token=graph.token, x=0)
        elif hasattr(stats_fns, "LoadAsyncGraphRequest"):
            req_load = stats_fns.LoadAsyncGraphRequest(token=graph.token, x=0)
        else:
            return []
        graph = await client(req_load)  # отримали фінальний граф

    # 4) Спроба напряму з .points
    pts = getattr(graph, "points", None)
    if pts:
        return [(int(p.x), int(p.y or 0)) for p in pts]

    # 5) Універсально — парсимо JSON графа
    j = getattr(graph, "json", None)
    if not j:
        return []

    try:
        data = json.loads(j)
    except Exception:
        return []

    points = []
    # Варіант A: {"items":[{"x":ts,"y":val}, ...]}
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        for it in data["items"]:
            x = it.get("x")
            y = it.get("y")
            if x is not None and y is not None:
                points.append((int(x), int(y or 0)))
    # Варіант B: {"x":[...], "y":[...]} або {"x":[...], "y":[{"data":[...]}]}
    elif isinstance(data, dict) and "x" in data and "y" in data:
        xs = data.get("x") or []
        ys = data.get("y") or []
        series = None
        if isinstance(ys, list):
            if ys and isinstance(ys[0], dict) and "data" in ys[0]:
                series = ys[0]["data"]
            else:
                series = ys
        elif isinstance(ys, dict) and "data" in ys:
            series = ys["data"]
        if isinstance(xs, list) and isinstance(series, list):
            for x, y in zip(xs, series):
                if x is not None and y is not None:
                    points.append((int(x), int(y or 0)))

    return points


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

        if not points:
            return jsonify([])

        by_hour = defaultdict(int)
        for x, y in points:
            ts = ts_to_lisbon(int(x))
            by_hour[ts.hour] += int(y)

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
    end = day.replace(hour=23, minute=59, second=59, microsecond=0)

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

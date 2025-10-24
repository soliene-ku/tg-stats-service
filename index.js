// app.js — stable version
import 'dotenv/config';
import express from 'express';
import { DateTime } from 'luxon';
import { Api, TelegramClient } from 'telegram';
import { StringSession } from 'telegram/sessions/index.js';
import { RPCError } from 'telegram/errors/RPCBaseErrors.js';

const app = express();

// ── Settings ───────────────────────────────────────────────────────────────────
const TZ = 'Europe/Lisbon';
const POSITIVE_EMOJIS = new Set([
  '👍', '❤️', '🔥', '👏', '😁', '😊', '🥳', '😻', '✨', '💯', '🙌', '😍', '😎',
]);

// ── Small helpers ──────────────────────────────────────────────────────────────
function envOk() {
  return !!(process.env.TG_API_ID && process.env.TG_API_HASH && process.env.TG_STRING_SESSION);
}

function tsToZone(tsSec, zone = TZ) {
  // tsSec -> Luxon DateTime in desired tz
  return DateTime.fromSeconds(Number(tsSec), { zone: 'utc' }).setZone(zone);
}

function jsonText(v) {
  if (v == null) return null;
  if (v instanceof Api.DataJSON && typeof v.data === 'string') return v.data;
  if (typeof v === 'string') return v;
  if (v instanceof Uint8Array || Buffer.isBuffer(v)) return Buffer.from(v).toString('utf8');
  return null;
}

function formatWeekWindow(endDt = DateTime.now().setZone(TZ)) {
  const today = endDt.startOf('day');
  const start = today.minus({ days: 6 });
  return { ws: start.toISODate(), we: today.toISODate() };
}

function mapTelegramError(e, fallbackMsg) {
  if (e instanceof RPCError) {
    const msg = e.errorMessage || '';
    if (/USERNAME_INVALID|USERNAME_NOT_OCCUPIED/i.test(msg)) {
      return { code: 400, body: { error: 'Channel username not found' } };
    }
    if (/CHANNEL_PRIVATE/i.test(msg)) {
      return { code: 403, body: { error: 'Channel is private or not accessible' } };
    }
    if (/CHAT_ADMIN_REQUIRED/i.test(msg)) {
      return { code: 403, body: { error: 'Your account must be an admin of this channel' } };
    }
    const fw = msg.match(/FLOOD_WAIT_(\d+)/i);
    if (fw) return { code: 503, body: { error: `Flood wait: retry after ${fw[1]} seconds` } };
    return { code: 409, body: { error: `Telegram RPC error: ${e.constructor.name}`, detail: msg || String(e) } };
  }
  return { code: 503, body: { error: fallbackMsg || String(e) } };
}

function withTimeout(promise, ms, label = 'operation') {
  return Promise.race([
    promise,
    new Promise((_, rej) =>
      setTimeout(() => rej(new Error(`${label} timed out after ${ms}ms`)), ms)
    ),
  ]);
}

// Handle Date vs number for msg.date across lib versions
function msgDateToLuxon(dtVal) {
  if (!dtVal) return null;
  if (typeof dtVal === 'number') return DateTime.fromSeconds(dtVal, { zone: 'utc' }).setZone(TZ);
  if (dtVal instanceof Date) return DateTime.fromJSDate(dtVal, { zone: 'utc' }).setZone(TZ);
  // some builds expose BigInt/Long-like—try to coerce
  const num = Number(dtVal);
  if (Number.isFinite(num)) return DateTime.fromSeconds(num, { zone: 'utc' }).setZone(TZ);
  return null;
}

// ── Telegram client (single, shared) ───────────────────────────────────────────
function createClient() {
  const apiId = Number(process.env.TG_API_ID);
  const apiHash = process.env.TG_API_HASH;
  const stringSession = process.env.TG_STRING_SESSION;
  if (!apiId || !apiHash || !stringSession) {
    throw new Error('Missing TG_API_ID / TG_API_HASH / TG_STRING_SESSION in environment');
  }
  if (!Number.isInteger(apiId)) {
    throw new Error('TG_API_ID must be an integer');
  }
  const session = new StringSession(stringSession);
  return new TelegramClient(session, apiId, apiHash, { connectionRetries: 5 });
}

const tgClient = createClient();
await tgClient.connect(); // top-level await (ESM)

// ── Tiny in-memory cache ───────────────────────────────────────────────────────
const cache = new Map(); // key -> { at:number, data:any }
function getCached(key, ttlMs = 15000) {
  const v = cache.get(key);
  if (v && Date.now() - v.at < ttlMs) return v.data;
}
function setCached(key, data) {
  cache.set(key, { at: Date.now(), data });
}

// ── Stats loader ───────────────────────────────────────────────────────────────
async function fetchStatsPoints(client, channelUsername) {
  const entity = await client.getEntity(channelUsername);

  const stats = await client.invoke(new Api.stats.GetBroadcastStats({ channel: entity }));

  let graph =
    stats.hoursGraph ??
    stats.hours_graph ??
    stats.topHoursGraph ??
    stats.top_hours_graph ??
    stats.viewsGraph ??
    stats.views_graph ??
    null;

  if (!graph) return [];

  if (graph instanceof Api.StatsGraphAsync) {
    graph = await client.invoke(
      new Api.stats.LoadAsyncGraph({ channel: entity, token: graph.token, x: 0 })
    );
  }

  if (graph instanceof Api.StatsGraph && Array.isArray(graph.points)) {
    return graph.points.map((p) => [Number(p.x ?? 0), Number(p.y ?? 0)]);
  }

  const j = jsonText(graph.json);
  if (!j) return [];
  let data;
  try {
    data = JSON.parse(j);
  } catch {
    return [];
  }

  const points = [];

  // Variant A
  if (data && typeof data === 'object' && Array.isArray(data.items)) {
    for (const it of data.items) {
      if (it?.x != null && it?.y != null) points.push([Number(it.x), Number(it.y || 0)]);
    }
    return points;
  }

  // Variant B
  if (data && typeof data === 'object' && 'x' in data && 'y' in data) {
    const xs = Array.isArray(data.x) ? data.x : [];
    let series = null;
    const y = data.y;

    if (Array.isArray(y)) {
      if (y.length && typeof y[0] === 'object' && 'data' in y[0]) series = y[0].data;
      else series = y;
    } else if (y && typeof y === 'object') {
      if ('data' in y) series = y.data;
      else if (Array.isArray(y.series) && y.series.length && 'data' in y.series[0]) {
        series = y.series[0].data;
      }
    }

    if (Array.isArray(xs) && Array.isArray(series)) {
      for (let i = 0; i < Math.min(xs.length, series.length); i++) {
        const x = xs[i];
        const yy = series[i];
        if (x != null && yy != null) points.push([Number(x), Number(yy || 0)]);
      }
    }
    return points;
  }

  return points;
}

// ── Routes ────────────────────────────────────────────────────────────────────
app.get('/', (_req, res) => {
  res.json({ ok: true, env_ok: envOk(), telethon: 'n/a (using telegram pkg)' });
});

// /hourly — 24 bins (hour-of-day) aggregated over last 7 days
app.get('/hourly', async (req, res) => {
  const channel = (req.query.channel || '').toString().trim();
  if (!channel) return res.status(400).json({ error: 'Missing ?channel=@your_channel' });

  const cacheKey = `hourly:${channel}`;
  const cached = getCached(cacheKey, 15000);
  if (cached) return res.json(cached);

  try {
    // Try broadcast stats first (fast)
    let points;
    try {
      points = await withTimeout(fetchStatsPoints(tgClient, channel), 15000, 'fetchStatsPoints');
    } catch (e) {
      const { code, body } = mapTelegramError(e, `Failed to fetch broadcast stats: ${e}`);
      return res.status(code).json(body);
    }

    if (points && points.length) {
      const byHour = new Map();
      const isHourIndex = points.every(([x]) => x >= 0 && x <= 23);

      if (isHourIndex) {
        for (const [x, y] of points) byHour.set(x, (byHour.get(x) || 0) + Number(y));
      } else {
        for (const [x, y] of points) {
          const h = tsToZone(x).hour;
          byHour.set(h, (byHour.get(h) || 0) + Number(y));
        }
      }

      const { ws, we } = formatWeekWindow();
      const out = Array.from({ length: 24 }, (_, h) => ({
        week_start: ws,
        week_end: we,
        hour: h,
        views: byHour.get(h) || 0,
      }));
      setCached(cacheKey, out);
      return res.json(out);
    }

    // Fallback: iterate messages in last 7 days
    let entity;
    try {
      entity = await tgClient.getEntity(channel);
    } catch (e) {
      return res.status(503).json({ error: `Failed to resolve entity for fallback: ${e}` });
    }

    const end = DateTime.now().setZone(TZ);
    const start = end.minus({ days: 7 });

    const byHour = new Map();

    try {
      await withTimeout(
        (async () => {
          const it = tgClient.iterMessages(entity, {
            offsetDate: Math.floor(end.toSeconds()), // number (seconds)
            limit: 3000, // safety cap
          });
          for await (const msg of it) {
            const dt = msgDateToLuxon(msg?.date);
            if (!dt) continue;
            if (dt < start) break;
            const views = msg.views || 0;
            const hour = dt.hour;
            byHour.set(hour, (byHour.get(hour) || 0) + views);
          }
        })(),
        20000,
        'iterMessages(hourly)'
      );
    } catch (e) {
      const { code, body } = mapTelegramError(e, `Failed during fallback aggregation: ${e}`);
      return res.status(code).json(body);
    }

    const { ws, we } = formatWeekWindow(end);
    const out = Array.from({ length: 24 }, (_, h) => ({
      week_start: ws,
      week_end: we,
      hour: h,
      views: byHour.get(h) || 0,
    }));
    setCached(cacheKey, out);
    return res.json(out);
  } catch (e) {
    return res.status(500).json({ error: 'Unhandled error in /hourly', detail: String(e) });
  }
});

// /daily — totals for one date
app.get('/daily', async (req, res) => {
  const channelRaw = (req.query.channel || '').toString().trim();
  const channel = channelRaw.startsWith('@') ? channelRaw : '@' + channelRaw;
  const dateStr = (req.query.date || '').toString().trim(); // YYYY-MM-DD

  if (!channel || !dateStr) {
    return res.status(400).json({ error: 'Missing ?channel=@your_channel&date=YYYY-MM-DD' });
  }

  const cacheKey = `daily:${channel}:${dateStr}`;
  const cached = getCached(cacheKey, 15000);
  if (cached) return res.json(cached);

  let day;
  try {
    day = DateTime.fromISO(dateStr, { zone: TZ });
    if (!day.isValid) throw new Error('invalid date');
  } catch {
    return res.status(400).json({ error: 'Invalid date format, expected YYYY-MM-DD' });
  }

  const start = day.startOf('day');
  const end = day.endOf('day');

  const totals = { views: 0, forwards: 0, pos: 0, other: 0 };

  try {
    let entity;
    try {
      entity = await tgClient.getEntity(channel);
    } catch (e) {
      const { code, body } = mapTelegramError(e, `Failed to access channel: ${e}`);
      body.channel = channel;
      return res.status(code).json(body);
    }

    try {
      await withTimeout(
        (async () => {
          const iter = tgClient.iterMessages(entity, {
            // start from end of day; go backward internally then we iterate forward via reverse=true
            offsetDate: Math.floor(start.toSeconds()),
            reverse: true, // oldest → newest from the offset
            limit: 2000,   // safety cap for a day
          });

          for await (const msg of iter) {
            const dt = msgDateToLuxon(msg?.date);
            if (!dt) continue;

            if (dt < start) continue;
            if (dt > end) break;

            totals.views += msg.views || 0;
            totals.forwards += msg.forwards || 0;

            const results = msg.reactions?.results || [];
            for (const r of results) {
              let emoji = null;
              if (r.reaction instanceof Api.ReactionEmoji) {
                emoji = r.reaction.emoticon;
              } else if (r.reaction?.emoticon) {
                emoji = r.reaction.emoticon;
              }
              if (typeof emoji === 'string' && POSITIVE_EMOJIS.has(emoji)) {
                totals.pos += r.count || 0;
              } else {
                totals.other += r.count || 0;
              }
            }
          }
        })(),
        20000,
        'iterMessages(daily)'
      );
    } catch (e) {
      const { code, body } = mapTelegramError(e, `Failed while iterating messages: ${e}`);
      return res.status(code).json(body);
    }

    const out = {
      date: dateStr,
      views_total: totals.views,
      shares_total: totals.forwards,
      reactions_positive: totals.pos,
      reactions_other: totals.other,
      reactions_total: totals.pos + totals.other,
    };
    setCached(cacheKey, out);
    return res.json(out);
  } catch (e) {
    return res.status(500).json({ error: 'Unhandled error in /daily', detail: String(e) });
  }
});

// ── Entrypoint ─────────────────────────────────────────────────────────────────
const port = Number(process.env.PORT || '8000');
app.listen(port, '0.0.0.0', () => {
  console.log(`Server listening on http://0.0.0.0:${port}`);
});

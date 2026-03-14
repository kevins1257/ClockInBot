import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime, timezone, timedelta
import csv
import io
import math
import os
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# CONFIG — only edit this section
# ─────────────────────────────────────────────────────────────
TOKEN   = os.getenv("DISCORD_TOKEN")
DB_PATH = 'clockin.db'

# ─────────────────────────────────────────────────────────────
# PERSISTENT DB CONNECTION  (auto-reconnects on disk I/O error)
# ─────────────────────────────────────────────────────────────
_db_conn = None

async def _open_db() -> aiosqlite.Connection:
    """Open a fresh connection and configure it."""
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = aiosqlite.Row
    return conn

async def get_db() -> aiosqlite.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = await _open_db()
    return _db_conn

async def reset_db() -> aiosqlite.Connection:
    """Close the stale connection and open a new one."""
    global _db_conn
    if _db_conn is not None:
        try:
            await _db_conn.close()
        except Exception:
            pass
        _db_conn = None
    _db_conn = await _open_db()
    return _db_conn

class _Db:
    """Async context manager that transparently reconnects on disk I/O errors."""
    async def __aenter__(self) -> aiosqlite.Connection:
        self._conn = await get_db()
        return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # If a disk I/O error bubbled up, reset the connection so the
        # next request gets a fresh one instead of a permanently broken one.
        if exc_type is not None:
            import sqlite3
            if issubclass(exc_type, (aiosqlite.OperationalError, sqlite3.OperationalError)):
                if 'disk' in str(exc_val).lower() or 'i/o' in str(exc_val).lower():
                    await reset_db()
        return False  # never suppress the exception — let commands handle it normally


# ─────────────────────────────────────────────────────────────
# COLORS
# ─────────────────────────────────────────────────────────────
COLOR_SUCCESS = 0x57F287
COLOR_ERROR   = 0xED4245
COLOR_INFO    = 0x5865F2
COLOR_WARN    = 0xFEE75C
COLOR_STATS   = 0xEB459E
COLOR_ADMIN   = 0xFFA500

# ─────────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)
bot_start_time = datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            server_id   INTEGER,
            mask        TEXT,
            start_time  TEXT,
            end_time    TEXT,
            duration    INTEGER,
            note        TEXT DEFAULT NULL
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS active (
            user_id    INTEGER,
            server_id  INTEGER,
            mask       TEXT,
            start_time TEXT,
            UNIQUE(user_id, server_id, mask)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS server_masks (
            server_id  INTEGER,
            mask_name  TEXT,
            enabled    INTEGER DEFAULT 0,
            role_id    INTEGER DEFAULT NULL,
            UNIQUE(server_id, mask_name)
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (
            server_id        INTEGER PRIMARY KEY,
            logging_channel  INTEGER DEFAULT NULL,
            logging_enabled  INTEGER DEFAULT 0,
            onerole          INTEGER DEFAULT NULL,
            giverole_enabled INTEGER DEFAULT 0,
            ephemeral        INTEGER DEFAULT 1,
            default_mask     TEXT    DEFAULT NULL,
            min_role         INTEGER DEFAULT NULL
        )''')
        await db.commit()


_settings_cache: set = set()
_masks_cache: set = set()


def _clear_caches():
    """Called after a reconnect so ensure_* re-runs against the new connection."""
    _settings_cache.clear()
    _masks_cache.clear()


async def ensure_settings(guild_id: int):
    """Insert default settings row if missing. Retries once after a disk I/O reconnect."""
    if guild_id in _settings_cache:
        return
    import sqlite3
    for attempt in range(2):
        try:
            async with _Db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO settings (server_id) VALUES (?)", (guild_id,))
                await db.commit()
            _settings_cache.add(guild_id)
            return
        except (aiosqlite.OperationalError, sqlite3.OperationalError) as e:
            if attempt == 0 and ('disk' in str(e).lower() or 'i/o' in str(e).lower()):
                _clear_caches()
                await reset_db()
            else:
                raise


async def ensure_masks(guild_id: int):
    """Insert default mask rows if none exist. Retries once after a disk I/O reconnect."""
    if guild_id in _masks_cache:
        return
    import sqlite3
    for attempt in range(2):
        try:
            async with _Db() as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM server_masks WHERE server_id=?", (guild_id,)) as cur:
                    count = (await cur.fetchone())[0]
                if count == 0:
                    for i in range(1, 13):
                        try:
                            await db.execute(
                                "INSERT INTO server_masks (server_id, mask_name, enabled) VALUES (?,?,0)",
                                (guild_id, f'mask{i}'))
                        except Exception:
                            pass
                    await db.commit()
            _masks_cache.add(guild_id)
            return
        except (aiosqlite.OperationalError, sqlite3.OperationalError) as e:
            if attempt == 0 and ('disk' in str(e).lower() or 'i/o' in str(e).lower()):
                _clear_caches()
                await reset_db()
            else:
                raise


async def get_setting(guild_id: int, key: str):
    async with _Db() as db:
        async with db.execute(
            f"SELECT {key} FROM settings WHERE server_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def is_ephemeral(guild_id: int) -> bool:
    val = await get_setting(guild_id, 'ephemeral')
    return bool(val) if val is not None else True


async def get_allowed_masks(guild_id: int) -> list[str]:
    async with _Db() as db:
        async with db.execute(
            "SELECT mask_name FROM server_masks WHERE server_id=? AND enabled=1",
            (guild_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]


async def check_min_role(interaction: discord.Interaction) -> bool:
    role_id = await get_setting(interaction.guild.id, 'min_role')
    if not role_id:
        return True
    return any(r.id == role_id for r in interaction.user.roles)


async def handle_role(interaction: discord.Interaction, mask: str, give: bool):
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        async with db.execute(
            "SELECT giverole_enabled, onerole FROM settings WHERE server_id=?",
            (interaction.guild.id,)) as cur:
            settings_row = await cur.fetchone()
        async with db.execute(
            "SELECT role_id FROM server_masks WHERE server_id=? AND mask_name=?",
            (interaction.guild.id, mask)) as cur:
            mask_row = await cur.fetchone()
    if not settings_row or not settings_row[0]:
        return
    role_id = (mask_row[0] if mask_row and mask_row[0] else None) or settings_row[1]
    if not role_id:
        return
    role = interaction.guild.get_role(role_id)
    if not role:
        return
    try:
        if give:
            await interaction.user.add_roles(role)
        else:
            await interaction.user.remove_roles(role)
    except discord.Forbidden:
        pass


async def log_action(guild: discord.Guild, embed: discord.Embed):
    await ensure_settings(guild.id)
    async with _Db() as db:
        async with db.execute(
            "SELECT logging_enabled, logging_channel FROM settings WHERE server_id=?",
            (guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0] or not row[1]:
        return
    channel = guild.get_channel(row[1])
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def format_duration(minutes: int) -> str:
    minutes = abs(int(minutes))
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_duration_long(seconds: int) -> str:
    """Returns e.g. '0 days, 2 hours, 15 minutes and 30 seconds'"""
    seconds = abs(int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{days} days, {hours} hours, {minutes} minutes and {secs} seconds"


def breakdown_minutes(total_minutes: int) -> tuple[int, int, int, int]:
    """Returns (days, hours, minutes, seconds) from a total-minutes value.
    Sessions are stored as integer minutes, so seconds will always be 0
    for completed sessions — live sessions use full seconds separately."""
    total_minutes = abs(int(total_minutes))
    total_seconds = total_minutes * 60
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return days, hours, minutes, seconds


def make_embed(title: str, description: str = None, color: int = COLOR_INFO,
               footer: str = None, timestamp: bool = True) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    if timestamp:
        embed.timestamp = datetime.now(timezone.utc)
    if footer:
        embed.set_footer(text=footer)
    return embed


def ts(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:R>"


def ts_full(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:F>"


# ─────────────────────────────────────────────────────────────
# EVENTS
# ─────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="⏱️ /clockin to start"))
    print(f'✅ Logged in as {bot.user} | {len(bot.guilds)} servers')


@bot.event
async def on_guild_join(guild: discord.Guild):
    await ensure_settings(guild.id)
    await ensure_masks(guild.id)


# ─────────────────────────────────────────────────────────────
# CLOCK IN
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='clockin', description='Clock in for a mask')
@app_commands.describe(mask='The mask to clock in for (uses default if not specified)')
async def clockin(interaction: discord.Interaction, mask: str = None):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)
    await ensure_masks(guild_id)

    if not await check_min_role(interaction):
        await interaction.followup.send(
            embed=make_embed("❌ Access Denied",
                             "You don't have the required role to clock in.", COLOR_ERROR),
            ephemeral=True)
        return

    if mask is None:
        mask = await get_setting(guild_id, 'default_mask')
    if mask is None:
        await interaction.followup.send(
            embed=make_embed("❌ No Mask Specified",
                             "Please provide a mask or ask an admin to set a default with `/settings setdefault`.",
                             COLOR_ERROR),
            ephemeral=True)
        return

    allowed = await get_allowed_masks(guild_id)
    if mask not in allowed:
        await interaction.followup.send(
            embed=make_embed("❌ Invalid Mask",
                             f"`{mask}` is not enabled. Use `/masklist` to see available masks.",
                             COLOR_ERROR),
            ephemeral=True)
        return

    user_id = interaction.user.id
    async with _Db() as db:
        async with db.execute(
            "SELECT start_time FROM active WHERE user_id=? AND server_id=? AND mask=?",
            (user_id, guild_id, mask)) as cur:
            existing = await cur.fetchone()
        if existing:
            elapsed = int((datetime.now() - datetime.fromisoformat(existing[0])).total_seconds() / 60)
            await interaction.followup.send(
                embed=make_embed("⚠️ Already Clocked In",
                                 f"You've been clocked into `{mask}` for **{format_duration(elapsed)}** already.",
                                 COLOR_WARN),
                ephemeral=True)
            return
        start_time = datetime.now().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO active VALUES (?,?,?,?)",
            (user_id, guild_id, mask, start_time))
        await db.commit()

    await handle_role(interaction, mask, give=True)

    # Log
    now = datetime.now(timezone.utc)
    log_embed = make_embed("🟢 Clocked In", color=COLOR_SUCCESS)
    log_embed.add_field(name="User",  value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Mask",  value=f"`{mask}`",               inline=True)
    log_embed.add_field(name="Time",  value=ts_full(now),              inline=False)
    log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await log_action(interaction.guild, log_embed)

    embed = make_embed(
        "✅ Clocked In!",
        f"You successfully clocked into {mask}!",
        COLOR_SUCCESS,
        footer=f"Use /clockout {mask} when you're done")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# CLOCK OUT
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='clockout', description='Clock out for a mask')
@app_commands.describe(mask='The mask to clock out of', note='Optional note for this session')
async def clockout(interaction: discord.Interaction, mask: str = None, note: str = None):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)
    user_id = interaction.user.id

    if mask is None:
        mask = await get_setting(guild_id, 'default_mask')
    if mask is None:
        await interaction.followup.send(
            embed=make_embed("❌ No Mask Specified",
                             "Please provide a mask or ask an admin to set a default with `/settings setdefault`.",
                             COLOR_ERROR),
            ephemeral=True)
        return

    async with _Db() as db:
        async with db.execute(
            "SELECT start_time FROM active WHERE user_id=? AND server_id=? AND mask=?",
            (user_id, guild_id, mask)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.followup.send(
                embed=make_embed("❌ Not Clocked In",
                                 f"You're not clocked into `{mask}`.", COLOR_ERROR),
                ephemeral=True)
            return
        start_time  = datetime.fromisoformat(row[0])
        end_time    = datetime.now()
        duration    = int((end_time - start_time).total_seconds() / 60)
        total_secs  = int((end_time - start_time).total_seconds())

        await db.execute(
            "INSERT INTO sessions (user_id, server_id, mask, start_time, end_time, duration, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, guild_id, mask, start_time.isoformat(),
             end_time.isoformat(), duration, note))
        await db.execute(
            "DELETE FROM active WHERE user_id=? AND server_id=? AND mask=?",
            (user_id, guild_id, mask))
        await db.commit()

    await handle_role(interaction, mask, give=False)

    # Log
    now = datetime.now(timezone.utc)
    log_embed = make_embed("🔴 Clocked Out", color=COLOR_ERROR)
    log_embed.add_field(name="User",     value=interaction.user.mention,   inline=True)
    log_embed.add_field(name="Mask",     value=f"`{mask}`",                 inline=True)
    log_embed.add_field(name="Duration", value=format_duration(duration),  inline=True)
    log_embed.add_field(name="Time",     value=ts_full(now),               inline=False)
    if note:
        log_embed.add_field(name="Note", value=note, inline=False)
    log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await log_action(interaction.guild, log_embed)

    embed = make_embed(
        "✅ Clocked Out!",
        f"You successfully clocked out of {mask} adding {duration} minutes!",
        COLOR_SUCCESS)
    embed.add_field(name="Mask",     value=f"`{mask}`",                inline=True)
    embed.add_field(name="Duration", value=f"**{format_duration(duration)}**", inline=True)
    if note:
        embed.add_field(name="📝 Note", value=note, inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='status', description='Check clock in/out status and total times')
@app_commands.describe(user='User to check (leave blank for yourself)')
async def status(interaction: discord.Interaction, user: discord.Member = None):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)
    target  = user or interaction.user
    user_id = target.id

    async with _Db() as db:
        async with db.execute(
            "SELECT mask, SUM(duration) FROM sessions WHERE user_id=? AND server_id=? GROUP BY mask",
            (user_id, guild_id)) as cur:
            totals = await cur.fetchall()
        async with db.execute(
            "SELECT mask, start_time FROM active WHERE user_id=? AND server_id=?",
            (user_id, guild_id)) as cur:
            active = await cur.fetchall()
        async with db.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=? AND server_id=?",
            (user_id, guild_id)) as cur:
            session_count = (await cur.fetchone())[0]

    total_all = sum(t for _, t in totals if t)

    if active:
        lines = []
        for mask, start in active:
            elapsed   = int((datetime.now() - datetime.fromisoformat(start)).total_seconds() / 60)
            start_dt  = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            lines.append(
                f"🟢 **`{mask}`** — {format_duration(elapsed)} *(started {ts(start_dt)})*")
        status_text = '\n'.join(lines)
    else:
        status_text = "🔴 Not clocked in"

    embed = make_embed("⏱️ Clock Status", color=COLOR_INFO)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.add_field(name="Currently Active", value=status_text, inline=False)

    if totals:
        lines = [f"**`{m}`** — {format_duration(t)}"
                 for m, t in sorted(totals, key=lambda x: -(x[1] or 0))]
        embed.add_field(name="📊 Total Per Mask", value='\n'.join(lines), inline=True)

    embed.add_field(name="🕐 All-Time Total", value=format_duration(total_all), inline=True)
    embed.add_field(name="📋 Sessions",       value=str(session_count),         inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# HISTORY
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='history', description='View your recent clock sessions')
@app_commands.describe(user='User to check', limit='Number of sessions to show (max 10)')
async def history(interaction: discord.Interaction,
                  user: discord.Member = None, limit: int = 5):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)
    target = user or interaction.user
    limit  = max(1, min(limit, 10))

    async with _Db() as db:
        async with db.execute(
            """SELECT mask, start_time, end_time, duration, note
               FROM sessions
               WHERE user_id=? AND server_id=? AND start_time != 'manual'
               ORDER BY end_time DESC LIMIT ?""",
            (target.id, guild_id, limit)) as cur:
            rows = await cur.fetchall()

    if not rows:
        await interaction.followup.send(
            embed=make_embed("📋 No History",
                             f"{target.display_name} has no recorded sessions.", COLOR_WARN),
            ephemeral=True)
        return

    embed = make_embed("📋 Recent Sessions", color=COLOR_INFO)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    for mask, start, end, duration, note in rows:
        try:
            end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
            time_str = ts_full(end_dt)
        except Exception:
            time_str = end
        val = f"**Duration:** {format_duration(duration)}\n**When:** {time_str}"
        if note:
            val += f"\n**Note:** {note}"
        embed.add_field(name=f"🎭 `{mask}`", value=val, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='report', description='Time report for a user')
@app_commands.describe(user='User to report on', days='Days to look back (default 7)')
async def report(interaction: discord.Interaction,
                 user: discord.Member = None, days: int = 7):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)
    target = user or interaction.user
    days   = max(1, min(days, 90))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    async with _Db() as db:
        async with db.execute(
            """SELECT mask, SUM(duration), COUNT(*) FROM sessions
               WHERE user_id=? AND server_id=? AND end_time > ? AND end_time != 'manual'
               GROUP BY mask ORDER BY SUM(duration) DESC""",
            (target.id, guild_id, cutoff)) as cur:
            rows = await cur.fetchall()

    embed = make_embed(f"📊 {days}-Day Report", color=COLOR_STATS)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.set_footer(text=f"Last {days} days")

    if not rows:
        embed.description = f"No sessions in the last {days} days."
    else:
        total = sum(r[1] for r in rows if r[1])
        lines = []
        for mask, dur, count in rows:
            pct         = (dur / total * 100) if total else 0
            bar_filled  = int(pct / 10)
            bar         = "█" * bar_filled + "░" * (10 - bar_filled)
            lines.append(
                f"**`{mask}`**\n`{bar}` {format_duration(dur)} "
                f"({pct:.0f}%) — {count} session{'s' if count != 1 else ''}")
        embed.description = '\n\n'.join(lines)
        embed.add_field(name="⏱️ Total",    value=format_duration(total),        inline=True)
        embed.add_field(name="📋 Sessions", value=str(sum(r[2] for r in rows)),  inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# LEADERBOARD
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='leaderboard', description='Top users by total clocked time')
@app_commands.describe(mask='Filter by mask', days='Days to look back (0 = all time)')
async def leaderboard(interaction: discord.Interaction,
                      mask: str = None, days: int = 0):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)

    cutoff = (datetime.now() - timedelta(days=days)).isoformat() if days > 0 else '0'

    async with _Db() as db:
        if mask:
            query = (
                "SELECT user_id, SUM(duration) as total FROM sessions "
                "WHERE server_id=? AND mask=? AND end_time > ? "
                "GROUP BY user_id ORDER BY total DESC LIMIT 10")
            args = (guild_id, mask, cutoff)
        else:
            query = (
                "SELECT user_id, SUM(duration) as total FROM sessions "
                "WHERE server_id=? AND end_time > ? "
                "GROUP BY user_id ORDER BY total DESC LIMIT 10")
            args = (guild_id, cutoff)
        async with db.execute(query, args) as cur:
            rows = await cur.fetchall()

    medals = ['🥇', '🥈', '🥉']
    title  = ("🏆 Leaderboard"
              + (f" — `{mask}`" if mask else "")
              + (f" ({days}d)" if days else " (All Time)"))
    embed  = make_embed(title, color=COLOR_STATS)

    if not rows:
        embed.description = "No data yet."
    else:
        lines = []
        for i, (uid, total) in enumerate(rows):
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User {uid}"
            medal  = medals[i] if i < 3 else f"`#{i+1}`"
            lines.append(f"{medal} **{name}** — {format_duration(total)}")
        embed.description = '\n'.join(lines)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# WHO'S CLOCKED IN
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='whoclocked', description='See everyone currently clocked in')
@app_commands.describe(mask='Filter by mask')
async def whoclocked(interaction: discord.Interaction, mask: str = None):
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True)
    await ensure_settings(guild_id)

    async with _Db() as db:
        if mask:
            async with db.execute(
                "SELECT user_id, mask, start_time FROM active WHERE server_id=? AND mask=?",
                (guild_id, mask)) as cur:
                active = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT user_id, mask, start_time FROM active WHERE server_id=?",
                (guild_id,)) as cur:
                active = await cur.fetchall()

    embed = make_embed("🟢 Currently Clocked In", color=COLOR_SUCCESS)
    if not active:
        embed.description = "Nobody is currently clocked in."
    else:
        lines = []
        for uid, m, start in sorted(active, key=lambda x: x[2]):
            elapsed  = int((datetime.now() - datetime.fromisoformat(start)).total_seconds() / 60)
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            member   = interaction.guild.get_member(uid)
            name     = member.display_name if member else f"User {uid}"
            lines.append(
                f"**{name}** — `{m}` · {format_duration(elapsed)} *(since {ts(start_dt)})*")
        embed.description = '\n'.join(lines)
        embed.set_footer(text=f"{len(active)} user{'s' if len(active) != 1 else ''} clocked in")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# EXPORT (personal CSV) — grouped totals per mask
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='export', description='Export your clocked time summary per mask to CSV')
async def export(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id   = interaction.guild.id
    user_id    = interaction.user.id
    username   = interaction.user.display_name

    # Pull total minutes per mask from completed sessions
    async with _Db() as db:
        async with db.execute(
            "SELECT mask, SUM(duration) FROM sessions "
            "WHERE user_id=? AND server_id=? GROUP BY mask ORDER BY mask",
            (user_id, guild_id)) as cur:
            mask_totals = await cur.fetchall()

        # Also grab any currently active sessions so live time is included
        async with db.execute(
            "SELECT mask, start_time FROM active WHERE user_id=? AND server_id=?",
            (user_id, guild_id)) as cur:
            active_rows = await cur.fetchall()

    if not mask_totals and not active_rows:
        await interaction.followup.send(
            embed=make_embed("📁 No Data", "You have no recorded sessions to export.", COLOR_WARN),
            ephemeral=True)
        return

    # Build a dict: mask -> total seconds (including live if clocked in)
    totals_by_mask: dict[str, int] = {}
    for mask, minutes in mask_totals:
        totals_by_mask[mask] = (minutes or 0) * 60

    for mask, start_str in active_rows:
        live_secs = int((datetime.now() - datetime.fromisoformat(start_str)).total_seconds())
        totals_by_mask[mask] = totals_by_mask.get(mask, 0) + live_secs

    # Build CSV
    output = io.StringIO()
    writer = csv.writer(output)

    # ── Header ───────────────────────────────────────────────
    writer.writerow([
        'Username',
        'Mask',
        'Total Time',
        'Days',
        'Hours',
        'Minutes',
        'Seconds',
        'Currently Active'
    ])

    active_mask_names = {m for m, _ in active_rows}
    grand_total_secs  = 0

    for mask in sorted(totals_by_mask):
        total_secs = totals_by_mask[mask]
        grand_total_secs += total_secs

        days, remainder = divmod(total_secs, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        # Human-readable summary string, e.g. "2d 4h 30m 15s"
        parts = []
        if days:    parts.append(f"{days}d")
        if hours:   parts.append(f"{hours}h")
        if minutes: parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        total_time_str = ' '.join(parts) if parts else '0s'

        is_active = '🟢 Yes' if mask in active_mask_names else '🔴 No'

        writer.writerow([
            username,
            mask,
            total_time_str,
            days,
            hours,
            minutes,
            seconds,
            is_active
        ])

    # ── Grand total row ───────────────────────────────────────
    writer.writerow([])  # blank separator
    g_days, g_rem  = divmod(grand_total_secs, 86400)
    g_hours, g_rem = divmod(g_rem, 3600)
    g_mins, g_secs = divmod(g_rem, 60)
    g_parts = []
    if g_days:  g_parts.append(f"{g_days}d")
    if g_hours: g_parts.append(f"{g_hours}h")
    if g_mins:  g_parts.append(f"{g_mins}m")
    g_parts.append(f"{g_secs}s")

    writer.writerow([
        username,
        'GRAND TOTAL',
        ' '.join(g_parts) if g_parts else '0s',
        g_days, g_hours, g_mins, g_secs,
        ''
    ])

    output.seek(0)
    filename = f"{username}_clockin_export.csv"
    file     = discord.File(
        fp=io.BytesIO(output.getvalue().encode()), filename=filename)

    mask_count = len(totals_by_mask)
    embed = make_embed(
        "📁 Your Time Export",
        f"Exported **{mask_count} mask{'s' if mask_count != 1 else ''}** · "
        f"Grand total: **{' '.join(g_parts) if g_parts else '0s'}**",
        COLOR_INFO)
    embed.set_author(name=username, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="Includes any currently active sessions · Times shown as D/H/M/S")
    await interaction.followup.send(embed=embed, file=file, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# ALL DATA (admin CSV) — grouped totals per user per mask
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='alldata', description='Admin: Export all server time totals to CSV')
@app_commands.describe(place='"channel" or "dm"')
async def alldata(interaction: discord.Interaction, place: str = 'dm'):
    await interaction.response.defer(ephemeral=(place == 'dm'))
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    # Totals per user per mask from completed sessions
    async with _Db() as db:
        async with db.execute(
            "SELECT user_id, mask, SUM(duration) FROM sessions "
            "WHERE server_id=? GROUP BY user_id, mask ORDER BY user_id, mask",
            (guild_id,)) as cur:
            session_rows = await cur.fetchall()

        # Live sessions
        async with db.execute(
            "SELECT user_id, mask, start_time FROM active WHERE server_id=?",
            (guild_id,)) as cur:
            active_rows = await cur.fetchall()

    if not session_rows and not active_rows:
        await interaction.followup.send(
            embed=make_embed("📁 No Data", "No session data recorded yet.", COLOR_WARN),
            ephemeral=True)
        return

    # Build structure: {user_id: {mask: total_seconds}}
    data: dict[int, dict[str, int]] = {}
    for uid, mask, minutes in session_rows:
        data.setdefault(uid, {})[mask] = (minutes or 0) * 60

    active_set: dict[int, set] = {}
    for uid, mask, start_str in active_rows:
        live_secs = int((datetime.now() - datetime.fromisoformat(start_str)).total_seconds())
        data.setdefault(uid, {})
        data[uid][mask] = data[uid].get(mask, 0) + live_secs
        active_set.setdefault(uid, set()).add(mask)

    # Sort users by grand total descending
    sorted_users = sorted(
        data.items(),
        key=lambda x: sum(x[1].values()),
        reverse=True)

    output = io.StringIO()
    writer = csv.writer(output)

    # ── Header ───────────────────────────────────────────────
    writer.writerow([
        'Username',
        'User ID',
        'Mask',
        'Total Time',
        'Days',
        'Hours',
        'Minutes',
        'Seconds',
        'Currently Active'
    ])

    total_users = len(sorted_users)

    for uid, mask_totals in sorted_users:
        member   = interaction.guild.get_member(uid)
        username = member.display_name if member else f"User {uid}"

        user_grand_secs = 0

        for mask in sorted(mask_totals):
            total_secs = mask_totals[mask]
            user_grand_secs += total_secs

            days, remainder = divmod(total_secs, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)

            parts = []
            if days:    parts.append(f"{days}d")
            if hours:   parts.append(f"{hours}h")
            if minutes: parts.append(f"{minutes}m")
            parts.append(f"{seconds}s")
            total_time_str = ' '.join(parts) if parts else '0s'

            is_active = '🟢 Yes' if mask in active_set.get(uid, set()) else '🔴 No'

            writer.writerow([
                username,
                uid,
                mask,
                total_time_str,
                days,
                hours,
                minutes,
                seconds,
                is_active
            ])

        # Per-user subtotal row
        g_days, g_rem  = divmod(user_grand_secs, 86400)
        g_hours, g_rem = divmod(g_rem, 3600)
        g_mins, g_secs = divmod(g_rem, 60)
        g_parts = []
        if g_days:  g_parts.append(f"{g_days}d")
        if g_hours: g_parts.append(f"{g_hours}h")
        if g_mins:  g_parts.append(f"{g_mins}m")
        g_parts.append(f"{g_secs}s")

        writer.writerow([
            username,
            uid,
            '── USER TOTAL ──',
            ' '.join(g_parts) if g_parts else '0s',
            g_days, g_hours, g_mins, g_secs,
            ''
        ])
        writer.writerow([])  # blank line between users

    output.seek(0)
    filename = f"{interaction.guild.name}_clockin_alldata.csv".replace(' ', '_')
    file     = discord.File(
        fp=io.BytesIO(output.getvalue().encode()), filename=filename)

    log_embed = make_embed("📁 All Data Exported", color=COLOR_ADMIN)
    log_embed.add_field(name="By",    value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Users", value=str(total_users),         inline=True)
    await log_action(interaction.guild, log_embed)

    embed = make_embed(
        "📁 Full Server Export",
        f"**{total_users}** users · Time shown as Days / Hours / Minutes / Seconds per mask.",
        COLOR_INFO)
    embed.set_footer(text="Includes active sessions · Each user has a subtotal row")

    if place == 'dm':
        await interaction.followup.send(
            embed=make_embed("📩 Sent", "Check your DMs!", COLOR_SUCCESS), ephemeral=True)
        await interaction.user.send(embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed, file=file)


# ─────────────────────────────────────────────────────────────
# ADD / REMOVE TIME (admin)
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='add', description='Admin: Manually add or remove time from a user')
@app_commands.describe(
    user='Target user', mask='Mask name',
    time='Minutes (negative to remove)', note='Reason')
async def add_time(interaction: discord.Interaction,
                   user: discord.Member, mask: str,
                   time: int, note: str = 'Manual adjustment'):
    await interaction.response.defer()
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    allowed = await get_allowed_masks(guild_id)
    if mask not in allowed:
        await interaction.followup.send(
            embed=make_embed("❌ Invalid Mask", f"`{mask}` is not enabled.", COLOR_ERROR),
            ephemeral=True)
        return

    async with _Db() as db:
        await db.execute(
            "INSERT INTO sessions (user_id, server_id, mask, start_time, end_time, duration, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (user.id, guild_id, mask, 'manual', 'manual', time, note))
        await db.commit()

    log_embed = make_embed(
        f"⚙️ Time {'Added' if time > 0 else 'Removed'}", color=COLOR_ADMIN)
    log_embed.add_field(name="Admin",  value=interaction.user.mention, inline=True)
    log_embed.add_field(name="User",   value=user.mention,             inline=True)
    log_embed.add_field(name="Mask",   value=f"`{mask}`",               inline=True)
    log_embed.add_field(name="Amount", value=format_duration(abs(time)), inline=True)
    log_embed.add_field(name="Note",   value=note,                     inline=False)
    await log_action(interaction.guild, log_embed)

    embed = make_embed(
        f"✅ Time {'Added' if time > 0 else 'Removed'}", color=COLOR_SUCCESS)
    embed.add_field(name="User",   value=user.mention,              inline=True)
    embed.add_field(name="Mask",   value=f"`{mask}`",                inline=True)
    embed.add_field(name="Amount", value=format_duration(abs(time)), inline=True)
    embed.add_field(name="Note",   value=note,                      inline=False)
    await interaction.followup.send(embed=embed)


# ─────────────────────────────────────────────────────────────
# FORCE OUT (admin)
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='forceout', description='Admin: Force a user to clock out')
@app_commands.describe(user='Target user', mask='Mask to force out of')
async def force_out(interaction: discord.Interaction,
                    user: discord.Member, mask: str):
    await interaction.response.defer()
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    async with _Db() as db:
        async with db.execute(
            "SELECT start_time FROM active WHERE user_id=? AND server_id=? AND mask=?",
            (user.id, guild_id, mask)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.followup.send(
                embed=make_embed("❌ Not Clocked In",
                                 f"{user.mention} is not clocked into `{mask}`.", COLOR_ERROR),
                ephemeral=True)
            return
        start_time = datetime.fromisoformat(row[0])
        end_time   = datetime.now()
        duration   = int((end_time - start_time).total_seconds() / 60)
        await db.execute(
            "INSERT INTO sessions (user_id, server_id, mask, start_time, end_time, duration, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (user.id, guild_id, mask,
             start_time.isoformat(), end_time.isoformat(),
             duration, 'Force clocked out by admin'))
        await db.execute(
            "DELETE FROM active WHERE user_id=? AND server_id=? AND mask=?",
            (user.id, guild_id, mask))
        await db.commit()

    # Remove role
    async with _Db() as db:
        async with db.execute(
            "SELECT giverole_enabled, onerole FROM settings WHERE server_id=?",
            (guild_id,)) as cur:
            s = await cur.fetchone()
        async with db.execute(
            "SELECT role_id FROM server_masks WHERE server_id=? AND mask_name=?",
            (guild_id, mask)) as cur:
            m_row = await cur.fetchone()
    if s and s[0]:
        role_id = (m_row[0] if m_row and m_row[0] else None) or s[1]
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await user.remove_roles(role)
                except discord.Forbidden:
                    pass

    log_embed = make_embed("⚠️ Force Clocked Out", color=COLOR_ADMIN)
    log_embed.add_field(name="Admin",    value=interaction.user.mention, inline=True)
    log_embed.add_field(name="User",     value=user.mention,             inline=True)
    log_embed.add_field(name="Mask",     value=f"`{mask}`",               inline=True)
    log_embed.add_field(name="Duration", value=format_duration(duration), inline=True)
    await log_action(interaction.guild, log_embed)

    embed = make_embed("✅ Force Clocked Out", color=COLOR_SUCCESS)
    embed.add_field(name="User",     value=user.mention,                     inline=True)
    embed.add_field(name="Mask",     value=f"`{mask}`",                       inline=True)
    embed.add_field(name="Duration", value=f"**{format_duration(duration)}**", inline=True)
    await interaction.followup.send(embed=embed)


# ─────────────────────────────────────────────────────────────
# CLEAR DATA (admin)
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='cleardata', description='Admin: Clear all time data for a user')
@app_commands.describe(user='User to clear', mask='Specific mask (blank = all)')
async def cleardata(interaction: discord.Interaction,
                    user: discord.Member, mask: str = None):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    async with _Db() as db:
        if mask:
            await db.execute(
                "DELETE FROM sessions WHERE user_id=? AND server_id=? AND mask=?",
                (user.id, guild_id, mask))
            await db.execute(
                "DELETE FROM active WHERE user_id=? AND server_id=? AND mask=?",
                (user.id, guild_id, mask))
        else:
            await db.execute(
                "DELETE FROM sessions WHERE user_id=? AND server_id=?",
                (user.id, guild_id))
            await db.execute(
                "DELETE FROM active WHERE user_id=? AND server_id=?",
                (user.id, guild_id))
        await db.commit()

    scope     = f"`{mask}`" if mask else "**all masks**"
    log_embed = make_embed("🗑️ Data Cleared", color=COLOR_ADMIN)
    log_embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="User",  value=user.mention,             inline=True)
    log_embed.add_field(name="Scope", value=scope,                    inline=True)
    await log_action(interaction.guild, log_embed)

    embed = make_embed(
        "✅ Data Cleared",
        f"Cleared {scope} data for {user.mention}.", COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# SUMMARY BUILDER (shared by /summary and /resetall)
# ─────────────────────────────────────────────────────────────
async def build_summary(guild: discord.Guild, guild_id: int):
    async with _Db() as db:
        async with db.execute(
            "SELECT user_id, mask, SUM(duration) FROM sessions "
            "WHERE server_id=? GROUP BY user_id, mask ORDER BY user_id, mask",
            (guild_id,)) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT user_id, mask, start_time FROM active WHERE server_id=?",
            (guild_id,)) as cur:
            active_rows = await cur.fetchall()

    user_totals: dict[int, dict[str, int]] = {}
    for uid, mask, total in rows:
        user_totals.setdefault(uid, {})[mask] = total or 0
    for uid, mask, start in active_rows:
        elapsed = int((datetime.now() - datetime.fromisoformat(start)).total_seconds() / 60)
        user_totals.setdefault(uid, {})
        user_totals[uid][mask] = user_totals[uid].get(mask, 0) + elapsed

    if not user_totals:
        return None, None

    embed = make_embed(
        "📊 Server Time Summary",
        "Total tracked time per user across all masks.\n*Includes active sessions.*",
        COLOR_STATS)
    embed.set_footer(text=f"{guild.name}  ·  {len(user_totals)} users")

    sorted_users = sorted(
        user_totals.items(), key=lambda x: sum(x[1].values()), reverse=True)

    for uid, mask_totals in sorted_users:
        member      = guild.get_member(uid)
        name        = member.display_name if member else f"User {uid}"
        grand_total = sum(mask_totals.values())
        lines       = [f"**`{m}`** — {format_duration(t)}"
                       for m, t in sorted(mask_totals.items())]
        lines.append(f"**Total: {format_duration(grand_total)}**")
        value = '\n'.join(lines)
        if len(value) > 1024:
            value = value[:1020] + '...'
        embed.add_field(name=f"👤 {name}", value=value, inline=True)
        if len(embed.fields) >= 24:
            remaining = len(sorted_users) - 24
            if remaining > 0:
                embed.add_field(
                    name="⚠️ Too many users",
                    value=f"...and {remaining} more. See the CSV for full data.",
                    inline=False)
            break

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ['User ID', 'Username', 'Mask', 'Total Time (min)', 'Total Time (formatted)'])
    for uid, mask_totals in sorted_users:
        member   = guild.get_member(uid)
        username = member.display_name if member else f"User {uid}"
        for mask, total in sorted(mask_totals.items()):
            writer.writerow([uid, username, mask, total, format_duration(total)])
        writer.writerow([uid, username, 'TOTAL',
                         sum(mask_totals.values()),
                         format_duration(sum(mask_totals.values()))])
        writer.writerow([])
    output.seek(0)
    file = discord.File(
        fp=io.BytesIO(output.getvalue().encode()), filename='server_summary.csv')
    return embed, file


# ─────────────────────────────────────────────────────────────
# SUMMARY (admin)
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='summary',
                  description='Admin: Total clocked time per user across all masks')
@app_commands.describe(place='"channel" or "dm" (default: dm)')
async def summary(interaction: discord.Interaction, place: str = 'dm'):
    await interaction.response.defer(ephemeral=(place == 'dm'))
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    embed, file = await build_summary(interaction.guild, guild_id)
    if embed is None:
        await interaction.followup.send(
            embed=make_embed("📊 No Data", "No session data recorded yet.", COLOR_WARN),
            ephemeral=True)
        return

    log_embed = make_embed("📊 Summary Exported", color=COLOR_ADMIN)
    log_embed.add_field(name="By", value=interaction.user.mention, inline=True)
    await log_action(interaction.guild, log_embed)

    if place == 'dm':
        await interaction.followup.send(
            embed=make_embed("📩 Sent to DMs",
                             "Check your DMs for the full summary!", COLOR_SUCCESS),
            ephemeral=True)
        await interaction.user.send(embed=embed, file=file)
    else:
        await interaction.followup.send(embed=embed, file=file)


# ─────────────────────────────────────────────────────────────
# RESET ALL (admin)
# ─────────────────────────────────────────────────────────────
class ConfirmResetView(discord.ui.View):
    def __init__(self, admin_id: int):
        super().__init__(timeout=30)
        self.admin_id  = admin_id
        self.confirmed = False

    @discord.ui.button(label='Yes, reset everything',
                       style=discord.ButtonStyle.danger, emoji='🗑️')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "Only the admin who triggered this can confirm.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        async with _Db() as db:
            await db.execute("DELETE FROM sessions WHERE server_id=?", (guild_id,))
            await db.execute("DELETE FROM active WHERE server_id=?",   (guild_id,))
            await db.commit()

        log_embed = make_embed("🗑️ Full Reset", color=COLOR_ADMIN)
        log_embed.add_field(name="By",     value=interaction.user.mention,            inline=True)
        log_embed.add_field(name="Action", value="All sessions and active clocks wiped", inline=False)
        await log_action(interaction.guild, log_embed)

        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send(
            embed=make_embed("✅ Reset Complete",
                             "All session data and active clocks have been wiped.",
                             COLOR_SUCCESS),
            ephemeral=True)

    @discord.ui.button(label='Cancel',
                       style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "Only the admin who triggered this can cancel.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            embed=make_embed("✅ Cancelled", "No data was deleted.", COLOR_SUCCESS),
            ephemeral=True)


@bot.tree.command(name='resetall',
                  description='Admin: Show full summary then wipe all server session data')
async def resetall(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    embed, file = await build_summary(interaction.guild, guild_id)
    if embed is None:
        await interaction.followup.send(
            embed=make_embed("📊 No Data",
                             "Nothing to reset — no session data exists yet.", COLOR_WARN),
            ephemeral=True)
        return

    try:
        embed.title = "📊 Pre-Reset Summary (saved to your DMs)"
        await interaction.user.send(
            embed=discord.Embed(
                title="📋 Summary Before Reset",
                description="Full record of all time data **before** it gets wiped.",
                color=COLOR_WARN))
        await interaction.user.send(embed=embed, file=file)
    except discord.Forbidden:
        pass

    confirm_embed = make_embed(
        "⚠️ Confirm Full Reset",
        "**This will permanently delete ALL session data and clock everyone out.**\n\n"
        "A full summary has been sent to your DMs.\n\n"
        "Are you sure you want to continue?",
        COLOR_WARN)
    confirm_embed.set_footer(text="This action cannot be undone • Expires in 30 seconds")

    view = ConfirmResetView(admin_id=interaction.user.id)
    await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# MASK COMMANDS
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='masklist', description='List all masks for this server')
async def masklist(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    await ensure_settings(guild_id)
    await ensure_masks(guild_id)

    async with _Db() as db:
        async with db.execute(
            "SELECT mask_name, enabled FROM server_masks WHERE server_id=? ORDER BY mask_name",
            (guild_id,)) as cur:
            masks = await cur.fetchall()

    embed = make_embed("🎭 Server Masks", color=0x9B59B6)
    lines = []
    for mask_name, enabled in masks:
        status = "✅ Enabled" if enabled else "❌ Disabled"
        lines.append(f"**{mask_name}** — {status}")
    embed.description = '\n'.join(lines)
    embed.set_footer(text="You have 12 masks. Upgrade for up to 30!")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name='maskchange', description='Admin: Rename a mask')
@app_commands.describe(old_name='Current name', new_name='New name')
async def maskchange(interaction: discord.Interaction, old_name: str, new_name: str):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    async with _Db() as db:
        async with db.execute(
            "SELECT * FROM server_masks WHERE server_id=? AND mask_name=?",
            (guild_id, old_name)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(
                    embed=make_embed("❌ Not Found",
                                     f"Mask `{old_name}` doesn't exist.", COLOR_ERROR),
                    ephemeral=True)
                return
        try:
            await db.execute(
                "UPDATE server_masks SET mask_name=? WHERE server_id=? AND mask_name=?",
                (new_name, guild_id, old_name))
            await db.execute(
                "UPDATE sessions SET mask=? WHERE server_id=? AND mask=?",
                (new_name, guild_id, old_name))
            await db.execute(
                "UPDATE active SET mask=? WHERE server_id=? AND mask=?",
                (new_name, guild_id, old_name))
            await db.commit()
        except aiosqlite.IntegrityError:
            await interaction.followup.send(
                embed=make_embed("❌ Already Exists",
                                 f"Mask `{new_name}` already exists.", COLOR_ERROR),
                ephemeral=True)
            return

    log_embed = make_embed("✏️ Mask Renamed", color=COLOR_ADMIN)
    log_embed.add_field(name="By",  value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Old", value=f"`{old_name}`",           inline=True)
    log_embed.add_field(name="New", value=f"`{new_name}`",           inline=True)
    await log_action(interaction.guild, log_embed)

    embed = make_embed("✅ Mask Renamed", f"`{old_name}` → `{new_name}`", COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name='masktoggle', description='Admin: Enable or disable a mask')
@app_commands.describe(mask='The mask to toggle')
async def masktoggle(interaction: discord.Interaction, mask: str):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id

    async with _Db() as db:
        async with db.execute(
            "SELECT enabled FROM server_masks WHERE server_id=? AND mask_name=?",
            (guild_id, mask)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.followup.send(
                embed=make_embed("❌ Not Found",
                                 f"Mask `{mask}` doesn't exist.", COLOR_ERROR),
                ephemeral=True)
            return
        new_val = 0 if row[0] else 1
        await db.execute(
            "UPDATE server_masks SET enabled=? WHERE server_id=? AND mask_name=?",
            (new_val, guild_id, mask))
        await db.commit()

    status_str = "enabled ✅" if new_val else "disabled ❌"
    log_embed  = make_embed("🔀 Mask Toggled", color=COLOR_ADMIN)
    log_embed.add_field(name="By",     value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Mask",   value=f"`{mask}`",               inline=True)
    log_embed.add_field(name="Status", value=status_str,               inline=True)
    await log_action(interaction.guild, log_embed)

    embed = make_embed("✅ Mask Toggled",
                       f"`{mask}` is now {status_str}", COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# SETTINGS GROUP
# ─────────────────────────────────────────────────────────────
settings_group = app_commands.Group(
    name='settings', description='Configure the bot (Admin only)')


@settings_group.command(name='view', description='View all current settings')
async def settings_view(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    guild_id = interaction.guild.id

    async with _Db() as db:
        async with db.execute(
            "SELECT * FROM settings WHERE server_id=?", (guild_id,)) as cur:
            row  = await cur.fetchone()
            cols = [d[0] for d in cur.description]
    if not row:
        await interaction.followup.send("No settings found.", ephemeral=True)
        return

    s = dict(zip(cols, row))
    embed   = make_embed("⚙️ Server Settings", color=COLOR_ADMIN)
    log_ch  = (interaction.guild.get_channel(s['logging_channel'])
               if s['logging_channel'] else None)
    role    = (interaction.guild.get_role(s['onerole'])
               if s['onerole'] else None)
    min_role = (interaction.guild.get_role(s['min_role'])
                if s['min_role'] else None)

    embed.add_field(name="Logging",       value="✅ Enabled" if s['logging_enabled'] else "❌ Disabled", inline=True)
    embed.add_field(name="Log Channel",   value=log_ch.mention if log_ch else "Not set",                 inline=True)
    embed.add_field(name="Ephemeral",     value="✅ Yes" if s['ephemeral'] else "❌ No",                  inline=True)
    embed.add_field(name="Default Mask",  value=f"`{s['default_mask']}`" if s['default_mask'] else "Not set", inline=True)
    embed.add_field(name="On-Role",       value=role.mention if role else "Not set",                     inline=True)
    embed.add_field(name="Give Role",     value="✅ Enabled" if s['giverole_enabled'] else "❌ Disabled", inline=True)
    embed.add_field(name="Min Role",      value=min_role.mention if min_role else "Not set",              inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='loggingchannel', description='Set the logging channel')
@app_commands.describe(channel='Channel to log to')
async def settings_loggingchannel(interaction: discord.Interaction,
                                   channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET logging_channel=? WHERE server_id=?",
            (channel.id, interaction.guild.id))
        await db.commit()
    embed = make_embed("✅ Logging Channel Set",
                       f"Logs will be sent to {channel.mention}.", COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='logging', description='Enable or disable action logging')
@app_commands.describe(enabled='True to enable, False to disable')
async def settings_logging(interaction: discord.Interaction, enabled: bool):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET logging_enabled=? WHERE server_id=?",
            (1 if enabled else 0, interaction.guild.id))
        await db.commit()
    embed = make_embed("✅ Logging Updated",
                       f"Logging is now **{'enabled' if enabled else 'disabled'}**.",
                       COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='ephemeral',
                         description='Toggle whether responses are only visible to the user')
@app_commands.describe(enabled='True = private, False = public')
async def settings_ephemeral(interaction: discord.Interaction, enabled: bool):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET ephemeral=? WHERE server_id=?",
            (1 if enabled else 0, interaction.guild.id))
        await db.commit()
    embed = make_embed(
        "✅ Ephemeral Updated",
        f"Responses are now **{'private' if enabled else 'public'}**.",
        COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='onerole',
                         description='Set a role to give on clock in for all masks')
@app_commands.describe(role='Role to assign on clock in')
async def settings_onerole(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET onerole=? WHERE server_id=?",
            (role.id, interaction.guild.id))
        await db.commit()
    embed = make_embed(
        "✅ Role Set",
        f"{role.mention} will be given on clock in.\nEnable with `/settings giverole True`.",
        COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='maskrole', description='Set a role for a specific mask')
@app_commands.describe(mask='The mask', role='Role for this mask')
async def settings_maskrole(interaction: discord.Interaction,
                             mask: str, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    guild_id = interaction.guild.id
    async with _Db() as db:
        async with db.execute(
            "SELECT * FROM server_masks WHERE server_id=? AND mask_name=?",
            (guild_id, mask)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(
                    embed=make_embed("❌ Not Found",
                                     f"Mask `{mask}` doesn't exist.", COLOR_ERROR),
                    ephemeral=True)
                return
        await db.execute(
            "UPDATE server_masks SET role_id=? WHERE server_id=? AND mask_name=?",
            (role.id, guild_id, mask))
        await db.commit()
    embed = make_embed(
        "✅ Mask Role Set",
        f"{role.mention} will be given when clocking into `{mask}`.",
        COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='giverole',
                         description='Enable or disable automatic role giving')
@app_commands.describe(enabled='True to enable, False to disable')
async def settings_giverole(interaction: discord.Interaction, enabled: bool):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET giverole_enabled=? WHERE server_id=?",
            (1 if enabled else 0, interaction.guild.id))
        await db.commit()
    embed = make_embed(
        "✅ Role Giving Updated",
        f"Role giving is now **{'enabled' if enabled else 'disabled'}**.",
        COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='setdefault',
                         description='Set the default mask for /button and maskless commands')
@app_commands.describe(mask='Default mask name')
async def settings_setdefault(interaction: discord.Interaction, mask: str):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    allowed = await get_allowed_masks(interaction.guild.id)
    if mask not in allowed:
        await interaction.followup.send(
            embed=make_embed("❌ Invalid Mask",
                             f"`{mask}` is not an enabled mask.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET default_mask=? WHERE server_id=?",
            (mask, interaction.guild.id))
        await db.commit()
    embed = make_embed("✅ Default Mask Set",
                       f"Default mask is now `{mask}`.", COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


@settings_group.command(name='minrole',
                         description='Set a minimum role required to clock in')
@app_commands.describe(role='Minimum role (blank = no restriction)')
async def settings_minrole(interaction: discord.Interaction,
                            role: discord.Role = None):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    async with _Db() as db:
        await db.execute(
            "UPDATE settings SET min_role=? WHERE server_id=?",
            (role.id if role else None, interaction.guild.id))
        await db.commit()
    if role:
        embed = make_embed("✅ Min Role Set",
                           f"Users need {role.mention} or higher to clock in.",
                           COLOR_SUCCESS)
    else:
        embed = make_embed("✅ Min Role Removed",
                           "Anyone can now clock in.", COLOR_SUCCESS)
    await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(settings_group)


# ─────────────────────────────────────────────────────────────
# BUTTON PANEL UI
# ─────────────────────────────────────────────────────────────
class MaskSelect(discord.ui.Select):
    def __init__(self, masks: list[str], action: str):
        self.action = action
        options = [discord.SelectOption(label=m, value=m, emoji="🎭") for m in masks[:25]]
        super().__init__(
            placeholder=f"Select a mask to {action}...",
            options=options,
            custom_id=f"mask_select_{action}")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        mask      = self.values[0]
        guild_id  = interaction.guild.id
        user_id   = interaction.user.id

        if self.action == 'clockin':
            if not await check_min_role(interaction):
                await interaction.followup.send(
                    embed=make_embed("❌ Access Denied",
                                     "You don't have the required role.", COLOR_ERROR),
                    ephemeral=True)
                return
            async with _Db() as db:
                async with db.execute(
                    "SELECT start_time FROM active WHERE user_id=? AND server_id=? AND mask=?",
                    (user_id, guild_id, mask)) as cur:
                    existing = await cur.fetchone()
                if existing:
                    elapsed = int(
                        (datetime.now() - datetime.fromisoformat(existing[0])).total_seconds() / 60)
                    await interaction.followup.send(
                        embed=make_embed("⚠️ Already Clocked In",
                                         f"You've been in `{mask}` for **{format_duration(elapsed)}**.",
                                         COLOR_WARN),
                        ephemeral=True)
                    return
                start_time = datetime.now().isoformat()
                await db.execute(
                    "INSERT OR IGNORE INTO active VALUES (?,?,?,?)",
                    (user_id, guild_id, mask, start_time))
                await db.commit()
            await handle_role(interaction, mask, give=True)
            log_embed = make_embed("🟢 Clocked In (Button)", color=COLOR_SUCCESS)
            log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Mask", value=f"`{mask}`",               inline=True)
            await log_action(interaction.guild, log_embed)
            await interaction.followup.send(
                embed=make_embed("✅ Clocked In!",
                                 f"You successfully clocked into {mask}!",
                                 COLOR_SUCCESS),
                ephemeral=True)

        elif self.action == 'clockout':
            async with _Db() as db:
                async with db.execute(
                    "SELECT start_time FROM active WHERE user_id=? AND server_id=? AND mask=?",
                    (user_id, guild_id, mask)) as cur:
                    row = await cur.fetchone()
                if not row:
                    await interaction.followup.send(
                        embed=make_embed("❌ Not Clocked In",
                                         f"You're not in `{mask}`.", COLOR_ERROR),
                        ephemeral=True)
                    return
                start_time = datetime.fromisoformat(row[0])
                end_time   = datetime.now()
                duration   = int((end_time - start_time).total_seconds() / 60)
                await db.execute(
                    "INSERT INTO sessions (user_id, server_id, mask, start_time, end_time, duration) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, guild_id, mask,
                     start_time.isoformat(), end_time.isoformat(), duration))
                await db.execute(
                    "DELETE FROM active WHERE user_id=? AND server_id=? AND mask=?",
                    (user_id, guild_id, mask))
                await db.commit()
            await handle_role(interaction, mask, give=False)
            log_embed = make_embed("🔴 Clocked Out (Button)", color=COLOR_ERROR)
            log_embed.add_field(name="User",     value=interaction.user.mention,  inline=True)
            log_embed.add_field(name="Mask",     value=f"`{mask}`",                inline=True)
            log_embed.add_field(name="Duration", value=format_duration(duration), inline=True)
            await log_action(interaction.guild, log_embed)
            embed = make_embed(
                "✅ Clocked Out!",
                f"You successfully clocked out of {mask} adding {duration} minutes!",
                COLOR_SUCCESS)
            embed.add_field(name="Mask",     value=f"`{mask}`",                        inline=True)
            embed.add_field(name="Duration", value=f"**{format_duration(duration)}**", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)


class ClockButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ── Clock In / Out toggle ──────────────────────────────
    @discord.ui.button(label='clock in / out',
                       style=discord.ButtonStyle.primary,
                       custom_id='clockinout_main_btn', row=0)
    async def clockinout_btn(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        user_id  = interaction.user.id

        default = await get_setting(guild_id, 'default_mask')
        allowed = await get_allowed_masks(guild_id)

        if not allowed:
            await interaction.followup.send(
                embed=make_embed("❌ No Masks",
                                 "No enabled masks. Ask an admin to enable one.",
                                 COLOR_ERROR),
                ephemeral=True)
            return

        # Resolve mask
        if default and default in allowed:
            mask = default
        elif len(allowed) == 1:
            mask = allowed[0]
        else:
            # Multiple masks — show select
            async with _Db() as db:
                async with db.execute(
                    "SELECT mask FROM active WHERE user_id=? AND server_id=?",
                    (user_id, guild_id)) as cur:
                    active_masks = [r[0] for r in await cur.fetchall()]
            if active_masks:
                view = discord.ui.View(timeout=60)
                view.add_item(MaskSelect(active_masks, 'clockout'))
                await interaction.followup.send(
                    embed=make_embed("🎭 Clock Out of which mask?",
                                     "Select the mask to clock out of.", COLOR_INFO),
                    view=view, ephemeral=True)
            else:
                view = discord.ui.View(timeout=60)
                view.add_item(MaskSelect(allowed, 'clockin'))
                await interaction.followup.send(
                    embed=make_embed("🎭 Clock Into which mask?",
                                     "Select the mask to clock into.", COLOR_INFO),
                    view=view, ephemeral=True)
            return

        # Single-mask toggle
        async with _Db() as db:
            async with db.execute(
                "SELECT start_time FROM active WHERE user_id=? AND server_id=? AND mask=?",
                (user_id, guild_id, mask)) as cur:
                existing = await cur.fetchone()

        if existing:
            # Clock out
            start_time = datetime.fromisoformat(existing[0])
            end_time   = datetime.now()
            duration   = int((end_time - start_time).total_seconds() / 60)
            async with _Db() as db:
                await db.execute(
                    "INSERT INTO sessions (user_id, server_id, mask, start_time, end_time, duration) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, guild_id, mask,
                     start_time.isoformat(), end_time.isoformat(), duration))
                await db.execute(
                    "DELETE FROM active WHERE user_id=? AND server_id=? AND mask=?",
                    (user_id, guild_id, mask))
                await db.commit()
            await handle_role(interaction, mask, give=False)
            log_embed = make_embed("🔴 Clocked Out (Button)", color=COLOR_ERROR)
            log_embed.add_field(name="User",     value=interaction.user.mention,  inline=True)
            log_embed.add_field(name="Mask",     value=f"`{mask}`",                inline=True)
            log_embed.add_field(name="Duration", value=format_duration(duration), inline=True)
            await log_action(interaction.guild, log_embed)
            embed = make_embed(
                "✅ Clocked Out!",
                f"You successfully clocked out of {mask} adding {duration} minutes!",
                COLOR_SUCCESS)
            embed.add_field(name="Mask",     value=f"`{mask}`",                        inline=True)
            embed.add_field(name="Duration", value=f"**{format_duration(duration)}**", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            # Clock in
            if not await check_min_role(interaction):
                await interaction.followup.send(
                    embed=make_embed("❌ Access Denied",
                                     "You don't have the required role.", COLOR_ERROR),
                    ephemeral=True)
                return
            async with _Db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO active VALUES (?,?,?,?)",
                    (user_id, guild_id, mask, datetime.now().isoformat()))
                await db.commit()
            await handle_role(interaction, mask, give=True)
            log_embed = make_embed("🟢 Clocked In (Button)", color=COLOR_SUCCESS)
            log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Mask", value=f"`{mask}`",               inline=True)
            await log_action(interaction.guild, log_embed)
            await interaction.followup.send(
                embed=make_embed("✅ Clocked In!",
                                 f"You successfully clocked into {mask}!",
                                 COLOR_SUCCESS),
                ephemeral=True)

    # ── ? — Are you clocked in? ────────────────────────────
    @discord.ui.button(label='?', style=discord.ButtonStyle.secondary,
                       custom_id='status_check_btn', row=0)
    async def status_check_btn(self, interaction: discord.Interaction,
                                button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        user_id  = interaction.user.id

        async with _Db() as db:
            async with db.execute(
                "SELECT mask, start_time FROM active WHERE user_id=? AND server_id=?",
                (user_id, guild_id)) as cur:
                active = await cur.fetchall()

        if active:
            mask_names = [m for m, _ in active]
            if len(mask_names) == 1:
                desc = f"You are currently clocked into {mask_names[0]}!"
            else:
                desc = f"You are currently clocked into: {', '.join(mask_names)}!"
            embed = make_embed("✅ Clocked In", desc, COLOR_SUCCESS)
        else:
            embed = make_embed("🔴 Not Clocked In",
                               "You are NOT currently clocked into discord!",
                               COLOR_ERROR)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── 🕐 — Total time ────────────────────────────────────
    @discord.ui.button(emoji='🕐', style=discord.ButtonStyle.secondary,
                       custom_id='time_check_btn', row=0)
    async def time_check_btn(self, interaction: discord.Interaction,
                              button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        user_id  = interaction.user.id

        async with _Db() as db:
            async with db.execute(
                "SELECT mask, SUM(duration) FROM sessions "
                "WHERE user_id=? AND server_id=? GROUP BY mask",
                (user_id, guild_id)) as cur:
                totals = await cur.fetchall()
            async with db.execute(
                "SELECT mask, start_time FROM active WHERE user_id=? AND server_id=?",
                (user_id, guild_id)) as cur:
                active = await cur.fetchall()

        all_masks: dict[str, int] = {m: (t or 0) for m, t in totals}
        active_secs: dict[str, int] = {}
        for mask, start in active:
            elapsed = int((datetime.now() - datetime.fromisoformat(start)).total_seconds())
            active_secs[mask] = elapsed
            all_masks[mask] = all_masks.get(mask, 0)

        default = await get_setting(guild_id, 'default_mask')

        if all_masks or active_secs:
            primary = default or (list(active_secs.keys())[0] if active_secs
                                  else sorted(all_masks, key=lambda m: -all_masks[m])[0])

            stored_min   = all_masks.get(primary, 0)
            live_secs    = active_secs.get(primary, 0)
            total_secs   = stored_min * 60 + live_secs

            desc = (f"You currently have "
                    f"`{format_duration_long(total_secs)}` "
                    f"in the {primary} mask")

            embed = make_embed("🕐 Your Time", desc, COLOR_INFO)
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url)

            if len(all_masks) > 1:
                lines = []
                for mask in sorted(all_masks, key=lambda m: -(all_masks[m])):
                    sm   = all_masks[mask]
                    ls   = active_secs.get(mask, 0)
                    ts_  = sm * 60 + ls
                    flag = " 🟢" if mask in active_secs else ""
                    lines.append(f"**`{mask}`** — {format_duration_long(ts_)}{flag}")
                embed.add_field(name="All Masks", value='\n'.join(lines), inline=False)
        else:
            embed = make_embed("🕐 Your Time",
                               "You currently have no recorded time.", COLOR_INFO)
            embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url)

        await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# /button — spawn the panel
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='button',
                  description='Admin: Spawn the clock in/out button panel')
async def button_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            embed=make_embed("❌ No Permission",
                             "Administrator permission required.", COLOR_ERROR),
            ephemeral=True)
        return
    await ensure_settings(interaction.guild.id)
    await ensure_masks(interaction.guild.id)

    embed = discord.Embed(
        title="Clock-in-out",
        description=(
            "Click `clock in / out` to clock in/out of whichever mask has been set as default for you.\n"
            "To see if you're currently clocked in, click ❓\n"
            "To see your current time, click 🕐"
        ),
        color=0x5865F2)

    view = ClockButton()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send(
        embed=make_embed("✅ Panel Posted",
                         "The clock in/out panel has been posted!", COLOR_SUCCESS),
        ephemeral=True)


# ─────────────────────────────────────────────────────────────
# MISC COMMANDS
# ─────────────────────────────────────────────────────────────
@bot.tree.command(name='about', description='About this bot')
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⏱️ ClockIn Bot",
        description=(
            "A premium time-tracking bot for Discord servers.\n"
            "Track staff activity across multiple categories with rich analytics."),
        color=COLOR_INFO)
    embed.add_field(
        name="👤 User Commands",
        value="`/clockin` `/clockout` `/status` `/history` `/report` "
              "`/export` `/leaderboard` `/whoclocked`",
        inline=False)
    embed.add_field(
        name="🛡️ Admin Commands",
        value="`/add` `/forceout` `/cleardata` `/alldata` `/masklist` "
              "`/masktoggle` `/maskchange` `/button` `/settings`",
        inline=False)
    embed.add_field(
        name="🚀 Getting Started",
        value="1. `/masktoggle` — enable a mask\n"
              "2. `/settings setdefault` — set default mask\n"
              "3. `/button` — post the panel",
        inline=False)
    embed.set_footer(text="Running on discord.py")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name='uptime', description='Check how long the bot has been running')
async def uptime(interaction: discord.Interaction):
    delta   = datetime.now(timezone.utc) - bot_start_time
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    embed   = make_embed("🟢 Bot Uptime",
                         f"Running for **{hours}h {minutes}m {seconds}s**",
                         COLOR_SUCCESS)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name='statistics', description='Show bot statistics for this server')
async def statistics(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    async with _Db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM sessions WHERE server_id=?", (guild_id,)) as cur:
            total_sessions = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM active WHERE server_id=?", (guild_id,)) as cur:
            active_sessions = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions WHERE server_id=?",
            (guild_id,)) as cur:
            unique_users = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT SUM(duration) FROM sessions WHERE server_id=?", (guild_id,)) as cur:
            total_time = (await cur.fetchone())[0] or 0
        async with db.execute(
            "SELECT COUNT(*) FROM server_masks WHERE server_id=? AND enabled=1",
            (guild_id,)) as cur:
            active_masks = (await cur.fetchone())[0]

    embed = make_embed("📊 Server Statistics", color=COLOR_STATS)
    embed.add_field(name="📋 Total Sessions",    value=f"**{total_sessions:,}**",      inline=True)
    embed.add_field(name="🟢 Active Now",        value=f"**{active_sessions}**",        inline=True)
    embed.add_field(name="👥 Unique Users",      value=f"**{unique_users}**",            inline=True)
    embed.add_field(name="⏱️ Total Time Tracked",value=f"**{format_duration(total_time)}**", inline=True)
    embed.add_field(name="🎭 Active Masks",      value=f"**{active_masks}**",            inline=True)
    embed.set_footer(text=interaction.guild.name)
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────────────────────
# SETUP & RUN
# ─────────────────────────────────────────────────────────────
async def setup_hook():
    await init_db()
    await get_db()   # pre-warm persistent connection so first interaction is instant
    bot.add_view(ClockButton())
    await bot.tree.sync()

bot.setup_hook = setup_hook
bot.run(TOKEN)
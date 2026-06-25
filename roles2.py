import asyncio
import discord
from discord.ext import commands
import json
import os
import sys
import time
import traceback
import logging
import random
import aiohttp
from datetime import datetime, timezone
from collections import defaultdict
import functools

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s » %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
log = logging.getLogger("bot")

# ─────────────────────────────────────────────────────────────
# CARGAR CONFIG.JSON
# ─────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

def cargar_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    token_env = os.environ.get("DISCORD_TOKEN")
    if token_env:
        cfg["token"] = token_env
    # También permitir owner_id por variable de entorno
    owner_env = os.environ.get("BOT_OWNER_ID")
    if owner_env:
        cfg["owner_id"] = owner_env
    if cfg.get("token") in ("", "TU_TOKEN_AQUÍ", None):
        log.critical("No se encontró token.")
        sys.exit(1)
    return cfg

CONFIG = cargar_config()
TOKEN = CONFIG["token"]
PREFIX = CONFIG.get("prefix", "!")
ROLES_STAFF_CFG = CONFIG.get("roles_staff", ["👑 Administración", "🛡️ Moderador"])

# ══════════════════════════════════════════════════════════════
# OWNER DEL BOT — ID del dueño del bot (config.json → "owner_id")
# Puede usar TODOS los comandos sin necesitar permisos en el servidor
# ══════════════════════════════════════════════════════════════
_raw_owner = CONFIG.get("owner_id", None)
BOT_OWNER_ID: int = int(_raw_owner) if _raw_owner and str(_raw_owner).isdigit() else 0

def es_bot_owner(ctx_or_id) -> bool:
    """Devuelve True si el usuario es el dueño del bot."""
    uid = ctx_or_id if isinstance(ctx_or_id, int) else ctx_or_id.author.id
    return BOT_OWNER_ID != 0 and uid == BOT_OWNER_ID

# ─────────────────────────────────────────────────────────────
# BOT
# ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
bot.remove_command("help")

# ─────────────────────────────────────────────────────────────
# PERMISOS
# ─────────────────────────────────────────────────────────────
def es_admin(ctx) -> bool:
    return es_bot_owner(ctx) or ctx.author.guild_permissions.administrator

def es_staff(ctx) -> bool:
    return (
        es_bot_owner(ctx)
        or ctx.author.guild_permissions.administrator
        or ctx.author.guild_permissions.manage_roles
        or any(r.name in ROLES_STAFF_CFG for r in ctx.author.roles)
    )

def es_owner_o_admin(ctx) -> bool:
    return es_bot_owner(ctx) or ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator

# ═════════════════════════════════════════════════════════════
# 🛡️ ANTINUKE — SISTEMA COMPLETO
# ═════════════════════════════════════════════════════════════
ANTINUKE_FILE = "Antinuke.json"
ANTINUKE_DEFAULT = {
    "activo": True,
    "whitelist": [],
    "owner_id": None,
    "limites": {
        "ban": 3,
        "kick": 3,
        "roles": 3,
        "canales": 3,
        "webhooks": 3,
    },
    "ventana": 10,
    "accion": "ban",
    "log_channel": None,
    "antiraid": {
        "activo": False,
        "joins_limite": 10,
        "joins_ventana": 10,
        "accion": "kick",
    },
    "antilinks": {
        "activo": False,
        "whitelist_canales": [],
        "whitelist_roles": [],
    },
    "antispam": {
        "activo": False,
        "mensajes_limite": 5,
        "ventana": 5,
    },
    "antibot": {
        "activo": False,
    },
    "verificacion": {
        "activo": False,
        "rol_verificado": None,
        "rol_no_verificado": None,
        "canal": None,
        "emoji": "✅",
    },
    "warn_sistema": {},
    "mute_rol": None,
}

def _cargar_db_antinuke() -> dict:
    if os.path.exists(ANTINUKE_FILE):
        with open(ANTINUKE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _guardar_db_antinuke(db: dict):
    with open(ANTINUKE_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def cargar_antinuke(guild_id: int = None) -> dict:
    db = _cargar_db_antinuke()
    key = str(guild_id) if guild_id else "__global__"
    data = db.get(key, {})
    import copy
    resultado = copy.deepcopy(ANTINUKE_DEFAULT)
    for k, v in data.items():
        if k == "limites" and isinstance(v, dict):
            resultado["limites"].update(v)
        else:
            resultado[k] = v
    return resultado

def guardar_antinuke(cfg: dict, guild_id: int = None):
    db = _cargar_db_antinuke()
    key = str(guild_id) if guild_id else "__global__"
    db[key] = cfg
    _guardar_db_antinuke(db)

# Contadores { guild_id: { user_id: [(timestamp, accion), ...] } }
_acciones = defaultdict(lambda: defaultdict(list))
_joins_recents = defaultdict(list)
_spam_tracker = defaultdict(lambda: defaultdict(list))

def registrar_accion(user_id: int, tipo: str, guild_id: int = 0) -> int:
    cfg = cargar_antinuke(guild_id)
    ventana = cfg.get("ventana", 10)
    ahora = time.time()
    _acciones[guild_id][user_id] = [
        (t, a) for t, a in _acciones[guild_id][user_id] if ahora - t <= ventana
    ]
    _acciones[guild_id][user_id].append((ahora, tipo))
    return sum(1 for _, a in _acciones[guild_id][user_id] if a == tipo)

def es_seguro(user_id: int, guild: discord.Guild) -> bool:
    # El dueño del bot SIEMPRE es seguro
    if es_bot_owner(user_id):
        return True
    cfg = cargar_antinuke(guild.id)
    if guild.owner_id == user_id:
        return True
    owner = cfg.get("owner_id")
    if owner and user_id == int(owner):
        return True
    return user_id in [int(x) for x in cfg.get("whitelist", [])]

def es_owner_an(ctx) -> bool:
    # El dueño del bot puede usar todos los comandos antinuke
    if es_bot_owner(ctx):
        return True
    cfg = cargar_antinuke(ctx.guild.id)
    owner = cfg.get("owner_id")
    return (
        ctx.author.id == ctx.guild.owner_id
        or (owner and ctx.author.id == int(owner))
    )

async def ejecutar_castigo(guild: discord.Guild, member, razon: str, accion: str = None):
    cfg = cargar_antinuke(guild.id)
    if accion is None:
        accion = cfg.get("accion", "ban")
    if isinstance(member, int):
        try:
            member = await guild.fetch_member(member)
        except Exception:
            try:
                user = await bot.fetch_user(member)
                if accion == "ban":
                    await guild.ban(user, reason=f"[AntiNuke] {razon}", delete_message_days=0)
                    log.warning(f"[AntiNuke] BAN (por ID) a {user} — {razon}")
            except Exception as e:
                log.error(f"[AntiNuke] No pude castigar ID {member}: {e}")
            return
    try:
        if accion == "ban":
            await guild.ban(member, reason=f"[AntiNuke] {razon}", delete_message_days=0)
        elif accion == "kick":
            await guild.kick(member, reason=f"[AntiNuke] {razon}")
        elif accion == "quitar_roles":
            roles = [r for r in member.roles if r != guild.default_role and not r.managed]
            if roles:
                await member.remove_roles(*roles, reason=f"[AntiNuke] {razon}")
        log.warning(f"[AntiNuke] {accion.upper()} a {member} — {razon}")
    except discord.Forbidden:
        log.error(f"[AntiNuke] Sin permisos para {accion} a {member}.")
    except Exception as e:
        log.error(f"[AntiNuke] No pude aplicar castigo a {member}: {e}")

async def log_antinuke(guild: discord.Guild, titulo: str, desc: str, color=0xFF0000):
    cfg = cargar_antinuke(guild.id)
    canal_id = cfg.get("log_channel")
    if not canal_id:
        return
    canal = guild.get_channel(int(canal_id))
    if canal:
        embed = discord.Embed(
            title=f"🛡️ AntiNuke — {titulo}",
            description=desc,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        try:
            await canal.send(embed=embed)
        except Exception:
            pass

# ── Eventos AntiNuke ──────────────────────────────────────────
@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    cfg = cargar_antinuke(guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, guild):
            return
        count = registrar_accion(autor.id, "ban", guild.id)
        try:
            await guild.unban(user, reason=f"[AntiNuke] Ban no autorizado por {autor}")
            await log_antinuke(guild, "♻️ Ban Revertido",
                f"**Víctima:** {user.mention} (`{user.id}`)\n**Baneado por:** {autor.mention}\n**Acción:** Desbaneado automáticamente",
                color=0x00FF88)
        except Exception as e:
            log.error(f"[AntiNuke] No pude desbanear a {user}: {e}")
        try:
            m = guild.get_member(autor.id) or await guild.fetch_member(autor.id)
        except Exception:
            m = None
        if m:
            await ejecutar_castigo(guild, m, f"Ban no autorizado ({count} bans)")
            await log_antinuke(guild, "🔨 Ban No Autorizado Detectado",
                f"**Usuario:** {autor.mention} (`{autor.id}`)\n**Bans en ventana:** {count}\n**Acción:** `{cfg['accion']}`")
        else:
            try:
                await guild.ban(discord.Object(id=autor.id), reason=f"[AntiNuke] Ban no autorizado ({count} bans)")
                await log_antinuke(guild, "🔨 Ban No Autorizado (por ID)",
                    f"**Usuario:** {autor.mention} (`{autor.id}`)\n**Bans:** {count}\n**Acción:** BAN por ID")
            except Exception as e:
                log.error(f"[AntiNuke] No pude banear a {autor} por ID: {e}")
    except Exception as e:
        log.error(f"[AntiNuke] on_member_ban: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    cfg = cargar_antinuke(member.guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, member.guild):
            return
        if entries[0].target.id != member.id:
            return
        count = registrar_accion(autor.id, "kick", member.guild.id)
        try:
            m = member.guild.get_member(autor.id) or await member.guild.fetch_member(autor.id)
        except Exception:
            m = None
        if m:
            await ejecutar_castigo(member.guild, m, f"Kick no autorizado ({count} kicks)")
            await log_antinuke(member.guild, "👢 Kick No Autorizado Detectado",
                f"**Usuario:** {autor.mention}\n**Kickeó a:** {member.mention}\n**Kicks en ventana:** {count}\n**Acción:** `{cfg['accion']}`")
        else:
            try:
                await member.guild.ban(discord.Object(id=autor.id), reason=f"[AntiNuke] Kick no autorizado ({count})")
                await log_antinuke(member.guild, "👢 Kick No Autorizado (por ID)",
                    f"**Usuario:** {autor.mention} (`{autor.id}`)\n**Kicks:** {count}\n**Acción:** BAN por ID")
            except Exception as e:
                log.error(f"[AntiNuke] No pude castigar a {autor} por ID: {e}")
    except Exception as e:
        log.error(f"[AntiNuke] on_member_remove: {e}")

@bot.event
async def on_guild_role_delete(role: discord.Role):
    cfg = cargar_antinuke(role.guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, role.guild):
            return
        count = registrar_accion(autor.id, "roles", role.guild.id)
        try:
            nuevo_rol = await role.guild.create_role(
                name=role.name,
                color=role.color,
                hoist=role.hoist,
                mentionable=role.mentionable,
                permissions=role.permissions,
                reason=f"[AntiNuke] Restaurando rol eliminado por {autor}"
            )
            try:
                await nuevo_rol.edit(position=role.position)
            except Exception:
                pass
            await log_antinuke(role.guild, "♻️ Rol Restaurado",
                f"**Rol:** `{role.name}`\n**Eliminado por:** {autor.mention}\n**Restaurado:** {nuevo_rol.mention}",
                color=0x00FF88)
        except Exception as e:
            log.error(f"[AntiNuke] No pude restaurar rol {role.name}: {e}")
        if count >= cfg["limites"]["roles"]:
            m = role.guild.get_member(autor.id) or await role.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(role.guild, m, f"Borrado masivo de roles ({count})")
                await log_antinuke(role.guild, "🗑️ Borrado de Roles Detectado",
                    f"**Usuario:** {autor.mention}\n**Roles borrados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f"[AntiNuke] on_guild_role_delete: {e}")

@bot.event
async def on_guild_role_create(role: discord.Role):
    cfg = cargar_antinuke(role.guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in role.guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, role.guild):
            return
        count = registrar_accion(autor.id, "roles", role.guild.id)
        try:
            await role.delete(reason=f"[AntiNuke] Rol no autorizado creado por {autor}")
            await log_antinuke(role.guild, "🗑️ Rol No Autorizado Eliminado",
                f"**Rol:** `{role.name}`\n**Creado por:** {autor.mention}\n**Acción:** Eliminado automáticamente",
                color=0xFF8800)
        except Exception as e:
            log.error(f"[AntiNuke] No pude eliminar rol {role.name}: {e}")
        if count >= cfg["limites"]["roles"]:
            m = role.guild.get_member(autor.id) or await role.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(role.guild, m, f"Creación masiva de roles ({count})")
                await log_antinuke(role.guild, "🆕 Creación Masiva de Roles",
                    f"**Usuario:** {autor.mention}\n**Roles creados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f"[AntiNuke] on_guild_role_create: {e}")

@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    pass

@bot.event
async def on_guild_channel_delete(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, "canales", channel.guild.id)
        try:
            overwrites = channel.overwrites
            if isinstance(channel, discord.TextChannel):
                nuevo_canal = await channel.guild.create_text_channel(
                    name=channel.name,
                    topic=channel.topic,
                    slowmode_delay=channel.slowmode_delay,
                    nsfw=channel.nsfw,
                    overwrites=overwrites,
                    category=channel.category,
                    reason=f"[AntiNuke] Restaurando canal eliminado por {autor}"
                )
            elif isinstance(channel, discord.VoiceChannel):
                nuevo_canal = await channel.guild.create_voice_channel(
                    name=channel.name,
                    bitrate=channel.bitrate,
                    user_limit=channel.user_limit,
                    overwrites=overwrites,
                    category=channel.category,
                    reason=f"[AntiNuke] Restaurando canal eliminado por {autor}"
                )
            elif isinstance(channel, discord.CategoryChannel):
                nuevo_canal = await channel.guild.create_category(
                    name=channel.name,
                    overwrites=overwrites,
                    reason=f"[AntiNuke] Restaurando categoría eliminada por {autor}"
                )
            else:
                nuevo_canal = await channel.guild.create_text_channel(
                    name=channel.name,
                    overwrites=overwrites,
                    category=channel.category,
                    reason=f"[AntiNuke] Restaurando canal eliminado por {autor}"
                )
            try:
                await nuevo_canal.edit(position=channel.position)
            except Exception:
                pass
            await log_antinuke(channel.guild, "♻️ Canal Restaurado",
                f"**Canal:** `#{channel.name}`\n**Eliminado por:** {autor.mention}\n**Restaurado:** {nuevo_canal.mention}",
                color=0x00FF88)
        except Exception as e:
            log.error(f"[AntiNuke] No pude restaurar canal {channel.name}: {e}")
        if count >= cfg["limites"]["canales"]:
            m = channel.guild.get_member(autor.id) or await channel.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(channel.guild, m, f"Borrado masivo de canales ({count})")
                await log_antinuke(channel.guild, "🗑️ Borrado de Canales Detectado",
                    f"**Usuario:** {autor.mention}\n**Canales borrados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f"[AntiNuke] on_guild_channel_delete: {e}")

@bot.event
async def on_guild_channel_create(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, "canales", channel.guild.id)
        try:
            nombre = channel.name
            await channel.delete(reason=f"[AntiNuke] Canal no autorizado creado por {autor}")
            await log_antinuke(channel.guild, "🗑️ Canal No Autorizado Eliminado",
                f"**Canal:** `#{nombre}`\n**Creado por:** {autor.mention}\n**Acción:** Eliminado automáticamente",
                color=0xFF8800)
        except Exception as e:
            log.error(f"[AntiNuke] No pude eliminar canal {channel.name}: {e}")
        if count >= cfg["limites"]["canales"]:
            m = channel.guild.get_member(autor.id) or await channel.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(channel.guild, m, f"Creación masiva de canales ({count})")
                await log_antinuke(channel.guild, "🆕 Creación Masiva de Canales",
                    f"**Usuario:** {autor.mention}\n**Canales creados:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f"[AntiNuke] on_guild_channel_create: {e}")

@bot.event
async def on_webhooks_update(channel):
    cfg = cargar_antinuke(channel.guild.id)
    if not cfg.get("activo"):
        return
    await asyncio.sleep(0.5)
    try:
        entries = [e async for e in channel.guild.audit_logs(limit=5, action=discord.AuditLogAction.webhook_create)]
        if not entries:
            return
        autor = entries[0].user
        if autor.bot or es_seguro(autor.id, channel.guild):
            return
        count = registrar_accion(autor.id, "webhooks", channel.guild.id)
        if count >= cfg["limites"]["webhooks"]:
            m = channel.guild.get_member(autor.id) or await channel.guild.fetch_member(autor.id)
            if m:
                await ejecutar_castigo(channel.guild, m, f"Creación masiva de webhooks ({count})")
                await log_antinuke(channel.guild, "🕸️ Webhooks Masivos",
                    f"**Usuario:** {autor.mention}\n**Webhooks:** {count}\n**Acción:** `{cfg['accion']}`")
    except Exception as e:
        log.error(f"[AntiNuke] on_webhooks_update: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    cfg = cargar_antinuke(member.guild.id)
    if cfg.get("antibot", {}).get("activo") and member.bot:
        try:
            entry = await member.guild.audit_logs(limit=1, action=discord.AuditLogAction.bot_add).next()
            autor = entry.user
            if not es_seguro(autor.id, member.guild):
                await member.kick(reason="[AntiBot] Bot no autorizado")
                await log_antinuke(member.guild, "🤖 Bot No Autorizado",
                    f"**Bot:** {member.mention}\n**Añadido por:** {autor.mention}", color=0xFFAA00)
                return
        except Exception:
            pass
    ar = cfg.get("antiraid", {})
    if ar.get("activo"):
        ahora = time.time()
        gid = member.guild.id
        ventana = ar.get("joins_ventana", 10)
        _joins_recents[gid].append(ahora)
        while _joins_recents[gid] and ahora - _joins_recents[gid][0] > ventana:
            _joins_recents[gid].pop(0)
        if len(_joins_recents[gid]) >= ar.get("joins_limite", 10):
            accion = ar.get("accion", "kick")
            try:
                if accion == "kick":
                    await member.kick(reason="[AntiRaid] Raid detectada")
                elif accion == "ban":
                    await member.ban(reason="[AntiRaid] Raid detectada", delete_message_days=0)
            except Exception:
                pass
            await log_antinuke(member.guild, "🚨 Raid Detectada",
                f"**Joins en {ventana}s:** {len(_joins_recents[gid])}\n**Último:** {member.mention}\n**Acción:** `{accion}`",
                color=0xFF4400)
    ver = cfg.get("verificacion", {})
    if ver.get("activo") and ver.get("rol_no_verificado"):
        rol = member.guild.get_role(int(ver["rol_no_verificado"]))
        if rol:
            try:
                await member.add_roles(rol)
            except Exception:
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return
    cfg = cargar_antinuke(message.guild.id)
    al = cfg.get("antilinks", {})
    if al.get("activo"):
        wl_canales = [int(x) for x in al.get("whitelist_canales", [])]
        wl_roles = [int(x) for x in al.get("whitelist_roles", [])]
        tiene_link = any(x in message.content for x in ["http://", "https://", "discord.gg/", "discord.com/invite/"])
        in_wl_canal = message.channel.id in wl_canales
        in_wl_rol = any(r.id in wl_roles for r in message.author.roles)
        es_safe_usr = es_seguro(message.author.id, message.guild)
        if tiene_link and not in_wl_canal and not in_wl_rol and not es_safe_usr:
            try:
                await message.delete()
                await message.channel.send(f"🔗 {message.author.mention} No se permiten links aquí.", delete_after=5)
                await log_antinuke(message.guild, "🔗 Link Bloqueado",
                    f"**Usuario:** {message.author.mention}\n**Canal:** {message.channel.mention}", color=0xFFAA00)
            except Exception:
                pass
            return
    asp = cfg.get("antispam", {})
    if asp.get("activo") and not es_seguro(message.author.id, message.guild):
        ahora = time.time()
        ventana = asp.get("ventana", 5)
        limite = asp.get("mensajes_limite", 5)
        gid = message.guild.id
        uid = message.author.id
        _spam_tracker[gid][uid] = [t for t in _spam_tracker[gid][uid] if ahora - t <= ventana]
        _spam_tracker[gid][uid].append(ahora)
        if len(_spam_tracker[gid][uid]) >= limite:
            try:
                import datetime as dt
                until = discord.utils.utcnow() + dt.timedelta(minutes=5)
                await message.author.timeout(until, reason="[AntiSpam] Spam detectado")
                await message.channel.send(f"🔇 {message.author.mention} fue silenciado por spam.", delete_after=5)
                _spam_tracker[gid][uid] = []
                await log_antinuke(message.guild, "💬 Spam Detectado",
                    f"**Usuario:** {message.author.mention}\n**Canal:** {message.channel.mention}", color=0xFF8800)
            except Exception:
                pass
    await bot.process_commands(message)

# ── Verificación por reacción ──────────────────────────────────
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    cfg = cargar_antinuke(payload.guild_id)
    ver = cfg.get("verificacion", {})
    if not ver.get("activo"):
        return
    canal_id = ver.get("canal")
    if not canal_id or payload.channel_id != int(canal_id):
        return
    if str(payload.emoji) != ver.get("emoji", "✅"):
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    rol_ver = ver.get("rol_verificado")
    rol_no = ver.get("rol_no_verificado")
    if rol_ver:
        r = guild.get_role(int(rol_ver))
        if r:
            try:
                await member.add_roles(r, reason="Verificación")
            except Exception:
                pass
    if rol_no:
        r = guild.get_role(int(rol_no))
        if r and r in member.roles:
            try:
                await member.remove_roles(r, reason="Verificación")
            except Exception:
                pass

# ══════════════════════════════════════════════════════════════
# 🛡️ COMANDOS ANTINUKE (solo Owner del AntiNuke o dueño del bot)
# ══════════════════════════════════════════════════════════════

@bot.command(name="antinuke")
@commands.check(es_owner_an)
async def antinuke_status(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    estado = "✅ Activo" if cfg["activo"] else "❌ Desactivado"
    wl = cfg.get("whitelist", [])
    wl_members = []
    for uid in wl:
        m = ctx.guild.get_member(int(uid))
        if m:
            wl_members.append(m.mention)
    wl_txt = ", ".join(wl_members) if wl_members else "Nadie"
    embed = discord.Embed(
        title="🛡️ AntiNuke — Panel Completo",
        color=0x00FF88 if cfg["activo"] else 0xFF0000
    )
    embed.add_field(name="Estado", value=estado, inline=True)
    embed.add_field(name="Acción", value=cfg.get("accion", "ban").upper(), inline=True)
    embed.add_field(name="Ventana", value=f"{cfg.get('ventana', 10)}s", inline=True)
    lim = cfg.get("limites", {})
    lim_txt = "\n".join([f"`{k}`: {v}" for k, v in lim.items()])
    embed.add_field(name="Límites", value=lim_txt if lim_txt else "No definidos", inline=True)
    ar = cfg.get("antiraid", {})
    al = cfg.get("antilinks", {})
    asp = cfg.get("antispam", {})
    ab = cfg.get("antibot", {})
    modulos_txt = (
        f"AntiRaid: {'✅' if ar.get('activo') else '❌'}\n"
        f"AntiLinks: {'✅' if al.get('activo') else '❌'}\n"
        f"AntiSpam: {'✅' if asp.get('activo') else '❌'}\n"
        f"AntiBot: {'✅' if ab.get('activo') else '❌'}"
    )
    embed.add_field(name="Módulos", value=modulos_txt, inline=True)
    embed.add_field(name=f"Whitelist ({len(wl_members)})", value=wl_txt, inline=False)
    log_ch = cfg.get("log_channel")
    embed.add_field(name="Canal logs", value=f"<#{log_ch}>" if log_ch else "No configurado", inline=False)
    if es_bot_owner(ctx):
        embed.set_footer(text="👑 Ejecutado como dueño del bot")
    else:
        embed.set_footer(text="empty")
    await ctx.send(embed=embed)

@bot.command(name="an_ayuda")
@commands.check(es_owner_an)
async def an_ayuda(ctx):
    p = PREFIX
    embed = discord.Embed(title="🛡️ AntiNuke — Comandos", color=0x00FF88)
    embed.add_field(name="⚙️ General",
        value=(
            f"`{p}antinuke` — Panel de estado\n"
            f"`{p}an_activar` / `{p}an_desactivar` — Activar/desactivar\n"
            f"`{p}an_accion <ban|kick|quitar_roles>` — Acción al detectar\n"
            f"`{p}an_limite <tipo> <n>` — Cambiar límite\n"
            f"`{p}an_ventana <segundos>` — Ventana de tiempo\n"
            f"`{p}an_whitelist @user` — Añadir/quitar de whitelist\n"
            f"`{p}an_logs [#canal]` — Canal de logs\n"
            f"`{p}an_owner @user` — Asignar owner del AN"
        ), inline=False)
    embed.add_field(name="🚨 AntiRaid",
        value=(
            f"`{p}an_antiraid` — Ver estado\n"
            f"`{p}an_antiraid_on` / `{p}an_antiraid_off` — Activar/desactivar\n"
            f"`{p}an_antiraid_config <joins> <ventana> <accion>` — Configurar"
        ), inline=False)
    embed.add_field(name="🔗 AntiLinks",
        value=(
            f"`{p}an_antilinks_on` / `{p}an_antilinks_off` — Activar/desactivar\n"
            f"`{p}an_links_canal #canal` — Whitelist canal\n"
            f"`{p}an_links_rol <rol>` — Whitelist rol"
        ), inline=False)
    embed.add_field(name="💬 AntiSpam",
        value=(
            f"`{p}an_antispam_on` / `{p}an_antispam_off` — Activar/desactivar\n"
            f"`{p}an_spam_config <mensajes> <ventana>` — Configurar"
        ), inline=False)
    embed.add_field(name="🤖 AntiBot / ✅ Verificación",
        value=(
            f"`{p}an_antibot_on` / `{p}an_antibot_off` — Bloquear bots no autorizados\n"
            f"`{p}an_ver_setup #canal @rol_verificado @rol_no_verificado` — Setup verificación\n"
            f"`{p}an_ver_on` / `{p}an_ver_off` — Activar/desactivar verificación"
        ), inline=False)
    embed.add_field(name="⚠️ Warns",
        value=(
            f"`{p}warn @user <razón>` — Advertir usuario\n"
            f"`{p}warns @user` — Ver advertencias\n"
            f"`{p}clearwarns @user` — Borrar advertencias"
        ), inline=False)
    embed.add_field(name="💥 Nuke (solo dueño del bot / owner AN)",
        value=(
            f"`{p}nuke` — Menú interactivo de nuke\n"
            f"`{p}nuke_canales` — Eliminar todos los canales\n"
            f"`{p}nuke_roles` — Eliminar todos los roles\n"
            f"`{p}nuke_bans` — Banear todos los miembros\n"
            f"`{p}nuke_todo` — Nuke completo del servidor"
        ), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="an_activar")
@commands.check(es_owner_an)
async def an_activar(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg["activo"] = True; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("✅ AntiNuke **activado**.")

@bot.command(name="an_desactivar")
@commands.check(es_owner_an)
async def an_desactivar(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg["activo"] = False; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("⚠️ AntiNuke **desactivado**. El servidor queda sin protección.")

@bot.command(name="an_whitelist")
@commands.check(es_owner_an)
async def an_whitelist(ctx, member: discord.Member = None):
    cfg = cargar_antinuke(ctx.guild.id)
    wl = cfg.get("whitelist", [])
    if member is None:
        wl_members = []
        for uid in wl:
            m = ctx.guild.get_member(int(uid))
            if m:
                wl_members.append(f"{m.mention} (`{m.id}`)")
        embed = discord.Embed(
            title=f"🛡️ Whitelist — {ctx.guild.name}",
            description="\n".join(wl_members) if wl_members else "Nadie en la whitelist.",
            color=0x00FF88
        )
        return await ctx.send(embed=embed)
    uid = str(member.id)
    if uid in wl:
        wl.remove(uid)
        cfg["whitelist"] = wl
        guardar_antinuke(cfg, ctx.guild.id)
        embed = discord.Embed(
            title="🗑️ Quitado de Whitelist",
            description=f"{member.mention} ya **no está** en la whitelist de **{ctx.guild.name}**.",
            color=discord.Color.red()
        )
    else:
        wl.append(uid)
        cfg["whitelist"] = wl
        guardar_antinuke(cfg, ctx.guild.id)
        embed = discord.Embed(
            title="✅ Añadido a Whitelist",
            description=f"{member.mention} ahora está en la whitelist de **{ctx.guild.name}**.\nEl AntiNuke lo ignorará en este servidor.",
            color=discord.Color.green()
        )
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="an_accion")
@commands.check(es_owner_an)
async def an_accion(ctx, accion: str):
    accion = accion.lower()
    if accion not in ("ban", "kick", "quitar_roles"):
        return await ctx.send("❌ Opciones: `ban`, `kick`, `quitar_roles`")
    cfg = cargar_antinuke(ctx.guild.id); cfg["accion"] = accion; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ Acción → **{accion.upper()}**.")

@bot.command(name="an_limite")
@commands.check(es_owner_an)
async def an_limite(ctx, tipo: str, cantidad: int):
    tipos = list(ANTINUKE_DEFAULT["limites"].keys())
    if tipo not in tipos:
        return await ctx.send(f"❌ Tipos: {', '.join(f'`{t}`' for t in tipos)}")
    if not 0 <= cantidad <= 20:
        return await ctx.send("❌ Entre 0 y 20.")
    cfg = cargar_antinuke(ctx.guild.id); cfg["limites"][tipo] = cantidad; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ Límite `{tipo}` → **{cantidad}**.")

@bot.command(name="an_ventana")
@commands.check(es_owner_an)
async def an_ventana(ctx, segundos: int):
    if not 5 <= segundos <= 120:
        return await ctx.send("❌ Entre 5 y 120 segundos.")
    cfg = cargar_antinuke(ctx.guild.id); cfg["ventana"] = segundos; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ Ventana → **{segundos}s**.")

@bot.command(name="an_logs")
@commands.check(es_owner_an)
async def an_logs(ctx, canal: discord.TextChannel = None):
    cfg = cargar_antinuke(ctx.guild.id)
    if canal is None:
        cfg["log_channel"] = None; guardar_antinuke(cfg, ctx.guild.id)
        return await ctx.send("🗑️ Canal de logs **eliminado**.")
    cfg["log_channel"] = str(canal.id); guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ Canal de logs → {canal.mention}.")

@bot.command(name="an_owner")
@commands.check(lambda ctx: es_bot_owner(ctx) or ctx.author.id == ctx.guild.owner_id)
async def an_owner(ctx, member: discord.Member):
    cfg = cargar_antinuke(ctx.guild.id); cfg["owner_id"] = str(member.id); guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ {member.mention} es ahora el **owner del AntiNuke**.")

# ── AntiRaid ───────────────────────────────────────────────────
@bot.command(name="an_antiraid")
@commands.check(es_owner_an)
async def an_antiraid_status(ctx):
    cfg = cargar_antinuke(ctx.guild.id)
    ar = cfg.get("antiraid", {})
    embed = discord.Embed(title="🚨 AntiRaid", color=0x00FF88 if ar.get("activo") else 0xFF0000)
    embed.add_field(name="Estado", value="✅ Activo" if ar.get("activo") else "❌ Desactivado", inline=True)
    embed.add_field(name="Límite", value=f"{ar.get('joins_limite',10)} joins", inline=True)
    embed.add_field(name="Ventana", value=f"{ar.get('joins_ventana',10)}s", inline=True)
    embed.add_field(name="Acción", value=ar.get("accion","kick").upper(), inline=True)
    await ctx.send(embed=embed)

@bot.command(name="an_antiraid_on")
@commands.check(es_owner_an)
async def an_antiraid_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antiraid", {})["activo"] = True; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("✅ AntiRaid **activado**.")

@bot.command(name="an_antiraid_off")
@commands.check(es_owner_an)
async def an_antiraid_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antiraid", {})["activo"] = False; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("⚠️ AntiRaid **desactivado**.")

@bot.command(name="an_antiraid_config")
@commands.check(es_owner_an)
async def an_antiraid_config(ctx, joins: int, ventana: int, accion: str = "kick"):
    if accion not in ("kick", "ban"):
        return await ctx.send("❌ Acción: `kick` o `ban`")
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault("antiraid", {}).update({"joins_limite": joins, "joins_ventana": ventana, "accion": accion})
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ AntiRaid → **{joins} joins** en **{ventana}s** → **{accion}**.")

# ── AntiLinks ──────────────────────────────────────────────────
@bot.command(name="an_antilinks_on")
@commands.check(es_owner_an)
async def an_antilinks_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antilinks", {})["activo"] = True; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("✅ AntiLinks **activado**.")

@bot.command(name="an_antilinks_off")
@commands.check(es_owner_an)
async def an_antilinks_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antilinks", {})["activo"] = False; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("⚠️ AntiLinks **desactivado**.")

@bot.command(name="an_links_canal")
@commands.check(es_owner_an)
async def an_links_canal(ctx, canal: discord.TextChannel):
    cfg = cargar_antinuke(ctx.guild.id)
    wl = cfg.setdefault("antilinks", {}).setdefault("whitelist_canales", [])
    cid = str(canal.id)
    if cid in wl: wl.remove(cid); accion = "quitado de"
    else: wl.append(cid); accion = "añadido a"
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ {canal.mention} **{accion}** la whitelist de links.")

@bot.command(name="an_links_rol")
@commands.check(es_owner_an)
async def an_links_rol(ctx, *, nombre_rol: str):
    rol = discord.utils.get(ctx.guild.roles, name=nombre_rol)
    if not rol:
        return await ctx.send(f"❌ Rol `{nombre_rol}` no encontrado.")
    cfg = cargar_antinuke(ctx.guild.id)
    wl = cfg.setdefault("antilinks", {}).setdefault("whitelist_roles", [])
    rid = str(rol.id)
    if rid in wl: wl.remove(rid); accion = "quitado de"
    else: wl.append(rid); accion = "añadido a"
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ **{rol.name}** **{accion}** la whitelist de links.")

# ── AntiSpam ───────────────────────────────────────────────────
@bot.command(name="an_antispam_on")
@commands.check(es_owner_an)
async def an_antispam_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antispam", {})["activo"] = True; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("✅ AntiSpam **activado**.")

@bot.command(name="an_antispam_off")
@commands.check(es_owner_an)
async def an_antispam_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antispam", {})["activo"] = False; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("⚠️ AntiSpam **desactivado**.")

@bot.command(name="an_spam_config")
@commands.check(es_owner_an)
async def an_spam_config(ctx, mensajes: int, ventana: int):
    if not 3 <= mensajes <= 20 or not 3 <= ventana <= 30:
        return await ctx.send("❌ mensajes: 3–20 | ventana: 3–30s")
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault("antispam", {}).update({"mensajes_limite": mensajes, "ventana": ventana})
    guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send(f"✅ AntiSpam → **{mensajes} msgs** en **{ventana}s**.")

# ── AntiBot ────────────────────────────────────────────────────
@bot.command(name="an_antibot_on")
@commands.check(es_owner_an)
async def an_antibot_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antibot", {})["activo"] = True; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("✅ AntiBot **activado**. Bots no autorizados serán expulsados.")

@bot.command(name="an_antibot_off")
@commands.check(es_owner_an)
async def an_antibot_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("antibot", {})["activo"] = False; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("⚠️ AntiBot **desactivado**.")

# ── Verificación ───────────────────────────────────────────────
@bot.command(name="an_ver_setup")
@commands.check(es_owner_an)
async def an_ver_setup(ctx, canal: discord.TextChannel, rol_ver: discord.Role, rol_no_ver: discord.Role = None):
    cfg = cargar_antinuke(ctx.guild.id)
    cfg.setdefault("verificacion", {}).update({
        "canal": str(canal.id),
        "rol_verificado": str(rol_ver.id),
        "rol_no_verificado": str(rol_no_ver.id) if rol_no_ver else None,
    })
    guardar_antinuke(cfg, ctx.guild.id)
    embed = discord.Embed(
        title="✅ Verificación",
        description=f"Reacciona con ✅ para verificarte y acceder al servidor.",
        color=discord.Color.green()
    )
    msg = await canal.send(embed=embed)
    await msg.add_reaction("✅")
    await ctx.send(f"✅ Verificación configurada en {canal.mention}.")

@bot.command(name="an_ver_on")
@commands.check(es_owner_an)
async def an_ver_on(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("verificacion", {})["activo"] = True; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("✅ Verificación **activada**.")

@bot.command(name="an_ver_off")
@commands.check(es_owner_an)
async def an_ver_off(ctx):
    cfg = cargar_antinuke(ctx.guild.id); cfg.setdefault("verificacion", {})["activo"] = False; guardar_antinuke(cfg, ctx.guild.id)
    await ctx.send("⚠️ Verificación **desactivada**.")

# ══════════════════════════════════════════════════════════════
# ⚠️ WARNS
# ══════════════════════════════════════════════════════════════
def cargar_warns(guild_id: int) -> dict:
    cfg = cargar_antinuke(guild_id)
    return cfg.get("warn_sistema", {})

def guardar_warns(guild_id: int, warns: dict):
    cfg = cargar_antinuke(guild_id)
    cfg["warn_sistema"] = warns
    guardar_antinuke(cfg, guild_id)

@bot.command(name="warn")
@commands.check(es_staff)
async def warn_cmd(ctx, member: discord.Member, *, razon: str = "Sin razón"):
    warns = cargar_warns(ctx.guild.id)
    uid = str(member.id)
    if uid not in warns:
        warns[uid] = []
    warns[uid].append({"razon": razon, "fecha": str(datetime.now(timezone.utc)), "mod": str(ctx.author.id)})
    guardar_warns(ctx.guild.id, warns)
    total = len(warns[uid])
    puntos_max = CONFIG.get("puntos_max", 7)
    embed = discord.Embed(
        title="⚠️ Advertencia",
        description=f"{member.mention} ha recibido una advertencia.\n**Razón:** {razon}\n**Total warns:** {total}/{puntos_max}",
        color=0xFFAA00
    )
    embed.set_footer(text=f"Moderado por {ctx.author}")
    await ctx.send(embed=embed)
    if total >= puntos_max:
        try:
            await ctx.guild.ban(member, reason=f"[Auto-ban] {puntos_max} warns alcanzados")
            await ctx.send(f"🔨 {member.mention} fue **baneado automáticamente** por alcanzar {puntos_max} warns.")
        except Exception as e:
            await ctx.send(f"❌ No pude banear a {member}: {e}")

@bot.command(name="warns")
@commands.check(es_staff)
async def warns_cmd(ctx, member: discord.Member):
    warns = cargar_warns(ctx.guild.id)
    uid = str(member.id)
    lista = warns.get(uid, [])
    if not lista:
        return await ctx.send(f"✅ {member.mention} no tiene advertencias.")
    puntos_max = CONFIG.get("puntos_max", 7)
    embed = discord.Embed(title=f"⚠️ Warns de {member}", color=0xFFAA00)
    for i, w in enumerate(lista, 1):
        embed.add_field(name=f"Warn #{i}", value=f"**Razón:** {w['razon']}\n**Fecha:** {w['fecha']}", inline=False)
    embed.set_footer(text=f"Total: {len(lista)}/{puntos_max}")
    await ctx.send(embed=embed)

@bot.command(name="clearwarns")
@commands.check(es_admin)
async def clearwarns_cmd(ctx, member: discord.Member):
    warns = cargar_warns(ctx.guild.id)
    uid = str(member.id)
    warns[uid] = []
    guardar_warns(ctx.guild.id, warns)
    await ctx.send(f"✅ Warns de {member.mention} **borrados**.")

# ══════════════════════════════════════════════════════════════
# 💥 NUKE — SOLO DUEÑO DEL BOT (BOT_OWNER_ID) O OWNER AN
# Con menú interactivo de botones
# ══════════════════════════════════════════════════════════════

def es_nuke_permitido(ctx) -> bool:
    """El dueño del bot puede usar nuke en cualquier servidor, sin importar permisos."""
    if es_bot_owner(ctx):
        return True
    # También permitir al owner del servidor o admin con permiso explícito en antinuke
    return es_owner_an(ctx)

# ── Vista de confirmación con botones ─────────────────────────
class NukeConfirmView(discord.ui.View):
    def __init__(self, accion_callback, timeout=30):
        super().__init__(timeout=timeout)
        self.accion_callback = accion_callback
        self.resultado = None

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.resultado = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.resultado = False
        self.stop()
        await interaction.response.send_message("❌ Operación cancelada.", ephemeral=True)

# ── Vista del menú principal de nuke ──────────────────────────
class NukeMenuView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("❌ Solo quien ejecutó el comando puede usar este menú.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🗑️ Borrar Canales", style=discord.ButtonStyle.danger, row=0)
    async def nuke_canales_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _ejecutar_nuke_canales(self.ctx)

    @discord.ui.button(label="🎭 Borrar Roles", style=discord.ButtonStyle.danger, row=0)
    async def nuke_roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _ejecutar_nuke_roles(self.ctx)

    @discord.ui.button(label="🔨 Banear Todos", style=discord.ButtonStyle.danger, row=1)
    async def nuke_bans_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _ejecutar_nuke_bans(self.ctx)

    @discord.ui.button(label="💥 NUKE COMPLETO", style=discord.ButtonStyle.danger, row=1)
    async def nuke_todo_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _ejecutar_nuke_todo(self.ctx)

    @discord.ui.button(label="❌ Cerrar", style=discord.ButtonStyle.secondary, row=2)
    async def cerrar_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.send_message("🚪 Menú cerrado.", ephemeral=True)

# ── Funciones internas de nuke ─────────────────────────────────
async def _ejecutar_nuke_canales(ctx):
    guild = ctx.guild
    msg = await ctx.send("🗑️ Borrando todos los canales...")
    eliminados = 0
    for channel in list(guild.channels):
        try:
            await channel.delete(reason=f"[NUKE] Ejecutado por {ctx.author}")
            eliminados += 1
            await asyncio.sleep(0.4)
        except Exception:
            pass
    log.warning(f"[NUKE] {ctx.author} borró {eliminados} canales en {guild.name}")

async def _ejecutar_nuke_roles(ctx):
    guild = ctx.guild
    await ctx.send("🎭 Borrando todos los roles...")
    eliminados = 0
    for role in list(guild.roles):
        if role.is_default() or role.managed or role >= guild.me.top_role:
            continue
        try:
            await role.delete(reason=f"[NUKE] Ejecutado por {ctx.author}")
            eliminados += 1
            await asyncio.sleep(0.4)
        except Exception:
            pass
    try:
        await ctx.send(f"🎭 {eliminados} roles eliminados.")
    except Exception:
        pass
    log.warning(f"[NUKE] {ctx.author} borró {eliminados} roles en {guild.name}")

async def _ejecutar_nuke_bans(ctx):
    guild = ctx.guild
    await ctx.send("🔨 Baneando todos los miembros...")
    baneados = 0
    for member in list(guild.members):
        if member.id == ctx.author.id or member.id == bot.user.id:
            continue
        if member.top_role >= guild.me.top_role:
            continue
        try:
            await member.ban(reason=f"[NUKE] Ejecutado por {ctx.author}", delete_message_days=0)
            baneados += 1
            await asyncio.sleep(0.4)
        except Exception:
            pass
    try:
        await ctx.send(f"🔨 {baneados} miembros baneados.")
    except Exception:
        pass
    log.warning(f"[NUKE] {ctx.author} baneó {baneados} miembros en {guild.name}")

async def _ejecutar_nuke_todo(ctx):
    guild = ctx.guild
    try:
        await ctx.send("💥 **INICIANDO NUKE COMPLETO...**")
    except Exception:
        pass
    await _ejecutar_nuke_bans(ctx)
    await _ejecutar_nuke_roles(ctx)
    await _ejecutar_nuke_canales(ctx)
    log.warning(f"[NUKE TOTAL] {ctx.author} ejecutó nuke completo en {guild.name}")

# ── Comandos de nuke ──────────────────────────────────────────
@bot.command(name="nuke")
@commands.check(es_nuke_permitido)
async def nuke_menu(ctx):
    """Muestra el menú interactivo de nuke."""
    embed = discord.Embed(
        title="💥 Panel de NUKE",
        description=(
            f"**Servidor:** {ctx.guild.name}\n"
            f"**Ejecutado por:** {ctx.author.mention}\n\n"
            "⚠️ Estas acciones son **irreversibles**. Selecciona una opción:"
        ),
        color=0xFF0000,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Solo el dueño del bot puede usar esto.")
    view = NukeMenuView(ctx)
    await ctx.send(embed=embed, view=view)

@bot.command(name="nuke_canales")
@commands.check(es_nuke_permitido)
async def nuke_canales_cmd(ctx):
    view = NukeConfirmView(None)
    msg = await ctx.send("⚠️ ¿Confirmar **eliminación de todos los canales**?", view=view)
    await view.wait()
    if view.resultado:
        await _ejecutar_nuke_canales(ctx)
    else:
        await msg.edit(content="❌ Cancelado.", view=None)

@bot.command(name="nuke_roles")
@commands.check(es_nuke_permitido)
async def nuke_roles_cmd(ctx):
    view = NukeConfirmView(None)
    msg = await ctx.send("⚠️ ¿Confirmar **eliminación de todos los roles**?", view=view)
    await view.wait()
    if view.resultado:
        await _ejecutar_nuke_roles(ctx)
    else:
        await msg.edit(content="❌ Cancelado.", view=None)

@bot.command(name="nuke_bans")
@commands.check(es_nuke_permitido)
async def nuke_bans_cmd(ctx):
    view = NukeConfirmView(None)
    msg = await ctx.send("⚠️ ¿Confirmar **banear a todos los miembros**?", view=view)
    await view.wait()
    if view.resultado:
        await _ejecutar_nuke_bans(ctx)
    else:
        await msg.edit(content="❌ Cancelado.", view=None)

@bot.command(name="nuke_todo")
@commands.check(es_nuke_permitido)
async def nuke_todo_cmd(ctx):
    view = NukeConfirmView(None)
    msg = await ctx.send("🚨 ¿Confirmar **NUKE COMPLETO** del servidor? Esto baneará miembros, borrará roles y canales.", view=view)
    await view.wait()
    if view.resultado:
        await _ejecutar_nuke_todo(ctx)
    else:
        await msg.edit(content="❌ Cancelado.", view=None)

# ══════════════════════════════════════════════════════════════
# COMANDOS DE MODERACIÓN GENERALES
# ══════════════════════════════════════════════════════════════

@bot.command(name="ban")
@commands.check(es_admin)
async def ban_cmd(ctx, member: discord.Member, *, razon: str = "Sin razón"):
    try:
        await ctx.guild.ban(member, reason=f"[Ban] {razon} — por {ctx.author}")
        embed = discord.Embed(
            title="🔨 Usuario Baneado",
            description=f"{member.mention} fue baneado.\n**Razón:** {razon}",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ No tengo permisos para banear a ese usuario.")

@bot.command(name="kick")
@commands.check(es_admin)
async def kick_cmd(ctx, member: discord.Member, *, razon: str = "Sin razón"):
    try:
        await member.kick(reason=f"[Kick] {razon} — por {ctx.author}")
        embed = discord.Embed(
            title="👢 Usuario Kickeado",
            description=f"{member.mention} fue expulsado.\n**Razón:** {razon}",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ No tengo permisos para kickear a ese usuario.")

@bot.command(name="unban")
@commands.check(es_admin)
async def unban_cmd(ctx, user_id: int):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        await ctx.send(f"✅ {user} fue **desbaneado**.")
    except discord.NotFound:
        await ctx.send("❌ Usuario no encontrado o no estaba baneado.")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="mute")
@commands.check(es_staff)
async def mute_cmd(ctx, member: discord.Member, minutos: int = 10, *, razon: str = "Sin razón"):
    try:
        import datetime as dt
        until = discord.utils.utcnow() + dt.timedelta(minutes=minutos)
        await member.timeout(until, reason=f"[Mute] {razon} — por {ctx.author}")
        embed = discord.Embed(
            title="🔇 Usuario Silenciado",
            description=f"{member.mention} fue silenciado por **{minutos} minutos**.\n**Razón:** {razon}",
            color=discord.Color.greyple()
        )
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("❌ No tengo permisos para silenciar a ese usuario.")

@bot.command(name="unmute")
@commands.check(es_staff)
async def unmute_cmd(ctx, member: discord.Member):
    try:
        await member.timeout(None, reason=f"[Unmute] por {ctx.author}")
        await ctx.send(f"✅ {member.mention} ya no está silenciado.")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="purge")
@commands.check(es_staff)
async def purge_cmd(ctx, cantidad: int = 10):
    if not 1 <= cantidad <= 100:
        return await ctx.send("❌ Entre 1 y 100 mensajes.")
    borrados = await ctx.channel.purge(limit=cantidad + 1)
    await ctx.send(f"🗑️ **{len(borrados)-1}** mensajes eliminados.", delete_after=5)

# ══════════════════════════════════════════════════════════════
# AYUDA GENERAL
# ══════════════════════════════════════════════════════════════

@bot.command(name="help")
async def help_cmd(ctx):
    p = PREFIX
    embed = discord.Embed(
        title="📖 Comandos del Bot",
        color=0x5865F2,
        description=f"Prefijo: `{p}`"
    )
    embed.add_field(name="🛡️ AntiNuke", value=f"`{p}an_ayuda` — Ver todos los comandos antinuke", inline=False)
    embed.add_field(name="⚠️ Moderación",
        value=(
            f"`{p}ban @user [razón]`\n"
            f"`{p}kick @user [razón]`\n"
            f"`{p}unban <id>`\n"
            f"`{p}mute @user [minutos] [razón]`\n"
            f"`{p}unmute @user`\n"
            f"`{p}purge <cantidad>`\n"
            f"`{p}warn @user <razón>`\n"
            f"`{p}warns @user`\n"
            f"`{p}clearwarns @user`"
        ), inline=False)
    if es_bot_owner(ctx):
        embed.add_field(name="💥 Nuke (Dueño del Bot)",
            value=(
                f"`{p}nuke` — Menú interactivo\n"
                f"`{p}nuke_canales` — Borrar canales\n"
                f"`{p}nuke_roles` — Borrar roles\n"
                f"`{p}nuke_bans` — Banear todos\n"
                f"`{p}nuke_todo` — Nuke completo"
            ), inline=False)
    embed.set_footer(text="Bot creado con discord.py")
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════
# ERRORES GLOBALES
# ══════════════════════════════════════════════════════════════

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ No tienes permiso para usar este comando.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Falta un argumento: `{error.param.name}`")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Usuario no encontrado.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Argumento inválido: {error}")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(f"Error en comando {ctx.command}: {error}")
        log.error(traceback.format_exc())

# ══════════════════════════════════════════════════════════════
# INICIO
# ══════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    log.info(f"✅ Bot conectado como {bot.user} (ID: {bot.user.id})")
    if BOT_OWNER_ID:
        log.info(f"👑 Owner del bot: ID {BOT_OWNER_ID}")
    else:
        log.warning("⚠️ No se configuró owner_id en config.json — los comandos nuke no estarán disponibles.")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=f"{PREFIX}help | AntiNuke")
    )

bot.run(TOKEN)

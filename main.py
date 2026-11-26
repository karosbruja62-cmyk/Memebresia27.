"""
Bot de Telegram para controlar membresías de un canal o grupo (v2).

- Registra solo a quien entra al canal (30 días por defecto).
- Envía recordatorios antes de vencer (una sola vez cada uno).
- Expulsa automáticamente al vencer (sin bloquear: puede volver si renueva).
- Usa Postgres si existe DATABASE_URL (Railway); si no, usa SQLite local.
- Varios canales: cada canal tiene un #alias; membresías, llaves y enlaces son por canal.
- Llaves de un solo uso (/llave, /canjear): el admin crea una llave con la
  duración que quiera; al canjearla empieza a correr el tiempo y al vencer
  se expulsa automáticamente. La llave queda inutilizable.

Variables de entorno:
    BOT_TOKEN         token de @BotFather                      (obligatoria)
    GROUP_ID          canal inicial (alias #principal)         (opcional)
                      Los demás canales se registran solos al agregar el bot como admin.
    ADMIN_IDS         tus IDs separados por coma               (obligatoria)
    DATABASE_URL      cadena de Postgres (Railway la da)       (opcional)
    DIAS_MEMBRESIA    días por defecto                         (def. 30)
    REMINDER_DAYS     recordatorios, días antes de vencer      (def. 5,2,1)
    REVISION_SEGUNDOS cada cuánto revisa vencimientos          (def. 60)
    TZ_OFFSET         diferencia horaria para mostrar fechas   (def. -5)
    AUTO_REGISTRO     1 registra a quien entra, 0 no           (def. 1)
    TELEGRAM_API_PROXY URL de un proxy hacia api.telegram.org  (opcional)
                      (usa 0 si vendes el acceso solo con llaves)
"""

import html
import logging
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO
)
# Evita que el token aparezca en los logs (httpx imprime la URL completa)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("membresias")

# ---------- Configuración ----------
TOKEN = os.environ["BOT_TOKEN"]
_g = os.environ.get("GROUP_ID", "").strip()
GROUP_ID = int(_g) if _g else None  # opcional: canal inicial (alias "principal")
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "membresias_v2.db")
DIAS_MEMBRESIA = int(os.environ.get("DIAS_MEMBRESIA", "30"))
REMINDER_DAYS = sorted(
    {int(x) for x in os.environ.get("REMINDER_DAYS", "5,2,1").split(",") if x.strip()},
    reverse=True,
)
REVISION_SEGUNDOS = int(os.environ.get("REVISION_SEGUNDOS", "60"))
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "-5"))))
AUTO_REGISTRO = os.environ.get("AUTO_REGISTRO", "1") == "1"
# Si el hosting no puede llegar a api.telegram.org, pasa por tu proxy (ej. Cloudflare Worker)
TELEGRAM_API_PROXY = os.environ.get("TELEGRAM_API_PROXY", "").strip().rstrip("/")
ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O/1/I para no confundir
UNIDADES = {"m": 60, "h": 3600, "d": 86400}
VIGENCIA_ENLACE = 600  # el enlace de invitación vence en 10 minutos

# ---------- Tiempo ----------
FMT = "%Y-%m-%dT%H:%M:%S"


def ahora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def dias_restantes(vence_iso):
    seg = (parse(vence_iso) - ahora()).total_seconds()
    return max(0, math.ceil(seg / 86400))


def fecha_local(vence_iso):
    return parse(vence_iso).astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")


def fmt_seg(seg):
    """Segundos a texto corto: 2d 3h 10m."""
    seg = max(int(seg), 0)
    d, r = divmod(seg, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    partes = []
    if d:
        partes.append(f"{d}d")
    if h:
        partes.append(f"{h}h")
    if m or not partes:
        partes.append(f"{m}m")
    return " ".join(partes)


def tiempo_restante(vence_iso):
    return fmt_seg((parse(vence_iso) - ahora()).total_seconds())


# ---------- Base de datos (Postgres o SQLite) ----------
def run(sql, params=(), fetch=None):
    """Ejecuta SQL escrito con '?'. fetch: None, 'one' o 'all'."""
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql.replace("?", "%s"), params)
                    if fetch == "all":
                        return [dict(r) for r in cur.fetchall()]
                    if fetch == "one":
                        r = cur.fetchone()
                        return dict(r) if r else None
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                cur = conn.execute(sql, params)
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                if fetch == "one":
                    r = cur.fetchone()
                    return dict(r) if r else None
        finally:
            conn.close()


def init_db():
    run(
        """
        CREATE TABLE IF NOT EXISTS miembros (
            user_id BIGINT PRIMARY KEY,
            nombre TEXT,
            ultimo_pago TEXT,
            vence TEXT NOT NULL,
            recordatorios TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activo'
        )
        """
    )
    for col in ("renovaciones INTEGER NOT NULL DEFAULT 0", "expulsado_en TEXT"):
        try:  # migración simple: ignora si la columna ya existe
            run(f"ALTER TABLE miembros ADD COLUMN {col}")
        except Exception:
            pass
    run(
        """
        CREATE TABLE IF NOT EXISTS llaves (
            codigo TEXT PRIMARY KEY,
            segundos BIGINT NOT NULL,
            creada TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'disponible',
            usada_por BIGINT,
            usada_en TEXT
        )
        """
    )
    try:  # migración simple: cada llave pertenece a un canal
        run("ALTER TABLE llaves ADD COLUMN chat_id BIGINT")
    except Exception:
        pass
    run(
        """
        CREATE TABLE IF NOT EXISTS canales (
            chat_id BIGINT PRIMARY KEY,
            alias TEXT UNIQUE NOT NULL,
            titulo TEXT,
            activo INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    run(
        """
        CREATE TABLE IF NOT EXISTS membresias (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            nombre TEXT,
            ultimo_pago TEXT,
            vence TEXT NOT NULL,
            recordatorios TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activo',
            renovaciones INTEGER NOT NULL DEFAULT 0,
            expulsado_en TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
        """
    )
    run(
        """
        CREATE TABLE IF NOT EXISTS pagos (
            id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            nombre TEXT,
            chat_id BIGINT NOT NULL,
            creado TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            resuelto_por BIGINT,
            resuelto_en TEXT,
            codigo TEXT
        )
        """
    )
    if GROUP_ID is not None and not run(
        "SELECT 1 AS x FROM canales WHERE chat_id = ?", (GROUP_ID,), "one"
    ):
        # Primera vez con varios canales: el canal de GROUP_ID hereda a los miembros actuales
        run(
            "INSERT INTO canales (chat_id, alias, activo) VALUES (?, 'principal', 1)",
            (GROUP_ID,),
        )
        try:
            run(
                "INSERT INTO membresias (user_id, chat_id, nombre, ultimo_pago, vence, "
                "recordatorios, estado, renovaciones, expulsado_en) "
                "SELECT m.user_id, ?, m.nombre, m.ultimo_pago, m.vence, m.recordatorios, "
                "m.estado, m.renovaciones, m.expulsado_en FROM miembros m WHERE 1=1 "
                "ON CONFLICT (user_id, chat_id) DO NOTHING",
                (GROUP_ID,),
            )
        except Exception:
            log.exception("No pude migrar los miembros anteriores")
    log.info("Base de datos: %s", "Postgres" if DATABASE_URL else f"SQLite ({DB_PATH})")



def obtener(uid, chat_id):
    return run(
        "SELECT * FROM membresias WHERE user_id = ? AND chat_id = ?", (uid, chat_id), "one"
    )


def registrar_td(uid, chat_id, nombre, delta, sumar=True):
    """Crea o renueva en UN canal. Si sumar=True y sigue vigente, suma desde su vencimiento."""
    ahora_ = ahora()
    fila = obtener(uid, chat_id)
    base = ahora_
    if sumar and fila and fila["estado"] == "activo" and parse(fila["vence"]) > base:
        base = parse(fila["vence"])
    vence = base + delta
    run(
        """
        INSERT INTO membresias (user_id, chat_id, nombre, ultimo_pago, vence, recordatorios, estado)
        VALUES (?, ?, ?, ?, ?, '', 'activo')
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            nombre = COALESCE(EXCLUDED.nombre, membresias.nombre),
            ultimo_pago = EXCLUDED.ultimo_pago,
            vence = EXCLUDED.vence,
            recordatorios = '',
            estado = 'activo',
            renovaciones = membresias.renovaciones + ?,
            expulsado_en = NULL
        """,
        (uid, chat_id, nombre, iso(ahora_), iso(vence), 1 if fila else 0),
    )
    return iso(vence)


def registrar(uid, chat_id, nombre, dias, sumar=True):
    return registrar_td(uid, chat_id, nombre, timedelta(days=dias), sumar)


# ---------- Canales ----------
def canales_activos():
    return run("SELECT * FROM canales WHERE activo = 1 ORDER BY alias", (), "all")


def alias_unico(titulo):
    base = re.sub(r"[^a-z0-9]", "", (titulo or "canal").lower())[:12] or "canal"
    alias, n = base, 1
    while run("SELECT 1 AS x FROM canales WHERE alias = ?", (alias,), "one"):
        n += 1
        alias = f"{base}{n}"
    return alias


def registrar_canal(chat_id, titulo, alias=None):
    """Alta o reactivación de un canal. Devuelve su alias."""
    f = run("SELECT alias FROM canales WHERE chat_id = ?", (chat_id,), "one")
    if f:
        run(
            "UPDATE canales SET titulo = ?, activo = 1 WHERE chat_id = ?", (titulo, chat_id)
        )
        return f["alias"]
    alias = alias or alias_unico(titulo)
    run(
        "INSERT INTO canales (chat_id, alias, titulo, activo) VALUES (?, ?, ?, 1)",
        (chat_id, alias, titulo),
    )
    return alias


def canal_activo(chat_id):
    return run(
        "SELECT * FROM canales WHERE chat_id = ? AND activo = 1", (chat_id,), "one"
    )


def etiqueta(f):
    """Nombre legible del canal de una fila que trae titulo/alias."""
    return f.get("titulo") or f.get("alias") or str(f.get("chat_id"))


def separar_alias(args):
    """Saca el primer '#alias' de los argumentos. Devuelve (alias, resto)."""
    alias, resto = None, []
    for a in args or []:
        if a.startswith("#") and len(a) > 1 and alias is None:
            alias = a[1:].lower()
        else:
            resto.append(a)
    return alias, resto


def elegir_canal(alias):
    """Devuelve (canal, error). Con un solo canal activo no hace falta #alias."""
    activos = canales_activos()
    if alias:
        for c in activos:
            if c["alias"] == alias:
                return c, None
        return None, f"No existe el canal #{alias}.\n" + texto_canales(activos)
    if len(activos) == 1:
        return activos[0], None
    if not activos:
        return None, "No hay canales registrados. Agrega el bot como administrador de un canal."
    return None, "Hay varios canales, indica cuál con #alias:\n" + texto_canales(activos)


def texto_canales(activos=None):
    activos = canales_activos() if activos is None else activos
    return "\n".join(f"#{c['alias']} - {c['titulo'] or c['chat_id']}" for c in activos) or "—"


# ---------- Utilidades ----------
def solo_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            await update.message.reply_text("No tienes permiso para usar este comando.")
            return
        return await func(update, context)

    return wrapper


async def avisar_admins(context, texto):
    for adm in ADMIN_IDS:
        try:
            await context.bot.send_message(adm, texto)
        except TelegramError as e:
            log.warning("No pude avisar al admin %s: %s", adm, e)



async def sacar_del_canal(context, chat_id, uid):
    """Expulsa sin bloquear. Devuelve True si funcionó."""
    try:
        await context.bot.ban_chat_member(chat_id, uid)
        await context.bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        return True
    except TelegramError as e:
        log.error("No pude expulsar a %s de %s: %s", uid, chat_id, e)
        return False


def uid_arg(args):
    if args and args[0].isdigit():
        return int(args[0])
    return None


def parse_duracion(txt):
    """'30d', '12h', '90m' -> segundos (o None si no es válido)."""
    m = re.fullmatch(r"(\d+)([mhd])", txt.strip().lower())
    if not m or int(m.group(1)) <= 0:
        return None
    return int(m.group(1)) * UNIDADES[m.group(2)]


def nuevo_codigo():
    return "-".join(
        "".join(secrets.choice(ALFABETO) for _ in range(4)) for _ in range(3)
    )


async def crear_enlace(context, chat_id):
    """Enlace de un solo uso y válido solo para ESE canal."""
    link = await context.bot.create_chat_invite_link(
        chat_id,
        member_limit=1,
        expire_date=int(ahora().timestamp()) + VIGENCIA_ENLACE,
    )
    return link.invite_link


async def ya_esta_en_canal(context, uid, chat_id):
    try:
        m = await context.bot.get_chat_member(chat_id, uid)
        return m.status in ("member", "administrator", "creator")
    except TelegramError:
        return False


# ---------- Comandos ----------
# Se muestran como menú con su descripción al escribir "/" en el chat.
COMANDOS_USUARIO = [
    ("start", "Cómo funciona el bot"),
    ("tiempo", "Ver cuánto te queda en cada canal"),
    ("canjear", "/canjear LLAVE: activa tu llave y recibe tu enlace"),
    ("enlace", "Pedir un nuevo enlace de entrada a tu canal"),
    ("pago", "Enviar la captura de tu pago para pedir tu llave"),
    ("miid", "Ver tu ID de Telegram"),
]
COMANDOS_ADMIN = COMANDOS_USUARIO + [
    ("ayuda", "Lista de comandos de administrador"),
    ("canales", "Ver los canales que administra el bot (#alias)"),
    ("addcanal", "/addcanal CHAT_ID [alias]: registrar un canal a mano"),
    ("quitarcanal", "/quitarcanal #alias: dejar de ofrecer un canal"),
    ("llave", "/llave 30d [#alias]: crear una llave para un canal"),
    ("llaves", "Ver llaves disponibles y usadas, con su canal"),
    ("revocar", "/revocar CODIGO: anular una llave sin usar"),
    ("pago", "/pago ID [Nombre] [#alias]: renovar (sin datos: flujo de usuario)"),
    ("agregar", "/agregar ID DIAS [Nombre] [#alias]: registrar o sumar días"),
    ("estado", "/estado ID [#alias]: días restantes de un usuario"),
    ("lista", "[#alias]: miembros activos"),
    ("usuarios", "[#alias]: activos y salidos, con tiempo y renovaciones"),
    ("vencer", "/vencer ID [#alias]: hacer vencer ahora (pruebas)"),
    ("quitar", "/quitar ID [#alias]: eliminar y expulsar de un canal"),
]


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands(
            [BotCommand(c, d[:256]) for c, d in COMANDOS_USUARIO],
            scope=BotCommandScopeDefault(),
        )
        for adm in ADMIN_IDS:
            await app.bot.set_my_commands(
                [BotCommand(c, d[:256]) for c, d in COMANDOS_ADMIN],
                scope=BotCommandScopeChat(adm),
            )
    except TelegramError as e:
        log.warning("No pude publicar el menú de comandos: %s", e)
    # Completa el título de canales sembrados desde GROUP_ID
    for c in run("SELECT chat_id FROM canales WHERE titulo IS NULL", (), "all"):
        try:
            ch = await app.bot.get_chat(c["chat_id"])
            run("UPDATE canales SET titulo = ? WHERE chat_id = ?", (ch.title, c["chat_id"]))
        except TelegramError as e:
            log.warning("No pude leer el canal %s: %s", c["chat_id"], e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola. Este bot controla las membresías de los canales.\n"
        "Si tienes una llave, actívala con: /canjear TU-LLAVE (cada llave es para un solo canal)\n"
        "Para ver cuánto tiempo te queda: /tiempo\n"
        "Si tu enlace venció y tu membresía sigue activa: /enlace\n"
        "Usa /miid para ver tu ID y compartirlo con el administrador."
    )


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


@solo_admin
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos de administrador:\n"
        + "\n".join(f"/{c} - {d}" for c, d in COMANDOS_ADMIN)
        + "\n\nCon varios canales añade #alias al comando (mira /canales). "
        "Con un solo canal no hace falta."
    )


# ---------- Administración de canales ----------
@solo_admin
async def canales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = texto_canales()
    await update.message.reply_text(
        f"Canales activos:\n{txt}\n\nPara agregar uno, añade el bot como administrador "
        "del canal (con permiso de invitar) y se registra solo."
    )


@solo_admin
async def addcanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if not a or not re.fullmatch(r"-?\d+", a[0]):
        await update.message.reply_text("Uso: /addcanal CHAT_ID [alias]")
        return
    chat_id = int(a[0])
    alias = re.sub(r"[^a-z0-9]", "", a[1].lower())[:12] if len(a) > 1 else None
    if alias and run("SELECT 1 AS x FROM canales WHERE alias = ? AND chat_id <> ?", (alias, chat_id), "one"):
        await update.message.reply_text("Ese alias ya está en uso.")
        return
    try:
        ch = await context.bot.get_chat(chat_id)
    except TelegramError as e:
        await update.message.reply_text(f"No pude acceder a ese canal: {e}")
        return
    alias = registrar_canal(chat_id, ch.title, alias or None)
    await update.message.reply_text(f"Canal registrado: #{alias} - {ch.title}")


@solo_admin
async def quitarcanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, _ = separar_alias(context.args)
    if not alias:
        await update.message.reply_text("Uso: /quitarcanal #alias")
        return
    c, err = elegir_canal(alias)
    if err:
        await update.message.reply_text(err)
        return
    run("UPDATE canales SET activo = 0 WHERE chat_id = ?", (c["chat_id"],))
    await update.message.reply_text(
        f"#{alias} ya no se ofrece. Las membresías existentes no se tocan "
        "y no se revisan mientras el canal esté desactivado."
    )


async def canal_cambio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """El bot fue agregado o quitado como administrador de un canal o grupo."""
    m = update.my_chat_member
    if m is None or m.chat.type == "private":
        return
    estado_nuevo = m.new_chat_member.status
    if estado_nuevo == "administrator":
        alias = registrar_canal(m.chat.id, m.chat.title)
        await avisar_admins(context, f"Canal registrado: #{alias} - {m.chat.title} ({m.chat.id}).")
    elif estado_nuevo in ("left", "kicked"):
        run("UPDATE canales SET activo = 0 WHERE chat_id = ?", (m.chat.id,))
        await avisar_admins(context, f"El bot salió de {m.chat.title}. Canal desactivado.")


# ---------- Membresías (admin) ----------
@solo_admin
async def agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, a = separar_alias(context.args)
    if len(a) < 2 or not a[0].isdigit() or not a[1].isdigit():
        await update.message.reply_text("Uso: /agregar ID DIAS [Nombre] [#alias]")
        return
    canal, err = elegir_canal(alias)
    if err:
        await update.message.reply_text(err)
        return
    uid, dias = int(a[0]), int(a[1])
    nombre = " ".join(a[2:]) or None
    vence = registrar(uid, canal["chat_id"], nombre, dias)
    log.info("Membresía registrada: %s (%s) +%s días en %s", nombre, uid, dias, canal["alias"])
    await update.message.reply_text(
        f"Membresía de {nombre or uid} en #{canal['alias']} activa hasta {fecha_local(vence)}."
    )


async def pago_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, a = separar_alias(context.args)
    uid = uid_arg(a)
    if uid is None:
        await update.message.reply_text("Uso: /pago ID [Nombre] [#alias]")
        return
    canal, err = elegir_canal(alias)
    if err:
        await update.message.reply_text(err)
        return
    nombre = " ".join(a[1:]) or None
    vence = registrar(uid, canal["chat_id"], nombre, DIAS_MEMBRESIA)
    log.info("Pago registrado: %s en %s (+%s días)", uid, canal["alias"], DIAS_MEMBRESIA)
    await update.message.reply_text(
        f"Pago registrado en #{canal['alias']}. Activa hasta {fecha_local(vence)} "
        f"({DIAS_MEMBRESIA} días)."
    )



async def pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin con argumentos: renueva a mano. Cualquier otro caso: flujo de pago con captura."""
    user = update.effective_user
    if user.id in ADMIN_IDS and context.args:
        return await pago_admin(update, context)
    if update.effective_chat.type != "private":
        await update.message.reply_text("Escríbeme por privado para enviar tu pago.")
        return
    activos = canales_activos()
    if not activos:
        await update.message.reply_text("No hay canales disponibles por ahora.")
        return
    context.user_data.pop("pago_canal", None)
    if len(activos) == 1:
        context.user_data["pago_canal"] = activos[0]["chat_id"]
        await update.message.reply_text(
            f"Envíame ahora la captura de tu pago para {etiqueta(activos[0])}."
        )
        return
    kb = [
        [InlineKeyboardButton(etiqueta(c), callback_data=f"pc:{c['chat_id']}")]
        for c in activos
    ]
    await update.message.reply_text(
        "¿A qué canal quieres entrar?", reply_markup=InlineKeyboardMarkup(kb)
    )


async def pago_canal_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    canal = canal_activo(int(q.data.split(":")[1]))
    if not canal:
        await q.edit_message_text("Ese canal ya no está disponible.")
        return
    context.user_data["pago_canal"] = canal["chat_id"]
    await q.edit_message_text(
        f"Canal elegido: {etiqueta(canal)}.\nEnvíame ahora la captura de tu pago."
    )


async def recibir_captura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    if msg.document and not (msg.document.mime_type or "").startswith("image/"):
        return
    chat_id = context.user_data.get("pago_canal")
    if chat_id is None:
        await msg.reply_text("Para enviar un pago escribe primero /pago.")
        return
    canal = canal_activo(chat_id)
    if not canal:
        context.user_data.pop("pago_canal", None)
        await msg.reply_text("Ese canal ya no está disponible. Escribe /pago de nuevo.")
        return
    pid = secrets.token_hex(4)
    run(
        "INSERT INTO pagos (id, user_id, nombre, chat_id, creado) VALUES (?, ?, ?, ?, ?)",
        (pid, user.id, user.full_name, chat_id, iso(ahora())),
    )
    context.user_data.pop("pago_canal", None)
    caption = (
        f"Pago por verificar\n{html.escape(user.full_name)} "
        f"(<code>{user.id}</code>) pagó para {html.escape(etiqueta(canal))}."
    )
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Aceptar", callback_data=f"pg:a:{pid}"),
            InlineKeyboardButton("Rechazar", callback_data=f"pg:r:{pid}"),
        ]]
    )
    enviado = False
    for adm in ADMIN_IDS:
        try:
            await context.bot.copy_message(
                adm, msg.chat_id, msg.message_id,
                caption=caption, parse_mode="HTML", reply_markup=kb,
            )
            enviado = True
        except TelegramError as e:
            log.warning("No pude enviar el pago al admin %s: %s", adm, e)
    if not enviado:
        run("DELETE FROM pagos WHERE id = ?", (pid,))
        await msg.reply_text("No pude avisar al administrador. Intenta de nuevo más tarde.")
        return
    log.info("Pago %s recibido de %s para %s", pid, user.id, canal["alias"])
    await msg.reply_text(
        "Recibí tu captura. El administrador la revisará y te enviaré tu llave si es aprobada."
    )


async def pago_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Sin permiso.", show_alert=True)
        return
    _, accion, pid = q.data.split(":")
    nuevo = "aceptado" if accion == "a" else "rechazado"
    # Un solo paso: si dos admins pulsan a la vez, solo uno gana
    p = run(
        "UPDATE pagos SET estado = ?, resuelto_por = ?, resuelto_en = ? "
        "WHERE id = ? AND estado = 'pendiente' RETURNING user_id, nombre, chat_id",
        (nuevo, q.from_user.id, iso(ahora()), pid),
        "one",
    )
    if not p:
        await q.answer("Este pago ya fue resuelto.", show_alert=True)
        return
    await q.answer()
    canal = run("SELECT * FROM canales WHERE chat_id = ?", (p["chat_id"],), "one") or {
        "chat_id": p["chat_id"]
    }
    base = f"{p['nombre']} ({p['user_id']}) - {etiqueta(canal)}"

    if accion == "r":
        try:
            await context.bot.send_message(
                p["user_id"],
                "Tu pago fue rechazado. Si crees que es un error, contacta al administrador "
                "y envía otra captura con /pago.",
            )
        except TelegramError as e:
            log.warning("No pude avisar del rechazo a %s: %s", p["user_id"], e)
        await q.edit_message_caption(f"Rechazado: {base}")
        return

    codigo = nuevo_codigo()
    seg = DIAS_MEMBRESIA * 86400
    run(
        "INSERT INTO llaves (codigo, segundos, creada, chat_id) VALUES (?, ?, ?, ?)",
        (codigo, seg, iso(ahora()), p["chat_id"]),
    )
    run("UPDATE pagos SET codigo = ? WHERE id = ?", (codigo, pid))
    log.info("Pago %s aceptado, llave %s", pid, codigo)
    try:
        await context.bot.send_message(
            p["user_id"],
            f"Tu pago fue aceptado. Tu llave para {etiqueta(canal)}:\n"
            f"<code>{codigo}</code>\n\nCanjéala con: /canjear {codigo}",
            parse_mode="HTML",
        )
        entregada = "llave enviada al usuario"
    except TelegramError as e:
        log.warning("No pude enviar la llave a %s: %s", p["user_id"], e)
        entregada = f"no pude escribirle; entrégale tú la llave {codigo}"
    await q.edit_message_caption(f"Aceptado: {base} ({entregada}).")


SQL_MEMB = (
    "SELECT m.*, c.titulo, c.alias FROM membresias m "
    "LEFT JOIN canales c ON c.chat_id = m.chat_id "
)


@solo_admin
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, a = separar_alias(context.args)
    uid = uid_arg(a)
    if uid is None:
        await update.message.reply_text("Uso: /estado ID [#alias]")
        return
    filas = run(SQL_MEMB + "WHERE m.user_id = ? ORDER BY c.alias", (uid,), "all")
    if alias:
        filas = [f for f in filas if f["alias"] == alias]
    if not filas:
        await update.message.reply_text("Ese usuario no está registrado.")
        return
    lineas = []
    for f in filas:
        if f["estado"] != "activo":
            lineas.append(f"{etiqueta(f)}: expulsado por vencimiento.")
        else:
            lineas.append(
                f"{etiqueta(f)}: quedan {tiempo_restante(f['vence'])} "
                f"(vence {fecha_local(f['vence'])})."
            )
    await update.message.reply_text(f"{filas[0]['nombre'] or uid}\n" + "\n".join(lineas))


async def tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filas = run(
        SQL_MEMB + "WHERE m.user_id = ? ORDER BY c.alias", (update.effective_user.id,), "all"
    )
    if not filas:
        await update.message.reply_text(
            "No tienes una membresía registrada. Para obtenerla usa /canjear TU-LLAVE."
        )
        return
    lineas = []
    for f in filas:
        if f["estado"] != "activo" or parse(f["vence"]) <= ahora():
            lineas.append(f"{etiqueta(f)}: venció. Renueva con /canjear TU-LLAVE.")
        else:
            lineas.append(
                f"{etiqueta(f)}: te quedan {tiempo_restante(f['vence'])} "
                f"(vence {fecha_local(f['vence'])})."
            )
    await update.message.reply_text("\n".join(lineas))


async def enviar_bloques(update, lineas):
    bloque = ""
    for linea in lineas:
        if len(bloque) + len(linea) > 3500:
            await update.message.reply_text(bloque)
            bloque = ""
        bloque += linea + "\n"
    if bloque:
        await update.message.reply_text(bloque)


@solo_admin
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, _ = separar_alias(context.args)
    filas = run(SQL_MEMB + "WHERE m.estado = 'activo' ORDER BY c.alias, m.vence", (), "all")
    if alias:
        filas = [f for f in filas if f["alias"] == alias]
    if not filas:
        await update.message.reply_text("No hay membresías activas.")
        return
    lineas = [f"Activos: {len(filas)}"] + [
        f"[#{f['alias']}] {f['nombre'] or '-'} ({f['user_id']}): {tiempo_restante(f['vence'])}"
        for f in filas
    ]
    await enviar_bloques(update, lineas)


@solo_admin
async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, _ = separar_alias(context.args)
    filas = run(SQL_MEMB + "ORDER BY c.alias, m.vence", (), "all")
    if alias:
        filas = [f for f in filas if f["alias"] == alias]
    if not filas:
        await update.message.reply_text("No hay usuarios registrados.")
        return
    activos = [f for f in filas if f["estado"] == "activo"]
    salidos = [f for f in filas if f["estado"] != "activo"]
    lineas = [f"ACTIVOS ({len(activos)})"]
    for f in activos:
        lineas.append(
            f"[#{f['alias']}] {f['nombre'] or '-'} ({f['user_id']}): quedan "
            f"{tiempo_restante(f['vence'])}, vence {fecha_local(f['vence'])}, "
            f"renovaciones: {f['renovaciones']}"
        )
    lineas.append("")
    lineas.append(f"SALIERON POR VENCIMIENTO ({len(salidos)})")
    for f in salidos:
        cuando = f" el {fecha_local(f['expulsado_en'])}" if f.get("expulsado_en") else ""
        lineas.append(
            f"[#{f['alias']}] {f['nombre'] or '-'} ({f['user_id']}): venció "
            f"{fecha_local(f['vence'])}, expulsado{cuando}, renovaciones: {f['renovaciones']}"
        )
    await enviar_bloques(update, lineas)


@solo_admin
async def vencer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, a = separar_alias(context.args)
    uid = uid_arg(a)
    if uid is None:
        await update.message.reply_text("Uso: /vencer ID [#alias]")
        return
    canal, err = elegir_canal(alias)
    if err:
        await update.message.reply_text(err)
        return
    if not obtener(uid, canal["chat_id"]):
        await update.message.reply_text("Ese usuario no está registrado en ese canal.")
        return
    run(
        "UPDATE membresias SET vence = ?, estado = 'activo' WHERE user_id = ? AND chat_id = ?",
        (iso(ahora() - timedelta(minutes=1)), uid, canal["chat_id"]),
    )
    await update.message.reply_text(
        f"Marcado como vencido en #{canal['alias']}. "
        f"Se expulsará en menos de {REVISION_SEGUNDOS} segundos."
    )


@solo_admin
async def quitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, a = separar_alias(context.args)
    uid = uid_arg(a)
    if uid is None:
        await update.message.reply_text("Uso: /quitar ID [#alias]")
        return
    canal, err = elegir_canal(alias)
    if err:
        await update.message.reply_text(err)
        return
    run(
        "DELETE FROM membresias WHERE user_id = ? AND chat_id = ?", (uid, canal["chat_id"])
    )
    ok = await sacar_del_canal(context, canal["chat_id"], uid)
    await update.message.reply_text(
        f"{uid} eliminado y expulsado de #{canal['alias']}."
        if ok
        else f"{uid} eliminado de la base, pero no pude expulsarlo de #{canal['alias']}."
    )


# ---------- Llaves de un solo uso (cada una vale para UN canal) ----------
def crear_llave(seg, canal):
    codigo = nuevo_codigo()
    run(
        "INSERT INTO llaves (codigo, segundos, creada, chat_id) VALUES (?, ?, ?, ?)",
        (codigo, seg, iso(ahora()), canal["chat_id"]),
    )
    log.info("Llave creada: %s (%s) para %s", codigo, fmt_seg(seg), canal["alias"])
    return (
        f"Llave creada para #{canal['alias']} ({canal['titulo'] or canal['chat_id']}):\n"
        f"<code>{codigo}</code>\n"
        f"Duración: {fmt_seg(seg)} (empieza a contar cuando el usuario la canjee). "
        "Solo abre ese canal."
    )


@solo_admin
async def llave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Crea las llaves por privado con el bot.")
        return
    alias, a = separar_alias(context.args)
    seg = parse_duracion(a[0]) if a else None
    if not seg:
        await update.message.reply_text(
            "Uso: /llave 30d [#alias]\n(m = minutos, h = horas, d = días)"
        )
        return
    if not alias and len(canales_activos()) > 1:
        kb = [
            [InlineKeyboardButton(c["titulo"] or c["alias"], callback_data=f"llave:{seg}:{c['chat_id']}")]
            for c in canales_activos()
        ]
        await update.message.reply_text(
            f"¿Para qué canal es la llave de {fmt_seg(seg)}?",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return
    canal, err = elegir_canal(alias)
    if err:
        await update.message.reply_text(err)
        return
    await update.message.reply_html(crear_llave(seg, canal))


async def llave_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Sin permiso.", show_alert=True)
        return
    _, seg, chat_id = q.data.split(":")
    canal = canal_activo(int(chat_id))
    await q.answer()
    if not canal:
        await q.edit_message_text("Ese canal ya no está activo.")
        return
    await q.edit_message_text(crear_llave(int(seg), canal), parse_mode="HTML")


@solo_admin
async def llaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    etq = {c["chat_id"]: c["alias"] for c in run("SELECT * FROM canales", (), "all")}

    def canal_de(f):
        return etq.get(f["chat_id"] or GROUP_ID, "?")

    disp = run(
        "SELECT codigo, segundos, chat_id FROM llaves WHERE estado = 'disponible' ORDER BY creada",
        (),
        "all",
    )
    usadas = run(
        "SELECT codigo, usada_por, usada_en, chat_id FROM llaves "
        "WHERE estado = 'usada' ORDER BY usada_en DESC LIMIT 10",
        (),
        "all",
    )
    txt = f"<b>Disponibles ({len(disp)})</b>\n"
    txt += (
        "\n".join(
            f"<code>{f['codigo']}</code> · {fmt_seg(f['segundos'])} · #{canal_de(f)}"
            for f in disp[:30]
        )
        or "—"
    )
    if len(disp) > 30:
        txt += f"\n…y {len(disp) - 30} más"
    txt += "\n\n<b>Usadas (últimas 10)</b>\n"
    txt += (
        "\n".join(
            f"<code>{f['codigo']}</code> · #{canal_de(f)} · {f['usada_por']} · "
            f"{fecha_local(f['usada_en'])}"
            for f in usadas
        )
        or "—"
    )
    await update.message.reply_html(txt)


@solo_admin
async def revocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /revocar CODIGO")
        return
    codigo = context.args[0].strip().upper()
    fila = run(
        "UPDATE llaves SET estado = 'revocada' "
        "WHERE codigo = ? AND estado = 'disponible' RETURNING codigo",
        (codigo,),
        "one",
    )
    await update.message.reply_text(
        "Llave revocada." if fila else "No existe o ya fue usada."
    )


async def canjear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Escríbeme por privado para canjear tu llave.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /canjear TU-LLAVE")
        return

    user = update.effective_user
    codigo = context.args[0].strip().upper()

    # Marca la llave como usada en un solo paso: si dos personas la canjean a la
    # vez, solo una recibe la fila de vuelta.
    fila = run(
        "UPDATE llaves SET estado = 'usada', usada_por = ?, usada_en = ? "
        "WHERE codigo = ? AND estado = 'disponible' RETURNING segundos, chat_id",
        (user.id, iso(ahora()), codigo),
        "one",
    )
    if not fila:
        await update.message.reply_text("Llave inválida o ya utilizada.")
        return

    def devolver_llave():
        run(
            "UPDATE llaves SET estado = 'disponible', usada_por = NULL, usada_en = NULL "
            "WHERE codigo = ?",
            (codigo,),
        )

    segundos = int(fila["segundos"])
    chat_id = fila["chat_id"] or GROUP_ID  # llaves antiguas sin canal: el canal original
    canal = canal_activo(chat_id) if chat_id is not None else None
    if not canal:
        devolver_llave()
        await update.message.reply_text(
            "El canal de esta llave no está disponible. Avisa al administrador."
        )
        return
    chat_id = canal["chat_id"]
    nombre_canal = canal["titulo"] or canal["alias"]

    try:
        vence = registrar_td(user.id, chat_id, user.full_name, timedelta(seconds=segundos))
    except Exception:
        log.exception("Error registrando la membresía de la llave %s", codigo)
        devolver_llave()
        await update.message.reply_text(
            "Hubo un error. Tu llave sigue vigente, intenta de nuevo en un momento."
        )
        return

    log.info("Llave %s canjeada por %s (%s) en %s", codigo, user.full_name, user.id, canal["alias"])
    await avisar_admins(
        context,
        f"Llave {codigo} canjeada por {user.full_name} ({user.id}) en #{canal['alias']}. "
        f"Vence {fecha_local(vence)}.",
    )

    texto = (
        f"Llave activada para {nombre_canal} ({fmt_seg(segundos)}). "
        f"Tu membresía vence el {fecha_local(vence)}."
    )
    if await ya_esta_en_canal(context, user.id, chat_id):
        await update.message.reply_text(texto + "\nYa estás dentro del canal.")
        return
    try:
        url = await crear_enlace(context, chat_id)
    except TelegramError as e:
        log.error("No pude crear el enlace: %s", e)
        await update.message.reply_text(
            texto + "\nNo pude generar el enlace, escribe /enlace en un momento."
        )
        return
    await update.message.reply_text(
        texto
        + f"\n\nEntra aquí (sirve una sola vez y vence en {VIGENCIA_ENLACE // 60} min):\n{url}"
    )


def membresias_vigentes(uid):
    filas = run(
        SQL_MEMB + "WHERE m.user_id = ? AND m.estado = 'activo' AND c.activo = 1 "
        "ORDER BY c.alias",
        (uid,),
        "all",
    )
    return [f for f in filas if parse(f["vence"]) > ahora()]


async def enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    vigentes = membresias_vigentes(update.effective_user.id)
    if not vigentes:
        await update.message.reply_text("No tienes una membresía activa.")
        return
    if len(vigentes) > 1:
        kb = [
            [InlineKeyboardButton(etiqueta(f), callback_data=f"enl:{f['chat_id']}")]
            for f in vigentes
        ]
        await update.message.reply_text(
            "¿De qué canal quieres el enlace?", reply_markup=InlineKeyboardMarkup(kb)
        )
        return
    await entregar_enlace(context, update.message.reply_text, vigentes[0])


async def entregar_enlace(context, responder, f):
    try:
        url = await crear_enlace(context, f["chat_id"])
    except TelegramError as e:
        log.error("No pude crear el enlace: %s", e)
        await responder("No pude generar el enlace, intenta de nuevo.")
        return
    await responder(
        f"{etiqueta(f)}: te quedan {tiempo_restante(f['vence'])}.\n"
        f"Enlace (una sola vez, vence en {VIGENCIA_ENLACE // 60} min):\n{url}"
    )


async def enlace_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = int(q.data.split(":")[1])
    # Solo se entrega si ESE usuario tiene membresía vigente en ESE canal
    for f in membresias_vigentes(q.from_user.id):
        if f["chat_id"] == chat_id:
            await entregar_enlace(context, q.message.reply_text, f)
            return
    await q.message.reply_text("No tienes una membresía activa en ese canal.")


# ---------- Entrada de nuevos miembros ----------
async def nuevo_miembro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return
    canal = canal_activo(cm.chat.id)
    if not canal:
        return
    chat_id = cm.chat.id
    antes = cm.old_chat_member.status
    despues = cm.new_chat_member.status
    user = cm.new_chat_member.user
    if user.is_bot or user.id in ADMIN_IDS:
        return
    if not (antes in ("left", "kicked") and despues == "member"):
        return

    nombre = user.full_name
    fila = obtener(user.id, chat_id)
    donde = f"#{canal['alias']}"

    # Ya estaba expulsado por vencimiento: no se regalan 30 días al volver a entrar
    if fila and fila["estado"] == "expulsado":
        log.info("Reingreso sin renovar: %s (%s) en %s", nombre, user.id, donde)
        await sacar_del_canal(context, chat_id, user.id)
        try:
            await context.bot.send_message(
                user.id,
                "Tu membresía venció. Contacta al administrador para renovar y volver a entrar.",
            )
        except TelegramError:
            pass
        await avisar_admins(
            context,
            f"Reingreso sin renovar en {donde}, expulsado de nuevo: {nombre} ({user.id}).",
        )
        return

    # Ya tiene membresía vigente
    if fila and fila["estado"] == "activo" and parse(fila["vence"]) > ahora():
        log.info("Usuario %s ya estaba registrado en %s", user.id, donde)
        return

    if not AUTO_REGISTRO:
        await avisar_admins(
            context, f"Entró sin membresía registrada a {donde}: {nombre} ({user.id})."
        )
        return

    vence = registrar(user.id, chat_id, nombre, DIAS_MEMBRESIA, sumar=False)
    log.info("Nuevo miembro: %s (%s) en %s", nombre, user.id, donde)
    await avisar_admins(
        context,
        f"Nuevo miembro registrado en {donde}: {nombre} ({user.id}). "
        f"Vence {fecha_local(vence)}.",
    )


# ---------- Temporizador: recordatorios y expulsión ----------
async def revisar(context: ContextTypes.DEFAULT_TYPE):
    filas = run(
        SQL_MEMB.replace("LEFT JOIN", "JOIN")
        + "WHERE m.estado = 'activo' AND c.activo = 1",
        (),
        "all",
    )
    ahora_ = ahora()

    for f in filas:
        uid = f["user_id"]
        chat_id = f["chat_id"]
        canal = etiqueta(f)
        nombre = f["nombre"] or uid
        restante = (parse(f["vence"]) - ahora_).total_seconds()

        # Vencido: expulsar
        if restante <= 0:
            if await sacar_del_canal(context, chat_id, uid):
                run(
                    "UPDATE membresias SET estado = 'expulsado', expulsado_en = ? "
                    "WHERE user_id = ? AND chat_id = ?",
                    (iso(ahora()), uid, chat_id),
                )
                log.info("EXPULSIÓN EXITOSA: %s (%s) de %s", nombre, uid, canal)
                try:
                    await context.bot.send_message(
                        uid,
                        f"Tu membresía de {canal} venció y fuiste retirado. "
                        "Contacta al administrador o canjea una llave nueva con /canjear.",
                    )
                except TelegramError:
                    pass
                await avisar_admins(
                    context, f"Expulsado por vencimiento de {canal}: {nombre} ({uid})."
                )
            continue

        # Recordatorios (cada uno se envía una sola vez)
        enviados = {x for x in f["recordatorios"].split(",") if x}
        # Duración total de esta membresía: no avisar "vence en 5 días" si dura menos
        total = None
        if f.get("ultimo_pago"):
            total = (parse(f["vence"]) - parse(f["ultimo_pago"])).total_seconds()
        alcanzados = [
            d
            for d in REMINDER_DAYS
            if restante <= d * 86400
            and str(d) not in enviados
            and (total is None or d * 86400 < total)
        ]
        if not alcanzados:
            continue
        enviados.update(str(d) for d in alcanzados)
        run(
            "UPDATE membresias SET recordatorios = ? WHERE user_id = ? AND chat_id = ?",
            (",".join(sorted(enviados)), uid, chat_id),
        )
        faltan = tiempo_restante(f["vence"])
        log.info("ACCIÓN: recordatorio a %s (%s) de %s, quedan %s", nombre, uid, canal, faltan)
        try:
            await context.bot.send_message(
                uid,
                f"Tu membresía de {canal} vence en {faltan} ({fecha_local(f['vence'])}). "
                "Contacta al administrador para renovar.",
            )
        except TelegramError as e:
            log.warning("No pude enviar recordatorio a %s: %s", uid, e)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error: %s", context.error)


def main():
    init_db()
    builder = Application.builder().token(TOKEN).post_init(post_init)
    if TELEGRAM_API_PROXY:
        builder = builder.base_url(TELEGRAM_API_PROXY + "/bot")
        log.info("Usando proxy para la API de Telegram")
    app = builder.build()

    for nombre, fn in [
        ("start", start), ("miid", miid), ("tiempo", tiempo), ("ayuda", ayuda),
        ("canales", canales), ("addcanal", addcanal), ("quitarcanal", quitarcanal),
        ("agregar", agregar), ("pago", pago), ("estado", estado), ("lista", lista),
        ("usuarios", usuarios), ("vencer", vencer), ("quitar", quitar),
        ("llave", llave), ("llaves", llaves), ("revocar", revocar),
        ("canjear", canjear), ("enlace", enlace),
    ]:
        app.add_handler(CommandHandler(nombre, fn))
    app.add_handler(CallbackQueryHandler(llave_boton, pattern=r"^llave:"))
    app.add_handler(CallbackQueryHandler(pago_canal_boton, pattern=r"^pc:"))
    app.add_handler(CallbackQueryHandler(pago_decision, pattern=r"^pg:"))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, recibir_captura)
    )
    app.add_handler(CallbackQueryHandler(enlace_boton, pattern=r"^enl:"))
    app.add_handler(ChatMemberHandler(nuevo_miembro, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(canal_cambio, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(revisar, interval=REVISION_SEGUNDOS, first=10)

    log.info("Bot en marcha...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

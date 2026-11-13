"""
Bot de Telegram para controlar membresías de un canal o grupo (v2).

- Registra solo a quien entra al canal (30 días por defecto).
- Envía recordatorios ante de vencer (una sola vez cada uno).
- Expulsa automáticamente al vencer (sin bloquear: puede volver si renueva).
- Usa Postgres si existe DATABASE_URL (Railway); si no, usa SQLite local.
- Sistema de llaves (códigos de activación) para que los usuarios canjeen días.
"""

import logging
import math
import os
import sqlite3
import secrets
import httpx
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO
)
# Evita que el token aparezca en los logs (httpx imprime la URL completa)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("membresias")

# ---------- Configuración ----------
TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"])
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
    run(
        """
        CREATE TABLE IF NOT EXISTS llaves (
            codigo TEXT PRIMARY KEY,
            dias INTEGER NOT NULL,
            usado_por BIGINT,
            fecha_creacion TEXT NOT NULL
        )
        """
    )
    log.info("Base de datos: %s", "Postgres" if DATABASE_URL else f"SQLite ({DB_PATH})")


def obtener(uid):
    return run("SELECT * FROM miembros WHERE user_id = ?", (uid,), "one")


def registrar(uid, nombre, dias, sumar=True):
    """Crea o renueva. Si sumar=True y sigue vigente, suma desde su vencimiento."""
    ahora_ = ahora()
    fila = obtener(uid)
    base = ahora_
    if sumar and fila and fila["estado"] == "activo" and parse(fila["vence"]) > base:
        base = parse(fila["vence"])
    vence = base + timedelta(days=dias)
    run(
        """
        INSERT INTO miembros (user_id, nombre, ultimo_pago, vence, recordatorios, estado)
        VALUES (?, ?, ?, ?, '', 'activo')
        ON CONFLICT (user_id) DO UPDATE SET
            nombre = COALESCE(EXCLUDED.nombre, miembros.nombre),
            ultimo_pago = EXCLUDED.ultimo_pago,
            vence = EXCLUDED.vence,
            recordatorios = '',
            estado = 'activo'
        """,
        (uid, nombre, iso(ahora_), iso(vence)),
    )
    return iso(vence)


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


async def sacar_del_canal(context, uid):
    """Expulsa sin bloquear. Devuelve True si funcionó."""
    try:
        await context.bot.ban_chat_member(GROUP_ID, uid)
        await context.bot.unban_chat_member(GROUP_ID, uid, only_if_is_banned=True)
        return True
    except TelegramError as e:
        log.error("No pude expulsar a %s: %s", uid, e)
        return False


def uid_arg(context):
    if context.args and context.args[0].isdigit():
        return int(context.args[0])
    return None


# ---------- Comandos ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola. Este bot controla las membresías del canal.\n"
        "Usa /miid para ver tu ID y compartirlo con el administrador.\n"
        "Si tienes una llave de activación, usa /canjear CODIGO."
    )


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


@solo_admin
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos de administrador:\n"
        "/pago ID [Nombre] - renueva con los días por defecto\n"
        "/agregar ID DIAS [Nombre] - registra o suma días\n"
        "/crear_llave DIAS - genera un código de activación\n"
        "/estado ID - días restantes\n"
        "/lista - miembros activos\n"
        "/vencer ID - hace vencer ahora (para pruebas)\n"
        "/quitar ID - elimina y expulsa"
    )


@solo_admin
async def agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) < 2 or not a[0].isdigit() or not a[1].isdigit():
        await update.message.reply_text("Uso: /agregar ID DIAS [Nombre]")
        return
    uid, dias = int(a[0]), int(a[1])
    nombre = " ".join(a[2:]) or None
    vence = registrar(uid, nombre, dias)
    log.info("Membresía registrada: %s (%s) +%s días", nombre, uid, dias)
    await update.message.reply_text(
        f"Membresía de {nombre or uid} activa hasta {fecha_local(vence)}."
    )


@solo_admin
async def pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = uid_arg(context)
    if uid is None:
        await update.message.reply_text("Uso: /pago ID [Nombre]")
        return
    nombre = " ".join(context.args[1:]) or None
    vence = registrar(uid, nombre, DIAS_MEMBRESIA)
    log.info("Pago registrado: %s (+%s días)", uid, DIAS_MEMBRESIA)
    await update.message.reply_text(
        f"Pago registrado. Activa hasta {fecha_local(vence)} ({DIAS_MEMBRESIA} días)."
    )


@solo_admin
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = uid_arg(context)
    if uid is None:
        await update.message.reply_text("Uso: /estado ID")
        return
    f = obtener(uid)
    if not f:
        await update.message.reply_text("Ese usuario no está registrado.")
        return
    if f["estado"] != "activo":
        await update.message.reply_text(f"{f['nombre'] or uid}: expulsado por vencimiento.")
        return
    await update.message.reply_text(
        f"{f['nombre'] or uid}: {dias_restantes(f['vence'])} días restantes "
        f"(vence {fecha_local(f['vence'])})."
    )


@solo_admin
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filas = run(
        "SELECT * FROM miembros WHERE estado = 'activo' ORDER BY vence", (), "all"
    )
    if not filas:
        await update.message.reply_text("No hay membresías activas.")
        return
    lineas = [
        f"{f['nombre'] or '-'} ({f['user_id']}): {dias_restantes(f['vence'])} días"
        for f in filas
    ]
    bloque = f"Activos: {len(filas)}\n"
    for linea in lineas:
        if len(bloque) + len(linea) > 3500:
            await update.message.reply_text(bloque)
            bloque = ""
        bloque += linea + "\n"
    await update.message.reply_text(bloque)


@solo_admin
async def vencer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = uid_arg(context)
    if uid is None:
        await update.message.reply_text("Uso: /vencer ID")
        return
    if not obtener(uid):
        await update.message.reply_text("Ese usuario no está registrado.")
        return
    run(
        "UPDATE miembros SET vence = ?, estado = 'activo' WHERE user_id = ?",
        (iso(ahora() - timedelta(minutes=1)), uid),
    )
    await update.message.reply_text(
        f"Marcado como vencido. Se expulsará en menos de {REVISION_SEGUNDOS} segundos."
    )


@solo_admin
async def quitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = uid_arg(context)
    if uid is None:
        await update.message.reply_text("Uso: /quitar ID")
        return
    run("DELETE FROM miembros WHERE user_id = ?", (uid,))
    ok = await sacar_del_canal(context, uid)
    await update.message.reply_text(
        f"{uid} eliminado y expulsado."
        if ok
        else f"{uid} eliminado de la base, pero no pude expulsarlo del canal."
    )


@solo_admin
async def crear_llave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) < 1 or not a[0].isdigit():
        await update.message.reply_text("Uso: /crear_llave DIAS (ej: /crear_llave 30)")
        return
    
    dias = int(a[0])
    codigo = secrets.token_hex(3).upper()
    
    run(
        "INSERT INTO llaves (codigo, dias, fecha_creacion) VALUES (?, ?, ?)",
        (codigo, dias, iso(ahora()))
    )
    await update.message.reply_text(
        f"✅ Llave creada por {dias} días:\n\n`{codigo}`\n\nEnvíasela al cliente.",
        parse_mode="Markdown"
    )


async def canjear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) < 1:
        await update.message.reply_text("Uso: /canjear CODIGO")
        return
    
    codigo = a[0].strip().upper()
    uid = update.effective_user.id
    nombre = update.effective_user.full_name
    
    llave = run("SELECT * FROM llaves WHERE codigo = ? AND usado_por IS NULL", (codigo,), "one")
    
    if not llave:
        await update.message.reply_text("❌ Llave inválida o ya fue utilizada.")
        return
        
    vence = registrar(uid, nombre, llave["dias"], sumar=True)
    run("UPDATE llaves SET usado_por = ? WHERE codigo = ?", (uid, codigo))
    
    await update.message.reply_text(
        f"🎉 ¡Llave canjeada!\n\n"
        f"Se te han agregado **{llave['dias']} días**.\n"
        f"Vence el: **{fecha_local(vence)}**",
        parse_mode="Markdown"
    )
    await avisar_admins(context, f"El usuario {nombre} ({uid}) canjeó la llave {codigo} (+{llave['dias']} días).")


# ---------- Entrada de nuevos miembros ----------
async def nuevo_miembro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None or cm.chat.id != GROUP_ID:
        return
    antes = cm.old_chat_member.status
    despues = cm.new_chat_member.status
    user = cm.new_chat_member.user
    if user.is_bot or user.id in ADMIN_IDS:
        return
    if not (antes in ("left", "kicked") and despues == "member"):
        return

    nombre = user.full_name
    fila = obtener(user.id)

    if fila and fila["estado"] == "expulsado":
        log.info("Reingreso sin renovar: %s (%s)", nombre, user.id)
        await sacar_del_canal(context, user.id)
        try:
            await context.bot.send_message(
                user.id,
                "Tu membresía venció. Contacta al administrador para renovar y volver a entrar.",
            )
        except TelegramError:
            pass
        await avisar_admins(
            context, f"Reingreso sin renovar, expulsado de nuevo: {nombre} ({user.id})."
        )
        return

    if fila and fila["estado"] == "activo" and parse(fila["vence"]) > ahora():
        log.info("Usuario %s ya estaba registrado", user.id)
        return

    if not AUTO_REGISTRO:
        await avisar_admins(
            context, f"Entró sin membresía registrada: {nombre} ({user.id})."
        )
        return

    vence = registrar(user.id, nombre, DIAS_MEMBRESIA, sumar=False)
    log.info("Nuevo miembro: %s (%s)", nombre, user.id)
    await avisar_admins(
        context,
        f"Nuevo miembro registrado: {nombre} ({user.id}). "
        f"Vence {fecha_local(vence)}.",
    )


# ---------- Temporizador: recordatorios y expulsión ----------
async def revisar(context: ContextTypes.DEFAULT_TYPE):
    filas = run("SELECT * FROM miembros WHERE estado = 'activo'", (), "all")
    ahora_ = ahora()

    for f in filas:
        uid = f["user_id"]
        nombre = f["nombre"] or uid
        restante = (parse(f["vence"]) - ahora_).total_seconds()

        if restante <= 0:
            if await sacar_del_canal(context, uid):
                run("UPDATE miembros SET estado = 'expulsado' WHERE user_id = ?", (uid,))
                log.info("EXPULSIÓN EXITOSA: %s (%s)", nombre, uid)
                try:
                    await context.bot.send_message(
                        uid,
                        "Tu membresía venció y fuiste retirado del canal. "
                        "Contacta al administrador para renovar.",
                    )
                except TelegramError:
                    pass
                await avisar_admins(
                    context, f"Expulsado por vencimiento: {nombre} ({uid})."
                )
            continue

        enviados = {x for x in f["recordatorios"].split(",") if x}
        alcanzados = [
            d for d in REMINDER_DAYS if restante <= d * 86400 and str(d) not in enviados
        ]
        if not alcanzados:
            continue
        enviados.update(str(d) for d in alcanzados)
        run(
            "UPDATE miembros SET recordatorios = ? WHERE user_id = ?",
            (",".join(sorted(enviados)), uid),
        )
        dias = dias_restantes(f["vence"])
        log.info("ACCIÓN: recordatorio a %s (%s), quedan %s días", nombre, uid, dias)
        try:
            await context.bot.send_message(
                uid,
                f"Tu membresía vence en {dias} día(s) ({fecha_local(f['vence'])}). "
                "Contacta al administrador para renovar.",
            )
        except TelegramError as e:
            log.warning("No pude enviar recordatorio a %s: %s", uid, e)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error: %s", context.error)


def main():
    init_db()
    
    # ---- SOLUCIÓN AL ERROR DE RED (IPV6) ----
    req = HTTPXRequest(httpx_kwargs={"transport": httpx.AsyncHTTPTransport(local_address="::")})
    app = Application.builder().token(TOKEN).request(req).get_updates_request(req).build()
    # ----------------------------------------

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miid", miid))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("agregar", agregar))
    app.add_handler(CommandHandler("pago", pago))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("vencer", vencer))
    app.add_handler(CommandHandler("quitar", quitar))
    
    app.add_handler(CommandHandler("crear_llave", crear_llave))
    app.add_handler(CommandHandler("canjear", canjear))

    app.add_handler(ChatMemberHandler(nuevo_miembro, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(revisar, interval=REVISION_SEGUNDOS, first=10)

    log.info("Bot en marcha...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

"""Bot de Telegram para controlar membresías por canal o grupo."""

import logging
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("membresias")

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "membresias_v2.db")
DIAS_MEMBRESIA = int(os.environ.get("DIAS_MEMBRESIA", "30"))
REMINDER_DAYS = sorted({int(x) for x in os.environ.get("REMINDER_DAYS", "5,2,1").split(",") if x.strip()}, reverse=True)
REVISION_SEGUNDOS = int(os.environ.get("REVISION_SEGUNDOS", "60"))
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "-5"))))
AUTO_REGISTRO = os.environ.get("AUTO_REGISTRO", "1") == "1"
TELEGRAM_API_PROXY = os.environ.get("TELEGRAM_API_PROXY", "").strip().rstrip("/")
ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
UNIDADES = {"m": 60, "h": 3600, "d": 86400}
VIGENCIA_ENLACE = 600
FMT = "%Y-%m-%dT%H:%M:%S"

# Nuevos canales/grupos separados por comas. Se conserva GROUP_ID por compatibilidad.
def leer_ids():
    texto = os.environ.get("GROUP_IDS", "")
    if not texto:
        antiguo = os.environ.get("GROUP_ID")
        texto = antiguo or ""
    return [int(x) for x in texto.split(",") if x.strip()]

GROUP_IDS_INICIO = leer_ids()


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


def run(sql, params=(), fetch=None):
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql.replace("?", "%s"), params)
                    if fetch == "all":
                        return [dict(r) for r in cur.fetchall()] if cur.description else []
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


def tabla_existe(nombre):
    if DATABASE_URL:
        fila = run("SELECT to_regclass(?) AS name", (f"public.{nombre}",), "one")
        return bool(fila and fila.get("name"))
    fila = run("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nombre,), "one")
    return bool(fila)


def init_db():
    run("""
    CREATE TABLE IF NOT EXISTS grupos_v2 (
        chat_id BIGINT PRIMARY KEY,
        nombre TEXT,
        activo TEXT NOT NULL DEFAULT '1'
    )
    """)
    run("""
    CREATE TABLE IF NOT EXISTS miembros_v2 (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        nombre TEXT,
        ultimo_pago TEXT,
        vence TEXT NOT NULL,
        recordatorios TEXT NOT NOT NULL DEFAULT '',
        estado TEXT NOT NULL DEFAULT 'activo',
        renovaciones INTEGER NOT NULL DEFAULT 0,
        expulsado_en TEXT,
        PRIMARY KEY (chat_id, user_id)
    )
    """)
    run("""
    CREATE TABLE IF NOT EXISTS llaves_v2 (
        codigo TEXT PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        segundos BIGINT NOT NULL,
        creada TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'disponible',
        usada_por BIGINT,
        usada_en TEXT
    )
    """)

    # Migra datos anteriores de un solo canal conservando ambos formatos de versión.
    for chat_id in GROUP_IDS_INICIO:
        run("INSERT INTO grupos_v2 (chat_id, nombre) VALUES (?, ?) ON CONFLICT (chat_id) DO NOTHING", (chat_id, str(chat_id)))

    if tabla_existe("miembros"):
        antiguas = run("SELECT * FROM miembros", (), "all") or []
        for fila in antiguas:
            for chat_id in GROUP_IDS_INICIO:
                run("""
                INSERT INTO miembros_v2
                (chat_id, user_id, nombre, ultimo_pago, vence, recordatorios, estado, renovaciones, expulsado_en)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (chat_id, user_id) DO NOTHING
                """, (chat_id, fila["user_id"], fila.get("nombre"), fila.get("ultimo_pago"), fila["vence"], fila.get("recordatorios") or "", fila.get("estado") or "activo", fila.get("renovaciones") or 0, fila.get("expulsado_en")))

    log.info("Base de datos: %s", "Postgres" if DATABASE_URL else f"SQLite ({DB_PATH})")


def grupos():
    filas = run("SELECT chat_id, nombre, activo FROM grupos_v2 WHERE activo='1' ORDER BY chat_id", (), "all") or []
    if not filas:
        for chat_id in GROUP_IDS_INICIO:
            run("INSERT INTO grupos_v2 (chat_id, nombre) VALUES (?, ?) ON CONFLICT (chat_id) DO NOTHING", (chat_id, str(chat_id)))
        filas = run("SELECT chat_id, nombre, activo FROM grupos_v2 WHERE activo='1' ORDER BY chat_id", (), "all") or []
    return filas


def grupo(chat_id):
    return run("SELECT * FROM grupos_v2 WHERE chat_id=?", (chat_id,), "one")


def obtener(chat_id, uid):
    return run("SELECT * FROM miembros_v2 WHERE chat_id=? AND user_id=?", (chat_id, uid), "one")


def registrar_td(chat_id, uid, nombre, delta, sumar=True):
    ahora_ = ahora()
    fila = obtener(chat_id, uid)
    base = ahora_
    if sumar and fila and fila["estado"] == "activo" and parse(fila["vence"]) > base:
        base = parse(fila["vence"])
    vence = base + delta
    run("""
    INSERT INTO miembros_v2
    (chat_id, user_id, nombre, ultimo_pago, vence, recordatorios, estado, renovaciones, expulsado_en)
    VALUES (?, ?, ?, ?, ?, '', 'activo', 0, NULL)
    ON CONFLICT (chat_id, user_id) DO UPDATE SET
        nombre=COALESCE(EXCLUDED.nombre, miembros_v2.nombre),
        ultimo_pago=EXCLUDED.ultimo_pago,
        vence=EXCLUDED.vence,
        recordatorios='',
        estado='activo',
        renovaciones=miembros_v2.renovaciones + ?,
        expulsado_en=NULL
    """, (chat_id, uid, nombre, iso(ahora_), iso(vence), 1 if fila else 0))
    return iso(vence)


def registrar(chat_id, uid, nombre, dias, sumar=True):
    return registrar_td(chat_id, uid, nombre, timedelta(days=dias), sumar)


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
    try:
        await context.bot.ban_chat_member(chat_id, uid)
        await context.bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        return True
    except TelegramError as e:
        log.error("No pude expulsar a %s de %s: %s", uid, chat_id, e)
        return False


def parse_duracion(txt):
    m = re.fullmatch(r"(\d+)([mhd])", txt.strip().lower())
    if not m or int(m.group(1)) <= 0:
        return None
    return int(m.group(1)) * UNIDADES[m.group(2)]


def nuevo_codigo():
    return "-".join("".join(secrets.choice(ALFABETO) for _ in range(4)) for _ in range(3))


async def crear_enlace(context, chat_id):
    link = await context.bot.create_chat_invite_link(chat_id, member_limit=1, expire_date=int(ahora().timestamp()) + VIGENCIA_ENLACE)
    return link.invite_link


async def ya_esta_en_canal(context, chat_id, uid):
    try:
        m = await context.bot.get_chat_member(chat_id, uid)
        return m.status in ("member", "administrator", "creator")
    except TelegramError:
        return False


def chat_por_argumento(context, posicion=0):
    if len(context.args) > posicion:
        try:
            chat_id = int(context.args[posicion])
            if grupo(chat_id):
                return chat_id
        except ValueError:
            pass
    lista = grupos()
    if len(lista) == 1:
        return int(lista[0]["chat_id"])
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola. Este bot controla membresías por canal o grupo.\n/tiempo - ver tu tiempo\n/canjear LLAVE - activar\n/enlace - solicitar enlace")


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


@solo_admin
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Administración multicanal:\n"
        "/canal CHAT_ID [Nombre] - registra un canal/grupo\n"
        "/grupos - lista canales registrados\n"
        "/agregar CHAT_ID ID DIAS [Nombre]\n"
        "/pago CHAT_ID ID [Nombre]\n"
        "/estado CHAT_ID ID\n"
        "/lista CHAT_ID\n"
        "/usuarios CHAT_ID\n"
        "/vencer CHAT_ID ID\n"
        "/quitar CHAT_ID ID\n"
        "/llave DURACION CHAT_ID\n"
        "/llaves - llaves disponibles y usadas\n"
        "/revocar CODIGO"
    )


@solo_admin
async def canal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not context.args:
        await update.message.reply_text("Uso privado: /canal CHAT_ID [Nombre]")
        return
    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("CHAT_ID debe ser un número negativo, ejemplo -1001234567890.")
        return
    nombre = " ".join(context.args[1:]) or str(chat_id)
    run("INSERT INTO grupos_v2 (chat_id,nombre) VALUES (?,?) ON CONFLICT (chat_id) DO UPDATE SET nombre=EXCLUDED.nombre, activo='1'", (chat_id, nombre))
    await update.message.reply_text(f"Canal/grupo registrado: {nombre} ({chat_id})")


@solo_admin
async def canales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filas = grupos()
    if not filas:
        await update.message.reply_text("No hay canales registrados.")
        return
    await update.message.reply_text("Canales registrados:\n" + "\n".join(f"{f['nombre']} ({f['chat_id']})" for f in filas))


@solo_admin
async def agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) < 3:
        await update.message.reply_text("Uso: /agregar CHAT_ID ID DIAS [Nombre]")
        return
    try:
        chat_id, uid, dias = int(a[0]), int(a[1]), int(a[2])
    except ValueError:
        await update.message.reply_text("CHAT_ID, ID y DIAS deben ser números.")
        return
    if not grupo(chat_id):
        await update.message.reply_text("Ese canal/grupo no está registrado. Usa /canal CHAT_ID.")
        return
    nombre = " ".join(a[3:]) or None
    vence = registrar(chat_id, uid, nombre, dias)
    await update.message.reply_text(f"Membresía activa hasta {fecha_local(vence)} en {chat_id}.")


@solo_admin
async def pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /pago CHAT_ID ID [Nombre]")
        return
    try:
        chat_id, uid = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("CHAT_ID e ID deben ser números.")
        return
    if not grupo(chat_id):
        await update.message.reply_text("Ese canal/grupo no está registrado.")
        return
    nombre = " ".join(context.args[2:]) or None
    vence = registrar(chat_id, uid, nombre, DIAS_MEMBRESIA)
    await update.message.reply_text(f"Pago registrado hasta {fecha_local(vence)} en {chat_id}.")


@solo_admin
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /estado CHAT_ID ID")
        return
    chat_id, uid = int(context.args[0]), int(context.args[1])
    f = obtener(chat_id, uid)
    if not f:
        await update.message.reply_text("Ese usuario no está registrado en ese canal/grupo.")
    elif f["estado"] != "activo":
        await update.message.reply_text(f"{f['nombre'] or uid}: expulsado por vencimiento.")
    else:
        await update.message.reply_text(f"{f['nombre'] or uid}: quedan {tiempo_restante(f['vence'])} (vence {fecha_local(f['vence'])}).")


async def tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = chat_por_argumento(context)
    if chat_id is None:
        await update.message.reply_text("Indica el canal: /tiempo CHAT_ID")
        return
    f = obtener(chat_id, update.effective_user.id)
    if not f or f["estado"] != "activo" or parse(f["vence"]) <= ahora():
        await update.message.reply_text("Tu membresía venció o no existe en ese canal/grupo.")
        return
    await update.message.reply_text(f"Te quedan {tiempo_restante(f['vence'])}.\nVence el {fecha_local(f['vence'])}.")


@solo_admin
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /lista CHAT_ID")
        return
    chat_id = int(context.args[0])
    filas = run("SELECT * FROM miembros_v2 WHERE chat_id=? AND estado='activo' ORDER BY vence", (chat_id,), "all") or []
    await update.message.reply_text("Activos: " + str(len(filas)) if not filas else "Activos: " + str(len(filas)) + "\n" + "\n".join(f"{f['nombre'] or '-'} ({f['user_id']}): {tiempo_restante(f['vence'])}" for f in filas)[:3500])


@solo_admin
async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /usuarios CHAT_ID")
        return
    chat_id = int(context.args[0])
    filas = run("SELECT * FROM miembros_v2 WHERE chat_id=? ORDER BY vence", (chat_id,), "all") or []
    lineas = [f"Usuarios del canal {chat_id}: {len(filas)}"]
    for f in filas:
        lineas.append(f"{f['nombre'] or '-'} ({f['user_id']}): {f['estado']} - vence {fecha_local(f['vence'])} - renovaciones {f['renovaciones']}")
    await update.message.reply_text("\n".join(lineas)[:4000])


@solo_admin
async def vencer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /vencer CHAT_ID ID")
        return
    chat_id, uid = int(context.args[0]), int(context.args[1])
    if not obtener(chat_id, uid):
        await update.message.reply_text("Ese usuario no está registrado.")
        return
    run("UPDATE miembros_v2 SET vence=?, estado='activo' WHERE chat_id=? AND user_id=?", (iso(ahora() - timedelta(minutes=1)), chat_id, uid))
    await update.message.reply_text("Marcado como vencido.")


@solo_admin
async def quitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /quitar CHAT_ID ID")
        return
    chat_id, uid = int(context.args[0]), int(context.args[1])
    run("DELETE FROM miembros_v2 WHERE chat_id=? AND user_id=?", (chat_id, uid))
    ok = await sacar_del_canal(context, chat_id, uid)
    await update.message.reply_text("Usuario eliminado y expulsado." if ok else "Eliminado de la base, pero no pude expulsarlo.")


@solo_admin
async def llave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not context.args:
        await update.message.reply_text("Uso privado: /llave DURACION CHAT_ID")
        return
    seg = parse_duracion(context.args[0])
    chat_id = int(context.args[1]) if len(context.args) > 1 else None
    if not seg:
        await update.message.reply_text("Uso: /llave 30d CHAT_ID (m, h o d)")
        return
    if chat_id is None:
        lista = grupos()
        if len(lista) == 1:
            chat_id = int(lista[0]["chat_id"])
        else:
            await update.message.reply_text("Indica el canal: /llave 30d CHAT_ID")
            return
    if not grupo(chat_id):
        await update.message.reply_text("Ese canal/grupo no está registrado.")
        return
    codigo = nuevo_codigo()
    run("INSERT INTO llaves_v2 (codigo,chat_id,segundos,creada) VALUES (?,?,?,?)", (codigo, chat_id, seg, iso(ahora())))
    await update.message.reply_html(f"Llave creada para <b>{chat_id}</b>:\n<code>{codigo}</code>\nDuración: {fmt_seg(seg)}")


@solo_admin
async def llaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    disponibles = run("SELECT codigo,chat_id,segundos FROM llaves_v2 WHERE estado='disponible' ORDER BY creada", (), "all") or []
    usadas = run("SELECT codigo,chat_id,usada_por,usada_en FROM llaves_v2 WHERE estado='usada' ORDER BY usada_en DESC LIMIT 10", (), "all") or []
    texto = "Disponibles:\n" + ("\n".join(f"{f['codigo']} - {f['chat_id']} - {fmt_seg(f['segundos'])}" for f in disponibles[:30]) or "—")
    texto += "\n\nUsadas:\n" + ("\n".join(f"{f['codigo']} - {f['chat_id']} - {f['usada_por']}" for f in usadas) or "—")
    await update.message.reply_text(texto)


@solo_admin
async def revocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /revocar CODIGO")
        return
    codigo = context.args[0].strip().upper()
    fila = run("UPDATE llaves_v2 SET estado='revocada' WHERE codigo=? AND estado='disponible' RETURNING codigo", (codigo,), "one")
    await update.message.reply_text("Llave revocada." if fila else "No existe o ya fue utilizada.")


async def canjear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Escríbeme por privado para canjear tu llave.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /canjear TU-LLAVE")
        return
    user = update.effective_user
    codigo = context.args[0].strip().upper()
    fila = run("UPDATE llaves_v2 SET estado='usada', usada_por=?, usada_en=? WHERE codigo=? AND estado='disponible' RETURNING chat_id,segundos", (user.id, iso(ahora()), codigo), "one")
    if not fila:
        await update.message.reply_text("Llave inválida o ya utilizada.")
        return
    chat_id = int(fila["chat_id"])
    segundos = int(fila["segundos"])
    try:
        vence = registrar_td(chat_id, user.id, user.full_name, timedelta(seconds=segundos))
    except Exception:
        log.exception("Error registrando llave %s", codigo)
        run("UPDATE llaves_v2 SET estado='disponible', usada_por=NULL, usada_en=NULL WHERE codigo=?", (codigo,))
        await update.message.reply_text("Hubo un error. Tu llave sigue vigente.")
        return
    texto = f"Llave activada para {chat_id}. ({fmt_seg(segundos)}) Vence el {fecha_local(vence)}."
    await avisar_admins(context, f"Llave {codigo} canjeada por {user.full_name} ({user.id}) en {chat_id}. Vence {fecha_local(vence)}.")
    if await ya_esta_en_canal(context, chat_id, user.id):
        await update.message.reply_text(texto + "\nYa estás dentro del canal.")
        return
    try:
        url = await crear_enlace(context, chat_id)
    except TelegramError as e:
        log.error("No pude crear enlace para %s: %s", chat_id, e)
        await update.message.reply_text(texto + "\nNo pude generar el enlace.")
        return
    await update.message.reply_text(texto + f"\n\nEnlace exclusivo de una sola vez:\n{url}")


async def enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    chat_id = chat_por_argumento(context)
    if chat_id is None:
        await update.message.reply_text("Indica el canal: /enlace CHAT_ID")
        return
    f = obtener(chat_id, update.effective_user.id)
    if not f or f["estado"] != "activo" or parse(f["vence"]) <= ahora():
        await update.message.reply_text("No tienes una membresía activa en ese canal/grupo.")
        return
    try:
        url = await crear_enlace(context, chat_id)
    except TelegramError:
        await update.message.reply_text("No pude generar el enlace.")
        return
    await update.message.reply_text(f"Enlace exclusivo de una sola vez para {chat_id}:\n{url}")


async def nuevo_miembro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if cm is None:
        return
    chat_id = cm.chat.id
    if not grupo(chat_id):
        return
    antes = cm.old_chat_member.status
    despues = cm.new_chat_member.status
    user = cm.new_chat_member.user
    if user.is_bot or user.id in ADMIN_IDS:
        return
    if not (antes in ("left", "kicked") and despues == "member"):
        return
    fila = obtener(chat_id, user.id)
    if fila and fila["estado"] == "expulsado":
        await sacar_del_canal(context, chat_id, user.id)
        await avisar_admins(context, f"Reingreso sin renovar en {chat_id}: {user.full_name} ({user.id}).")
        return
    if fila and fila["estado"] == "activo" and parse(fila["vence"]) > ahora():
        return
    if not AUTO_REGISTRO:
        await avisar_admins(context, f"Entró sin membresía registrada en {chat_id}: {user.full_name} ({user.id}).")
        return
    vence = registrar(chat_id, user.id, user.full_name, DIAS_MEMBRESIA, sumar=False)
    await avisar_admins(context, f"Nuevo miembro en {chat_id}: {user.full_name} ({user.id}). Vence {fecha_local(vence)}.")


async def revisar(context: ContextTypes.DEFAULT_TYPE):
    filas = run("SELECT * FROM miembros_v2 WHERE estado='activo'", (), "all") or []
    ahora_ = ahora()
    for f in filas:
        uid = int(f["user_id"])
        chat_id = int(f["chat_id"])
        restante = (parse(f["vence"]) - ahora_).total_seconds()
        if restante <= 0:
            if await sacar_del_canal(context, chat_id, uid):
                run("UPDATE miembros_v2 SET estado='expulsado', expulsado_en=? WHERE chat_id=? AND user_id=?", (iso(ahora()), chat_id, uid))
                try:
                    await context.bot.send_message(uid, f"Tu membresía del canal {chat_id} venció y fuiste retirado.")
                except TelegramError:
                    pass
            continue
        enviados = {x for x in f["recordatorios"].split(",") if x}
        alcanzados = [d for d in REMINDER_DAYS if restante <= d * 86400 and str(d) not in enviados]
        if alcanzados:
            enviados.update(str(d) for d in alcanzados)
            run("UPDATE miembros_v2 SET recordatorios=? WHERE chat_id=? AND user_id=?", (",".join(sorted(enviados)), chat_id, uid))
            try:
                await context.bot.send_message(uid, f"Tu membresía de {chat_id} vence en {tiempo_restante(f['vence'])} ({fecha_local(f['vence'])}).")
            except TelegramError as e:
                log.warning("No pude enviar recordatorio a %s: %s", uid, e)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error: %s", context.error)


def main():
    init_db()
    builder = Application.builder().token(TOKEN)
    if TELEGRAM_API_PROXY:
        builder = builder.base_url(TELEGRAM_API_PROXY + "/bot")
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miid", miid))
    app.add_handler(CommandHandler("tiempo", tiempo))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("canal", canal))
    app.add_handler(CommandHandler("grupos", canales))
    app.add_handler(CommandHandler("agregar", agregar))
    app.add_handler(CommandHandler("pago", pago))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("usuarios", usuarios))
    app.add_handler(CommandHandler("vencer", vencer))
    app.add_handler(CommandHandler("quitar", quitar))
    app.add_handler(CommandHandler("llave", llave))
    app.add_handler(CommandHandler("llaves", llaves))
    app.add_handler(CommandHandler("revocar", revocar))
    app.add_handler(CommandHandler("canjear", canjear))
    app.add_handler(CommandHandler("enlace", enlace))
    app.add_handler(ChatMemberHandler(nuevo_miembro, ChatMemberHandler.CHAT_MEMBER))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(revisar, interval=REVISION_SEGUNDOS, first=10)
    log.info("Bot multicanal en marcha")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

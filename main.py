"""
Bot de Telegram para controlar membresías de un canal o grupo (v2).

- Registra solo a quien entra al canal (30 días por defecto).
- Envía recordatorios antes de vencer (una sola vez cada uno).
- Expulsa automáticamente al vencer (sin bloquear: puede volver si renueva).
- Usa Postgres si existe DATABASE_URL (Railway); si no, usa SQLite local.
- Llaves de un solo uso (/llave, /canjear): el admin crea una llave con la
  duración que quiera; al canjearla empieza a correr el tiempo y al vencer
  se expulsa automáticamente. La llave queda inutilizable.

Variables de entorno:
    BOT_TOKEN         token de @BotFather                      (obligatoria)
    GROUP_ID          ID del canal/grupo, ej: -1001234567890   (obligatoria)
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

import logging
import math
import os
import re
import asyncio
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import TelegramError
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
# Si el hosting no puede llegar a api.telegram.org, pasa por tu proxy (ej. Cloudflare Worker)
TELEGRAM_API_PROXY = os.environ.get("TELEGRAM_API_PROXY", "").strip().rstrip("/")
# Wompi (opcional: si falta WOMPI_PUBLIC_KEY el cobro queda desactivado)
WOMPI_PUBLIC_KEY = os.environ.get("WOMPI_PUBLIC_KEY", "").strip()
WOMPI_INTEGRITY_SECRET = os.environ.get("WOMPI_INTEGRITY_SECRET", "").strip()
WOMPI_EVENTS_SECRET = os.environ.get("WOMPI_EVENTS_SECRET", "").strip()
WOMPI_PRECIO_COP = int(os.environ.get("WOMPI_PRECIO_COP", "0"))  # en pesos
WOMPI_REDIRECT_URL = os.environ.get("WOMPI_REDIRECT_URL", "").strip()
PORT = int(os.environ.get("PORT", "8080"))
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
    run(
        """
        CREATE TABLE IF NOT EXISTS pagos_wompi (
            transaccion TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            monto_centavos BIGINT,
            creado TEXT NOT NULL
        )
        """
    )
    log.info("Base de datos: %s", "Postgres" if DATABASE_URL else f"SQLite ({DB_PATH})")


def obtener(uid):
    return run("SELECT * FROM miembros WHERE user_id = ?", (uid,), "one")


def registrar_td(uid, nombre, delta, sumar=True):
    """Crea o renueva. Si sumar=True y sigue vigente, suma desde su vencimiento."""
    ahora_ = ahora()
    fila = obtener(uid)
    base = ahora_
    if sumar and fila and fila["estado"] == "activo" and parse(fila["vence"]) > base:
        base = parse(fila["vence"])
    vence = base + delta
    run(
        """
        INSERT INTO miembros (user_id, nombre, ultimo_pago, vence, recordatorios, estado)
        VALUES (?, ?, ?, ?, '', 'activo')
        ON CONFLICT (user_id) DO UPDATE SET
            nombre = COALESCE(EXCLUDED.nombre, miembros.nombre),
            ultimo_pago = EXCLUDED.ultimo_pago,
            vence = EXCLUDED.vence,
            recordatorios = '',
            estado = 'activo',
            renovaciones = miembros.renovaciones + ?,
            expulsado_en = NULL
        """,
        (uid, nombre, iso(ahora_), iso(vence), 1 if fila else 0),
    )
    return iso(vence)


def registrar(uid, nombre, dias, sumar=True):
    return registrar_td(uid, nombre, timedelta(days=dias), sumar)


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
        await context.bot.unban_chat_member(GROUP_ID, uid, only_if_banned=True)
        return True
    except TelegramError as e:
        log.error("No pude expulsar a %s: %s", uid, e)
        return False


def uid_arg(context):
    if context.args and context.args[0].isdigit():
        return int(context.args[0])
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


async def crear_enlace(context):
    link = await context.bot.create_chat_invite_link(
        GROUP_ID,
        member_limit=1,
        expire_date=int(ahora().timestamp()) + VIGENCIA_ENLACE,
    )
    return link.invite_link


async def ya_esta_en_canal(context, uid):
    try:
        m = await context.bot.get_chat_member(GROUP_ID, uid)
        return m.status in ("member", "administrator", "creator")
    except TelegramError:
        return False


# ---------- Wompi ----------
def wompi_activo():
    return bool(
        WOMPI_PUBLIC_KEY and WOMPI_INTEGRITY_SECRET and WOMPI_EVENTS_SECRET
        and WOMPI_PRECIO_COP > 0
    )


def wompi_enlace(uid):
    """Enlace de checkout firmado. La referencia lleva el ID de Telegram."""
    referencia = f"M27-{uid}-{int(ahora().timestamp())}"
    centavos = WOMPI_PRECIO_COP * 100
    firma = hashlib.sha256(
        f"{referencia}{centavos}COP{WOMPI_INTEGRITY_SECRET}".encode()
    ).hexdigest()
    params = {
        "public-key": WOMPI_PUBLIC_KEY,
        "currency": "COP",
        "amount-in-cents": centavos,
        "reference": referencia,
        "signature:integrity": firma,
    }
    if WOMPI_REDIRECT_URL:
        params["redirect-url"] = WOMPI_REDIRECT_URL
    return "https://checkout.wompi.co/p/?" + urlencode(params, safe=":/")


def wompi_checksum_valido(evento):
    """checksum = sha256(valores de signature.properties + timestamp + secreto de eventos)."""
    try:
        sig = evento["signature"]
        valores = ""
        for prop in sig["properties"]:
            nodo = evento["data"]
            for parte in prop.split("."):
                nodo = nodo[parte]
            valores += str(nodo)
        calculado = hashlib.sha256(
            f"{valores}{evento['timestamp']}{WOMPI_EVENTS_SECRET}".encode()
        ).hexdigest()
        return hmac.compare_digest(calculado, str(sig["checksum"]).lower())
    except (KeyError, TypeError):
        return False


async def procesar_pago_wompi(app, evento):
    tx = evento["data"]["transaction"]
    if tx.get("status") != "APPROVED":
        return
    m = re.fullmatch(r"M27-(\d+)-\d+", str(tx.get("reference", "")))
    if not m:
        log.warning("Pago Wompi con referencia desconocida: %s", tx.get("reference"))
        return
    if WOMPI_PRECIO_COP and tx.get("amount_in_cents") != WOMPI_PRECIO_COP * 100:
        log.warning("Pago Wompi %s con monto inesperado", tx.get("id"))
        return
    uid = int(m.group(1))
    # Idempotencia: Wompi puede reenviar el mismo evento
    try:
        run(
            "INSERT INTO pagos_wompi (transaccion, user_id, monto_centavos, creado) "
            "VALUES (?, ?, ?, ?)",
            (str(tx["id"]), uid, tx.get("amount_in_cents"), iso(ahora())),
        )
    except Exception:
        log.info("Pago Wompi %s ya procesado", tx.get("id"))
        return
    vence = registrar(uid, None, DIAS_MEMBRESIA)
    log.info("Pago Wompi %s: %s renovado", tx["id"], uid)

    class Ctx:  # imita el context que usan las utilidades
        bot = app.bot
    ctx = Ctx()
    await avisar_admins(ctx, f"Pago Wompi aprobado de {uid}. Vence {fecha_local(vence)}.")
    texto = f"Pago recibido. Tu membresía vence el {fecha_local(vence)}."
    try:
        if await ya_esta_en_canal(ctx, uid):
            await app.bot.send_message(uid, texto + "\nYa estás dentro del canal.")
        else:
            url = await crear_enlace(ctx)
            await app.bot.send_message(
                uid,
                texto + f"\n\nEntra aquí (sirve una sola vez y vence en "
                f"{VIGENCIA_ENLACE // 60} min):\n{url}",
            )
    except TelegramError as e:
        log.error("No pude avisar a %s: %s", uid, e)


def iniciar_servidor_webhook(app, loop):
    class Handler(BaseHTTPRequestHandler):
        def _responder(self, code, texto="ok"):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(texto.encode())

        def do_GET(self):
            self._responder(200)  # healthcheck

        def do_POST(self):
            if self.path != "/wompi/webhook" or not wompi_activo():
                return self._responder(404, "no")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                evento = json.loads(self.rfile.read(min(n, 1_000_000)))
            except Exception:
                return self._responder(400, "bad")
            if not wompi_checksum_valido(evento):
                log.warning("Webhook Wompi con firma inválida")
                return self._responder(401, "firma")
            if evento.get("event") == "transaction.updated":
                fut = asyncio.run_coroutine_threadsafe(
                    procesar_pago_wompi(app, evento), loop
                )
                try:
                    fut.result(timeout=30)
                except Exception:
                    log.exception("Error procesando pago Wompi")
                    return self._responder(500, "error")  # Wompi reintenta
            self._responder(200)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Servidor HTTP en el puerto %s (webhook Wompi: %s)", PORT,
             "activo" if wompi_activo() else "desactivado")


async def pagar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Escríbeme por privado para pagar.")
        return
    if not wompi_activo():
        await update.message.reply_text("El pago en línea no está disponible por ahora.")
        return
    await update.message.reply_text(
        f"Membresía de {DIAS_MEMBRESIA} días: ${WOMPI_PRECIO_COP:,} COP.\n"
        f"Paga aquí y te llega el acceso automáticamente:\n{wompi_enlace(update.effective_user.id)}"
    )


# ---------- Comandos ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola. Este bot controla las membresías del canal.\n"
        "Para pagar en línea con Wompi: /pagar\n"
        "Para ver cuánto tiempo te queda: /tiempo\n"
        "Si tienes una llave, actívala con: /canjear TU-LLAVE\n"
        "Si tu enlace venció y tu membresía sigue activa: /enlace\n"
        "Usa /miid para ver tu ID y compartirlo con el administrador."
    )


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


@solo_admin
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos de administrador:\n"
        "/pago ID [Nombre] - renueva con los días por defecto\n"
        "/agregar ID DIAS [Nombre] - registra o suma días\n"
        "/estado ID - días restantes\n"
        "/lista - miembros activos\n"
        "/usuarios - todos: activos, salidos, tiempo y renovaciones\n"
        "/vencer ID - hace vencer ahora (para pruebas)\n"
        "/quitar ID - elimina y expulsa\n"
        "/llave 30d - crea una llave (m = minutos, h = horas, d = días)\n"
        "/llaves - ver llaves disponibles y usadas\n"
        "/revocar CODIGO - anula una llave sin usar"
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
        f"{f['nombre'] or uid}: quedan {tiempo_restante(f['vence'])} "
        f"(vence {fecha_local(f['vence'])})."
    )


async def tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = obtener(update.effective_user.id)
    if not f:
        await update.message.reply_text(
            "No tienes una membresía registrada. Para obtenerla usa /pagar "
            "o /canjear TU-LLAVE."
        )
        return
    if f["estado"] != "activo" or parse(f["vence"]) <= ahora():
        await update.message.reply_text(
            "Tu membresía venció. Para renovarla usa /pagar o /canjear TU-LLAVE."
        )
        return
    await update.message.reply_text(
        f"Te quedan {tiempo_restante(f['vence'])}.\n"
        f"Tu membresía vence el {fecha_local(f['vence'])}."
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
        f"{f['nombre'] or '-'} ({f['user_id']}): {tiempo_restante(f['vence'])}"
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
async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filas = run("SELECT * FROM miembros ORDER BY vence", (), "all")
    if not filas:
        await update.message.reply_text("No hay usuarios registrados.")
        return
    activos = [f for f in filas if f["estado"] == "activo"]
    salidos = [f for f in filas if f["estado"] != "activo"]
    lineas = [f"ACTIVOS ({len(activos)})"]
    for f in activos:
        lineas.append(
            f"{f['nombre'] or '-'} ({f['user_id']}): quedan {tiempo_restante(f['vence'])}"
            f", vence {fecha_local(f['vence'])}, renovaciones: {f['renovaciones']}"
        )
    lineas.append("")
    lineas.append(f"SALIERON POR VENCIMIENTO ({len(salidos)})")
    for f in salidos:
        cuando = f" el {fecha_local(f['expulsado_en'])}" if f.get("expulsado_en") else ""
        lineas.append(
            f"{f['nombre'] or '-'} ({f['user_id']}): venció {fecha_local(f['vence'])}"
            f", expulsado{cuando}, renovaciones: {f['renovaciones']}"
        )
    bloque = ""
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


# ---------- Llaves de un solo uso ----------
@solo_admin
async def llave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Crea las llaves por privado con el bot.")
        return
    seg = parse_duracion(context.args[0]) if context.args else None
    if not seg:
        await update.message.reply_text(
            "Uso: /llave 30d\n(m = minutos, h = horas, d = días)"
        )
        return
    codigo = nuevo_codigo()
    run(
        "INSERT INTO llaves (codigo, segundos, creada) VALUES (?, ?, ?)",
        (codigo, seg, iso(ahora())),
    )
    log.info("Llave creada: %s (%s)", codigo, fmt_seg(seg))
    await update.message.reply_html(
        f"Llave creada:\n<code>{codigo}</code>\n"
        f"Duración: {fmt_seg(seg)} (empieza a contar cuando el usuario la canjee)."
    )


@solo_admin
async def llaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    disp = run(
        "SELECT codigo, segundos FROM llaves WHERE estado = 'disponible' ORDER BY creada",
        (),
        "all",
    )
    usadas = run(
        "SELECT codigo, usada_por, usada_en FROM llaves "
        "WHERE estado = 'usada' ORDER BY usada_en DESC LIMIT 10",
        (),
        "all",
    )
    txt = f"<b>Disponibles ({len(disp)})</b>\n"
    txt += (
        "\n".join(f"<code>{f['codigo']}</code> · {fmt_seg(f['segundos'])}" for f in disp[:30])
        or "—"
    )
    if len(disp) > 30:
        txt += f"\n…y {len(disp) - 30} más"
    txt += "\n\n<b>Usadas (últimas 10)</b>\n"
    txt += (
        "\n".join(
            f"<code>{f['codigo']}</code> · {f['usada_por']} · {fecha_local(f['usada_en'])}"
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
        "WHERE codigo = ? AND estado = 'disponible' RETURNING segundos",
        (user.id, iso(ahora()), codigo),
        "one",
    )
    if not fila:
        await update.message.reply_text("Llave inválida o ya utilizada.")
        return

    segundos = int(fila["segundos"])
    try:
        vence = registrar_td(user.id, user.full_name, timedelta(seconds=segundos))
    except Exception:
        log.exception("Error registrando la membresía de la llave %s", codigo)
        run(
            "UPDATE llaves SET estado = 'disponible', usada_por = NULL, usada_en = NULL "
            "WHERE codigo = ?",
            (codigo,),
        )
        await update.message.reply_text(
            "Hubo un error. Tu llave sigue vigente, intenta de nuevo en un momento."
        )
        return

    log.info("Llave %s canjeada por %s (%s)", codigo, user.full_name, user.id)
    await avisar_admins(
        context,
        f"Llave {codigo} canjeada por {user.full_name} ({user.id}). "
        f"Vence {fecha_local(vence)}.",
    )

    texto = (
        f"Llave activada ({fmt_seg(segundos)}). "
        f"Tu membresía vence el {fecha_local(vence)}."
    )
    if await ya_esta_en_canal(context, user.id):
        await update.message.reply_text(texto + "\nYa estás dentro del canal.")
        return
    try:
        url = await crear_enlace(context)
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


async def enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    f = obtener(update.effective_user.id)
    if not f or f["estado"] != "activo" or parse(f["vence"]) <= ahora():
        await update.message.reply_text("No tienes una membresía activa.")
        return
    try:
        url = await crear_enlace(context)
    except TelegramError as e:
        log.error("No pude crear el enlace: %s", e)
        await update.message.reply_text("No pude generar el enlace, intenta de nuevo.")
        return
    await update.message.reply_text(
        f"Te quedan {tiempo_restante(f['vence'])}.\n"
        f"Enlace (una sola vez, vence en {VIGENCIA_ENLACE // 60} min):\n{url}"
    )


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

    # Ya estaba expulsado por vencimiento: no se regalan 30 días al volver a entrar
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

    # Ya tiene membresía vigente
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

        # Vencido: expulsar
        if restante <= 0:
            if await sacar_del_canal(context, uid):
                run(
                    "UPDATE miembros SET estado = 'expulsado', expulsado_en = ? "
                    "WHERE user_id = ?",
                    (iso(ahora()), uid),
                )
                log.info("EXPULSIÓN EXITOSA: %s (%s)", nombre, uid)
                try:
                    await context.bot.send_message(
                        uid,
                        "Tu membresía venció y fuiste retirado del canal. "
                        "Contacta al administrador o canjea una llave nueva con /canjear.",
                    )
                except TelegramError:
                    pass
                await avisar_admins(
                    context, f"Expulsado por vencimiento: {nombre} ({uid})."
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
            "UPDATE miembros SET recordatorios = ? WHERE user_id = ?",
            (",".join(sorted(enviados)), uid),
        )
        faltan = tiempo_restante(f["vence"])
        log.info("ACCIÓN: recordatorio a %s (%s), quedan %s", nombre, uid, faltan)
        try:
            await context.bot.send_message(
                uid,
                f"Tu membresía vence en {faltan} ({fecha_local(f['vence'])}). "
                "Contacta al administrador para renovar.",
            )
        except TelegramError as e:
            log.warning("No pude enviar recordatorio a %s: %s", uid, e)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error: %s", context.error)


def main():
    init_db()
    builder = Application.builder().token(TOKEN)
    if TELEGRAM_API_PROXY:
        builder = builder.base_url(TELEGRAM_API_PROXY + "/bot")
        log.info("Usando proxy para la API de Telegram")
    async def post_init(application):
        iniciar_servidor_webhook(application, asyncio.get_running_loop())

    app = builder.post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("miid", miid))
    app.add_handler(CommandHandler("pagar", pagar))
    app.add_handler(CommandHandler("tiempo", tiempo))
    app.add_handler(CommandHandler("ayuda", ayuda))
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

    log.info("Bot en marcha...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

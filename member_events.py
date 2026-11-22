"""Persistencia de eventos de membresía por canal.

Compatible con SQLite y PostgreSQL. Llamar a init_eventos(run) después de
inicializar la base de datos y a registrar_evento(run, ...) desde el bot.
"""

from datetime import datetime, timezone


def _ahora_iso():
    # Microsegundos para desempatar eventos ocurridos en el mismo segundo.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_eventos(run):
    """Crea la tabla de historial e índice en SQLite o PostgreSQL."""
    run(
        """
        CREATE TABLE IF NOT EXISTS eventos_membresia (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            nombre TEXT,
            evento TEXT NOT NULL,
            fecha TEXT NOT NULL
        )
        """
    )
    run(
        "CREATE INDEX IF NOT EXISTS idx_eventos_membresia_usuario_canal_fecha "
        "ON eventos_membresia (user_id, chat_id, fecha)"
    )


def registrar_evento(run, user_id, chat_id, nombre, evento, fecha=None):
    """Guarda entro, salio, expulsado o renovo para un usuario y canal."""
    permitidos = {"entro", "salio", "expulsado", "renovo"}
    if evento not in permitidos:
        raise ValueError(f"Tipo de evento inválido: {evento}")
    run(
        "INSERT INTO eventos_membresia (user_id, chat_id, nombre, evento, fecha) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, nombre, evento, fecha or _ahora_iso()),
    )


def ultimo_evento(run, user_id, chat_id):
    """Devuelve el evento más reciente de un usuario en un canal, si existe."""
    return run(
        "SELECT evento, fecha FROM eventos_membresia "
        "WHERE user_id = ? AND chat_id = ? ORDER BY fecha DESC LIMIT 1",
        (user_id, chat_id),
        "one",
    )

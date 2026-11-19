# Bot de membresías multicanal para Telegram

Administra membresías independientes para varios canales o grupos.

## Variables

- `BOT_TOKEN`: token de BotFather.
- `ADMIN_IDS`: IDs de Telegram separados por comas.
- `GROUP_IDS`: IDs negativos separados por comas. Ejemplo:
  `-1004443921046,-1004368462644`
- `DATABASE_URL`: PostgreSQL de Railway (opcional).
- `DB_PATH`: SQLite local (por defecto `membresias_v2.db`).
- `DIAS_MEMBRESIA`, `REMINDER_DAYS`, `REVISION_SEGUNDOS`, `TZ_OFFSET`, `AUTO_REGISTRO`.

## Comandos principales

- `/canal CHAT_ID [Nombre]`
- `/grupos`
- `/llave 30d CHAT_ID`
- `/llaves`
- `/revocar CODIGO`
- `/agregar CHAT_ID ID DIAS [Nombre]`
- `/pago CHAT_ID ID [Nombre]`
- `/estado CHAT_ID ID`
- `/lista CHAT_ID`
- `/usuarios CHAT_ID`
- `/vencer CHAT_ID ID`
- `/quitar CHAT_ID ID`
- `/tiempo CHAT_ID`
- `/enlace CHAT_ID`
- `/canjear LLAVE`

Cada llave queda asociada a un canal específico y solo puede canjearse una vez. Los enlaces tienen límite de una persona y caducidad de 10 minutos.

El bot debe ser administrador en todos los canales/grupos y tener permiso para invitar miembros y expulsar usuarios.

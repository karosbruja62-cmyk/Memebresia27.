# Bot de membresías para Telegram

Registra a quien entra al canal, avisa antes de vencer y expulsa al vencer.

## Variables (se ponen en Railway, nunca en el código)

| Variable | Valor |
|---|---|
| BOT_TOKEN | token nuevo de @BotFather |
| GROUP_ID | ID del canal, ej: -1004443921046 |
| ADMIN_IDS | tu ID, ej: 8383881215 |
| DATABASE_URL | `${{Postgres.DATABASE_URL}}` |

Opcionales: DIAS_MEMBRESIA (30), REMINDER_DAYS (5,2,1), REVISION_SEGUNDOS (60), TZ_OFFSET (-5), AUTO_REGISTRO (1).

## Comandos de administrador

- /pago ID [Nombre]: renueva con los días por defecto
- /agregar ID DIAS [Nombre]: registra o suma días
- /tiempo: cualquier usuario ve cuánto le queda
- /usuarios: listado completo (activos, salidos por vencimiento, tiempo restante, renovaciones)
- /estado ID, /lista
- /vencer ID: hace vencer ahora (pruebas)
- /quitar ID: elimina y expulsa

## Desplegar

1. Sube estos archivos a un repositorio PRIVADO de GitHub.
2. En Railway: New Project > Deploy from GitHub repo.
3. New > Database > PostgreSQL.
4. En el servicio del bot > Variables, agrega las de arriba.
5. Detén cualquier copia del bot que corra en otro lado (Termux) con el mismo token.

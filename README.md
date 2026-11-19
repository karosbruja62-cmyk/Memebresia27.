# Bot de membresías para Telegram

Registra a quien entra al canal, avisa antes de vencer y expulsa al vencer.

## Variables (se ponen en Railway, nunca en el código)

| Variable | Valor |
|---|---|
| BOT_TOKEN | token nuevo de @BotFather |
| GROUP_ID | (opcional) canal inicial, ej: -1004443921046. Queda con alias `#principal` y conserva a los miembros actuales |
| ADMIN_IDS | tu ID, ej: 8383881215 |
| DATABASE_URL | `${{Postgres.DATABASE_URL}}` |

Opcionales: DIAS_MEMBRESIA (30), REMINDER_DAYS (5,2,1), REVISION_SEGUNDOS (60), TZ_OFFSET (-5), AUTO_REGISTRO (1).

## Varios canales

- Agrega el bot como **administrador** (con permiso de invitar) a cada canal o grupo: se registra solo y te avisa con su `#alias`.
- Cada llave y cada enlace de entrada valen para UN canal; la membresía y el vencimiento también son por canal.
- Con un solo canal no hace falta escribir el alias. Con varios, añade `#alias` al comando (`/llave 30d #vip`); `/llave 30d` sin alias muestra botones para elegir.
- Si un usuario tiene varios canales, `/enlace` le muestra botones y solo entrega el de un canal donde su membresía esté vigente.
- /canales: lista los canales y su alias. /addcanal CHAT_ID [alias]: alta manual. /quitarcanal #alias: deja de ofrecerlo.
- Los comandos aparecen con su descripción al escribir `/` (el menú de admin solo lo ves tú).

## Comandos de administrador

Los que operan sobre un usuario aceptan `#alias` al final.

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

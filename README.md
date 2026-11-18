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

## Pagos con Wompi (opcional)

| Variable | Valor |
|---|---|
| WOMPI_PUBLIC_KEY | llave pública (pub_test_... / pub_prod_...) |
| WOMPI_INTEGRITY_SECRET | secreto de integridad |
| WOMPI_EVENTS_SECRET | secreto de eventos |
| WOMPI_PRECIO_COP | precio en pesos, ej: 30000 |
| WOMPI_REDIRECT_URL | (opcional) a dónde vuelve el cliente tras pagar |

En el dashboard de Wompi (Desarrolladores), pon la URL de eventos: `https://TU-DOMINIO/wompi/webhook`.
El usuario escribe /pagar al bot, paga, y recibe su enlace de acceso solo.

## Comandos de administrador

- /pago ID [Nombre]: renueva con los días por defecto
- /agregar ID DIAS [Nombre]: registra o suma días
- /tiempo: cualquier usuario ve cuánto le queda
- /estado ID, /lista
- /vencer ID: hace vencer ahora (pruebas)
- /quitar ID: elimina y expulsa

## Desplegar

1. Sube estos archivos a un repositorio PRIVADO de GitHub.
2. En Railway: New Project > Deploy from GitHub repo.
3. New > Database > PostgreSQL.
4. En el servicio del bot > Variables, agrega las de arriba.
5. Detén cualquier copia del bot que corra en otro lado (Termux) con el mismo token.

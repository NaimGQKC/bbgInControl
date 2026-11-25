# Sparrow AI — informe para la demo del 26 nov 2026

*Escrito la noche del 25 nov. Una página + anexo.*

## 0. Lo que me bloquea y solo puedes resolver tú
1. **Prueba real de WhatsApp: NO hecha.** La cuenta demo (#19, "Alejandro Monge", `live`) envía desde el **mismo número que tu teléfono de prueba (+34 623 485 133)**: un número no puede escribirse a sí mismo. No borré la cuenta (no habría liberado el número, que es el remitente en Meta, y rompería la demo). Necesito **otro móvil con WhatsApp**.
2. El **+1 561 576 6099** no lo usé: no pude entrar a GoHighLevel (no tengo tu Chrome conectado ni equipo vinculado a esta sesión) y un número de GoHighLevel normalmente recibe SMS, **no WhatsApp**. Si quieres usarlo, comprueba que tenga WhatsApp.
3. Fuera de horario (L-S 9:00-21:00 Madrid) la app **retiene** el primer mensaje hasta las 9:00 aunque tú lo autorices. No lo toqué (es una protección).

## a) Semáforo para mañana
| | Estado |
|---|---|
| Producción desplegada con el código nuevo (legales idénticas a `main`) | VERDE |
| Pruebas automáticas: 237 | VERDE |
| Landing, /privacidad, /terminos, /admin (con login) | VERDE (200) |
| Cuenta demo #19: `live`, Google activo, Meta activo, plantillas `lead_first_contact_v2` y las 5 más APROBADAS | VERDE |
| `PROSPECTING_ENABLED` / prospección: ninguna cuenta activa | VERDE (apagado) |
| IA real (es/fr/en): 14 conversaciones ensayadas | VERDE (ver anexo) |
| Mensaje real de WhatsApp de principio a fin | **ROJO: no probado** (punto 0) |
| Cuenta #1 (número de prueba de Meta): le falta la plantilla v2, hay un job fallido ("Template name does not exist") | AMARILLO: usa la #19 en la demo, no la #1 |
| Facturación (€30/cita) | AMARILLO: manual, no construida |

## b) Qué cambié (PR #13, ya fusionado en `main`)
- Mensaje de **borrado de datos** o **amenaza clara** de cualquier lead: avisa al agente (antes: baja silenciosa). "Abogada"/"policía"/"ya puse una denuncia" en una venta normal NO cuentan. Un lead ya dado de baja que pide borrar datos también avisa al agente.
- **Prompt es/fr/en**: decir su precio o "no me gusta pagar comisiones" ya no pasa al agente; la objeción se reconoce una vez; no repite el mismo mensaje; respeta a quien tiene exclusiva con otra agencia; "¿de dónde sacasteis mi número?" ahora se responde.
- Lead con cita que **cancela en el chat**: se paran los recordatorios (la cita sigue "booked" y cuenta).
- **Recordatorios de llamada** en español ya no dicen "pasa por por teléfono".
- **Una sola ventana horaria** (9-21) en `in_allowed_hours` y `next_allowed_time`.
- **Primer mensaje no entregable** (sin WhatsApp / fijo): avisa al agente (no a leads de prospección, para no inundar).
- 15 + 7 pruebas nuevas. 5 escenarios nuevos en `scripts/ai_rehearsal.py`.
- Revisión de código: pr-review-toolkit + SuperMatt `code-review` (3 ejes). Encontró 1 P1 y 3 P2 en mis propios arreglos; todos corregidos.

## c) Sigue siendo arriesgado
- Meta real nunca probado de punta a punta con esta versión (entrega real, ecos de Coexistence, Google OAuth).
- Recordatorio 24h de llamada en español: "Laura pasa por el teléfono (te llamará)": entendible pero torpe (cambiarlo exige plantilla nueva en Meta).
- Sin facturación; sin resumen de cita por WhatsApp al agente; cita cancelada en el chat queda `booked`.
- Sin resolver: francés ofrece dos veces el mismo día; texto "valoración gratuita" de la primera plantilla (decídelo tú); `/start` crea cuentas sin límite; `/help` escribe al operador; admin sin protección CSRF; hash de baja SHA-256 simple; columnas de Postgres sin verificar con nombres muy largos.

## d) Guion de la demo (jueves 26 nov, entre 10:00 y 20:00)
1. **9:30** abre `app.sparrowai.eu/admin` → cuenta #19: debe decir `live`, plantillas APROBADAS.
2. **Antes de empezar**: el móvil de la otra persona debe tener WhatsApp y **no** ser el +34 623 485 133.
3. En la cuenta #19 → "Lead de prueba": nombre, el teléfono de esa persona, zona (p. ej. Chamberí), casilla **prueba** marcada.
4. A los ~30 s llega el primer mensaje ("soy el asistente virtual de ..."). Responde "sí, vendo".
5. La IA hace 1-2 preguntas y ofrece 2 huecos; elige uno, da una dirección.
6. Confirmación en el chat + evento en Google Calendar (marcado TEST).
7. Si algo falla: `/admin` → lead → ver mensajes y jobs fallidos.

## e) Lo único que tienes que hacer tú
1. Darme **otro número con WhatsApp** (o hacer tú el paso 3 con el móvil de la otra persona).
2. Decidir el texto "valoración gratuita" y si quieres plantilla nueva para llamadas.
3. Confirmar que Meta/Google siguen conectados en la #19 (yo solo puedo leer).

## Anexo: conversaciones con el modelo real
Archivos en `docs/rehearsal/`: `1_before_fixes.txt` (antes), `2_after_fixes_14_scenarios.txt` y `3_after_final_prompt_tweak.txt` (después). Resumen:
- **Antes**: "pido 250.000..." → traspaso inmediato; "no me gusta pagar comisiones" → traspaso sin reconocer; oferta de huecos repetida 3 veces; "os denuncio a la AEPD, borrad mis datos" → baja silenciosa.
- **Después**: sigue calificando y lleva a la visita; reconoce la objeción en una frase; huecos distintos; aviso al agente en la amenaza; comisión directa y comprador → traspaso correcto; exclusiva con otra agencia → una oferta y despedida amable; nota de voz → pide resumen; español roto → paciente y sencillo.
- Pendiente de mejora: un emoji suelto ("👍") aún puede recibir una pregunta genérica; el flujo salta pronto a los huecos (decisión de diseño).

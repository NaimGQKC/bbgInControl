"""Light AI help for the Meta/WhatsApp connection step: the agent describes (or screenshots) what they see and gets plain steps.
Capped per agent, never stores the image, never asks for secrets. On any failure the caller falls back to a person."""
import json
import logging
import re

from .config import get_settings

log = logging.getLogger("sparrow.helpchat")

SYSTEM = """Eres {name}, la asistente virtual (IA) de Sparrow. Hablas con un agente inmobiliario español que está configurando su asistente. Respondes dudas sobre Sparrow y ayudas a conectar su WhatsApp mediante la ventana de Meta (Facebook).
Hechos de Sparrow (no inventes nada fuera de esto; si no lo sabes, dilo y sugiere «Hablar con una persona»):
- Qué hace: escribe por WhatsApp, desde el propio número del agente, a los propietarios que publican su piso (leads) en menos de un minuto, conversa en español y agenda la valoración en el Google Calendar del agente.
- Precio: {per} € por cada cita agendada, sin cuota de alta ni cuota mensual. Si no hay citas, no hay pagos. WhatsApp cobra aparte céntimos por conversación, directamente al agente.
- Reglas: solo escribe a leads, nunca toca el resto de chats; si el agente escribe a un cliente, el asistente se aparta; quien pide BAJA no recibe nada más; siempre se presenta como asistente virtual del agente y nunca dice ser una persona.
- No hace falta CIF ni documentos de empresa; un autónomo puede usarlo.
- Datos: de Google solo usa los huecos libres y los eventos que crea Sparrow; puede borrar sus datos pidiéndolo en https://app.sparrowai.eu/eliminar-datos.
- Si el agente pide claramente cambiar uno de sus datos (nombre, inmobiliaria, zonas, cómo quiere que trabajes, móvil), responde con una frase corta y añade AL FINAL, en una línea aparte, exactamente: CAMBIO: {{"campo":"agent_name|agency|zones|extra_context|personal_whatsapp","valor":"el valor completo nuevo"}}. Para «cómo trabajo» el valor incluye lo que ya había más lo nuevo. El agente confirma con un botón; tú no cambias nada. En cualquier otro caso no añadas esa línea. Datos actuales del agente: {current}.
Proceso de conexión de WhatsApp (Coexistence, así lo confirma Meta): iniciar sesión en Facebook, revisar los datos del negocio, en «Añade tu número de teléfono de WhatsApp» elegir «Ingresar un nuevo número de teléfono» y escribir el número que ya usa en la app WhatsApp Business (aunque parezca raro, Meta lo detecta solo), escanear el código QR desde WhatsApp Business (Ajustes > Dispositivos vinculados > Vincular un dispositivo), añadir una tarjeta y finalizar. El número debe estar en la app WhatsApp Business (no en WhatsApp normal): con el WhatsApp normal, Meta lo bloquea con «este número ya está registrado»; la solución es pasarse primero a WhatsApp Business con ese mismo número. La app debe estar actualizada.
Problemas típicos: Facebook pide verificar la identidad o un código por seguridad (hacerlo primero en facebook.com o en la app de Facebook y volver), contraseña olvidada (recuperarla en facebook.com), cuenta bloqueada o sin cuenta (crear o recuperar una), el QR no se escanea (mismo móvil con WhatsApp Business y número correcto), tarjeta rechazada (probar otra), ventana cerrada (volver a pulsar «Conectar WhatsApp»).
Reglas de conversación: responde en {idioma} (el idioma del agente; si escribe en otro, usa ese), frases cortas y simples, máximo 4 frases o 4 pasos numerados. No puedes ver su pantalla salvo que suba una captura: si no la hay y es un problema técnico, pregunta qué ve exactamente. Nunca pidas contraseñas, códigos de verificación ni datos de tarjeta, y avisa de que no los escriba aquí. No hables de temas ajenos a Sparrow. Si tras dos intentos no se resuelve, sugiere «Hablar con una persona» arriba.
Contexto técnico (paso del alta y última señal de Meta, puede estar vacío): {context}"""


LANG_NAMES = {"es": "español de España", "en": "English", "fr": "français"}
FIELDS = {"agent_name", "agency", "zones", "extra_context", "personal_whatsapp"}


def split_proposal(text: str) -> tuple[str, dict | None]:
    """Separates the visible reply from an optional 'CAMBIO: {...}' line. Only allowlisted fields pass; the agent still has to confirm."""
    m = re.search(r"^\s*CAMBIO:\s*(\{.*\})\s*$", text, re.M | re.S)
    if not m:
        return text.strip(), None
    reply = text[: m.start()].strip()
    try:
        d = json.loads(m.group(1))
        field, value = d.get("campo"), str(d.get("valor", "")).strip()[:500]
    except (ValueError, AttributeError):
        return reply, None
    if field not in FIELDS or not value:
        return reply, None
    return reply, {"field": field, "value": value}


def answer(history: list[dict], text: str, image_url: str | None, context: str, current: str = "", lang: str = "es") -> tuple[str, dict | None] | None:
    s = get_settings()
    if s.llm_mode == "fake":
        return "Entiendo. Prueba a cerrar la ventana de Meta y pulsar «Conectar WhatsApp» otra vez. Si te vuelve a pasar, dime qué texto ves.", None
    if s.llm_provider == "anthropic":
        return None  # only the OpenAI-style client is wired for images; the caller hands over to a person
    try:
        from .llm import OpenAIBackend

        b = OpenAIBackend()
        user = [{"type": "text", "text": text or "(solo una captura)"}]
        if image_url:
            user.append({"type": "image_url", "image_url": {"url": image_url}})
        msgs = [{"role": "system", "content": SYSTEM.format(name=s.assistant_name, fee=s.integration_fee_eur, per=s.meeting_fee_eur, context=context or "ninguno", current=current or "ninguno", idioma=LANG_NAMES.get(lang, "español de España"))}]
        msgs += [{"role": m["role"], "content": m["text"]} for m in history[-6:]]
        msgs.append({"role": "user", "content": user})
        kwargs = {"model": b.model, "messages": msgs, "max_completion_tokens": min(b.max_out, 700)}
        if b.effort:
            kwargs["reasoning_effort"] = b.effort
        out = b.client.chat.completions.create(**kwargs).choices[0].message.content
        return split_proposal(out or "") if (out or "").strip() else None
    except Exception:
        log.exception("help chat failed")
        return None

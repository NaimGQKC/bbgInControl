"""Light AI help for the Meta/WhatsApp connection step: the agent describes (or screenshots) what they see and gets plain steps.
Capped per agent, never stores the image, never asks for secrets. On any failure the caller falls back to a person."""
import logging

from .config import get_settings

log = logging.getLogger("sparrow.helpchat")

SYSTEM = """Eres {name}, la asistente virtual (IA) de Sparrow. Ayudas a un agente inmobiliario español a conectar su WhatsApp con Sparrow mediante la ventana de Meta (Facebook).
Cómo es el proceso normal: iniciar sesión en Facebook, revisar los datos del negocio, elegir conectar la app de WhatsApp Business, escribir el número, escanear un código QR desde WhatsApp Business (Ajustes > Dispositivos vinculados), añadir una tarjeta y finalizar.
Problemas típicos: Facebook pide verificar la identidad o un código por seguridad (hacerlo primero en facebook.com o en la app de Facebook y volver), contraseña olvidada (recuperarla en facebook.com), cuenta bloqueada o sin cuenta (crear o recuperar una), el QR no se escanea (mismo móvil con WhatsApp Business y número correcto), tarjeta rechazada (probar otra), ventana cerrada (volver a pulsar «Conectar WhatsApp»).
Reglas: responde en español de España, con frases cortas y simples, máximo 4 frases o 4 pasos numerados. No puedes ver su pantalla salvo que suba una captura: si no la hay, pregunta qué ve exactamente. Nunca pidas contraseñas, códigos de verificación ni datos de tarjeta, y avisa de que no los escriba aquí. No hables de otros temas. Si tras dos intentos no se resuelve, sugiere pulsar «Hablar con una persona» arriba.
Contexto técnico (última señal de Meta, puede estar vacía): {context}"""


def answer(history: list[dict], text: str, image_url: str | None, context: str) -> str | None:
    s = get_settings()
    if s.llm_mode == "fake":
        return "Entiendo. Prueba a cerrar la ventana de Meta y pulsar «Conectar WhatsApp» otra vez. Si te vuelve a pasar, dime qué texto ves."
    if s.llm_provider == "anthropic":
        return None  # only the OpenAI-style client is wired for images; the caller hands over to a person
    try:
        from .llm import OpenAIBackend

        b = OpenAIBackend()
        user = [{"type": "text", "text": text or "(solo una captura)"}]
        if image_url:
            user.append({"type": "image_url", "image_url": {"url": image_url}})
        msgs = [{"role": "system", "content": SYSTEM.format(name=s.assistant_name, context=context or "ninguna")}]
        msgs += [{"role": m["role"], "content": m["text"]} for m in history[-6:]]
        msgs.append({"role": "user", "content": user})
        kwargs = {"model": b.model, "messages": msgs, "max_completion_tokens": min(b.max_out, 700)}
        if b.effort:
            kwargs["reasoning_effort"] = b.effort
        out = b.client.chat.completions.create(**kwargs).choices[0].message.content
        return (out or "").strip() or None
    except Exception:
        log.exception("help chat failed")
        return None

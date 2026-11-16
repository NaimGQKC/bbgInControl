"""The language of an agent's leads (and of the agent's own alerts): Spanish (default), French or English.
Stored in `account.settings["lang"]` (existing JSON column: no schema change). Everything lead-facing goes through here."""
from datetime import datetime

LANGS = ("es", "fr", "en")
CC = {"es": "34", "fr": "33", "en": "34"}  # default country calling code for national phone numbers
TZ = {"es": "Europe/Madrid", "fr": "Europe/Paris", "en": "Europe/Madrid"}

DAYS = {
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
MONTHS = {
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}


def lead_lang(account) -> str:
    v = (getattr(account, "settings", None) or {}).get("lang")
    return v if v in LANGS else "es"


def set_lead_lang(account, lang: str) -> None:
    """Changes the language of this agent's leads; keeps the country defaults in step (timezone, phone prefix)."""
    if lang not in LANGS:
        return
    account.settings = {**(account.settings or {}), "lang": lang}
    if account.timezone in (TZ["es"], TZ["fr"]):
        account.timezone = TZ[lang]


def agent_hi(account) -> str:
    """The agent's first name for alerts (or a greeting word when we don't have it)."""
    parts = (getattr(account, "agent_name", "") or "").split()
    return parts[0] if parts else text("agent_hi", lead_lang(account))


def cc_for(account) -> str:
    return CC[lead_lang(account)]


def fmt_time(d: datetime, lang: str) -> str:
    return f"{d:%H}h{d:%M}" if lang == "fr" else f"{d:%H:%M}"


def when_label(d_local: datetime, lang: str) -> str:
    """'jueves 29 de octubre, 10:00' / 'jeudi 29 octobre à 10h00' / 'Thursday 29 October, 10:00' (d_local is already local)."""
    day, month = DAYS[lang][d_local.weekday()], MONTHS[lang][d_local.month - 1]
    if lang == "fr":
        return f"{day} {d_local.day} {month} à {fmt_time(d_local, lang)}"
    if lang == "en":
        return f"{day} {d_local.day} {month}, {fmt_time(d_local, lang)}"
    return f"{day} {d_local.day} de {month}, {fmt_time(d_local, lang)}"


def short_when(d_local: datetime, lang: str) -> str:
    """The day and hour inside a sentence: 'martes 30 a las 18:00' / 'mardi 30 à 18h00' / 'Tuesday 30 at 18:00'."""
    day, t = DAYS[lang][d_local.weekday()], fmt_time(d_local, lang)
    return {"es": f"{day} {d_local.day} a las {t}", "fr": f"{day} {d_local.day} à {t}", "en": f"{day} {d_local.day} at {t}"}[lang]


def where_phrase(address: str, meeting_type: str, lang: str) -> str:
    """Where the meeting happens, ready for a sentence."""
    if address:
        return {"es": address, "fr": f"à {address}", "en": f"at {address}"}[lang]
    if meeting_type == "call":
        return {"es": "por teléfono", "fr": "par téléphone", "en": "by phone"}[lang]
    return {"es": "tu vivienda", "fr": "chez vous", "en": "at your home"}[lang]


# Free-form texts and small words, by language (the key is stable, the text is what the person reads)
TEXTS: dict[str, dict[str, str]] = {
    "opt_out_confirmation": {"es": "Entendido, no te escribiremos más. Un saludo.", "fr": "C'est bien noté, nous ne vous écrirons plus. Bonne journée.", "en": "Understood, we won't write to you again. Take care."},
    "handoff_to_lead": {"es": "Perfecto, se lo paso a {agent_first_name} y te escribe en breve.", "fr": "Parfait, je transmets à {agent_first_name} qui vous écrira très bientôt.", "en": "Perfect, I'll pass this to {agent_first_name}, who will write to you shortly."},
    "voice_not_supported": {"es": "Perdona, ahora mismo no puedo escuchar audios. ¿Me lo puedes escribir?", "fr": "Désolé, je ne peux pas écouter les messages vocaux pour le moment. Pouvez-vous me l'écrire ?", "en": "Sorry, I can't listen to voice notes right now. Could you write it to me?"},
    "ai_no_reply_fallback": {"es": "Entendido, gracias por responder. Si más adelante quieres una valoración, escríbeme por aquí.", "fr": "C'est noté, merci pour votre réponse. Si vous souhaitez une estimation plus tard, écrivez-moi ici.", "en": "Understood, thanks for replying. If you'd like a valuation later, write to me here."},
    "conversation_start": {"es": "[Inicio de la conversación]", "fr": "[Début de la conversation]", "en": "[Start of the conversation]"},
    "agent_wrote": {"es": "[Mensaje escrito en persona por {agent_first}, no por ti]", "fr": "[Message écrit en personne par {agent_first}, pas par vous]", "en": "[Message written in person by {agent_first}, not by you]"},
    "msg_clipped": {"es": "… [mensaje recortado]", "fr": "… [message tronqué]", "en": "… [message cut]"},
    "intro_phrase": {"es": "Soy el asistente virtual de {agent_first}. ", "fr": "Je suis l'assistant virtuel de {agent_first}. ", "en": "I'm the virtual assistant of {agent_first}. "},
    "intro_marker": {"es": "asistente virtual", "fr": "assistant virtuel", "en": "virtual assistant"},
    "agent_hi": {"es": "hola", "fr": "bonjour", "en": "hello"},
    "resume_ambiguous": {"es": "No he encontrado a una sola persona con ese nombre. Conversaciones en pausa: {names}", "fr": "Je n'ai pas trouvé une seule personne avec ce nom. Conversations en pause : {names}", "en": "I didn't find exactly one person with that name. Paused conversations: {names}"},
    "resume_none": {"es": "ninguna", "fr": "aucune", "en": "none"},
    "resume_done": {"es": "Hecho, sigo yo con {name}.", "fr": "C'est fait, je reprends avec {name}.", "en": "Done, I'm continuing with {name}."},
    "alert_reconnect_google": {"es": "vuelve a conectar tu Google Calendar desde tu enlace de Sparrow", "fr": "reconnectez votre Google Agenda depuis votre lien Sparrow", "en": "reconnect your Google Calendar from your Sparrow link"},
    "alert_template_rejected": {"es": "WhatsApp rechazó un mensaje; lo revisamos nosotros", "fr": "WhatsApp a refusé un message ; nous nous en occupons", "en": "WhatsApp rejected a message; we're looking into it"},
    "send_failed_reason": {"es": "no se pudo enviar la respuesta ({err})", "fr": "la réponse n'a pas pu être envoyée ({err})", "en": "the reply could not be sent ({err})"},
    "resume_help": {"es": "Para que tu asistente retome una conversación, escribe: Sigue con <nombre del cliente>", "fr": "Pour que votre assistant reprenne une conversation, écrivez : Reprends avec <nom du client>", "en": "To make your assistant resume a conversation, write: Continue with <client name>"},
    "greeting_fallback": {"es": "buenas", "fr": "bonjour", "en": "there"},
    "your_zone": {"es": "tu zona", "fr": "votre secteur", "en": "your area"},
    "your_agency": {"es": "tu inmobiliaria", "fr": "votre agence", "en": "your agency"},
    "your_agent": {"es": "tu agente", "fr": "votre agent", "en": "your agent"},
    "the_agent": {"es": "el agente", "fr": "l'agent", "en": "the agent"},
    # handoff reasons shown to the agent
    "why_tech_error": {"es": "error técnico del asistente", "fr": "erreur technique de l'assistant", "en": "technical error in the assistant"},
    "why_limit": {"es": "límite de mensajes del asistente (posible abuso)", "fr": "limite de messages de l'assistant (abus possible)", "en": "assistant message limit (possible abuse)"},
    "why_unsure": {"es": "el asistente no está seguro", "fr": "l'assistant n'est pas sûr", "en": "the assistant is not sure"},
    "why_no_reply": {"es": "el asistente no generó respuesta", "fr": "l'assistant n'a pas généré de réponse", "en": "the assistant produced no reply"},
    # calendar event
    "ev_summary": {"es": "Valoración", "fr": "Estimation", "en": "Valuation"},
    "ev_reason": {"es": "Motivo", "fr": "Motif", "en": "Reason"},
    "ev_timeline": {"es": "Plazo", "fr": "Délai", "en": "Timeline"},
    "ev_occupancy": {"es": "Ocupación", "fr": "Occupation", "en": "Occupancy"},
    "ev_other_agency": {"es": "¿Otra agencia?", "fr": "Autre agence ?", "en": "Other agency?"},
    "ev_type": {"es": "Tipo", "fr": "Type", "en": "Type"},
    "ev_visit": {"es": "visita", "fr": "visite", "en": "visit"},
    "ev_call": {"es": "llamada", "fr": "appel", "en": "call"},
    "ev_origin": {"es": "Origen", "fr": "Origine", "en": "Source"},
    "ev_call_where": {"es": "Llamada · {phone}", "fr": "Appel · {phone}", "en": "Call · {phone}"},
}


def text(key: str, lang: str, **kw) -> str:
    s = TEXTS[key].get(lang) or TEXTS[key]["es"]
    return s.format(**kw) if kw else s

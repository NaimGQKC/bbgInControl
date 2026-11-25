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


def reminder_where(address: str, meeting_type: str, which: str, lang: str) -> str:
    """The {where} value of the reminder templates. Their Spanish text reads "pasa por {where}" (24h) / "estará en {where}" (2h),
    so a phone call needs its own wording there."""
    if meeting_type == "call" and lang == "es":
        return "el teléfono (te llamará)" if which == "24h" else "contacto por teléfono"
    return where_phrase(address, meeting_type, lang)


def where_phrase(address: str, meeting_type: str, lang: str) -> str:
    """Where the meeting happens, ready for a sentence."""
    if meeting_type == "call":
        return {"es": "por teléfono", "fr": "par téléphone", "en": "by phone"}[lang]
    if address:
        return {"es": address, "fr": f"à {address}", "en": f"at {address}"}[lang]
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
    # cold owners (prospecting): never name the data provider, only what the owner can check themselves
    "cold_origin": {"es": "anuncio que el propietario publicó en un portal inmobiliario", "fr": "annonce publiée par le propriétaire sur un portail immobilier",
                    "en": "listing the owner posted on a property portal"},
    "your_property": {"es": "vivienda", "fr": "bien", "en": "property"},
    # the ONE answer to a soft no ("no quiero agencias"): an acknowledgement, then the agent's own pitch or this default
    "cold_soft_no_ack": {"es": "Entendido, gracias por contestarme.", "fr": "C'est noté, merci de m'avoir répondu.", "en": "Understood, thanks for getting back to me."},
    "cold_door_open": {"es": "Si al final por tu cuenta no sale como esperas, {agent_first} estará por aquí para ayudarte; solo tienes que escribirme.",
                       "fr": "Si finalement la vente par vous-même ne se passe pas comme prévu, {agent_first} reste disponible pour vous aider ; il suffit de m'écrire.",
                       "en": "If selling on your own doesn't go as planned, {agent_first} will be here to help; just write to me."},
    # a data-protection request or a threat: the only message they get before we stop for good
    "privacy_ack": {"es": "Entendido, no te escribiremos más. He pasado tu petición a {agent_first} para que la gestione.",
                    "fr": "C'est bien noté, nous ne vous écrirons plus. J'ai transmis votre demande à {agent_first}.",
                    "en": "Understood, we won't write to you again. I've passed your request to {agent_first}."},
    "why_cold_data": {"es": "pide ver o borrar sus datos (RGPD): respóndele tú antes de un mes",
                      "fr": "demande ses données ou leur effacement (RGPD) : répondez sous un mois",
                      "en": "asks for or to delete their data (GDPR): answer within one month"},
    "why_cold_threat": {"es": "propietario contactado en frío amenaza con denunciar; ya no le escribimos",
                        "fr": "propriétaire contacté à froid : menace de plainte, on ne lui écrit plus",
                        "en": "cold-contacted owner threatens to report us; we no longer write to them"},
    "alert_cold_template_rejected": {"es": "WhatsApp rechazó tu primer mensaje a propietarios ({reason}); cámbialo desde tu enlace de Sparrow",
                                     "fr": "WhatsApp a refusé votre premier message aux propriétaires ({reason}) ; modifiez-le depuis votre lien Sparrow",
                                     "en": "WhatsApp rejected your first message to owners ({reason}); change it from your Sparrow link"},
    # handoff reasons shown to the agent
    "why_tech_error": {"es": "error técnico del asistente", "fr": "erreur technique de l'assistant", "en": "technical error in the assistant"},
    "why_limit": {"es": "límite de mensajes del asistente (posible abuso)", "fr": "limite de messages de l'assistant (abus possible)", "en": "assistant message limit (possible abuse)"},
    "why_unsure": {"es": "el asistente no está seguro", "fr": "l'assistant n'est pas sûr", "en": "the assistant is not sure"},
    "why_first_failed": {"es": "No se pudo enviar el primer mensaje (¿número sin WhatsApp o fijo?)", "fr": "Le premier message n'a pas pu être envoyé (numéro sans WhatsApp ou fixe ?)", "en": "The first message could not be delivered (number without WhatsApp, or a landline?)"},
    "why_first_failed_tech": {"es": "No se pudo enviar el primer mensaje (fallo técnico: lo revisamos)", "fr": "Le premier message n'a pas pu être envoyé (problème technique : nous vérifions)", "en": "The first message could not be sent (technical problem: we are checking)"},
    "booked_fallback": {
        "es": "Perfecto {name}, tu cita con {agent} queda confirmada para el {when}. Si necesitas cambiarla, escríbeme por aquí.",
        "fr": "Parfait {name}, votre rendez-vous avec {agent} est confirmé le {when}. Pour le modifier, répondez simplement ici.",
        "en": "Perfect {name}, your appointment with {agent} is confirmed for {when}. If you need to change it, write to me here.",
    },
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


# Property types as prospecting stores them (Spanish words, or the provider's English key) → the lead's language
PROPERTY_TYPES = {
    "piso": {"es": "piso", "fr": "appartement", "en": "flat"},
    "apartment": {"es": "piso", "fr": "appartement", "en": "flat"},
    "casa": {"es": "casa", "fr": "maison", "en": "house"},
    "house": {"es": "casa", "fr": "maison", "en": "house"},
    "chalet": {"es": "chalet", "fr": "maison", "en": "house"},
    "estudio": {"es": "estudio", "fr": "studio", "en": "studio"},
    "studio": {"es": "estudio", "fr": "studio", "en": "studio"},
    "dúplex": {"es": "dúplex", "fr": "duplex", "en": "duplex"},
    "duplex": {"es": "dúplex", "fr": "duplex", "en": "duplex"},
    "ático": {"es": "ático", "fr": "appartement en attique", "en": "penthouse"},
    "penthouse": {"es": "ático", "fr": "appartement en attique", "en": "penthouse"},
}


def property_word(ptype: str | None, lang: str) -> str:
    """'piso' → 'piso' / 'appartement' / 'flat'; unknown or empty → 'vivienda' / 'bien' / 'property'
    (a raw provider word we don't know never reaches the owner)."""
    p = PROPERTY_TYPES.get((ptype or "").strip().lower())
    return p.get(lang, p["es"]) if p else text("your_property", lang)


def text(key: str, lang: str, **kw) -> str:
    s = TEXTS[key].get(lang) or TEXTS[key]["es"]
    return s.format(**kw) if kw else s

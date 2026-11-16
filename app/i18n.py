"""Agent-facing languages: Spanish (default), English and French. Leads in Spain always get Spanish."""
from fastapi import Request

LANGS = ("es", "en", "fr")
DEFAULT = "es"


def pick_lang(request: Request) -> str:
    """?lang=xx wins, then the cookie, then the browser's Accept-Language, then Spanish."""
    q = request.query_params.get("lang", "")
    if q in LANGS:
        return q
    c = request.cookies.get("lang", "")
    if c in LANGS:
        return c
    for part in (request.headers.get("accept-language") or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return DEFAULT


def remember(response, request: Request, lang: str) -> None:
    if request.query_params.get("lang") == lang or request.cookies.get("lang") != lang:
        response.set_cookie("lang", lang, max_age=31536000, samesite="lax")


LANDING = {
    "es": {"title": "Sparrow AI · Asistente de WhatsApp para inmobiliarias",
           "text": "Tu asistente de WhatsApp escribe a cada nuevo lead en 30 segundos y te agenda la valoración en tu Google Calendar. Solo pagas por cita agendada.",
           "cta": "Configurar mi asistente", "preparing": "Preparando tu asistente…",
           "privacy": "Privacidad", "terms": "Términos", "deletion": "Eliminación de datos"},
    "en": {"title": "Sparrow AI · WhatsApp assistant for real estate agencies",
           "text": "Your WhatsApp assistant messages every new lead within 30 seconds and books the valuation in your Google Calendar. You only pay per booked appointment.",
           "cta": "Set up my assistant", "preparing": "Preparing your assistant…",
           "privacy": "Privacy", "terms": "Terms", "deletion": "Data deletion"},
    "fr": {"title": "Sparrow AI · Assistant WhatsApp pour les agences immobilières",
           "text": "Votre assistant WhatsApp écrit à chaque nouveau prospect en 30 secondes et planifie l'estimation dans votre Google Agenda. Vous ne payez que par rendez-vous planifié.",
           "cta": "Configurer mon assistant", "preparing": "Préparation de votre assistant…",
           "privacy": "Confidentialité", "terms": "Conditions", "deletion": "Suppression des données"},
}
ONB_TITLE = {"es": "Configura tu asistente · Sparrow AI", "en": "Set up your assistant · Sparrow AI", "fr": "Configurez votre assistant · Sparrow AI"}

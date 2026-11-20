"""The agent-written cold first message (private owners found through prospecting).

The agent edits the body; Sparrow ALWAYS appends the footer (assistant disclosure, opt-out word, where the number
came from). The footer cannot be removed: it is what Meta, the GDPR information duty and the opt-out rule need.
Pure functions only: no database, no network.
"""
import re

MAX_BODY = 600
ALLOWED_FIELDS = ("nombre", "tipo", "zona", "agente", "agencia")  # {nombre} is the owner's first name or a neutral greeting

# {{n}} order used when the text goes to Meta as a template (fixed, so the example list is stable).
FIELD_ORDER = ALLOWED_FIELDS

DEFAULT_BODY = {
    "es": "Hola {nombre}, soy el asistente virtual de {agente} ({agencia}). Hemos visto tu anuncio de {tipo} en {zona}. ¿Sigues vendiéndolo por tu cuenta?",
    "fr": "Bonjour {nombre}, je suis l'assistant virtuel de {agente} ({agencia}). Nous avons vu votre annonce de {tipo} à {zona}. Vendez-vous toujours par vous-même ?",
    "en": "Hi {nombre}, I'm the virtual assistant of {agente} ({agencia}). We saw your listing for a {tipo} in {zona}. Are you still selling it yourself?",
}

# Appended after the agent's text. {privacy} is the public privacy-policy URL.
FOOTER = {
    "es": "Vi tu anuncio publicado en un portal inmobiliario. Si no quieres que te escriba más, responde BAJA. Más información: {privacy}",
    "fr": "J'ai vu votre annonce publiée sur un portail immobilier. Pour ne plus recevoir de messages, répondez STOP. Plus d'informations : {privacy}",
    "en": "I saw your listing published on a property portal. If you don't want me to write again, reply STOP. More information: {privacy}",
}

SAMPLE = {
    "es": {"nombre": "Marta", "tipo": "piso", "zona": "Zaragoza", "agente": "Laura", "agencia": "Inmobiliaria Sur"},
    "fr": {"nombre": "Marie", "tipo": "appartement", "zona": "Lyon", "agente": "Laura", "agencia": "Agence Sud"},
    "en": {"nombre": "Emma", "tipo": "flat", "zona": "Madrid", "agente": "Laura", "agencia": "South Realty"},
}

# Risks the onboarding chat must tell the agent about before they accept (the agent decides; we only warn).
RISKS = {
    "es": [
        "Meta puede rechazar el texto o bajar la calidad de tu número de WhatsApp si muchos propietarios lo bloquean o lo denuncian; en el peor caso, Meta limita o suspende tu número.",
        "Escribir a propietarios que no te conocen exige una base legal (por ejemplo interés legítimo) y respetar la baja inmediata; eres el responsable de esos datos y de ese texto.",
        "Los portales tienen sus propias condiciones de uso; tú respondes de tu acuerdo con ellos.",
        "Evita promesas de precio, urgencias falsas y cifras que no puedas demostrar.",
    ],
    "fr": [
        "Meta peut refuser le texte ou dégrader la qualité de votre numéro WhatsApp si beaucoup de propriétaires le bloquent ou le signalent ; au pire, Meta limite ou suspend votre numéro.",
        "Écrire à des propriétaires qui ne vous connaissent pas exige une base légale (par exemple l'intérêt légitime) et le respect immédiat du refus ; vous êtes responsable de ces données et de ce texte.",
        "Les portails ont leurs propres conditions d'utilisation ; vous répondez de votre accord avec eux.",
        "Évitez les promesses de prix, les fausses urgences et les chiffres que vous ne pouvez pas prouver.",
    ],
    "en": [
        "Meta can reject the text or lower your WhatsApp number's quality if many owners block or report it; in the worst case Meta limits or suspends your number.",
        "Writing to owners who don't know you needs a legal basis (for example legitimate interest) and an immediate opt-out; you are responsible for that data and that text.",
        "Portals have their own terms of use; you answer for your agreement with them.",
        "Avoid price promises, false urgency and figures you can't prove.",
    ],
}

_FIELD_RE = re.compile(r"\{(\w+)\}")
_URL_RE = re.compile(r"(https?://|www\.)", re.I)
_PROMISE_RE = re.compile(r"\b(garantiz\w+|100\s?%|gratis|sin compromiso y sin coste|garanti\w+|gratuit\w*|guarantee\w*|free)\b", re.I)


def default_body(lang: str = "es") -> str:
    return DEFAULT_BODY.get(lang, DEFAULT_BODY["es"])


def footer(lang: str, privacy_url: str) -> str:
    return FOOTER.get(lang, FOOTER["es"]).format(privacy=privacy_url)


def validate(body: str) -> tuple[list[str], list[str]]:
    """Return (problems, warnings). Problems block saving; warnings are shown to the agent, who decides."""
    body = (body or "").strip()
    problems: list[str] = []
    warnings: list[str] = []
    if len(body) < 20:
        problems.append("too_short")
    if len(body) > MAX_BODY:
        problems.append("too_long")
    bad = sorted({f for f in _FIELD_RE.findall(body) if f not in ALLOWED_FIELDS})
    if bad:
        problems.append("unknown_field:" + ",".join(bad))
    if _URL_RE.search(body):
        problems.append("link_not_allowed")  # our footer carries the only link; links in cold texts get templates rejected
    if body[:1] == "{" or body[-1:] == "}":
        problems.append("starts_or_ends_with_field")  # Meta rejects bodies that start or end with a variable
    if _PROMISE_RE.search(body):
        warnings.append("promise_words")
    letters = [c for c in body if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.4:
        warnings.append("shouting")
    return problems, warnings


def full_text(body: str, lang: str, privacy_url: str) -> str:
    """Agent text + the mandatory footer (still with {fields})."""
    return f"{body.strip()}\n\n{footer(lang, privacy_url)}"


def to_template_body(body: str, lang: str, privacy_url: str) -> tuple[str, list[str]]:
    """Turn the agent text + footer into a Meta template body with {{1}}.. in FIELD_ORDER, plus the example values.
    Only fields actually used get a number (Meta needs every variable to have an example)."""
    text = full_text(body, lang, privacy_url)
    used = [f for f in FIELD_ORDER if "{%s}" % f in text]
    for i, f in enumerate(used, start=1):
        text = text.replace("{%s}" % f, "{{%d}}" % i)
    return text, [SAMPLE.get(lang, SAMPLE["es"])[f] for f in used]


def used_fields(body: str) -> list[str]:
    text = body
    return [f for f in FIELD_ORDER if "{%s}" % f in text]


def render(body: str, lang: str, privacy_url: str, values: dict) -> str:
    """What the owner actually reads: agent text + footer with the real values (missing nombre -> neutral greeting)."""
    vals = dict(values)
    if not vals.get("nombre"):
        vals["nombre"] = {"es": "buenas", "fr": "bonjour", "en": "there"}.get(lang, "buenas")
    text = full_text(body, lang, privacy_url)
    for k, v in vals.items():
        text = text.replace("{%s}" % k, str(v))
    return text

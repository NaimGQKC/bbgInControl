"""ALL WhatsApp template names and texts live here (they will be resubmitted under our own Meta account later).

Rules Meta applies: variables are {{1}}, {{2}}... in order, a body can't start or end with a variable,
and every variable needs an example. Change the version suffix when you change a text.
"""
from dataclasses import dataclass, field

from .lang import TEXTS as _TEXTS


@dataclass(frozen=True)
class Template:
    """`body`/`example` are the Spanish version (the original). `texts` holds the other languages: {"fr": (body, example), ...}.
    The same template name exists in each language at Meta (a translation), so names do not change."""
    name: str
    category: str  # MARKETING | UTILITY
    body: str
    params: tuple[str, ...]  # meaning of {{1}}, {{2}}...
    example: tuple[str, ...]
    number: str = "agent"  # "agent" = sent from the agent's number, "sparrow" = from the Sparrow number
    texts: dict = field(default_factory=dict, hash=False, compare=False)

    def body_for(self, lang: str = "es") -> str:
        return self.texts.get(lang, (self.body, self.example))[0]

    def example_for(self, lang: str = "es") -> tuple[str, ...]:
        return self.texts.get(lang, (self.body, self.example))[1]

    def render(self, values: list[str], lang: str = "es") -> str:
        text = self.body_for(lang)
        for i, v in enumerate(values, start=1):
            text = text.replace("{{%d}}" % i, str(v))
        return text

    def components_for_submit(self, lang: str = "es") -> list[dict]:
        return [{"type": "BODY", "text": self.body_for(lang), "example": {"body_text": [list(self.example_for(lang))]}}]


LEAD_FIRST_CONTACT = Template(
    name="lead_first_contact_v2",
    category="MARKETING",
    body=(
        "Hola {{1}}, soy el asistente virtual de {{2}} ({{3}}). Vi tu anuncio en {{4}} y puedo prepararte "
        "una valoración gratuita. ¿Lo tienes reformado, en buen estado o para actualizar?\n\n"
        "Si no quieres más mensajes, responde BAJA."
    ),
    params=("lead_name", "agent_first_name", "agency", "zone"),
    example=("Marta", "Laura", "RE/MAX Zaragoza", "Delicias"),
    texts={
        "fr": ("Bonjour {{1}}, je suis l'assistant virtuel de {{2}} ({{3}}). J'ai vu votre annonce à {{4}} et je peux vous préparer "
               "une estimation gratuite. Votre bien est-il rénové, en bon état ou à rafraîchir ?\n\n"
               "Si vous ne souhaitez plus recevoir de messages, répondez STOP.", ("Marie", "Laura", "Agence Soleil", "Lyon 3e")),
        "en": ("Hi {{1}}, I'm the virtual assistant of {{2}} ({{3}}). I saw your listing in {{4}} and I can prepare "
               "a free valuation for you. Is the property renovated, in good condition or in need of updating?\n\n"
               "If you'd rather not receive more messages, reply STOP.", ("Marta", "Laura", "Sunrise Realty", "Delicias")),
    },
)

LEAD_FOLLOWUP_1 = Template(
    name="lead_followup_1_v1",
    category="MARKETING",
    body="Hola {{1}}, sigo por aquí para la valoración de tu vivienda en {{2}}. ¿Te viene mejor esta semana o la próxima?",
    params=("lead_name", "zone"),
    example=("Marta", "Delicias"),
    texts={
        "fr": ("Bonjour {{1}}, je reste à votre disposition pour l'estimation de votre bien à {{2}}. Cette semaine ou la semaine prochaine, qu'est-ce qui vous arrange le mieux ?", ("Marie", "Lyon 3e")),
        "en": ("Hi {{1}}, I'm still here for the valuation of your property in {{2}}. Does this week or next week suit you better?", ("Marta", "Delicias")),
    },
)

LEAD_FOLLOWUP_2 = Template(
    name="lead_followup_2_v1",
    category="MARKETING",
    body=(
        "Hola {{1}}, ¿sigue en pie lo de valorar tu vivienda? Si ya no lo necesitas, sin problema: "
        "respóndeme \"no\" y no te escribo más."
    ),
    params=("lead_name",),
    example=("Marta",),
    texts={
        "fr": ("Bonjour {{1}}, souhaitez-vous toujours faire estimer votre bien ? Si ce n'est plus le cas, aucun souci : répondez « non » et je ne vous écrirai plus.", ("Marie",)),
        "en": ("Hi {{1}}, do you still want your property valued? If not, no problem: reply \"no\" and I won't write to you again.", ("Marta",)),
    },
)

MEETING_CONFIRMATION = Template(
    name="meeting_confirmation_v1",
    category="UTILITY",
    body="Hola {{1}}, tu cita de valoración con {{2}} queda confirmada para el {{3}} en {{4}}. Si necesitas cambiarla, escríbeme por aquí.",
    params=("lead_name", "agent_first_name", "when", "where"),
    example=("Marta", "Laura", "martes 30 a las 18:00", "Calle Delicias 12"),
    texts={
        "fr": ("Bonjour {{1}}, votre rendez-vous d'estimation avec {{2}} est confirmé le {{3}}, {{4}}. Pour le modifier, répondez simplement ici.", ("Marie", "Laura", "mardi 30 à 18h00", "à 12 rue Delicias")),
        "en": ("Hi {{1}}, your valuation appointment with {{2}} is confirmed for {{3}}, {{4}}. If you need to change it, write to me here.", ("Marta", "Laura", "Tuesday 30 at 18:00", "at 12 Delicias Street")),
    },
)

MEETING_REMINDER_24H = Template(
    name="meeting_reminder_24h_v1",
    category="UTILITY",
    body="Hola {{1}}, te recuerdo que mañana a las {{2}} {{3}} pasa por {{4}} para la valoración. ¿Todo bien con esa hora?",
    params=("lead_name", "time", "agent_first_name", "where"),
    example=("Marta", "18:00", "Laura", "Calle Delicias 12"),
    texts={
        "fr": ("Bonjour {{1}}, petit rappel : demain à {{2}}, {{3}} vous retrouvera {{4}} pour l'estimation. Cet horaire vous convient-il toujours ?", ("Marie", "18h00", "Laura", "à 12 rue Delicias")),
        "en": ("Hi {{1}}, a reminder: tomorrow at {{2}}, {{3}} will meet you {{4}} for the valuation. Does that time still suit you?", ("Marta", "18:00", "Laura", "at 12 Delicias Street")),
    },
)

MEETING_REMINDER_2H = Template(
    name="meeting_reminder_2h_v1",
    category="UTILITY",
    body="Hola {{1}}, {{2}} estará en {{3}} a las {{4}} para la valoración. Cualquier cambio, escríbeme por aquí.",
    params=("lead_name", "agent_first_name", "where", "time"),
    example=("Marta", "Laura", "Calle Delicias 12", "18:00"),
    texts={
        "fr": ("Bonjour {{1}}, {{2}} vous retrouvera {{3}} à {{4}} pour l'estimation. Pour tout changement, répondez ici.", ("Marie", "Laura", "à 12 rue Delicias", "18h00")),
        "en": ("Hi {{1}}, {{2}} will meet you {{3}} at {{4}} for the valuation. For any change, write to me here.", ("Marta", "Laura", "at 12 Delicias Street", "18:00")),
    },
)

# --- Sparrow number (alerts to the agent; in the agent's language) ---
AGENT_NEEDS_YOU = Template(
    name="agent_needs_you_v1",
    category="UTILITY",
    body="Hola {{1}}, un lead te necesita: {{2}}. Motivo: {{3}}. Ábrelo en tu WhatsApp Business.",
    params=("agent_first_name", "lead_summary", "reason"),
    example=("Laura", "Marta · piso Delicias · vende por traslado", "pregunta por la comisión"),
    number="sparrow",
    texts={
        "fr": ("Bonjour {{1}}, un prospect a besoin de vous : {{2}}. Motif : {{3}}. Ouvrez la conversation dans votre WhatsApp Business.", ("Laura", "Marie · appartement Lyon 3e · vend pour mutation", "demande les honoraires")),
        "en": ("Hi {{1}}, a lead needs you: {{2}}. Reason: {{3}}. Open it in your WhatsApp Business.", ("Laura", "Marta · flat Delicias · selling due to relocation", "asks about the commission")),
    },
)

AGENT_ACTION_REQUIRED = Template(
    name="agent_action_required_v1",
    category="UTILITY",
    body="Hola {{1}}, tu asistente de Sparrow necesita que revises algo: {{2}}. Gracias.",
    params=("agent_first_name", "what"),
    example=("Laura", "reconecta tu Google Calendar"),
    number="sparrow",
    texts={
        "fr": ("Bonjour {{1}}, votre assistant Sparrow a besoin que vous vérifiiez quelque chose : {{2}}. Merci.", ("Laura", "reconnectez votre Google Agenda")),
        "en": ("Hi {{1}}, your Sparrow assistant needs you to check something: {{2}}. Thanks.", ("Laura", "reconnect your Google Calendar")),
    },
)

AGENT_PHONE_INACTIVE = Template(
    name="agent_phone_inactive_v1",
    category="UTILITY",
    body="Hola {{1}}, abre WhatsApp Business en tu móvil hoy: si pasa 14 días sin abrirse, tu asistente se desconecta.",
    params=("agent_first_name",),
    example=("Laura",),
    number="sparrow",
    texts={
        "fr": ("Bonjour {{1}}, ouvrez WhatsApp Business sur votre mobile aujourd'hui : si l'application reste 14 jours sans être ouverte, votre assistant se déconnecte.", ("Laura",)),
        "en": ("Hi {{1}}, open WhatsApp Business on your phone today: if it stays unopened for 14 days, your assistant disconnects.", ("Laura",)),
    },
)

AGENT_ONBOARDING_NUDGE = Template(
    name="agent_onboarding_nudge_v1",
    category="UTILITY",
    body="Hola {{1}}, tu asistente de Sparrow está casi listo. Puedes terminarlo aquí: {{2}} Gracias.",
    params=("agent_first_name", "link"),
    example=("Laura", "https://sparrow.ai/o/abc"),
    number="sparrow",
    texts={
        "fr": ("Bonjour {{1}}, votre assistant Sparrow est presque prêt. Vous pouvez terminer ici : {{2}} Merci.", ("Laura", "https://sparrow.ai/o/abc")),
        "en": ("Hi {{1}}, your Sparrow assistant is almost ready. You can finish here: {{2}} Thanks.", ("Laura", "https://sparrow.ai/o/abc")),
    },
)

AGENT_LIVE = Template(
    name="agent_live_v1",
    category="UTILITY",
    body="Hola {{1}}, tu asistente de Sparrow ya está activo ✅ Desde ahora, cada lead nuevo recibe un WhatsApp en 30 segundos y las citas aparecen en tu Google Calendar. Gracias.",
    params=("agent_first_name",),
    example=("Laura",),
    number="sparrow",
    texts={
        "fr": ("Bonjour {{1}}, votre assistant Sparrow est actif ✅ Désormais, chaque nouveau prospect reçoit un WhatsApp en 30 secondes et les rendez-vous apparaissent dans votre Google Agenda. Merci.", ("Laura",)),
        "en": ("Hi {{1}}, your Sparrow assistant is now active ✅ From now on, every new lead gets a WhatsApp within 30 seconds and appointments appear in your Google Calendar. Thanks.", ("Laura",)),
    },
)

# To you (the operator), not to agents: always Spanish
OPS_NEEDS_HELP = Template(
    name="ops_needs_help_v1",
    category="UTILITY",
    body="Hola, {{1}} necesita ayuda con la configuración de Sparrow (paso: {{2}}). Contacto: {{3}}. Mensaje: {{4}}. Gracias.",
    params=("agent", "step", "contact", "message"),
    example=("Laura García", "whatsapp", "+34611111111", "no me sale el código QR"),
    number="sparrow",
)

AGENT_TEMPLATES = [
    LEAD_FIRST_CONTACT,
    LEAD_FOLLOWUP_1,
    LEAD_FOLLOWUP_2,
    MEETING_CONFIRMATION,
    MEETING_REMINDER_24H,
    MEETING_REMINDER_2H,
]
SPARROW_TEMPLATES = [AGENT_NEEDS_YOU, AGENT_ACTION_REQUIRED, AGENT_PHONE_INACTIVE, AGENT_ONBOARDING_NUDGE, AGENT_LIVE, OPS_NEEDS_HELP]
ALL = {t.name: t for t in AGENT_TEMPLATES + SPARROW_TEMPLATES}

# Free-form texts (only inside the 24h window), in the lead's language: see app/lang.py (`text(key, lang)`).
# Spanish versions kept under their old names for anything that still imports them.
OPT_OUT_CONFIRMATION = _TEXTS["opt_out_confirmation"]["es"]
HANDOFF_TO_LEAD = _TEXTS["handoff_to_lead"]["es"]
VOICE_NOT_SUPPORTED = _TEXTS["voice_not_supported"]["es"]

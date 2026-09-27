"""ALL WhatsApp template names and texts live here (they will be resubmitted under our own Meta account later).

Rules Meta applies: variables are {{1}}, {{2}}... in order, a body can't start or end with a variable,
and every variable needs an example. Change the version suffix when you change a text.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    name: str
    category: str  # MARKETING | UTILITY
    body: str
    params: tuple[str, ...]  # meaning of {{1}}, {{2}}...
    example: tuple[str, ...]
    number: str = "agent"  # "agent" = sent from the agent's number, "sparrow" = from the Sparrow number

    def render(self, values: list[str]) -> str:
        text = self.body
        for i, v in enumerate(values, start=1):
            text = text.replace("{{%d}}" % i, str(v))
        return text

    def components_for_submit(self) -> list[dict]:
        return [{"type": "BODY", "text": self.body, "example": {"body_text": [list(self.example)]}}]


LEAD_FIRST_CONTACT = Template(
    name="lead_first_contact_v1",
    category="MARKETING",
    body=(
        "Hola {{1}}, soy el asistente virtual de {{2}} ({{3}}). Te escribo por tu vivienda en {{4}}: "
        "{{5}} puede prepararte una valoración gratuita y ajustada al mercado. Para afinarla, "
        "¿dirías que está reformada, en buen estado o para actualizar?\n\n"
        "Si prefieres no recibir más mensajes, responde BAJA."
    ),
    params=("lead_name", "agent_name", "agency", "zone", "agent_first_name"),
    example=("Marta", "Laura García", "RE/MAX Zaragoza", "Delicias", "Laura"),
)

LEAD_FOLLOWUP_1 = Template(
    name="lead_followup_1_v1",
    category="MARKETING",
    body="Hola {{1}}, sigo por aquí para la valoración de tu vivienda en {{2}}. ¿Te viene mejor esta semana o la próxima?",
    params=("lead_name", "zone"),
    example=("Marta", "Delicias"),
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
)

MEETING_CONFIRMATION = Template(
    name="meeting_confirmation_v1",
    category="UTILITY",
    body="Hola {{1}}, tu cita de valoración con {{2}} queda confirmada para el {{3}} en {{4}}. Si necesitas cambiarla, escríbeme por aquí.",
    params=("lead_name", "agent_first_name", "when", "where"),
    example=("Marta", "Laura", "martes 30 a las 18:00", "Calle Delicias 12"),
)

MEETING_REMINDER_24H = Template(
    name="meeting_reminder_24h_v1",
    category="UTILITY",
    body="Hola {{1}}, te recuerdo que mañana a las {{2}} {{3}} pasa por {{4}} para la valoración. ¿Todo bien con esa hora?",
    params=("lead_name", "time", "agent_first_name", "where"),
    example=("Marta", "18:00", "Laura", "Calle Delicias 12"),
)

MEETING_REMINDER_2H = Template(
    name="meeting_reminder_2h_v1",
    category="UTILITY",
    body="Hola {{1}}, {{2}} estará en {{3}} a las {{4}} para la valoración. Cualquier cambio, escríbeme por aquí.",
    params=("lead_name", "agent_first_name", "where", "time"),
    example=("Marta", "Laura", "Calle Delicias 12", "18:00"),
)

# --- Sparrow number (alerts to the agent) ---
AGENT_NEEDS_YOU = Template(
    name="agent_needs_you_v1",
    category="UTILITY",
    body="Hola {{1}}, un lead te necesita: {{2}}. Motivo: {{3}}. Ábrelo en tu WhatsApp Business.",
    params=("agent_first_name", "lead_summary", "reason"),
    example=("Laura", "Marta · piso Delicias · vende por traslado", "pregunta por la comisión"),
    number="sparrow",
)

AGENT_ACTION_REQUIRED = Template(
    name="agent_action_required_v1",
    category="UTILITY",
    body="Hola {{1}}, tu asistente de Sparrow necesita que revises algo: {{2}}. Gracias.",
    params=("agent_first_name", "what"),
    example=("Laura", "reconecta tu Google Calendar"),
    number="sparrow",
)

AGENT_PHONE_INACTIVE = Template(
    name="agent_phone_inactive_v1",
    category="UTILITY",
    body="Hola {{1}}, abre WhatsApp Business en tu móvil hoy: si pasa 14 días sin abrirse, tu asistente se desconecta.",
    params=("agent_first_name",),
    example=("Laura",),
    number="sparrow",
)

AGENT_ONBOARDING_NUDGE = Template(
    name="agent_onboarding_nudge_v1",
    category="UTILITY",
    body="Hola {{1}}, tu asistente de Sparrow está casi listo. Puedes terminarlo aquí: {{2}} Gracias.",
    params=("agent_first_name", "link"),
    example=("Laura", "https://sparrow.ai/o/abc"),
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
SPARROW_TEMPLATES = [AGENT_NEEDS_YOU, AGENT_ACTION_REQUIRED, AGENT_PHONE_INACTIVE, AGENT_ONBOARDING_NUDGE]
ALL = {t.name: t for t in AGENT_TEMPLATES + SPARROW_TEMPLATES}

# Free-form texts (only inside the 24h window)
OPT_OUT_CONFIRMATION = "Entendido, no te escribiremos más. Un saludo."
HANDOFF_TO_LEAD = "Perfecto, se lo paso a {agent_first_name} y te escribe en breve."
VOICE_NOT_SUPPORTED = "Perdona, ahora mismo no puedo escuchar audios. ¿Me lo puedes escribir?"

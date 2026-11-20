"""Intake adapters: each source turns its payload into the same standard lead dict.

Standard lead dict: name, phone, lead_type (seller|buyer|unknown), property_type, zone, address, price,
listing_url, stage, tags, source, source_ref.
"""

ALIASES = {
    "name": ["name", "nombre", "full_name", "contact_name", "propietario", "owner_name", "nom", "prenom", "prénom", "propriétaire", "proprietaire"],
    "phone": ["phone", "telefono", "teléfono", "movil", "móvil", "mobile", "phone_number", "tel", "tél", "telephone", "téléphone", "portable"],
    "lead_type": ["lead_type", "tipo_lead", "tipo_contacto", "operation"],
    "property_type": ["property_type", "tipo", "tipo_inmueble", "tipologia", "tipología", "type_de_bien", "type_bien", "bien"],
    "zone": ["zone", "zona", "barrio", "district", "localidad", "city", "ville", "quartier", "secteur"],
    "address": ["address", "direccion", "dirección", "adresse"],
    "price": ["price", "precio", "prix"],
    "listing_url": ["listing_url", "url", "anuncio", "link", "annonce", "lien"],
    "stage": ["stage", "etapa", "estado", "fase", "etape", "étape", "statut"],
    "tags": ["tags", "etiquetas", "labels", "étiquettes", "etiquettes"],
    "source_ref": ["source_ref", "id", "ref", "referencia", "lead_id"],
    "email": ["email", "correo", "courriel", "mail"],
}

BUYER_WORDS = {"buyer", "comprador", "compra", "demanda", "busca", "acheteur", "achat", "recherche"}
SELLER_WORDS = {"seller", "vendedor", "venta", "propietario", "captacion", "captación", "particular", "vendeur", "vente", "propriétaire", "proprietaire", "particulier", "estimation"}


def normalize(payload: dict, source: str) -> dict:
    """Generic adapter: accepts English or Spanish field names (Lystos exports, Zapier, forms...)."""
    low = {str(k).strip().lower(): v for k, v in (payload or {}).items()}
    out: dict = {"source": source}
    for field, keys in ALIASES.items():
        for k in keys:
            if low.get(k) not in (None, ""):
                out[field] = low[k]
                break
    lt = str(out.get("lead_type", "")).strip().lower()
    if lt in BUYER_WORDS:
        out["lead_type"] = "buyer"
    elif lt in SELLER_WORDS or not lt:
        out["lead_type"] = "seller"  # v1: leads are treated as sellers unless the source says buyer
    else:
        out["lead_type"] = "unknown"
    if isinstance(out.get("tags"), str):
        out["tags"] = [t.strip() for t in out["tags"].split(",") if t.strip()]
    if out.get("price") not in (None, ""):
        try:
            out["price"] = float(str(out["price"]).replace("€", "").replace("\u202f", "").replace("\xa0", "").replace(" ", "").replace(".", "").replace(",", ".").strip())
        except ValueError:
            out["price"] = None
    return out


def from_lystos(payload: dict) -> dict:
    """Placeholder until we see Lystos' real payload (design doc section 10). Uses the generic mapping."""
    return normalize(payload, "lystos")

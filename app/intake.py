"""Intake adapters: each source turns its payload into the same standard lead dict.

Standard lead dict: name, phone, lead_type (seller|buyer|unknown), property_type, zone, address, price,
listing_url, stage, tags, source, source_ref.
"""

ALIASES = {
    "name": ["name", "nombre", "full_name", "contact_name", "propietario", "owner_name"],
    "phone": ["phone", "telefono", "teléfono", "movil", "móvil", "mobile", "phone_number", "tel"],
    "lead_type": ["lead_type", "tipo_lead", "tipo_contacto", "operation"],
    "property_type": ["property_type", "tipo", "tipo_inmueble", "tipologia", "tipología"],
    "zone": ["zone", "zona", "barrio", "district", "localidad", "city"],
    "address": ["address", "direccion", "dirección"],
    "price": ["price", "precio"],
    "listing_url": ["listing_url", "url", "anuncio", "link"],
    "stage": ["stage", "etapa", "estado", "fase"],
    "tags": ["tags", "etiquetas", "labels"],
    "source_ref": ["source_ref", "id", "ref", "referencia", "lead_id"],
    "email": ["email", "correo"],
}

BUYER_WORDS = {"buyer", "comprador", "compra", "demanda", "busca"}
SELLER_WORDS = {"seller", "vendedor", "venta", "propietario", "captacion", "captación", "particular"}


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
            out["price"] = float(str(out["price"]).replace("€", "").replace(".", "").replace(",", ".").strip())
        except ValueError:
            out["price"] = None
    return out


def from_lystos(payload: dict) -> dict:
    """Placeholder until we see Lystos' real payload (design doc section 10). Uses the generic mapping."""
    return normalize(payload, "lystos")

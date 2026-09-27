"""Voice notes → text. Optional: without OPENAI_API_KEY the AI asks the lead to write instead."""
import logging

import httpx

from .config import get_settings

log = logging.getLogger("sparrow.transcribe")


def transcribe(audio: bytes, mime: str) -> str | None:
    s = get_settings()
    if s.whatsapp_mode == "fake":
        return "Es un piso de tres habitaciones, reformado hace dos años."
    ext = "ogg" if "ogg" in mime else mime.split("/")[-1]
    if s.azure_openai_endpoint and s.azure_openai_key and s.azure_transcribe_deployment:
        url = f"{s.azure_openai_endpoint.rstrip('/')}/openai/deployments/{s.azure_transcribe_deployment}/audio/transcriptions?api-version=2024-06-01"
        headers, data = {"api-key": s.azure_openai_key}, {"language": "es"}
    elif s.openai_api_key:
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers, data = {"Authorization": f"Bearer {s.openai_api_key}"}, {"model": "whisper-1", "language": "es"}
    else:
        return None
    try:
        r = httpx.post(url, headers=headers, data=data, files={"file": (f"audio.{ext}", audio, mime)}, timeout=60)
        r.raise_for_status()
        return (r.json().get("text") or "").strip() or None
    except Exception:
        log.exception("transcription failed")
        return None

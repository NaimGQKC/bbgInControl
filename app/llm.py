"""AI tool-use loop (Azure OpenAI / OpenAI / Claude, plus a scripted fake for tests). The engine gives it tools; code enforces the hard rules inside each tool."""
import json
import logging
from collections import deque
from typing import Callable

from .config import get_settings

log = logging.getLogger("sparrow.llm")

TOOLS = [
    {
        "name": "get_free_slots",
        "description": (
            "Devuelve huecos libres reales del calendario del agente para la valoración. "
            "Solo puedes ofrecer huecos que devuelva esta herramienta. Ofrece 2 al lead, no todos."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "book_slot",
        "description": (
            "Reserva un hueco cuando el lead ha aceptado uno concreto. Usa el slot_id exacto de get_free_slots. "
            "Pide antes la dirección exacta si es una visita."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot_id": {"type": "string"},
                "meeting_type": {"type": "string", "enum": ["visit", "call"]},
                "address": {"type": "string", "description": "Dirección de la vivienda (si es visita)"},
            },
            "required": ["slot_id", "meeting_type"],
        },
    },
    {
        "name": "handoff",
        "description": (
            "Pasa la conversación al agente humano. Úsalo si piden una persona, preguntan por comisiones, precio, "
            "temas legales/hipoteca/impuestos, negociación, se enfadan, es un comprador, quieren cambiar o cancelar "
            "una cita, o no estás seguro."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Motivo breve, en el idioma indicado en las instrucciones"}},
            "required": ["reason"],
        },
    },
    {
        "name": "stop",
        "description": (
            "Deja de escribir a este lead. opted_out = pide claramente que no le escribamos más; "
            "not_interested = no quiere valorar ahora."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "enum": ["opted_out", "not_interested"]}},
            "required": ["reason"],
        },
    },
    {
        "name": "save_facts",
        "description": "Guarda lo que has aprendido del lead (motivo, plazo, estado, ocupación, otra agencia, decisor, dirección...).",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "reason": {"type": "string"},
                "timeline": {"type": "string"},
                "occupancy": {"type": "string"},
                "decision_maker": {"type": "string"},
                "other_agency": {"type": "string"},
                "address": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    },
]


class RealBackend:
    def __init__(self):
        import anthropic

        s = get_settings()
        self.client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        self.model = s.anthropic_model

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        resp = self.client.messages.create(
            model=self.model, max_tokens=800, system=system, messages=messages, tools=tools
        )
        content = []
        for b in resp.content:
            if b.type == "text":
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return {"content": content, "stop_reason": resp.stop_reason}


class OpenAIBackend:
    """OpenAI chat completions, on Azure (credits) or openai.com. Our loop keeps messages in the Anthropic
    block format; this class translates both ways."""

    def __init__(self):
        from openai import OpenAI

        s = get_settings()
        if s.llm_provider == "azure_openai":
            if not (s.azure_openai_endpoint and s.azure_openai_key):
                raise RuntimeError("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_KEY missing")
            self.client = OpenAI(base_url=s.azure_openai_endpoint.rstrip("/") + "/openai/v1/", api_key=s.azure_openai_key,
                                 default_headers={"api-key": s.azure_openai_key}, timeout=25, max_retries=1)
            self.model = s.azure_openai_llm_deployment
        else:
            self.client = OpenAI(api_key=s.openai_api_key, timeout=25, max_retries=1)
            self.model = s.openai_model
        self.effort = s.llm_reasoning_effort
        self.max_out = s.llm_max_output_tokens

    @staticmethod
    def to_openai(system: str, messages: list[dict], tools: list[dict]) -> tuple[list[dict], list[dict]]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
                continue
            if m["role"] == "user":  # tool results
                for b in c:
                    if b["type"] == "tool_result":
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
                    elif b["type"] == "text":
                        out.append({"role": "user", "content": b["text"]})
            else:
                text = "\n".join(b["text"] for b in c if b["type"] == "text")
                calls = [
                    {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                    for b in c
                    if b["type"] == "tool_use"
                ]
                msg = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
        fns = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        return out, fns

    @staticmethod
    def from_openai(message) -> dict:
        content = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        for tc in message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        return {"content": content, "stop_reason": stop}

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        msgs, fns = self.to_openai(system, messages, tools)
        kwargs = {"model": self.model, "messages": msgs, "tools": fns, "max_completion_tokens": self.max_out}
        if self.effort:
            kwargs["reasoning_effort"] = self.effort
        resp = self.client.chat.completions.create(**kwargs)
        return self.from_openai(resp.choices[0].message)


class FakeBackend:
    """Scripted responses for tests. Each queued item is a list of steps:
    {"tool": name, "input": {...}} or {"text": "..."}; one item per model call."""

    queue: deque = deque()
    calls: list = []
    raise_error: Exception | None = None
    model = "fake"

    @classmethod
    def reset(cls):
        cls.queue.clear()
        cls.calls.clear()
        cls.raise_error = None

    @classmethod
    def push(cls, *steps: dict):
        cls.queue.append(list(steps))

    def complete(self, system, messages, tools):
        FakeBackend.calls.append({"system": system, "messages": json.loads(json.dumps(messages, default=str))})
        if FakeBackend.raise_error:
            raise FakeBackend.raise_error
        steps = FakeBackend.queue.popleft() if FakeBackend.queue else [{"text": "Perfecto. ¿Te propongo un par de horas para la valoración?"}]
        content = []
        for i, st in enumerate(steps):
            if "tool" in st:
                content.append({"type": "tool_use", "id": f"tu_{len(FakeBackend.calls)}_{i}", "name": st["tool"], "input": st.get("input", {})})
            else:
                content.append({"type": "text", "text": st["text"]})
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        return {"content": content, "stop_reason": stop}


def get_backend():
    s = get_settings()
    if s.llm_mode == "fake":
        return FakeBackend()
    return RealBackend() if s.llm_provider == "anthropic" else OpenAIBackend()


def run_tool_loop(
    system: str,
    messages: list[dict],
    execute: Callable[[str, dict], dict],
    *,
    max_steps: int = 6,
    stop_after: Callable[[], bool] = lambda: False,
) -> tuple[str, list[dict], str]:
    """Runs the model until it answers with text. Returns (final_text, transcript, model)."""
    backend = get_backend()
    transcript: list[dict] = []
    msgs = list(messages)
    final_text = ""
    for _ in range(max_steps):
        out = backend.complete(system, msgs, TOOLS)
        transcript.append({"assistant": out["content"]})
        texts = [c["text"] for c in out["content"] if c["type"] == "text"]
        tool_uses = [c for c in out["content"] if c["type"] == "tool_use"]
        if not tool_uses:
            final_text = "\n".join(t.strip() for t in texts if t.strip())
            break
        msgs.append({"role": "assistant", "content": out["content"]})
        results = []
        for tu in tool_uses:
            try:
                res = execute(tu["name"], tu.get("input") or {})
            except Exception as e:  # tools never crash the loop; the model sees the error
                log.exception("tool %s failed", tu["name"])
                res = {"ok": False, "error": str(e)}
            transcript.append({"tool": tu["name"], "input": tu.get("input"), "result": res})
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(res, ensure_ascii=False, default=str)})
        msgs.append({"role": "user", "content": results})
        if stop_after():
            # A terminal tool (handoff / stop) ran: the engine sends its own fixed text.
            final_text = "\n".join(t.strip() for t in texts if t.strip())
            break
    else:
        raise RuntimeError("LLM exceeded max tool steps")
    return final_text, transcript, getattr(backend, "model", "")

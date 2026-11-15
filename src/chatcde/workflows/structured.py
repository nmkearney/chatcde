"""One structured (JSON-schema) request to the agent model, shared by the workflows.

``chatcde gaps`` and ``chatcde experts`` both end with a single request: a
versioned system prompt, the evidence as JSON, and a reply that must follow a
pydantic model. The plumbing lives here so both do it the same way.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from chatcde.agent import AgentError, load_prompt
from chatcde.tools.context import ToolContext


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """``model`` as a strict JSON schema: no titles or ``$ref``, all fields required."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return clean(defs[node["$ref"].rsplit("/", 1)[-1]])
            out = {k: clean(v) for k, v in node.items() if k != "title"}
            if out.get("type") == "object":
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}))
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return clean(schema)


def parse_reply[M: BaseModel](content: str, model: type[M]) -> M:
    """Read a JSON reply into ``model`` (tolerating a Markdown code fence).

    Raises:
        pydantic.ValidationError: The text is not valid JSON for ``model``.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return model.model_validate_json(text)


def request_structured[M: BaseModel](
    ctx: ToolContext,
    *,
    model_name: str,
    prompt: str,
    reply_model: type[M],
    schema_name: str,
    evidence: dict[str, Any],
    temperature: float,
    max_tokens: int,
    structured_output: bool,
    tokens_setting: str,
    reply_label: str = "reply",
) -> tuple[M, dict[str, int]]:
    """Send one request and return the parsed reply with the token usage.

    The prompt file ``<prompts.dir>/<prompt>.md`` may hold ``{schema}``, which is
    replaced by the reply schema. With ``structured_output`` the schema is also
    sent as ``response_format``.

    Args:
        ctx: Tool context (config and endpoint client).
        model_name: The model to ask.
        prompt: Prompt name, e.g. ``gaps.v2``.
        reply_model: The pydantic model the reply must follow.
        schema_name: Name sent with the JSON schema.
        evidence: The user message, sent as JSON.
        temperature: Sampling temperature.
        max_tokens: Reply budget.
        structured_output: Send the schema as ``response_format``.
        tokens_setting: Config key to name when the reply is cut off.
        reply_label: What the reply is, for the error message (``judgement``).

    Raises:
        AgentError: The reply is missing, cut off, not JSON or not valid.
        chatcde.store.endpoint.EndpointError: The request fails.
    """
    cfg = ctx.cfg
    schema = strict_schema(reply_model)
    system = load_prompt(cfg.prompts.dir, prompt).replace(
        "{schema}", json.dumps(schema, ensure_ascii=False)
    )
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(evidence, ensure_ascii=False, indent=1),
            },
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        **cfg.agent.extra_body,
    }
    if structured_output:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
    body = ctx.endpoint().post_json("/chat/completions", payload)
    choices = body.get("choices") or []
    message = choices[0].get("message") if choices else None
    if not isinstance(message, dict) or not message.get("content"):
        raise AgentError(f"no answer in the reply: {str(body)[:200]}")
    if choices[0].get("finish_reason") == "length":
        raise AgentError(f"the reply was cut off; raise {tokens_setting}")
    try:
        parsed = parse_reply(message["content"], reply_model)
    except ValidationError as exc:
        raise AgentError(f"the reply is not a valid {reply_label}: {exc}") from exc
    usage = {k: v for k, v in (body.get("usage") or {}).items() if isinstance(v, int)}
    return parsed, usage

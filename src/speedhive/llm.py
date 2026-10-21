"""LLM client (Anthropic Claude) used by the track-record parsers in
speedhive.utils.llm_track_records.

Config is env-var based -- ANTHROPIC_API_KEY / ANTHROPIC_MODEL, or their
per-org variants ANTHROPIC_API_KEY_<org_id> / ANTHROPIC_MODEL_<org_id> when an
org_id is given, falling back to the bare name as a shared default. Resolution
goes through speedhive.settings, which any process (a CLI invocation or a host
application) resolves the same way. The model defaults to DEFAULT_MODEL when
nothing is configured.
"""
import copy
import json
import os
from typing import Any, List, Optional

import anthropic

from speedhive.settings import get_org_env_var

DEFAULT_MODEL = "claude-haiku-5-5"

# Extraction is a classification-shaped task; low effort keeps adaptive
# thinking short and the per-scan cost down.
DEFAULT_EFFORT = "low"


def get_anthropic_api_key(org_id: Optional[int] = None) -> Optional[str]:
    if org_id is not None:
        return get_org_env_var("ANTHROPIC_API_KEY", org_id)
    return os.environ.get("ANTHROPIC_API_KEY")


def get_anthropic_model(org_id: Optional[int] = None) -> str:
    if org_id is not None:
        configured = get_org_env_var("ANTHROPIC_MODEL", org_id)
    else:
        configured = os.environ.get("ANTHROPIC_MODEL")
    return configured or DEFAULT_MODEL


def _strict_schema(schema: dict) -> dict:
    """Claude structured outputs require `additionalProperties: false` on every
    object. The shared schemas in llm_track_records stay provider-neutral, so
    the adaptation happens here on a copy."""
    schema = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def call_claude_json(
    prompt: str,
    response_schema: Optional[dict] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    timeout_s: Optional[float] = None,
    effort: str = DEFAULT_EFFORT,
    org_id: Optional[int] = None,
) -> Any:
    """Call Claude and return the parsed JSON response.

    Raises RuntimeError if no API key is configured or the response is not
    usable (refusal, truncation), or the underlying anthropic SDK exception if
    the call itself fails.
    """
    key = api_key or get_anthropic_api_key(org_id)
    if not key:
        suffix = f" (or ANTHROPIC_API_KEY_{org_id})" if org_id is not None else ""
        raise RuntimeError(
            f"No Anthropic API key configured. Set the ANTHROPIC_API_KEY{suffix} environment variable."
        )

    model_name = model or get_anthropic_model(org_id)

    client_kwargs: dict = {"api_key": key}
    if timeout_s:
        client_kwargs["timeout"] = timeout_s
    client = anthropic.Anthropic(**client_kwargs)

    output_config: dict = {"effort": effort}
    if response_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": _strict_schema(response_schema)}
    else:
        prompt = f"{prompt}\n\nRespond with a single JSON value and nothing else."

    # Streamed so a large max_tokens (the bulk path) never trips the SDK's
    # long-request guard or an idle-connection timeout.
    with client.messages.stream(
        model=model_name,
        max_tokens=max_output_tokens or 16000,
        output_config=output_config,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request ({model_name}).")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Claude response truncated at max_tokens={max_output_tokens or 16000} ({model_name})."
        )

    text = "".join(block.text for block in message.content if block.type == "text")
    return json.loads(text)


def parse_track_record_text_with_claude(text: str, org_id: Optional[int] = None) -> Optional[dict]:
    """Drop-in replacement for speedhive.utils.lap_analysis.parse_track_record_text,
    backed by the configured Claude model instead of a regex."""
    from speedhive.utils.llm_track_records import parse_track_record_text_llm

    def _call(prompt: str, schema: dict) -> Any:
        return call_claude_json(prompt, response_schema=schema, org_id=org_id)

    return parse_track_record_text_llm(text, _call)


def parse_track_records_bulk_with_claude(texts: List[str], org_id: Optional[int] = None) -> List[Optional[dict]]:
    """Drop-in bulk replacement: parses an entire list of announcement texts
    in a single Claude call instead of one call per announcement. Returns a
    list aligned with `texts` (record dict or None per position)."""
    from speedhive.utils.llm_track_records import parse_track_record_texts_llm_bulk

    def _call(prompt: str, schema: dict) -> Any:
        # A single call covering hundreds/thousands of announcements needs
        # more room (both to generate and to respond) than the per-item path.
        return call_claude_json(
            prompt,
            response_schema=schema,
            max_output_tokens=65536,
            timeout_s=600.0,
            org_id=org_id,
        )

    return parse_track_record_texts_llm_bulk(texts, _call)

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


SENTINEL = "data not available"


@dataclass
class PropagationExample:
    annotation_id: str
    document_id: str
    code: str
    value: str
    exact: str = ""
    prefix: str = ""
    suffix: str = ""


@dataclass
class PropagationTarget:
    document_id: str
    canonical_url: str
    content_text: str


_WS_RE = re.compile(r"\s+")


def quote_norm(value: str) -> str:
    return _WS_RE.sub(" ", (value or "").strip()).lower()


def openai_output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts)


def clean_openai_values(values: Any, content_text: str, *, max_doc_chars: int) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    doc_norm = quote_norm(content_text[:max_doc_chars])
    for value in values or []:
        s = str(value or "").strip()
        norm = quote_norm(s)
        if not s or norm in {"", SENTINEL}:
            continue
        if norm not in doc_norm:
            continue
        if s in seen:
            continue
        seen.add(s)
        clean.append(s)
    return clean


def extract_values_with_openai(
    *,
    api_key: str,
    model: str,
    code: str,
    examples: list[PropagationExample],
    target: PropagationTarget,
    max_doc_chars: int,
    timeout: int,
    post: Callable[..., Any] = requests.post,
) -> list[str]:
    example_lines: list[str] = []
    for i, ex in enumerate(examples, start=1):
        context = " ".join(part.strip() for part in [ex.prefix, ex.exact, ex.suffix] if part and part.strip())
        example_lines.append(
            f"Example {i}\n"
            f"Code: {code}\n"
            f"Accepted value: {ex.value}\n"
            f"Highlighted text/context: {context[:1200]}"
        )

    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You propagate legal review tags from examples. Return JSON only. "
                    "Extract values for the requested code from the target judgment. "
                    "Every returned value must be copied exactly from the target judgment. "
                    "If the target does not contain evidence for the code, return an empty values array."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Propagate this Hypothesis field/tag: {code}\n\n"
                    "Reviewer-approved examples:\n\n"
                    + "\n\n".join(example_lines)
                    + "\n\nTARGET JUDGMENT:\n"
                    + target.content_text[:max_doc_chars]
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "propagated_values",
                "strict": True,
                "schema": schema,
            }
        },
    }

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if response.status_code in {408, 409, 429} or response.status_code >= 500:
                raise RuntimeError(f"OpenAI transient failure: {response.status_code} {response.text[:1000]}")
            if response.status_code >= 300:
                raise RuntimeError(f"OpenAI Responses API failed: {response.status_code} {response.text[:1000]}")
            obj = json.loads(openai_output_text(response.json()))
            return clean_openai_values(obj.get("values") or [], target.content_text, max_doc_chars=max_doc_chars)
        except Exception as exc:
            last_error = exc
            if attempt == 4:
                raise RuntimeError(f"OpenAI request failed after retries: {last_error}") from exc
            time.sleep(min(30.0, 2.0 ** attempt))

    return []


def stable_propagation_suggestion_id(project_id: str, document_id: str, code: str, value: str) -> str:
    raw = json.dumps(
        {
            "kind": "hypothesis_review_propagation",
            "project_id": str(project_id),
            "document_id": document_id,
            "code": code,
            "value": value,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_propagated_suggestion_payload(
    *,
    group_id: str,
    project_id: str,
    target: PropagationTarget,
    code: str,
    value: str,
    selector: dict | None,
    example_count: int,
    model: str,
) -> tuple[str, dict]:
    suggestion_id = stable_propagation_suggestion_id(project_id, target.document_id, code, value)
    tags = [
        f"field:{code}",
        "source:model_suggestion",
        "status:suggested",
        "propagation:hypothesis_review_examples",
        "anchored:quote" if selector else "anchored:none",
        "bot:hitl",
    ]
    payload = {
        "group": group_id,
        "uri": target.canonical_url,
        "text": "\n".join(
            [
                "[PROPAGATED MODEL SUGGESTION]",
                f"Code: {code}",
                f"Suggested value: {value}",
                "",
                "This suggestion was created from approved reviewer examples in this project.",
                "If this is correct, leave it unchanged. To reject or correct it, add your own review annotation "
                "or reply with review:reject / review:corrected in the project review group.",
            ]
        ),
        "tags": tags,
        "permissions": {"read": [f"group:{group_id}"]},
    }
    if selector:
        payload["target"] = [{"source": target.canonical_url, "selector": [selector]}]
    return suggestion_id, payload

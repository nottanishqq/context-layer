"""The generation side: a thin wrapper around the Claude Messages API.

Kept deliberately small. The context layer's job is deciding *what goes into*
the prompt; this module only sends it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import anthropic

from contextlayer.config import Settings


class RefusalError(RuntimeError):
    pass


class LLM(Protocol):
    """What the context layer needs from a model. Implement these three to swap providers
    (the tests' ScriptedLLM is a second implementation)."""

    def create(self, *, system: str, messages: list[dict[str, Any]],
               tools: list[dict[str, Any]] | None = None, max_tokens: int = 16000) -> Any:
        """One Messages API turn; the agent loop reads `.stop_reason` and `.content`."""
        ...

    def text(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str:
        """A single prompt in, plain text out (used by `context.answer`)."""
        ...

    def json(self, *, system: str, prompt: str, schema: dict[str, Any], max_tokens: int = 16000) -> dict[str, Any]:
        """A single prompt in, an object matching `schema` out (text-to-SurrealQL, reconciler)."""
        ...


class ClaudeLLM:
    def __init__(self, model: str | None = None, effort: str = "medium", client: anthropic.Anthropic | None = None):
        self.model = model or Settings().llm_model
        self.effort = effort
        self.client = client or anthropic.Anthropic()

    def create(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 16000,
        output_format: dict[str, Any] | None = None,
    ) -> Any:
        output_config: dict[str, Any] = {"effort": self.effort}
        if output_format:
            output_config["format"] = output_format
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config=output_config,
            # Cache the stable prefix (tools + system) across turns of an agent loop.
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, retry server-side on a suitable model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
        if response.stop_reason == "refusal":
            details = response.stop_details
            raise RefusalError(f"Model declined: {details.category if details else 'unknown'}")
        return response

    def text(self, *, system: str, prompt: str, max_tokens: int = 16000) -> str:
        response = self.create(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens)
        return "".join(b.text for b in response.content if b.type == "text")

    def json(self, *, system: str, prompt: str, schema: dict[str, Any], max_tokens: int = 16000) -> dict[str, Any]:
        response = self.create(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            output_format={"type": "json_schema", "schema": schema},
        )
        return json.loads(next(b.text for b in response.content if b.type == "text"))

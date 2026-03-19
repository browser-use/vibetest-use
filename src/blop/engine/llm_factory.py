"""LLM factory: supports google (Gemini), anthropic, and openai backends.

Usage:
    from blop.engine.llm_factory import make_planning_llm, make_agent_llm, make_message

    llm = make_planning_llm(temperature=0.3, max_output_tokens=2000)
    response = await llm.ainvoke([make_message(prompt)])
    text = str(response.content)
"""
from __future__ import annotations

import os
from typing import Any


def make_planning_llm(temperature: float = 0.3, max_output_tokens: int = 2000) -> Any:
    """Return a chat model for planning/classification/remediation calls."""
    provider = os.getenv("BLOP_LLM_PROVIDER", "google").lower()
    model_override = os.getenv("BLOP_LLM_MODEL", "")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # type: ignore[import-untyped]
        model = model_override or "claude-3-5-haiku-20241022"
        api_key = os.getenv("ANTHROPIC_API_KEY", "") or None
        return ChatAnthropic(model=model, api_key=api_key, temperature=temperature, max_tokens=max_output_tokens)

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # type: ignore[import-untyped]
        model = model_override or "gpt-4o-mini"
        api_key = os.getenv("OPENAI_API_KEY", "") or None
        return ChatOpenAI(model=model, api_key=api_key, temperature=temperature, max_tokens=max_output_tokens)

    # Default: Google Gemini via browser_use
    from browser_use.llm import ChatGoogle
    model = model_override or "gemini-2.5-flash"
    api_key = os.getenv("GOOGLE_API_KEY", "")
    return ChatGoogle(model=model, api_key=api_key, temperature=temperature, max_output_tokens=max_output_tokens)


def make_agent_llm() -> Any:
    """Return a chat model for use as the Browser-Use agent backbone."""
    provider = os.getenv("BLOP_LLM_PROVIDER", "google").lower()
    model_override = os.getenv("BLOP_LLM_MODEL", "")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # type: ignore[import-untyped]
        model = model_override or "claude-3-5-sonnet-20241022"
        api_key = os.getenv("ANTHROPIC_API_KEY", "") or None
        return ChatAnthropic(model=model, api_key=api_key, temperature=0.7)

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # type: ignore[import-untyped]
        model = model_override or "gpt-4o"
        api_key = os.getenv("OPENAI_API_KEY", "") or None
        return ChatOpenAI(model=model, api_key=api_key, temperature=0.7)

    # Default: Google Gemini
    from browser_use.llm import ChatGoogle
    model = model_override or "gemini-2.5-flash"
    api_key = os.getenv("GOOGLE_API_KEY", "")
    return ChatGoogle(model=model, api_key=api_key, temperature=0.7)


def make_message(prompt: str) -> Any:
    """Return the appropriate message type for the configured LLM provider.

    Google/browser_use uses UserMessage; anthropic/openai use HumanMessage.
    Falls back to UserMessage so existing google paths stay unchanged.
    """
    provider = os.getenv("BLOP_LLM_PROVIDER", "google").lower()
    if provider in ("anthropic", "openai"):
        from langchain_core.messages import HumanMessage
        return HumanMessage(content=prompt)
    # google (browser_use)
    from browser_use.llm.messages import UserMessage
    return UserMessage(content=prompt)

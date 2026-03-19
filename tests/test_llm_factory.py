"""Tests for make_planning_llm, make_agent_llm, and make_message from blop.engine.llm_factory."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from blop.engine import llm_factory


def test_planning_llm_google_default():
    """make_planning_llm with BLOP_LLM_PROVIDER=google calls ChatGoogle with model=gemini-2.5-flash."""
    mock_chat_google = MagicMock()
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "google", "GOOGLE_API_KEY": "test-key"}):
        with patch("browser_use.llm.ChatGoogle", mock_chat_google):
            llm_factory.make_planning_llm(temperature=0.3, max_output_tokens=2000)
    mock_chat_google.assert_called_once()
    call_kwargs = mock_chat_google.call_args[1]
    assert call_kwargs["model"] == "gemini-2.5-flash"
    assert call_kwargs["temperature"] == 0.3
    assert call_kwargs["max_output_tokens"] == 2000


def test_planning_llm_anthropic():
    """make_planning_llm with BLOP_LLM_PROVIDER=anthropic calls ChatAnthropic with model=claude-3-5-haiku-20241022."""
    mock_chat_anthropic = MagicMock()
    mock_anthropic_mod = MagicMock()
    mock_anthropic_mod.ChatAnthropic = mock_chat_anthropic
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "anthropic"}):
        with patch.dict(sys.modules, {"langchain_anthropic": mock_anthropic_mod}):
            llm_factory.make_planning_llm(temperature=0.3, max_output_tokens=2000)
    mock_chat_anthropic.assert_called_once()
    call_kwargs = mock_chat_anthropic.call_args[1]
    assert call_kwargs["model"] == "claude-3-5-haiku-20241022"
    assert call_kwargs["temperature"] == 0.3
    assert call_kwargs["max_tokens"] == 2000


def test_planning_llm_openai():
    """make_planning_llm with BLOP_LLM_PROVIDER=openai calls ChatOpenAI with model=gpt-4o-mini."""
    mock_chat_openai = MagicMock()
    mock_openai_mod = MagicMock()
    mock_openai_mod.ChatOpenAI = mock_chat_openai
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "openai"}):
        with patch.dict(sys.modules, {"langchain_openai": mock_openai_mod}):
            llm_factory.make_planning_llm(temperature=0.3, max_output_tokens=2000)
    mock_chat_openai.assert_called_once()
    call_kwargs = mock_chat_openai.call_args[1]
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert call_kwargs["max_tokens"] == 2000


def test_planning_llm_model_override():
    """make_planning_llm uses BLOP_LLM_MODEL override when set."""
    mock_chat_google = MagicMock()
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "google", "BLOP_LLM_MODEL": "custom-model", "GOOGLE_API_KEY": "key"}):
        with patch("browser_use.llm.ChatGoogle", mock_chat_google):
            llm_factory.make_planning_llm(temperature=0.3, max_output_tokens=2000)
    call_kwargs = mock_chat_google.call_args[1]
    assert call_kwargs["model"] == "custom-model"


def test_agent_llm_google():
    """make_agent_llm returns Google model by default."""
    mock_chat_google = MagicMock()
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "google", "GOOGLE_API_KEY": "key"}):
        with patch("browser_use.llm.ChatGoogle", mock_chat_google):
            llm_factory.make_agent_llm()
    mock_chat_google.assert_called_once()
    call_kwargs = mock_chat_google.call_args[1]
    assert call_kwargs["model"] == "gemini-2.5-flash"


def test_make_message_google():
    """make_message returns UserMessage for google provider."""
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "google"}):
        mock_user_message = MagicMock()
        with patch("browser_use.llm.messages.UserMessage", mock_user_message):
            msg = llm_factory.make_message("hello")
    mock_user_message.assert_called_once_with(content="hello")
    assert msg is mock_user_message.return_value


def test_make_message_anthropic():
    """make_message returns HumanMessage for anthropic provider."""
    with patch.dict(os.environ, {"BLOP_LLM_PROVIDER": "anthropic"}):
        mock_human_message = MagicMock()
        with patch("langchain_core.messages.HumanMessage", mock_human_message):
            msg = llm_factory.make_message("hello")
    mock_human_message.assert_called_once_with(content="hello")
    assert msg is mock_human_message.return_value

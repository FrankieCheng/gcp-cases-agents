"""Pytest configuration: tests run offline against the deterministic chat engine.

The Gemini-backed agent path (CHAT_ENGINE=llm, the production default) is validated with
`agents-cli eval run`, not pytest, because LLM output is non-deterministic.
"""

import os

os.environ.setdefault("CHAT_ENGINE", "deterministic")

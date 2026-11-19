"""$0 moment picker: route llm_backend's JSON calls to the local ``claude`` CLI.

``LLM_PROVIDER=claude_cli`` makes every text stage that goes through
``llm_backend.generate_json`` (window scoring, clip detail with hooks, titles
and descriptions, pro-mode titles, the stream scout) run headless on the
logged-in Claude subscription instead of an API key.

The prompt goes in on stdin (Windows caps a command line at ~32k chars; a
scoring batch is bigger). The call is stripped of tools, MCP servers, settings
and session files: without that the CLI loads the user's whole setup and each
call costs ~80k context tokens of quota instead of ~1.5k.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple, Type

from pydantic import BaseModel

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 600.0


def model_name() -> str:
    return (os.environ.get("CLAUDE_CLI_MODEL") or "").strip() or DEFAULT_MODEL


def binary() -> str:
    return (os.environ.get("CLAUDE_CLI_BIN") or "").strip() or shutil.which("claude") or "claude"


def command(schema: dict, model: Optional[str] = None) -> list:
    return [binary(), "-p", "--model", model or model_name(),
            "--permission-mode", "dontAsk", "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence",
            "--system-prompt", "You answer with a single JSON object that matches the schema."]


def parse_output(stdout: str) -> Tuple[dict, dict]:
    """(structured payload, usage) from ``--output-format json``; raises on error."""
    data = json.loads(stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude CLI error: {str(data.get('result'))[:300]}")
    payload = data.get("structured_output")
    if payload is None:  # older CLIs put the JSON text in "result"
        import gemini_worker
        payload = gemini_worker._parse_json_response_text(data.get("result") or "")
    return payload, data.get("usage") or {}


def generate_json(prompt: str, schema: Type[BaseModel], model: Optional[str] = None,
                  ) -> Tuple[dict, Optional[dict]]:
    """Same contract as ``llm_backend.generate_json``: (validated dict, cost)."""
    model = model or model_name()
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    # Neutral cwd so no project CLAUDE.md gets pulled into the call.
    proc = subprocess.run(command(schema.model_json_schema(), model),
                          input=prompt.encode("utf-8"), capture_output=True,
                          timeout=timeout, cwd=tempfile.gettempdir())
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0 and not out.strip():
        raise RuntimeError(f"claude CLI exit {proc.returncode}: "
                           f"{proc.stderr.decode('utf-8', 'replace')[:300]}")
    payload, usage = parse_output(out)
    validated = schema.model_validate(payload).model_dump()
    cost = {
        "input_tokens": int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "thinking_tokens": 0, "input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0,
        "model": f"claude-cli:{model}", "price_estimated": False, "local": True,
    }
    return validated, cost

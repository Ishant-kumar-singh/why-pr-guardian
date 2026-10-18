"""Environment-backed configuration for Why?.

Every value comes from the environment (optionally loaded from a local .env).
No secrets are hard-coded anywhere in the source tree.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
STATIC_DIR = BASE_DIR / "static"

# --- GitHub ---------------------------------------------------------------
GITHUB_TOKEN: str | None = os.getenv("GITHUB_TOKEN") or None
GITHUB_API = "https://api.github.com"
REPO: str = os.getenv("REPO", "psf/requests")
GUARD_REPO: str = os.getenv("GUARD_REPO", "").strip()
MAX_PRS: int = int(os.getenv("MAX_PRS", "150"))
HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "30"))

# --- LLM (any OpenAI-compatible endpoint) ---------------------------------
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://localhost:20128/v1")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "not-needed-for-local-gateway")
LLM_MODEL: str = os.getenv("LLM_MODEL", "")

# --- Behaviour ------------------------------------------------------------
DRY_RUN: bool = os.getenv("DRY_RUN", "true").strip().lower() not in {"false", "0", "no", "off"}
AUTO_POST: bool = os.getenv("AUTO_POST", "false").strip().lower() in {"true", "1", "yes", "on"}

# --- FalkorDB -------------------------------------------------------------
FALKORDB_HOST: str = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT: int = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_GRAPH: str = os.getenv("FALKORDB_GRAPH", "why")


def require_llm_model() -> str:
    """Return LLM_MODEL or fail loudly with an actionable message."""
    if not LLM_MODEL:
        raise SystemExit(
            "LLM_MODEL is not set. Add it to your environment or .env, e.g.\n"
            "  LLM_MODEL=auto/fast\n"
            "Any OpenAI-compatible model id works (see .env.example)."
        )
    return LLM_MODEL

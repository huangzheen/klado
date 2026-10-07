"""LLM configuration — which model each AI feature calls, and where to reach it.

Read from the **environment** (see `.env.example`), never from the database.
A deployment's model choice is then reviewable in one place, and a row left
behind by an older build can never silently override it.

`get_endpoint()` maps a model name onto its provider's chat-completions URL and
API key, so callers only ever pass a model name around.
"""
from __future__ import annotations
import os

# The defaults a stock checkout runs with. Every one of them is overridable from
# the environment — see `_ENV_OVERRIDES` — so a deployment never has to edit this
# file to change models.
DEFAULTS: dict[str, str] = {
    "doc_topic_model": "deepseek-v4-flash",
    "image_generation_model": "image-01",
}

_ENV_OVERRIDES: dict[str, str] = {
    "doc_topic_model": "DOC_TOPIC_MODEL",
    "image_generation_model": "MINIMAX_IMAGE_MODEL",
}


def get() -> dict:
    """Return the current LLM config: the defaults, with environment overrides applied."""
    config = dict(DEFAULTS)
    for key, env_name in _ENV_OVERRIDES.items():
        value = (os.environ.get(env_name) or "").strip()
        if value:
            config[key] = value
    return config


def get_endpoint(model: str) -> tuple[str, str]:
    """Return (chat_completions_url, api_key) for the given model name."""
    if model.startswith("deepseek-"):
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        return f"{base}/chat/completions", os.environ.get("DEEPSEEK_API_KEY", "")
    if model.lower().startswith("minimax"):
        base = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1").rstrip("/")
        return f"{base}/chat/completions", os.environ.get("MINIMAX_API_KEY", "")
    # Default: Volcengine
    base = os.environ.get("VOLCENGINE_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").rstrip("/")
    return f"{base}/chat/completions", os.environ.get("VOLCENGINE_API_KEY", "")
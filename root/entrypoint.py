import json
import os
import subprocess
import sys
from pathlib import Path

# LLM provider specs
_PROVIDERS = {
    "anthropic": {"model": "claude-sonnet-4-6", "context": 16384, "summary": "claude-3-5-haiku-latest"},
    "gemini":    {"model": "gemini-3-flash-preview", "context": 65536, "summary": "gemini-3.1-flash-lite-preview"},
}

def resolve_llm_spec(provider):
    return _PROVIDERS.get((provider or "anthropic").lower(), _PROVIDERS["anthropic"])

# ── Configuration ──────────────────────────────────────────────────────────
# Edit these values to tune your nanobot
MAX_SESSION_MESSAGES   = 20             # Max messages kept in short-term session context
RECENT_FULL_MESSAGES   = 4              # Most recent messages sent at full resolution (not compressed)
TOON_COMPRESSION       = True           # Compress older messages with TOON encoding to save tokens
COMPRESSED_MAX_CHARS   = 150            # Max characters per message when TOON-compressing
EXEC_TIMEOUT           = 300            # Seconds before a shell command times out
HISTORY_LOG_FILE       = "/root/workspace/HISTORY.md"  # Archive for pruned session summaries ("" to disable)
RESTRICT_TO_WORKSPACE  = True           # Force LLM tools (exec, files) to only see /root/workspace
GENERATE_TEMPLATES     = True           # Auto-create templates if missing
TEMPLATE_FILES         = ["IDENTITY.md", "USER.md"] # Which templates to generate if GENERATE_TEMPLATES is True
KEEP_CRON_DB           = False           # Keep the scheduled jobs list across restarts

# ── Paths ──────────────────────────────────────────────────────────────────
# Resolve dynamically so it works on host OS (Windows/Mac) and inside Docker
_ROOT_DIR   = Path(__file__).resolve().parent
CONFIG_PATH = _ROOT_DIR / "config.json"
WORKSPACE   = _ROOT_DIR / "workspace"

# ── Model selection ────────────────────────────────────────────────────────
_provider_name = os.environ.get("LLM_PROVIDER", "anthropic").lower()
_llm = resolve_llm_spec(_provider_name)

# ── Build the config skeleton ──────────────────────────────────────────────
_config_skeleton = {
    "agents": {
        "defaults": {
            "model":               _llm["model"],
            "provider":            _provider_name,
            "contextWindowTokens": _llm.get("context", 16384),
            "workspace":           str(WORKSPACE),
        }
    },
    "channels": {
        "sendToolHints": True,
        "sendProgress":  True,
        "telegram": {
            "enabled": True,
            "token":     os.environ.get("TELEGRAM_TOKEN", ""),
            "allowFrom": os.environ.get("TELEGRAM_ALLOW_FROM", "").split(",") if os.environ.get("TELEGRAM_ALLOW_FROM") else [],
        }
    },
    "tools": {
        "exec":            {"timeout": EXEC_TIMEOUT},
        "restrictToWorkspace": RESTRICT_TO_WORKSPACE,
    },
    "session": {
        "maxSessionMessages":   MAX_SESSION_MESSAGES,
        "recentFullMessages":    RECENT_FULL_MESSAGES,
        "toonCompression":      TOON_COMPRESSION,
        "compressedMsgMaxChars": COMPRESSED_MAX_CHARS,
        "historyFile":          HISTORY_LOG_FILE,
    },
}

# ── Write/Merge config ─────────────────────────────────────────────────────
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text(json.dumps(_config_skeleton, indent=2))
else:
    try:
        existing = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        existing = {}

    def _deep_merge(base: dict, defaults: dict) -> dict:
        merged = dict(defaults)
        for k, v in base.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = _deep_merge(v, merged[k])
            else:
                merged[k] = v
        return merged

    merged = _deep_merge(existing, _config_skeleton)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2))

print(f"--- Bootstrapping Nanobot ({_provider_name}) ---")
print(f"Config:    {CONFIG_PATH}")
print(f"Workspace: {WORKSPACE}")

# ── Cleanups & Env ─────────────────────────────────────────────────────────
if GENERATE_TEMPLATES:
    os.environ["NANOBOT_SYNC_TEMPLATES"] = ",".join(TEMPLATE_FILES) if TEMPLATE_FILES else "none"
else:
    os.environ["NANOBOT_SYNC_TEMPLATES"] = "none"

if not KEEP_CRON_DB:
    import shutil
    shutil.rmtree(CONFIG_PATH.parent / "cron", ignore_errors=True)
# ── Launch ────────────────────────────────────────────────────────────────
os.execv(
    sys.executable,
    [
        sys.executable, "-m", "nanobot", "gateway",
        "--config", str(CONFIG_PATH),
        "--workspace", str(WORKSPACE),
    ],
)

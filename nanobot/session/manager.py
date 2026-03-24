"""Session management for conversation history."""

import json
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.utils.helpers import ensure_dir, safe_filename

if TYPE_CHECKING:
    from nanobot.config.schema import SessionConfig


# Keys stripped from assistant messages before saving to session history.
_ASSISTANT_STRIP_KEYS = {"tool_calls", "tool_call_id"}


def _strip_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool results, tool calls, and thinking/reasoning from message list."""
    cleaned = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant":
            m = {k: v for k, v in m.items() if k not in _ASSISTANT_STRIP_KEYS}
            content = m.get("content")
            if isinstance(content, list):
                content = [b for b in content if b.get("type") not in ("tool_use", "tool_result")]
                if not content:
                    continue
                m = {**m, "content": content}
            if not m.get("content"):
                continue
        if role == "user":
            content = m.get("content")
            if isinstance(content, list):
                content = [b for b in content if b.get("type") not in ("tool_result", "tool_use")]
                if not content:
                    continue
                m = {**m, "content": content}
        cleaned.append(m)
    return cleaned


def _ensure_alternating(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role messages for provider compatibility."""
    if not messages:
        return messages
    result = [messages[0]]
    for m in messages[1:]:
        if m["role"] == result[-1]["role"]:
            prev = result[-1]["content"]
            curr = m.get("content", "")
            if isinstance(prev, str) and isinstance(curr, str):
                result[-1] = {**result[-1], "content": prev + "\n\n" + curr}
            elif isinstance(prev, list) and isinstance(curr, list):
                result[-1] = {**result[-1], "content": prev + curr}
            elif isinstance(prev, str) and isinstance(curr, list):
                result[-1] = {**result[-1], "content": [{"type": "text", "text": prev}] + curr}
            elif isinstance(prev, list) and isinstance(curr, str):
                result[-1] = {**result[-1], "content": prev + [{"type": "text", "text": curr}]}
        else:
            result.append(m)
    return result


def _toon_encode(messages: list[dict[str, Any]], max_chars: int = 150) -> str:
    """Encode conversation messages using the TOON library."""
    try:
        from toon_format import encode
    except ImportError:
        logger.warning("toon-python not installed. Skipping compression.")
        return ""

    clean_msgs = []
    for m in messages:
        role = "U" if m.get("role") == "user" else "IO"
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        content = str(content)[:max_chars].replace("\n", " ").strip()
        clean_msgs.append({"role": role, "msg": content})

    return encode({"history": clean_msgs}, {"lengthMarker": ""}).strip()


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 500,
        recent_full_messages: int = 0,
        toon_compression: bool = False,
        compressed_msg_max_chars: int = 150,
    ) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn.

        When toon_compression=True, older messages beyond recent_full_messages
        are compressed into a single TOON block to reduce token usage.
        """
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:] if max_messages else unconsolidated

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            if m.get("reasoning_content") is not None:
                entry["reasoning_content"] = m["reasoning_content"]
            if m.get("thinking_blocks"):
                entry["thinking_blocks"] = m["thinking_blocks"]
            out.append(entry)

        # Apply strip + TOON compression if requested
        msgs = _strip_tool_messages(out)

        if not toon_compression or recent_full_messages <= 0 or len(msgs) <= recent_full_messages:
            return _ensure_alternating(msgs)

        # Find the split point, ensuring we start on a user turn
        split = len(msgs) - recent_full_messages
        while split < len(msgs) and msgs[split].get("role") != "user":
            split += 1

        if split >= len(msgs):
            # All messages fall into the "old" bucket — compress everything
            toon = _toon_encode(msgs, compressed_msg_max_chars)
            return [
                {"role": "user", "content": f"[Previous conversation]\n{toon}"},
                {"role": "assistant", "content": "Understood."},
            ]

        old = msgs[:split]
        recent = msgs[split:]
        toon = _toon_encode(old, compressed_msg_max_chars)
        context = {"role": "user", "content": f"[Previous conversation]\n{toon}"}
        ack = {"role": "assistant", "content": "Understood."}
        return _ensure_alternating([context, ack] + recent)

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    Pass a SessionConfig to enable native pruning, TOON compression,
    and background summarisation (previously done via monkey-patching).
    """

    def __init__(self, workspace: Path, session_config: "SessionConfig | None" = None):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}
        self._session_config = session_config

    # ------------------------------------------------------------------
    # Background summarisation helpers
    # ------------------------------------------------------------------

    def _call_summary_llm(self, prompt: str, summary_model: str) -> str | None:
        """Call the summary model synchronously (runs in background thread)."""
        try:
            import requests
        except ImportError:
            logger.warning("requests not installed. Background summarisation disabled.")
            return None

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()

        if provider == "gemini" and gemini_key:
            model = summary_model or "gemini-2.0-flash-lite"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            body = {"contents": [{"parts": [{"text": prompt}]}]}
            resp = requests.post(url, json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

        if anthropic_key:
            model = summary_model or "claude-3-5-haiku-latest"
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": model,
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip()

        return None

    def _trim_history(self, history_file: str, max_entries: int) -> None:
        """Keep history file under max_entries entries."""
        if not history_file or not Path(history_file).exists():
            return
        with open(history_file) as f:
            content = f.read()
        entries = [ln for ln in content.strip().split("\n") if ln.strip() and ln.startswith("[")]
        if len(entries) <= max_entries:
            return
        with open(history_file, "w") as f:
            f.write("\n\n".join(entries[-max_entries:]) + "\n")

    def _summarize_and_archive(self, messages: list[dict[str, Any]]) -> None:
        """Summarize discarded messages and append to history file. Runs in a thread."""
        cfg = self._session_config
        if not cfg or not cfg.history_file:
            return

        lines = []
        total = 0
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role not in ("user", "assistant") or not content:
                continue
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if b.get("type") == "text"
                )
            text = str(content)[:500]
            label = "User" if role == "user" else "IO"
            line = f"{label}: {text}"
            if total + len(line) > cfg.summary_max_chars:
                break
            lines.append(line)
            total += len(line)

        conversation = "\n".join(lines)
        if not conversation.strip():
            return

        prompt = (
            "Summarize this conversation in 2-3 concise sentences. "
            "Include tool names, file names, and decisions made.\n\n"
            f"Conversation:\n{conversation}"
        )

        try:
            summary = self._call_summary_llm(prompt, cfg.summary_model)
            if not summary:
                return

            timestamp = datetime.now().strftime("%m-%d")
            entry = f"[{timestamp}] {summary}\n\n"

            history_path = Path(cfg.history_file)
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(history_path, "a") as f:
                f.write(entry)

            self._trim_history(cfg.history_file, cfg.history_max_entries)
            print(f"session: archived {len(messages)} messages to {cfg.history_file}", file=sys.stderr)
        except Exception as e:
            print(f"session: summary failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """Save a session to disk.

        When a SessionConfig is supplied, also:
        - Strips tool/reasoning messages from saved history
        - Prunes messages beyond max_session_messages
        - Fires a background thread to archive discarded messages
        """
        cfg = self._session_config

        if cfg:
            # Strip tool results and intermediate reasoning before saving
            session.messages = _strip_tool_messages(session.messages)

            # Prune to max_session_messages, archiving discarded messages
            if len(session.messages) > cfg.max_session_messages:
                discarded = session.messages[:-cfg.max_session_messages]
                session.messages = session.messages[-cfg.max_session_messages:]
                if discarded and cfg.history_file:
                    threading.Thread(
                        target=self._summarize_and_archive,
                        args=(discarded,),
                        daemon=True,
                    ).start()

        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

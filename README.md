# Nanobot Gateway

A token-optimized, configurable Dockerized AI orchestrator built on the [nanobot](https://github.com/HKUDS/nanobot) framework. This repository uses a custom `entrypoint.py` to natively orchestrate the conversational engine's configuration, focusing on API token efficiency (via native [TOON Format](https://toonformat.dev) compression), secure conversation isolation, and seamless [TensorZero](https://www.tensorzero.com) integration out of the box.

## Architecture

```text
Project Root/
├── .env                        # secrets (API keys, tokens)
├── .env.example                # public template for secrets
├── docker-compose.yml          # stack orchestrator
└── root/                       # mounted into docker as /root
    ├── config.json             # generated at startup (no API keys on disk)
    ├── entrypoint.py           # custom launcher (all configuration lives here)
    └── workspace/
        ├── IDENTITY.md         # LLM persona
        ├── USER.md             # User persona
        ├── memory.db           # core memory database
        ├── memory/
        │   ├── MEMORY.md       # persistent memory
        │   └── HISTORY.md      # summarized conversation archive
        ├── sessions/*.jsonl    # active conversation sessions
        └── skills/             # installable agent skills
            └── my-custom-skill/# optional custom background scripts
```

Two primary ways to run this instance:
- **nanobot-gateway** — the main conversational Telegram bot
- **[Optional Background Containers]** — independent skills orchestrated via `docker-compose.yml`

### Security & Container Isolation
This agent is Dockerized for operational security. LLMs equipped with shell execution (`exec`) and filesystem tools are vulnerable to prompt injection and hallucinated destructive commands. 

By running the orchestration inside a Docker container:
- **Host Sandboxing:** If the agent accidentally or maliciously executes `rm -rf /` or attempts to install rogue pip dependencies, it only destroys the disposable Linux container rather than your host Windows desktop.
- **Strict File Boundaries:** The LLM's filesystem visibility is hard-restricted to the `/root/workspace` mount. It physically cannot read your personal files outside of that scope.
- **Secret Protection:** API keys are never written to disk in the container; they are held securely in environment variables via your host's `.env` file.

### The Dockerized Cron Philosophy
Upstream `nanobot` natively uses an LLM-driven `CronService` and `HeartbeatService`. While powerful, waking an LLM up on intervals just to check if "something happened" burns massive amounts of tokens over time. 

In this repository, we deliberately force `KEEP_CRON_DB = False` and completely delete the internal nanobot cron engine. 

Instead, any background jobs or scheduled tasks are moved into cheap, token-free, independent **Docker containers** (using the `docker-compose.yml` orchestrator) running standard Python infinite loops. They quietly poll APIs in the background for $0 and only interact with the user via Telegram directly (or by handing data to the `nanobot-gateway-zero` layer via file storage) when there is actually actionable data.

## Setup (Telegram Default)

This repository is currently constructed, configured, and token-optimized out-of-the-box exclusively for **Telegram**. To use other channels (Slack, Discord, Matrix), you will need to add their bindings to your `.env` file and optionally update the `entrypoint.py` configuration dict.

1. Create `.env` in the nanobot root:

```env
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
TELEGRAM_TOKEN=123456789:ABC...
TELEGRAM_ALLOW_FROM=your_telegram_user_id
LLM_PROVIDER=anthropic
```

2. Start the containers:

```bash
docker compose up -d
```

## Switching LLM Providers

Change `LLM_PROVIDER` in `.env` to `anthropic` or `gemini`, then restart:

| Provider | Chat Model | Summary Model | Context Window |
|---|---|---|---|
| `anthropic` | claude-sonnet-4-6 | claude-3-5-haiku-latest | 16,384 |
| `gemini` | gemini-3-flash-preview | gemini-3.1-flash-lite-preview | 65,536 |

## The `entrypoint.py` Orchestrator

The `entrypoint.py` script serves as a dynamic configuration orchestrator that runs ahead of the core system:

### 1. Dynamic Configuration Merging
Instead of managing a hardcoded `config.json`, the entrypoint constructs a minimal configuration block at startup. It acts as an abstraction layer:
- Pulls your `LLM_PROVIDER` choice from your `.env` file natively.
- Dynamically assigns optimal model specifications (e.g., configuring `gemini-3-flash-preview`'s 65,536 token context window versus Claude's defaults).
- Forces useful channel defaults globally (e.g., standardizing `sendToolHints` and `sendProgress` interactions).

### 2. Workspace Hygiene & Template Generation
Automatically manages local file clutter. 
- **System Templates:** Forces nanobot to only synchronize the system `.md` templates you explicitly permit via the `TEMPLATE_FILES` configuration.
- **Background Processes:** Erases the ghost `cron` service directory entirely on boot if you intend to run without background jobs enabled.

### 3. Native Feature Enablement & Token Optimization
Passes hardcoded Python constants straight into the core engine memory configuration, ensuring your agent scales efficiently across large conversations:
- **Conversation Management:** Forces a `MAX_SESSION_MESSAGES` cap to dictate when the LLM must summarize the chat and append it to `HISTORY.md`. 
- **TOON Compression:** Directly pushes `TOON_COMPRESSION` and `COMPRESSED_MAX_CHARS` triggers into the engine, instructing it to compress older multi-turn messages down to tiny strings to prevent context blooming.
- **Tool Trimming:** Registers the `UNREGISTER_TOOLS` array (e.g., dropping `web_search`) to physically strip heavy JSON tool schemas out of the LLM context, saving hundreds of prompt tokens per turn.

## Token Efficiency vs Upstream

The upstream `nanobot` repository prioritizes maximum functionality out-of-the-box (enabling all tools, all background services, and verbose system templates). This repository is explicitly tuned to minimize token consumption and reduce cloud API costs (Anthropic/Google) without losing conversational quality.

Here is a breakdown of the specific savings orchestrated by `entrypoint.py`:

| Component | Default Upstream (`HKUDS`) | This Optimized Setup | How it works |
|-----------|---------------------------|----------------------|--------------|
| **System Prompts** | ~2,500+ tokens | ~300 tokens | Upstream automatically generates and injects verbose `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`, and `SOUL.md` rules into the engine's system prompt context. We bypass these using the strict `TEMPLATE_FILES` config, forcing the model to only load your tiny `IDENTITY.md` and `USER.md`. |
| **Tool Schemas** | ~1,200 tokens | ~300 tokens | Upstream injects complex schemas for `web_search`, `cron`, `spawn`, `message`, and filesystem interactions into every single API call payload. We aggressively prune unused tools via `UNREGISTER_TOOLS` to drop their definitions entirely. |
| **Chat Memory** | Unbounded | Capped (`MAX`=20) | Active chat turns are strictly capped. Older messages are immediately ripped out of the live context, summarized by a cheaper fractional model, appended to `HISTORY.md`, and purged to reset the window size automatically. |
| **History Formatting** | standard JSON (`{"role": "user"}`) | **TOON Encoded** | To maintain long-term memory without blowing up the context window, older messages are losslessly encoded using [TOON Format](https://toonformat.dev). By dropping heavy JSON brackets (`[{}]`) and collapsing arrays into tabular CSV-style rows (`history{role,msg}:`), TOON natively uses **~60% fewer tokens** while actually improving LLM parsing accuracy (testing at ~74% accuracy vs standard JSON's 70%). |

## Configuration Constants

All tunable values live at the top of `entrypoint.py`:

| Constant | Default | Description |
|---|---|---|
| `MAX_SESSION_MESSAGES` | `20` | Hard cap on stored session messages |
| `RECENT_FULL_MESSAGES` | `4` | Recent messages sent as full turns |
| `COMPRESSED_MAX_CHARS` | `150` | Max chars per TOON-compressed message |
| `TOON_COMPRESSION` | `True` | Enable background conversation compression |
| `UNREGISTER_TOOLS` | `["web_search"]` | Tools removed from the registry |
| `GENERATE_TEMPLATES` | `True` | Create missing system MD files automatically |
| `TEMPLATE_FILES` | `["IDENTITY.md", "USER.md"]` | Which templates to sync if enabled |
| `KEEP_CRON_DB` | `False` | Retain the background scheduler jobs across reboots |

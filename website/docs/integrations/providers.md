---
title: "AI Providers"
sidebar_label: "AI Providers"
sidebar_position: 1
---

# AI Providers

This page covers setting up inference providers for Hermes Agent — from cloud APIs like OpenRouter and Anthropic, to self-hosted endpoints like Ollama and vLLM, to advanced routing and fallback configurations. You need at least one provider configured to use Hermes.

## Inference Providers

You need at least one way to connect to an LLM. Use `hermes model` to switch providers and models interactively, or configure directly:

| Provider | Setup |
|----------|-------|
| **Nous Portal** | `hermes model` (OAuth, subscription-based) |
| **OpenAI Codex** | `hermes model` (ChatGPT OAuth, uses Codex models) |
| **GitHub Copilot** | `hermes model` (OAuth device code flow, `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `gh auth token`) |
| **GitHub Copilot ACP** | `hermes model` (spawns local `copilot --acp --stdio`) |
| **Anthropic** | `hermes model` (Claude Pro/Max via Claude Code auth, Anthropic API key, or manual setup-token) |
| **OpenRouter** | `OPENROUTER_API_KEY` in `~/.hermes/.env` |
| **AI Gateway** | `AI_GATEWAY_API_KEY` in `~/.hermes/.env` (provider: `ai-gateway`) |
| **z.ai / GLM** | `GLM_API_KEY` in `~/.hermes/.env` (provider: `zai`) |
| **Kimi / Moonshot** | `KIMI_API_KEY` in `~/.hermes/.env` (provider: `kimi-coding`) |
| **MiniMax** | `MINIMAX_API_KEY` in `~/.hermes/.env` (provider: `minimax`) |
| **MiniMax China** | `MINIMAX_CN_API_KEY` in `~/.hermes/.env` (provider: `minimax-cn`) |
| **Alibaba Cloud** | `DASHSCOPE_API_KEY` in `~/.hermes/.env` (provider: `alibaba`, aliases: `dashscope`, `qwen`) |
| **Kilo Code** | `KILOCODE_API_KEY` in `~/.hermes/.env` (provider: `kilocode`) |
| **OpenCode Zen** | `OPENCODE_ZEN_API_KEY` in `~/.hermes/.env` (provider: `opencode-zen`) |
| **OpenCode Go** | `OPENCODE_GO_API_KEY` in `~/.hermes/.env` (provider: `opencode-go`) |
| **DeepSeek** | `DEEPSEEK_API_KEY` in `~/.hermes/.env` (provider: `deepseek`) |
| **Hugging Face** | `HF_TOKEN` in `~/.hermes/.env` (provider: `huggingface`, aliases: `hf`) |
| **Google / Gemini** | `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) in `~/.hermes/.env` (provider: `gemini`) |
| **Custom Endpoint** | `hermes model` → choose "Custom endpoint" (saved in `config.yaml`) |

:::tip Model key alias
In the `model:` config section, you can use either `default:` or `model:` as the key name for your model ID. Both `model: { default: my-model }` and `model: { model: my-model }` work identically.
:::

:::info Codex Note
The OpenAI Codex provider authenticates via device code (open a URL, enter a code). Hermes stores the resulting credentials in its own auth store under `~/.hermes/auth.json` and can import existing Codex CLI credentials from `~/.codex/auth.json` when present. No Codex CLI installation is required.
:::

:::warning
Even when using Nous Portal, Codex, or a custom endpoint, some tools (vision, web summarization, MoA) use a separate "auxiliary" model — by default Gemini Flash via OpenRouter. An `OPENROUTER_API_KEY` enables these tools automatically. You can also configure which model and provider these tools use — see [Auxiliary Models](/docs/user-guide/configuration#auxiliary-models).
:::

### Anthropic (Native)

Use Claude models directly through the Anthropic API — no OpenRouter proxy needed. Supports three auth methods:

```bash
# With an API key (pay-per-token)
export ANTHROPIC_API_KEY=***
hermes chat --provider anthropic --model claude-sonnet-4-6

# Preferred: authenticate through `hermes model`
# Hermes will use Claude Code's credential store directly when available
hermes model

# Manual override with a setup-token (fallback / legacy)
export ANTHROPIC_TOKEN=***  # setup-token or manual OAuth token
hermes chat --provider anthropic

# Auto-detect Claude Code credentials (if you already use Claude Code)
hermes chat --provider anthropic  # reads Claude Code credential files automatically
```

When you choose Anthropic OAuth through `hermes model`, Hermes prefers Claude Code's own credential store over copying the token into `~/.hermes/.env`. That keeps refreshable Claude credentials refreshable.

Or set it permanently:
```yaml
model:
  provider: "anthropic"
  default: "claude-sonnet-4-6"
```

:::tip Aliases
`--provider claude` and `--provider claude-code` also work as shorthand for `--provider anthropic`.
:::

### GitHub Copilot

Hermes supports GitHub Copilot as a first-class provider with two modes:

**`copilot` — Direct Copilot API** (recommended). Uses your GitHub Copilot subscription to access GPT-5.x, Claude, Gemini, and other models through the Copilot API.

```bash
hermes chat --provider copilot --model gpt-5.4
```

**Authentication options** (checked in this order):

1. `COPILOT_GITHUB_TOKEN` environment variable
2. `GH_TOKEN` environment variable
3. `GITHUB_TOKEN` environment variable
4. `gh auth token` CLI fallback

If no token is found, `hermes model` offers an **OAuth device code login** — the same flow used by the Copilot CLI and opencode.

:::warning Token types
The Copilot API does **not** support classic Personal Access Tokens (`ghp_*`). Supported token types:

| Type | Prefix | How to get |
|------|--------|------------|
| OAuth token | `gho_` | `hermes model` → GitHub Copilot → Login with GitHub |
| Fine-grained PAT | `github_pat_` | GitHub Settings → Developer settings → Fine-grained tokens (needs **Copilot Requests** permission) |
| GitHub App token | `ghu_` | Via GitHub App installation |

If your `gh auth token` returns a `ghp_*` token, use `hermes model` to authenticate via OAuth instead.
:::

**API routing**: GPT-5+ models (except `gpt-5-mini`) automatically use the Responses API. All other models (GPT-4o, Claude, Gemini, etc.) use Chat Completions. Models are auto-detected from the live Copilot catalog.

**`copilot-acp` — Copilot ACP agent backend**. Spawns the local Copilot CLI as a subprocess:

```bash
hermes chat --provider copilot-acp --model copilot-acp
# Requires the GitHub Copilot CLI in PATH and an existing `copilot login` session
```

**Permanent config:**
```yaml
model:
  provider: "copilot"
  default: "gpt-5.4"
```

| Environment variable | Description |
|---------------------|-------------|
| `COPILOT_GITHUB_TOKEN` | GitHub token for Copilot API (first priority) |
| `HERMES_COPILOT_ACP_COMMAND` | Override the Copilot CLI binary path (default: `copilot`) |
| `HERMES_COPILOT_ACP_ARGS` | Override ACP args (default: `--acp --stdio`) |

### First-Class Chinese AI Providers

These providers have built-in support with dedicated provider IDs. Set the API key and use `--provider` to select:

```bash
# z.ai / ZhipuAI GLM
hermes chat --provider zai --model glm-5
# Requires: GLM_API_KEY in ~/.hermes/.env

# Kimi / Moonshot AI
hermes chat --provider kimi-coding --model kimi-for-coding
# Requires: KIMI_API_KEY in ~/.hermes/.env

# MiniMax (global endpoint)
hermes chat --provider minimax --model MiniMax-M2.7
# Requires: MINIMAX_API_KEY in ~/.hermes/.env

# MiniMax (China endpoint)
hermes chat --provider minimax-cn --model MiniMax-M2.7
# Requires: MINIMAX_CN_API_KEY in ~/.hermes/.env

# Alibaba Cloud / DashScope (Qwen models)
hermes chat --provider alibaba --model qwen3.5-plus
# Requires: DASHSCOPE_API_KEY in ~/.hermes/.env
```

Or set the provider permanently in `config.yaml`:
```yaml
model:
  provider: "zai"       # or: kimi-coding, minimax, minimax-cn, alibaba
  default: "glm-5"
```

Base URLs can be overridden with `GLM_BASE_URL`, `KIMI_BASE_URL`, `MINIMAX_BASE_URL`, `MINIMAX_CN_BASE_URL`, or `DASHSCOPE_BASE_URL` environment variables.

:::note Z.AI Endpoint Auto-Detection
When using the Z.AI / GLM provider, Hermes automatically probes multiple endpoints (global, China, coding variants) to find one that accepts your API key. You don't need to set `GLM_BASE_URL` manually — the working endpoint is detected and cached automatically.
:::

### xAI (Grok) Prompt Caching

When using xAI as a provider (any base URL containing `x.ai`), Hermes automatically enables prompt caching by sending the `x-grok-conv-id` header with every API request. This routes requests to the same server within a conversation session, allowing xAI's infrastructure to reuse cached system prompts and conversation history.

No configuration is needed — caching activates automatically when an xAI endpoint is detected and a session ID is available. This reduces latency and cost for multi-turn conversations.

### Hugging Face Inference Providers

[Hugging Face Inference Providers](https://huggingface.co/docs/inference-providers) routes to 20+ open models through a unified OpenAI-compatible endpoint (`router.huggingface.co/v1`). Requests are automatically routed to the fastest available backend (Groq, Together, SambaNova, etc.) with automatic failover.

```bash
# Use any available model
hermes chat --provider huggingface --model Qwen/Qwen3-235B-A22B-Thinking-2507
# Requires: HF_TOKEN in ~/.hermes/.env

# Short alias
hermes chat --provider hf --model deepseek-ai/DeepSeek-V3.2
```

Or set it permanently in `config.yaml`:
```yaml
model:
  provider: "huggingface"
  default: "Qwen/Qwen3-235B-A22B-Thinking-2507"
```

Get your token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) — make sure to enable the "Make calls to Inference Providers" permission. Free tier included ($0.10/month credit, no markup on provider rates).

You can append routing suffixes to model names: `:fastest` (default), `:cheapest`, or `:provider_name` to force a specific backend.

The base URL can be overridden with `HF_BASE_URL`.

### Qoder preview

Qoder is an opt-in text chat provider using the official Qoder Agent SDK and its bundled CLI. In your Hermes environment, install the optional dependency and enable the preview.

```bash
uv pip install -e '.[qoder]'
hermes model
```

Add the token and preview settings to the `.env` loaded by your Hermes process (normally `~/.hermes/.env`):

```dotenv
QODER_PERSONAL_ACCESS_TOKEN=your_qoder_personal_access_token
HERMES_QODER_PREVIEW=1
HERMES_QODER_WORKSPACE_READ=1
```

`HERMES_QODER_WORKSPACE_READ` is optional. Enable it only when Qoder should be able to use Hermes' confined `read_file` and `search_files` tools in the active workspace. To allow edits as well, add the separate write opt-in:

```dotenv
HERMES_QODER_WORKSPACE_WRITE=1
```

Keep the `.env` private and out of version control. For a running gateway, apply the environment to the gateway service and restart it after making changes. In `hermes model`, choose **More providers** and then **Qoder**. In an existing chat, use `/model qoder` for the Qoder account default or `/model qoder:<model-id>` for an account-visible model. Add `--global` to save a chat model switch.

Hermes stores the transcript and sends the bounded text history with each turn. Replies stream as they arrive. Attachments and Qoder Cloud Agent are outside this preview. Hermes reports Qoder credits when the SDK returns them. Auxiliary tasks use Hermes' auxiliary-provider configuration.

An experimental read-only workspace bridge is available behind a second opt-in flag:

```bash
export HERMES_QODER_WORKSPACE_READ=1
```

It exposes only Hermes' existing `read_file` and `search_files` dispatch to Qoder and confines paths to the workspace active when the turn starts. File writes remain disabled unless the separate write flag below is also enabled. Do not enable it for untrusted workspaces. The bridge remains a pilot until the authenticated release checks below pass.

After validating the read pilot, workspace edits can be enabled separately:

```bash
export HERMES_QODER_WORKSPACE_WRITE=1
```

The write flag has effect only when `HERMES_QODER_WORKSPACE_READ=1` is also set. It exposes Hermes' existing `write_file` and `patch` tools, confines their targets to the workspace, and requires a one-shot Hermes approval for every distinct edit. Duplicate dispatches in the same turn reuse the first result rather than applying the edit again.

[Qoder SDK authentication](https://docs.qoder.com/cli/sdk/authentication) · [Qoder Python SDK reference](https://docs.qoder.com/cli/sdk/references-python)

#### Authenticated release checks

From the Hermes checkout, activate its test environment and supply a Qoder personal access token in the environment. Run the checks explicitly. They make billable Qoder requests using synthetic inputs.

```bash
HERMES_HOME="$(mktemp -d)" HERMES_QODER_LIVE_TESTS=1 \
  python -m pytest -n 0 -q -s tests/run_agent/test_qoder_live.py
```

The suite exercises streaming, persisted history, cancellation and subprocess cleanup, CLI and gateway model selection, and a test-only MCP record lookup. Before removing the text preview flag, repeat the chat, restart, cancellation, and switch-back checks in a disposable gateway session connected to a test chat. Record the SDK and CLI versions and the results. The fixture tool is registered only inside the test.

#### Pending work checklist

Qoder remains a text-only preview. Results below were recorded on 2026-11-25 with Qoder Agent SDK 1.0.17 and bundled `qodercli` 1.1.79.

- [x] The restarted test process can read `QODER_PERSONAL_ACCESS_TOKEN` without displaying it, and the `qoder` extra is installed in the Hermes test environment.
- [ ] Authenticated text suite: direct SDK and Hermes-adapter probes both completed two first turns, two saved-history follow-ups, and repeated discovery. Connect took 4.5–6.5s, first chunks 6.0–8.6s from startup, final results 6.6–9.1s, discovery under 0.1s after connect, and disconnect about 10s. The apparent intermittent failures were the test harness's cumulative 30-second `SIGALRM`, which interrupted the main thread after Qoder had already returned successful results. Qoder live tests now use an explicit 180-second per-test budget; production request timeouts and retries were not changed. The final authenticated suite passed `6 passed in 300.67s`. A focused disposable Telegram-identity harness using the real `GatewayRunner._run_agent` path passed Qoder selection, a first turn, runner recreation with saved-history follow-up, and switch-back in 114.96s. Telegram transport was mocked, so external message delivery, chat-side approval controls, and cancellation from an actual test chat remain pending. Keep `HERMES_QODER_PREVIEW=1` until that connected-chat check passes.
- [x] Test-only MCP fixture lookup: focused authenticated rerun passed with one observed tool block, exactly one fixture handler invocation, and the returned value in the final answer. The SDK requires the tool in both `tools` and `allowed_tools`.
- [x] Opt-in workspace read pilot: authenticated checks passed for an allowed read through Hermes dispatch, traversal denial before dispatch, transcript persistence, and cancellation with exactly one read dispatch.
- [x] Opt-in workspace edit pilot: Qoder tool blocks are accepted only for explicitly enabled workspace tools, post-dispatch failures are non-retryable, duplicate edit dispatches reuse the first result, and SDK handlers use a true single-argument signature. Authenticated checks passed for approved-once writes, denied edits leaving the file untouched, and cancellation with exactly one write dispatch. File writes remain behind both workspace opt-in flags and one-shot Hermes approvals.

#### Disposable Telegram release runbook

The connected-chat release check is currently blocked. A presence-only environment audit on 2026-11-25 found `QODER_PERSONAL_ACCESS_TOKEN`, but not `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL`, or `OPENROUTER_API_KEY`. No Telegram message was sent. Supply a disposable bot token, put only the explicitly authorized tester's numeric user ID in `TELEGRAM_ALLOWED_USERS`, set the private test chat ID in `TELEGRAM_HOME_CHANNEL`, and supply an OpenRouter key for the final provider switch. Keep all values in the process environment; do not paste them into chat or test logs.

Use a temporary Hermes profile and workspace so the restart preserves the test conversation while file access remains confined to synthetic data:

```bash
QODER_SMOKE_ROOT="$(mktemp -d)"
chmod 700 "$QODER_SMOKE_ROOT"
mkdir -p "$QODER_SMOKE_ROOT/profile" "$QODER_SMOKE_ROOT/workspace"
printf '%s\n' 'allowed-read-marker' > "$QODER_SMOKE_ROOT/workspace/read-allowed.txt"
printf '%s\n' 'approved-before' > "$QODER_SMOKE_ROOT/workspace/approved.txt"
printf '%s\n' 'denied-before' > "$QODER_SMOKE_ROOT/workspace/denied.txt"
printf '%s\n' 'outside-marker' > "$QODER_SMOKE_ROOT/outside-denied.txt"
cd "$QODER_SMOKE_ROOT/workspace"
HERMES_HOME="$QODER_SMOKE_ROOT/profile" \
  HERMES_QODER_PREVIEW=1 \
  HERMES_QODER_WORKSPACE_READ=1 \
  HERMES_QODER_WORKSPACE_WRITE=1 \
  hermes gateway
```

Run the following checks only in the authorized private chat. Save sanitized timestamps, request IDs, gateway logs, and before/after file hashes; never save credential values or Telegram tokens.

- [ ] Select Qoder with `/model qoder`, request a multi-part synthetic response, and observe streamed delivery rather than only a final message.
- [ ] Ask Qoder to remember a synthetic nonce. Stop the foreground gateway, rerun the same command with the same `HERMES_HOME` and working directory, and verify a follow-up can recover the nonce from saved history.
- [ ] Switch to a known OpenRouter model with `/model openrouter:<model>` and verify the next response uses OpenRouter.
- [ ] Ask Qoder to read `read-allowed.txt` and return its marker. Then request the absolute path to `outside-denied.txt` and verify denial occurs before Hermes file-tool dispatch.
- [ ] Ask Qoder to replace `approved-before` in `approved.txt` with a synthetic value. Click `✅ Allow Once` and verify one dispatch and the exact expected file hash. Clicking the resolved button again must not apply another edit.
- [ ] Ask for a different edit to `denied.txt`, click `❌ Deny`, and verify zero write dispatches and an unchanged file hash.
- [ ] Start a deliberately long Qoder turn, send `/stop` while it is active, and verify the chat reports that the session was force-stopped. Confirm the Qoder subprocess exits, no tool invocation is replayed, and a new synthetic message succeeds in the same chat.

Record the connected platform, Qoder Agent SDK version, bundled CLI version, gateway commit, and pass/fail evidence for every item above in this checklist. Keep `HERMES_QODER_PREVIEW=1` until the live text, restart, switch-back, and cancellation checks pass. Once they pass, remove only the text preview gate; workspace reads and writes must remain independently opt-in.

## Custom & Self-Hosted LLM Providers

Hermes Agent works with **any OpenAI-compatible API endpoint**. If a server implements `/v1/chat/completions`, you can point Hermes at it. This means you can use local models, GPU inference servers, multi-provider routers, or any third-party API.

### General Setup

Three ways to configure a custom endpoint:

**Interactive setup (recommended):**
```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter: API base URL, API key, Model name
```

**Manual config (`config.yaml`):**
```yaml
# In ~/.hermes/config.yaml
model:
  default: your-model-name
  provider: custom
  base_url: http://localhost:8000/v1
  api_key: your-key-or-leave-empty-for-local
```

:::warning Legacy env vars
`OPENAI_BASE_URL` and `LLM_MODEL` in `.env` are **deprecated**. `OPENAI_BASE_URL` is no longer consulted for endpoint resolution — `config.yaml` is the single source of truth. The CLI ignores `LLM_MODEL` entirely (only the gateway reads it as a fallback). Use `hermes model` or edit `config.yaml` directly — both persist correctly across restarts and Docker containers.
:::

Both approaches persist to `config.yaml`, which is the source of truth for model, provider, and base URL.

### Switching Models with `/model`

Once a custom endpoint is configured, you can switch models mid-session:

```
/model custom:qwen-2.5          # Switch to a model on your custom endpoint
/model custom                    # Auto-detect the model from the endpoint
/model openrouter:claude-sonnet-4 # Switch back to a cloud provider
```

If you have **named custom providers** configured (see below), use the triple syntax:

```
/model custom:local:qwen-2.5    # Use the "local" custom provider with model qwen-2.5
/model custom:work:llama3       # Use the "work" custom provider with llama3
```

When switching providers, Hermes persists the base URL and provider to config so the change survives restarts. When switching away from a custom endpoint to a built-in provider, the stale base URL is automatically cleared.

:::tip
`/model custom` (bare, no model name) queries your endpoint's `/models` API and auto-selects the model if exactly one is loaded. Useful for local servers running a single model.
:::

Everything below follows this same pattern — just change the URL, key, and model name.

---

### vLLM — High-Performance GPU Inference

[vLLM](https://docs.vllm.ai/) is the standard for production LLM serving. Best for: maximum throughput on GPU hardware, serving large models, continuous batching.

```bash
pip install vllm
vllm serve meta-llama/Llama-3.1-70B-Instruct \
  --port 8000 \
  --max-model-len 65536 \
  --tensor-parallel-size 2 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
```

Then configure Hermes:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:8000/v1
# Skip API key (or enter one if you configured vLLM with --api-key)
# Enter model name: meta-llama/Llama-3.1-70B-Instruct
```

**Context length:** vLLM reads the model's `max_position_embeddings` by default. If that exceeds your GPU memory, it errors and asks you to set `--max-model-len` lower. You can also use `--max-model-len auto` to automatically find the maximum that fits. Set `--gpu-memory-utilization 0.95` (default 0.9) to squeeze more context into VRAM.

**Tool calling requires explicit flags:**

| Flag | Purpose |
|------|---------|
| `--enable-auto-tool-choice` | Required for `tool_choice: "auto"` (the default in Hermes) |
| `--tool-call-parser <name>` | Parser for the model's tool call format |

Supported parsers: `hermes` (Qwen 2.5, Hermes 2/3), `llama3_json` (Llama 3.x), `mistral`, `deepseek_v3`, `deepseek_v31`, `xlam`, `pythonic`. Without these flags, tool calls won't work — the model will output tool calls as text.

:::tip
vLLM supports human-readable sizes: `--max-model-len 64k` (lowercase k = 1000, uppercase K = 1024).
:::

---

### SGLang — Fast Serving with RadixAttention

[SGLang](https://github.com/sgl-project/sglang) is an alternative to vLLM with RadixAttention for KV cache reuse. Best for: multi-turn conversations (prefix caching), constrained decoding, structured output.

```bash
pip install "sglang[all]"
python -m sglang.launch_server \
  --model meta-llama/Llama-3.1-70B-Instruct \
  --port 30000 \
  --context-length 65536 \
  --tp 2 \
  --tool-call-parser qwen
```

Then configure Hermes:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:30000/v1
# Enter model name: meta-llama/Llama-3.1-70B-Instruct
```

**Context length:** SGLang reads from the model's config by default. Use `--context-length` to override. If you need to exceed the model's declared maximum, set `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`.

**Tool calling:** Use `--tool-call-parser` with the appropriate parser for your model family: `qwen` (Qwen 2.5), `llama3`, `llama4`, `deepseekv3`, `mistral`, `glm`. Without this flag, tool calls come back as plain text.

:::caution SGLang defaults to 128 max output tokens
If responses seem truncated, add `max_tokens` to your requests or set `--default-max-tokens` on the server. SGLang's default is only 128 tokens per response if not specified in the request.
:::

---

### llama.cpp / llama-server — CPU & Metal Inference

[llama.cpp](https://github.com/ggml-org/llama.cpp) runs quantized models on CPU, Apple Silicon (Metal), and consumer GPUs. Best for: running models without a datacenter GPU, Mac users, edge deployment.

```bash
# Build and start llama-server
cmake -B build && cmake --build build --config Release
./build/bin/llama-server \
  --jinja -fa \
  -c 32768 \
  -ngl 99 \
  -m models/qwen2.5-coder-32b-instruct-Q4_K_M.gguf \
  --port 8080 --host 0.0.0.0
```

**Context length (`-c`):** Recent builds default to `0` which reads the model's training context from the GGUF metadata. For models with 128k+ training context, this can OOM trying to allocate the full KV cache. Set `-c` explicitly to what you need (32k–64k is a good range for agent use). If using parallel slots (`-np`), the total context is divided among slots — with `-c 32768 -np 4`, each slot only gets 8k.

Then configure Hermes to point at it:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:8080/v1
# Skip API key (local servers don't need one)
# Enter model name — or leave blank to auto-detect if only one model is loaded
```

This saves the endpoint to `config.yaml` so it persists across sessions.

:::caution `--jinja` is required for tool calling
Without `--jinja`, llama-server ignores the `tools` parameter entirely. The model will try to call tools by writing JSON in its response text, but Hermes won't recognize it as a tool call — you'll see raw JSON like `{"name": "web_search", ...}` printed as a message instead of an actual search.

Native tool calling support (best performance): Llama 3.x, Qwen 2.5 (including Coder), Hermes 2/3, Mistral, DeepSeek, Functionary. All other models use a generic handler that works but may be less efficient. See the [llama.cpp function calling docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md) for the full list.

You can verify tool support is active by checking `http://localhost:8080/props` — the `chat_template` field should be present.
:::

:::tip
Download GGUF models from [Hugging Face](https://huggingface.co/models?library=gguf). Q4_K_M quantization offers the best balance of quality vs. memory usage.
:::

---

### LM Studio — Desktop App with Local Models

[LM Studio](https://lmstudio.ai/) is a desktop app for running local models with a GUI. Best for: users who prefer a visual interface, quick model testing, developers on macOS/Windows/Linux.

Start the server from the LM Studio app (Developer tab → Start Server), or use the CLI:

```bash
lms server start                        # Starts on port 1234
lms load qwen2.5-coder --context-length 32768
```

Then configure Hermes:

```bash
hermes model
# Select "Custom endpoint (self-hosted / VLLM / etc.)"
# Enter URL: http://localhost:1234/v1
# Skip API key (LM Studio doesn't require one)
# Enter model name
```

:::caution Context length often defaults to 2048
LM Studio reads context length from the model's metadata, but many GGUF models report low defaults (2048 or 4096). **Always set context length explicitly** in the LM Studio model settings:

1. Click the gear icon next to the model picker
2. Set "Context Length" to at least 16384 (preferably 32768)
3. Reload the model for the change to take effect

Alternatively, use the CLI: `lms load model-name --context-length 32768`

To set persistent per-model defaults: My Models tab → gear icon on the model → set context size.
:::

**Tool calling:** Supported since LM Studio 0.3.6. Models with native tool-calling training (Qwen 2.5, Llama 3.x, Mistral, Hermes) are auto-detected and shown with a tool badge. Other models use a generic fallback that may be less reliable.

---

### WSL2 Networking (Windows Users)

Since Hermes Agent requires a Unix environment, Windows users run it inside WSL2. If your model server (Ollama, LM Studio, etc.) runs on the **Windows host**, you need to bridge the network gap — WSL2 uses a virtual network adapter with its own subnet, so `localhost` inside WSL2 refers to the Linux VM, **not** the Windows host.

:::tip Both in WSL2? No problem.
If your model server also runs inside WSL2 (common for vLLM, SGLang, and llama-server), `localhost` works as expected — they share the same network namespace. Skip this section.
:::

#### Option 1: Mirrored Networking Mode (Recommended)

Available on **Windows 11 22H2+**, mirrored mode makes `localhost` work bidirectionally between Windows and WSL2 — the simplest fix.

1. Create or edit `%USERPROFILE%\.wslconfig` (e.g., `C:\Users\YourName\.wslconfig`):
   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

2. Restart WSL from PowerShell:
   ```powershell
   wsl --shutdown
   ```

3. Reopen your WSL2 terminal. `localhost` now reaches Windows services:
   ```bash
   curl http://localhost:11434/v1/models   # Ollama on Windows — works
   ```

:::note Hyper-V Firewall
On some Windows 11 builds, the Hyper-V firewall blocks mirrored connections by default. If `localhost` still doesn't work after enabling mirrored mode, run this in an **Admin PowerShell**:
```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```
:::

#### Option 2: Use the Windows Host IP (Windows 10 / older builds)

If you can't use mirrored mode, find the Windows host IP from inside WSL2 and use that instead of `localhost`:

```bash
# Get the Windows host IP (the default gateway of WSL2's virtual network)
ip route show | grep -i default | awk '{ print $3 }'
# Example output: 172.29.192.1
```

Use that IP in your Hermes config:

```yaml
model:
  default: qwen2.5-coder:32b
  provider: custom
  base_url: http://172.29.192.1:11434/v1   # Windows host IP, not localhost
```

:::tip Dynamic helper
The host IP can change on WSL2 restart. You can grab it dynamically in your shell:
```bash
export WSL_HOST=$(ip route show | grep -i default | awk '{ print $3 }')
echo "Windows host at: $WSL_HOST"
curl http://$WSL_HOST:11434/v1/models   # Test Ollama
```

Or use your machine's mDNS name (requires `libnss-mdns` in WSL2):
```bash
sudo apt install libnss-mdns
curl http://$(hostname).local:11434/v1/models
```
:::

#### Server Bind Address (Required for NAT Mode)

If you're using **Option 2** (NAT mode with the host IP), the model server on Windows must accept connections from outside `127.0.0.1`. By default, most servers only listen on localhost — WSL2 connections in NAT mode come from a different virtual subnet and will be refused. In mirrored mode, `localhost` maps directly so the default `127.0.0.1` binding works fine.

| Server | Default bind | How to fix |
|--------|-------------|------------|
| **Ollama** | `127.0.0.1` | Set `OLLAMA_HOST=0.0.0.0` environment variable before starting Ollama (System Settings → Environment Variables on Windows, or edit the Ollama service) |
| **LM Studio** | `127.0.0.1` | Enable **"Serve on Network"** in the Developer tab → Server settings |
| **llama-server** | `127.0.0.1` | Add `--host 0.0.0.0` to the startup command |
| **vLLM** | `0.0.0.0` | Already binds to all interfaces by default |
| **SGLang** | `127.0.0.1` | Add `--host 0.0.0.0` to the startup command |

**Ollama on Windows (detailed):** Ollama runs as a Windows service. To set `OLLAMA_HOST`:
1. Open **System Properties** → **Environment Variables**
2. Add a new **System variable**: `OLLAMA_HOST` = `0.0.0.0`
3. Restart the Ollama service (or reboot)

#### Windows Firewall

Windows Firewall treats WSL2 as a separate network (in both NAT and mirrored mode). If connections still fail after the steps above, add a firewall rule for your model server's port:

```powershell
# Run in Admin PowerShell — replace PORT with your server's port
New-NetFirewallRule -DisplayName "Allow WSL2 to Model Server" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11434
```

Common ports: Ollama `11434`, vLLM `8000`, SGLang `30000`, llama-server `8080`, LM Studio `1234`.

#### Quick Verification

From inside WSL2, test that you can reach your model server:

```bash
# Replace URL with your server's address and port
curl http://localhost:11434/v1/models          # Mirrored mode
curl http://172.29.192.1:11434/v1/models       # NAT mode (use your actual host IP)
```

If you get a JSON response listing your models, you're good. Use that same URL as the `base_url` in your Hermes config.

---

### Troubleshooting Local Models

These issues affect **all** local inference servers when used with Hermes.

#### "Connection refused" from WSL2 to a Windows-hosted model server

If you're running Hermes inside WSL2 and your model server on the Windows host, `http://localhost:<port>` won't work in WSL2's default NAT networking mode. See [WSL2 Networking](#wsl2-networking-windows-users) above for the fix.

#### Tool calls appear as text instead of executing

The model outputs something like `{"name": "web_search", "arguments": {...}}` as a message instead of actually calling the tool.

**Cause:** Your server doesn't have tool calling enabled, or the model doesn't support it through the server's tool calling implementation.

| Server | Fix |
|--------|-----|
| **llama.cpp** | Add `--jinja` to the startup command |
| **vLLM** | Add `--enable-auto-tool-choice --tool-call-parser hermes` |
| **SGLang** | Add `--tool-call-parser qwen` (or appropriate parser) |
| **Ollama** | Tool calling is enabled by default — make sure your model supports it (check with `ollama show model-name`) |
| **LM Studio** | Update to 0.3.6+ and use a model with native tool support |

#### Model seems to forget context or give incoherent responses

**Cause:** Context window is too small. When the conversation exceeds the context limit, most servers silently drop older messages. Hermes's system prompt + tool schemas alone can use 4k–8k tokens.

**Diagnosis:**

```bash
# Check what Hermes thinks the context is
# Look at startup line: "Context limit: X tokens"

# Check your server's actual context
# Ollama: ollama ps (CONTEXT column)
# llama.cpp: curl http://localhost:8080/props | jq '.default_generation_settings.n_ctx'
# vLLM: check --max-model-len in startup args
```

**Fix:** Set context to at least **32,768 tokens** for agent use. See each server's section above for the specific flag.

#### "Context limit: 2048 tokens" at startup

Hermes auto-detects context length from your server's `/v1/models` endpoint. If the server reports a low value (or doesn't report one at all), Hermes uses the model's declared limit which may be wrong.

**Fix:** Set it explicitly in `config.yaml`:

```yaml
model:
  default: your-model
  provider: custom
  base_url: http://localhost:11434/v1
  context_length: 32768
```

#### Responses get cut off mid-sentence

**Possible causes:**
1. **Low `max_tokens` on the server** — SGLang defaults to 128 tokens per response. Set `--default-max-tokens` on the server or configure Hermes with `model.max_tokens` in config.yaml.
2. **Context exhaustion** — The model filled its context window. Increase context length or enable [context compression](/docs/user-guide/configuration#context-compression) in Hermes.

---

### LiteLLM Proxy — Multi-Provider Gateway

[LiteLLM](https://docs.litellm.ai/) is an OpenAI-compatible proxy that unifies 100+ LLM providers behind a single API. Best for: switching between providers without config changes, load balancing, fallback chains, budget controls.

```bash
# Install and start
pip install "litellm[proxy]"
litellm --model anthropic/claude-sonnet-4 --port 4000

# Or with a config file for multiple models:
litellm --config litellm_config.yaml --port 4000
```

Then configure Hermes with `hermes model` → Custom endpoint → `http://localhost:4000/v1`.

Example `litellm_config.yaml` with fallback:
```yaml
model_list:
  - model_name: "best"
    litellm_params:
      model: anthropic/claude-sonnet-4
      api_key: sk-ant-...
  - model_name: "best"
    litellm_params:
      model: openai/gpt-4o
      api_key: sk-...
router_settings:
  routing_strategy: "latency-based-routing"
```

---

### ClawRouter — Cost-Optimized Routing

[ClawRouter](https://github.com/BlockRunAI/ClawRouter) by BlockRunAI is a local routing proxy that auto-selects models based on query complexity. It classifies requests across 14 dimensions and routes to the cheapest model that can handle the task. Payment is via USDC cryptocurrency (no API keys).

```bash
# Install and start
npx @blockrun/clawrouter    # Starts on port 8402
```

Then configure Hermes with `hermes model` → Custom endpoint → `http://localhost:8402/v1` → model name `blockrun/auto`.

Routing profiles:
| Profile | Strategy | Savings |
|---------|----------|---------|
| `blockrun/auto` | Balanced quality/cost | 74-100% |
| `blockrun/eco` | Cheapest possible | 95-100% |
| `blockrun/premium` | Best quality models | 0% |
| `blockrun/free` | Free models only | 100% |
| `blockrun/agentic` | Optimized for tool use | varies |

:::note
ClawRouter requires a USDC-funded wallet on Base or Solana for payment. All requests route through BlockRun's backend API. Run `npx @blockrun/clawrouter doctor` to check wallet status.
:::

---

### Other Compatible Providers

Any service with an OpenAI-compatible API works. Some popular options:

| Provider | Base URL | Notes |
|----------|----------|-------|
| [Together AI](https://together.ai) | `https://api.together.xyz/v1` | Cloud-hosted open models |
| [Groq](https://groq.com) | `https://api.groq.com/openai/v1` | Ultra-fast inference |
| [DeepSeek](https://deepseek.com) | `https://api.deepseek.com/v1` | DeepSeek models |
| [Fireworks AI](https://fireworks.ai) | `https://api.fireworks.ai/inference/v1` | Fast open model hosting |
| [Cerebras](https://cerebras.ai) | `https://api.cerebras.ai/v1` | Wafer-scale chip inference |
| [Mistral AI](https://mistral.ai) | `https://api.mistral.ai/v1` | Mistral models |
| [OpenAI](https://openai.com) | `https://api.openai.com/v1` | Direct OpenAI access |
| [Azure OpenAI](https://azure.microsoft.com) | `https://YOUR.openai.azure.com/` | Enterprise OpenAI |
| [LocalAI](https://localai.io) | `http://localhost:8080/v1` | Self-hosted, multi-model |
| [Jan](https://jan.ai) | `http://localhost:1337/v1` | Desktop app with local models |

Configure any of these with `hermes model` → Custom endpoint, or in `config.yaml`:

```yaml
model:
  default: meta-llama/Llama-3.1-70B-Instruct-Turbo
  provider: custom
  base_url: https://api.together.xyz/v1
  api_key: your-together-key
```

---

### Context Length Detection

Hermes uses a multi-source resolution chain to detect the correct context window for your model and provider:

1. **Config override** — `model.context_length` in config.yaml (highest priority)
2. **Custom provider per-model** — `custom_providers[].models.<id>.context_length`
3. **Persistent cache** — previously discovered values (survives restarts)
4. **Endpoint `/models`** — queries your server's API (local/custom endpoints)
5. **Anthropic `/v1/models`** — queries Anthropic's API for `max_input_tokens` (API-key users only)
6. **OpenRouter API** — live model metadata from OpenRouter
7. **Nous Portal** — suffix-matches Nous model IDs against OpenRouter metadata
8. **[models.dev](https://models.dev)** — community-maintained registry with provider-specific context lengths for 3800+ models across 100+ providers
9. **Fallback defaults** — broad model family patterns (128K default)

For most setups this works out of the box. The system is provider-aware — the same model can have different context limits depending on who serves it (e.g., `claude-opus-4.6` is 1M on Anthropic direct but 128K on GitHub Copilot).

To set the context length explicitly, add `context_length` to your model config:

```yaml
model:
  default: "qwen3.5:9b"
  base_url: "http://localhost:8080/v1"
  context_length: 131072  # tokens
```

For custom endpoints, you can also set context length per model:

```yaml
custom_providers:
  - name: "My Local LLM"
    base_url: "http://localhost:11434/v1"
    models:
      qwen3.5:27b:
        context_length: 32768
      deepseek-r1:70b:
        context_length: 65536
```

`hermes model` will prompt for context length when configuring a custom endpoint. Leave it blank for auto-detection.

:::tip When to set this manually
- You're using Ollama with a custom `num_ctx` that's lower than the model's maximum
- You want to limit context below the model's maximum (e.g., 8k on a 128k model to save VRAM)
- You're running behind a proxy that doesn't expose `/v1/models`
:::

---

### Named Custom Providers

If you work with multiple custom endpoints (e.g., a local dev server and a remote GPU server), you can define them as named custom providers in `config.yaml`:

```yaml
custom_providers:
  - name: local
    base_url: http://localhost:8080/v1
    # api_key omitted — Hermes uses "no-key-required" for keyless local servers
  - name: work
    base_url: https://gpu-server.internal.corp/v1
    api_key: corp-api-key
    api_mode: chat_completions   # optional, auto-detected from URL
  - name: anthropic-proxy
    base_url: https://proxy.example.com/anthropic
    api_key: proxy-key
    api_mode: anthropic_messages  # for Anthropic-compatible proxies
```

Switch between them mid-session with the triple syntax:

```
/model custom:local:qwen-2.5       # Use the "local" endpoint with qwen-2.5
/model custom:work:llama3-70b      # Use the "work" endpoint with llama3-70b
/model custom:anthropic-proxy:claude-sonnet-4  # Use the proxy
```

You can also select named custom providers from the interactive `hermes model` menu.

---

### Choosing the Right Setup

| Use Case | Recommended |
|----------|-------------|
| **Just want it to work** | OpenRouter (default) or Nous Portal |
| **Local models, easy setup** | Ollama |
| **Production GPU serving** | vLLM or SGLang |
| **Mac / no GPU** | Ollama or llama.cpp |
| **Multi-provider routing** | LiteLLM Proxy or OpenRouter |
| **Cost optimization** | ClawRouter or OpenRouter with `sort: "price"` |
| **Maximum privacy** | Ollama, vLLM, or llama.cpp (fully local) |
| **Enterprise / Azure** | Azure OpenAI with custom endpoint |
| **Chinese AI models** | z.ai (GLM), Kimi/Moonshot, or MiniMax (first-class providers) |

:::tip
You can switch between providers at any time with `hermes model` — no restart required. Your conversation history, memory, and skills carry over regardless of which provider you use.
:::

## Optional API Keys

| Feature | Provider | Env Variable |
|---------|----------|--------------|
| Web scraping | [Firecrawl](https://firecrawl.dev/) | `FIRECRAWL_API_KEY`, `FIRECRAWL_API_URL` |
| Browser automation | [Browserbase](https://browserbase.com/) | `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` |
| Image generation | [FAL](https://fal.ai/) | `FAL_KEY` |
| Premium TTS voices | [ElevenLabs](https://elevenlabs.io/) | `ELEVENLABS_API_KEY` |
| OpenAI TTS + voice transcription | [OpenAI](https://platform.openai.com/api-keys) | `VOICE_TOOLS_OPENAI_KEY` |
| RL Training | [Tinker](https://tinker-console.thinkingmachines.ai/) + [WandB](https://wandb.ai/) | `TINKER_API_KEY`, `WANDB_API_KEY` |
| Cross-session user modeling | [Honcho](https://honcho.dev/) | `HONCHO_API_KEY` |
| Semantic long-term memory | [Supermemory](https://supermemory.ai) | `SUPERMEMORY_API_KEY` |

### Self-Hosting Firecrawl

By default, Hermes uses the [Firecrawl cloud API](https://firecrawl.dev/) for web search and scraping. If you prefer to run Firecrawl locally, you can point Hermes at a self-hosted instance instead. See Firecrawl's [SELF_HOST.md](https://github.com/firecrawl/firecrawl/blob/main/SELF_HOST.md) for complete setup instructions.

**What you get:** No API key required, no rate limits, no per-page costs, full data sovereignty.

**What you lose:** The cloud version uses Firecrawl's proprietary "Fire-engine" for advanced anti-bot bypassing (Cloudflare, CAPTCHAs, IP rotation). Self-hosted uses basic fetch + Playwright, so some protected sites may fail. Search uses DuckDuckGo instead of Google.

**Setup:**

1. Clone and start the Firecrawl Docker stack (5 containers: API, Playwright, Redis, RabbitMQ, PostgreSQL — requires ~4-8 GB RAM):
   ```bash
   git clone https://github.com/firecrawl/firecrawl
   cd firecrawl
   # In .env, set: USE_DB_AUTHENTICATION=false, HOST=0.0.0.0, PORT=3002
   docker compose up -d
   ```

2. Point Hermes at your instance (no API key needed):
   ```bash
   hermes config set FIRECRAWL_API_URL http://localhost:3002
   ```

You can also set both `FIRECRAWL_API_KEY` and `FIRECRAWL_API_URL` if your self-hosted instance has authentication enabled.

## OpenRouter Provider Routing

When using OpenRouter, you can control how requests are routed across providers. Add a `provider_routing` section to `~/.hermes/config.yaml`:

```yaml
provider_routing:
  sort: "throughput"          # "price" (default), "throughput", or "latency"
  # only: ["anthropic"]      # Only use these providers
  # ignore: ["deepinfra"]    # Skip these providers
  # order: ["anthropic", "google"]  # Try providers in this order
  # require_parameters: true  # Only use providers that support all request params
  # data_collection: "deny"   # Exclude providers that may store/train on data
```

**Shortcuts:** Append `:nitro` to any model name for throughput sorting (e.g., `anthropic/claude-sonnet-4:nitro`), or `:floor` for price sorting.

## Fallback Model

Configure a backup provider:model that Hermes switches to automatically when your primary model fails (rate limits, server errors, auth failures):

```yaml
fallback_model:
  provider: openrouter                    # required
  model: anthropic/claude-sonnet-4        # required
  # base_url: http://localhost:8000/v1    # optional, for custom endpoints
  # api_key_env: MY_CUSTOM_KEY           # optional, env var name for custom endpoint API key
```

When activated, the fallback swaps the model and provider mid-session without losing your conversation. It fires **at most once** per session.

Supported providers: `openrouter`, `nous`, `openai-codex`, `copilot`, `copilot-acp`, `anthropic`, `huggingface`, `zai`, `kimi-coding`, `minimax`, `minimax-cn`, `deepseek`, `ai-gateway`, `opencode-zen`, `opencode-go`, `kilocode`, `alibaba`, `custom`.

:::tip
Fallback is configured exclusively through `config.yaml` — there are no environment variables for it. For full details on when it triggers, supported providers, and how it interacts with auxiliary tasks and delegation, see [Fallback Providers](/docs/user-guide/features/fallback-providers).
:::

## Smart Model Routing

Optional cheap-vs-strong routing lets Hermes keep your main model for complex work while sending very short/simple turns to a cheaper model.

```yaml
smart_model_routing:
  enabled: true
  max_simple_chars: 160
  max_simple_words: 28
  cheap_model:
    provider: openrouter
    model: google/gemini-2.5-flash
    # base_url: http://localhost:8000/v1  # optional custom endpoint
    # api_key_env: MY_CUSTOM_KEY          # optional env var name for that endpoint's API key
```

How it works:
- If a turn is short, single-line, and does not look code/tool/debug heavy, Hermes may route it to `cheap_model`
- If the turn looks complex, Hermes stays on your primary model/provider
- If the cheap route cannot be resolved cleanly, Hermes falls back to the primary model automatically

This is intentionally conservative. It is meant for quick, low-stakes turns like:
- short factual questions
- quick rewrites
- lightweight summaries

It will avoid routing prompts that look like:
- coding/debugging work
- tool-heavy requests
- long or multi-line analysis asks

Use this when you want lower latency or cost without fully changing your default model.

---

## See Also

- [Configuration](/docs/user-guide/configuration) — General configuration (directory structure, config precedence, terminal backends, memory, compression, and more)
- [Environment Variables](/docs/reference/environment-variables) — Complete reference of all environment variables

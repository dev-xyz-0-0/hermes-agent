# General guide: adding model providers to Hermes

I would standardize your customized Hermes around this lifecycle:

```text
                     Hermes
                        │
                model.provider
                        │
                        ▼
               PROVIDER_REGISTRY
                        │
            ┌───────────┴───────────┐
            │                       │
         API Key                  OAuth
            │                       │
            ▼                       ▼
       environment            auth_<name>.py
            │                       │
            └───────────┬───────────┘
                        ▼
              Runtime Credentials
                        │
              ┌─────────┴─────────┐
              │                   │
        OpenAI-compatible     Responses API
              │                   │
              ▼                   ▼
          /chat/...           /responses
              │                   │
              └─────────┬─────────┘
                        ▼
                      LLM
```

There are therefore **four separate questions** whenever you add a model:

1. Who is the **provider**?
2. How does Hermes **authenticate**?
3. Which **API protocol** does it use?
4. Which **model IDs** does that provider expose?

Do not combine these concepts.

For example:

```text
Provider       Auth             Transport              Model
────────────────────────────────────────────────────────────────
OpenAI         API key          Responses/Chat          gpt-*
OpenAI Codex   OAuth            Responses               gpt-*
xAI            API key          OpenAI-compatible       grok-*
xAI OAuth      OAuth            Responses               grok-*
Anthropic      API key/OAuth    Anthropic Messages      claude-*
OpenRouter     API key          OpenAI-compatible       many
```

That separation will make your fork much easier to maintain.

## Step 1 — Register the provider

Every provider should first get an entry in:

```python
PROVIDER_REGISTRY
```

For a basic API-key provider:

```python
"example": ProviderConfig(
    id="example",
    name="Example AI",
    auth_type="api_key",
    inference_base_url="https://api.example.com/v1",
    api_key_env_vars=("EXAMPLE_API_KEY",),
    base_url_env_var="EXAMPLE_BASE_URL",
),
```

Commit:

```bash
git commit -m "feat(provider): register Example AI provider"
```

This commit should contain **no transport implementation**.

---

# Step 2 — Decide authentication type

I recommend supporting three auth classes in your customized Hermes.

### Type A — API key

Simplest:

```text
.env
 │
 └── XAI_API_KEY
       │
       ▼
auth.py
       │
       ▼
runtime credentials
```

Provider:

```python
ProviderConfig(
    id="xai",
    name="xAI",
    auth_type="api_key",
    inference_base_url="https://api.x.ai/v1",
    api_key_env_vars=("XAI_API_KEY",),
)
```

No separate authentication module should be necessary.

Commit:

```bash
git commit -m "feat(auth): support Example AI API key"
```

### Type B — OAuth

This is the pattern we're implementing now:

```text
auth.py
   │
   └── xai-oauth
          │
          ▼
    auth_xai.py
          │
          ├── login
          ├── refresh
          ├── persistence
          └── runtime resolution
```

Keep OAuth complexity outside `auth.py`.

I recommend naming these consistently:

```text
auth_xai.py
auth_anthropic.py
auth_codex.py
auth_google.py
```

Each module should expose approximately:

```python
login_<provider>()
refresh_<provider>()
get_<provider>_auth_status()
resolve_<provider>_runtime_credentials()
```

For xAI:

```python
_login_xai_oauth()

refresh_xai_oauth_pure()

get_xai_oauth_auth_status()

resolve_xai_oauth_runtime_credentials()
```

Commit:

```bash
git commit -m "feat(auth): add Example AI OAuth authentication"
```

### Type C — external CLI/session authentication

Some providers may be better implemented through an existing CLI/session rather than handling consumer OAuth yourself.

Conceptually:

```text
Hermes
   │
   ▼
Provider adapter
   │
   ▼
External CLI
   │
   ├── owns authentication
   └── owns refresh
```

Keep that separate from API-key/OAuth provider logic.

---

# Step 3 — Standardize runtime credentials

This is the interface I would make universal in your fork.

Every authentication mechanism eventually returns:

```python
{
    "provider": "xai-oauth",
    "api_key": "...",
    "base_url": "https://api.x.ai/v1",
    "source": "hermes-auth-store",
}
```

Potentially add:

```python
{
    "api_mode": "responses"
}
```

Then your agent loop doesn't care whether the token originated from:

```text
.env
API key
OAuth
device code
credential store
external CLI
```

It simply does:

```python
credentials = resolve_runtime_credentials(provider)
```

followed by:

```python
client = create_client(credentials)
```

This is the most important architectural boundary to preserve.

Commit:

```bash
git commit -m "feat(provider): resolve Example AI runtime credentials"
```

---

# Step 4 — Map provider to transport

Do not create a new HTTP implementation for every provider.

Create a small number of transports.

I would aim for:

```text
agent/providers/

├── openai_chat.py
├── responses.py
├── anthropic_messages.py
└── ...
```

Then map:

```python
PROVIDER_TRANSPORTS = {
    "openai": "responses",
    "openai-codex": "responses",

    "xai": "openai_chat",
    "xai-oauth": "responses",

    "anthropic": "anthropic_messages",

    "openrouter": "openai_chat",
}
```

This means adding:

```text
Provider #20
```

doesn't necessarily mean writing:

```text
Transport #20
```

That is a major maintainability improvement.

---

# Step 5 — Keep models separate from providers

Avoid:

```python
if model == "grok-4.6":
    provider = "xai-oauth"
```

because Grok might be available through multiple routes.

Instead:

```yaml
model:
  provider: xai-oauth
  default: grok-4.6
```

or:

```yaml
model:
  provider: xai
  default: grok-4.6
```

Same model:

```text
                 grok-4.6
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
         xai              xai-oauth
          │                   │
       API key              OAuth
```

Likewise:

```text
                 Claude
                    │
         ┌──────────┴─────────┐
         ▼                    ▼
     Anthropic            another router
```

This prevents your model catalog from becoming coupled to authentication.

---

# Step 6 — Add aliases carefully

Aliases should improve UX without silently changing authentication.

Good:

```python
PROVIDER_ALIASES = {
    "x-ai": "xai",
    "x.ai": "xai",
    "grok": "xai",

    "grok-oauth": "xai-oauth",
}
```

Bad:

```python
"grok": "xai-oauth"
```

if `grok` previously meant API-key xAI.

That would break existing configurations.

Commit:

```bash
git commit -m "feat(provider): add Example AI provider aliases"
```

---

# Step 7 — Tests every provider must pass

I would create a standard provider contract.

Every new provider needs:

```text
Registration
✓ provider exists
✓ aliases resolve
✓ provider name correct

Authentication
✓ missing credentials fail cleanly
✓ valid credentials resolve
✓ secrets never appear in status output

Runtime
✓ correct base URL
✓ correct authorization
✓ correct transport
✓ model preserved

OAuth providers
✓ login
✓ token persistence
✓ expiry detection
✓ refresh
✓ refresh-token rotation
✓ invalid refresh handling

Transport
✓ request serialization
✓ response parsing
✓ tool calls
✓ errors

Regression
✓ existing providers unchanged
```

Then your provider-specific test file becomes:

```text
tests/
├── test_provider_contract.py
├── test_xai_oauth.py
├── test_anthropic.py
├── test_openai_codex.py
└── test_openrouter.py
```

---

# Step 8 — Use a consistent Git workflow

For each future provider:

```bash
git checkout -b feature/provider-<name>
```

Then:

```text
Commit 1
feat(provider): register <provider>

Commit 2
feat(auth): add <provider> authentication

Commit 3
feat(provider): add <provider> runtime credential resolution

Commit 4
feat(inference): route <provider> through <transport>

Commit 5
feat(model): add <provider> model catalog

Commit 6
test(provider): add <provider> integration coverage

Commit 7
docs(provider): document <provider> setup
```

This makes cherry-picking and reverting much safer.

## Recommended target for your fork

Given the work you've already done, I would gradually move toward:

```text
hermes_cli/
├── auth.py
│    └── common auth/router
│
├── auth_xai.py
│    └── xAI OAuth
│
├── auth_anthropic.py
│    └── Anthropic OAuth
│
├── auth_codex.py
│    └── OpenAI Codex OAuth
│
└── providers.py
     └── provider registry

agent/
├── providers/
│   ├── openai_chat.py
│   ├── responses.py
│   └── anthropic.py
│
└── model_catalog.py

tests/
├── test_auth_xai.py
├── test_auth_anthropic.py
├── test_auth_codex.py
├── test_provider_registry.py
└── test_provider_transports.py
```

The rule I would follow is:

> **Provider → Authentication → Runtime Credentials → Transport → Model**

rather than:

> **Model → custom implementation**

That gives you a reusable provider architecture where adding Grok, Claude, OpenAI/Codex, Gemini, OpenRouter, or another provider becomes a small integration rather than another modification to the core agent.

For the current xAI work, your next commits should therefore be `feat(auth): register xAI OAuth provider` → `feat(auth): add xAI device-code OAuth authentication` → `feat(provider): resolve xAI OAuth runtime credentials` → `test(auth): add xAI OAuth authentication coverage`.
# General Guide: Adding Model Providers to Hermes

The cleanest way to add a model provider to Hermes is to treat **provider configuration, authentication, model discovery, runtime credentials, and inference transport as separate concerns**.

The target lifecycle is:

```text
Provider
   │
   ▼
Authentication
   │
   ▼
Runtime Credentials
   │
   ▼
Model Discovery
   │
   ▼
Model Selection
   │
   ▼
Inference Transport
   │
   ▼
Model API
```

For example, the xAI OAuth integration becomes:

```text
xai-oauth
   │
   ├── auth.py
   │      provider registration
   │
   ├── auth_xai.py
   │      OAuth login
   │      token storage
   │      token refresh
   │      runtime credentials
   │
   ├── xai_models.py
   │      model discovery
   │      fallback models
   │      model filtering
   │      retirement handling
   │
   └── main.py
          login orchestration
          model selection
          configuration update
```

## 1. Define the provider

Start by deciding what the provider actually represents.

Do not assume:

```text
provider == model
```

A provider describes **how Hermes connects**, while the model is what Hermes requests.

For example:

```text
Provider        Authentication     Model
------------------------------------------------
openai-api      API key            gpt-*
openai-codex    OAuth              gpt-*
xai             API key            grok-*
xai-oauth       OAuth              grok-*
anthropic       API key/OAuth      claude-*
```

This distinction is particularly important when one vendor supports multiple authentication mechanisms.

For xAI, we deliberately preserve:

```text
xai
    API-key authentication

xai-oauth
    OAuth authentication
```

rather than replacing `xai`.

---

# 2. Register the provider in `auth.py`

Add the provider to `PROVIDER_REGISTRY`.

Conceptually:

```python
"xai-oauth": ProviderConfig(
    id="xai-oauth",
    name="xAI Grok OAuth",
    auth_type="oauth_external",
    inference_base_url="https://api.x.ai/v1",
),
```

If aliases are required, make them explicit:

```python
"xai-oauth": "xai-oauth",
"grok-oauth": "xai-oauth",
```

Avoid changing an existing alias unexpectedly.

For example, if Hermes already has:

```python
"grok": "xai"
```

keep it that way.

Otherwise an existing command using API-key authentication could silently switch to OAuth.

---

# 3. Isolate complex authentication

Simple API-key providers can often remain inside `auth.py`.

OAuth providers should normally receive their own module:

```text
hermes_cli/
├── auth.py
├── auth_codex.py
├── auth_xai.py
└── ...
```

The provider-specific authentication module should own things such as:

```text
OAuth discovery
device authorization
browser/device login
polling
access tokens
refresh tokens
token expiration
token rotation
provider-specific OAuth errors
```

For xAI:

```text
auth_xai.py
│
├── discovery
├── device code request
├── device authorization polling
├── token persistence
├── JWT expiration checking
├── refresh
└── runtime credential resolution
```

The rest of Hermes should not need to understand those details.

---

# 4. Standardize runtime credentials

This is one of the most important boundaries.

Every authentication mechanism should eventually produce something resembling:

```python
{
    "provider": "xai-oauth",
    "base_url": "https://api.x.ai/v1",
    "api_key": access_token,
    "source": "hermes-auth-store",
    "auth_mode": "oauth_device_code",
}
```

The name `api_key` can represent the runtime bearer credential even when it originated from OAuth.

That allows inference code to remain relatively authentication-agnostic:

```text
OAuth
  │
  └──────┐
         │
API key ─┼──► Runtime Credentials ───► Inference
         │
CLI auth ┘
```

The runtime resolver should also handle refresh when necessary:

```python
creds = resolve_xai_oauth_runtime_credentials()
```

Conceptually:

```text
load token
    │
    ▼
token valid?
 │       │
yes      no
 │       │
 │       ▼
 │    refresh
 │       │
 └───┬───┘
     ▼
runtime credentials
```

---

# 5. Keep status separate from credentials

Do not use a status function as your main credential API.

Prefer:

```python
status = get_xai_oauth_auth_status()

if status["logged_in"]:
    ...
```

for UI/status purposes.

Then:

```python
creds = resolve_xai_oauth_runtime_credentials()
```

for inference.

This keeps bearer tokens out of status output.

The separation should be:

```text
get_*_auth_status()
│
├── logged_in
├── provider
├── account information
└── safe metadata


resolve_*_runtime_credentials()
│
├── api_key / bearer
├── base_url
├── provider
├── auth mode
└── refresh handling
```

---

# 6. Create a provider-specific model module

Providers with dynamic model catalogs should have a dedicated model module.

For example:

```text
hermes_cli/
├── codex_models.py
├── xai_models.py
└── ...
```

Expose a small public interface:

```python
get_xai_model_ids(
    access_token=None,
    base_url=None,
)
```

The caller should not need to understand how model discovery works.

---

# 7. Use live model discovery where supported

For xAI:

```text
OAuth credential
      │
      ▼
GET /v1/models
      │
      ▼
parse data[]
      │
      ▼
filter models
      │
      ▼
model picker
```

Conceptually:

```python
models = get_xai_model_ids(
    access_token=creds["api_key"],
    base_url=creds["base_url"],
)
```

The discovery module should handle failures gracefully.

A useful resolution order is:

```text
live provider API
       │
       ▼
successful?
 │          │
yes         no
 │          │
 ▼          ▼
models    curated defaults
```

This mirrors the general approach used by `codex_models.py`, although provider-specific fallback mechanisms can differ. Your Codex implementation, for example, also uses local configuration/cache and synthetic forward-compatible models. Pasted markdown

---

# 8. Maintain conservative fallback models

Do not make Hermes unusable merely because `/models` is temporarily unavailable.

Provide a small fallback:

```python
DEFAULT_XAI_MODELS = [
    "grok-4.6",
]
```

Then:

```python
if live_models:
    return live_models

return DEFAULT_XAI_MODELS
```

The fallback catalog should be conservative rather than trying to maintain every model ever released.

Live discovery should remain authoritative whenever possible.

---

# 9. Handle provider-specific model lifecycle rules

Different providers may require special model handling.

For xAI we incorporated retirement detection directly into `xai_models.py`:

```text
model returned
    │
    ▼
valid Grok model?
    │
    ▼
retired?
 │       │
yes      no
 │       │
skip     keep
```

It can also detect an old model already present in Hermes configuration:

```python
info = get_retirement_info(current_model)
```

and recommend:

```text
old model
    │
    ▼
replacement model
    +
optional configuration changes
```

Keep this provider-specific logic out of generic Hermes code.

---

# 10. Add the model flow to `main.py`

`main.py` should orchestrate the pieces rather than implement them.

For example:

```python
def _model_flow_xai_oauth(config, current_model=""):
    ...
```

Its responsibilities should remain roughly:

```text
1. Check configured model
2. Check authentication
3. Login if required
4. Resolve runtime credentials
5. Discover models
6. Ask user to select model
7. Save selection
8. Update provider configuration
```

It should call provider modules rather than implementing OAuth or API parsing itself.

The resulting xAI flow is:

```text
_model_flow_xai_oauth()
        │
        ├── get_xai_oauth_auth_status()
        │
        ├── _login_xai_oauth()
        │
        ├── resolve_xai_oauth_runtime_credentials()
        │
        ├── get_xai_model_ids()
        │
        ├── _prompt_model_selection()
        │
        ├── _save_model_choice()
        │
        └── _update_config_for_provider()
```

---

# 11. Keep inference transport separate

Authentication alone does not make a provider compatible with Hermes.

You also need to know the API protocol used for inference.

Examples might include:

```text
Provider       Authentication     Transport
--------------------------------------------------
OpenAI API     API key            Responses / Chat
Codex          OAuth              Responses
xAI API        API key            OpenAI-compatible
xAI OAuth      OAuth              Responses-compatible
Anthropic      API key/OAuth      Messages
```

This distinction prevents provider-specific conditionals from spreading throughout the agent.

A longer-term structure could be:

```text
agent/providers/
├── openai_chat.py
├── responses.py
└── anthropic_messages.py
```

with provider configuration deciding which transport to use.

---

# 12. Test each layer independently

A provider integration should have at least three categories of tests.

### Authentication tests

For xAI:

```text
tests/test_xai_oauth.py
```

Test:

```text
device authorization
polling
token storage
token refresh
refresh-token rotation
expiration handling
status secret omission
runtime credential resolution
```

### Model tests

```text
tests/test_xai_models.py
```

Test:

```text
normalization
model filtering
deduplication
live discovery
Authorization header
API failure
fallback models
retirement detection
replacement recommendation
```

### Model-flow integration

Also verify:

```text
runtime credential
      │
      ▼
get_xai_model_ids()
      │
      ▼
_prompt_model_selection()
```

Your existing Codex test follows this pattern by checking that the runtime access token reaches `get_codex_model_ids()`. Pasted markdown

---

# 13. Recommended provider contract

For future providers, aim for a predictable interface.

Authentication:

```python
get_<provider>_auth_status()

_login_<provider>()

resolve_<provider>_runtime_credentials()
```

Models:

```python
get_<provider>_model_ids()
```

Main flow:

```python
_model_flow_<provider>()
```

For example:

```text
xAI
────────────────────────────────────────────

get_xai_oauth_auth_status()

_login_xai_oauth()

resolve_xai_oauth_runtime_credentials()

get_xai_model_ids()

_model_flow_xai_oauth()
```

This makes additional integrations much easier to reason about.

---

# 14. Recommended directory structure

For your customized Hermes version, a practical structure is:

```text
hermes-agent/
│
├── hermes_cli/
│   │
│   ├── main.py
│   ├── auth.py
│   │
│   ├── auth_codex.py
│   ├── codex_models.py
│   │
│   ├── auth_xai.py
│   ├── xai_models.py
│   │
│   └── ...
│
├── tests/
│   │
│   ├── test_codex_models.py
│   │
│   ├── test_xai_oauth.py
│   ├── test_xai_models.py
│   │
│   └── ...
│
└── config.yaml
```

The dependency direction should stay simple:

```text
main.py
   │
   ├──────► auth.py
   │
   │          │
   │          └────► auth_xai.py
   │
   └──────► xai_models.py
```

Avoid making:

```text
auth_xai.py → main.py
xai_models.py → main.py
```

That reduces circular-import problems.

---

# 15. Git workflow for adding a provider

Implement providers capability-by-capability instead of as one large commit.

```bash
git checkout -b feature/<provider>
```

Recommended sequence:

```text
feat(provider): register <provider>

feat(auth): add <provider> authentication

feat(auth): add <provider> runtime credential resolution

feat(model): add <provider> model discovery

feat(inference): add <provider> inference transport

test(auth): add <provider> authentication coverage

test(model): add <provider> model discovery coverage

docs(provider): document <provider> setup
```

For the xAI work specifically:

```text
feat(auth): register xAI OAuth provider

feat(auth): add xAI device-code OAuth authentication

feat(auth): add xAI OAuth runtime credential resolution

feat(xai): add Grok model discovery and retirement handling

feat(model): add xAI OAuth model selection flow

test(auth): add xAI OAuth authentication coverage

test(xai): add Grok model discovery and retirement coverage
```

## Final design rule

When adding another provider, avoid asking:

> "Where do I add support for this model?"

Instead break the integration into five questions:

```text
1. PROVIDER
   How does Hermes identify it?

2. AUTHENTICATION
   How does the user authenticate?

3. RUNTIME CREDENTIALS
   What does inference receive?

4. MODELS
   How does Hermes discover and validate models?

5. TRANSPORT
   How are requests actually sent?
```

If those five layers remain separate, adding another provider becomes a relatively repeatable integration rather than another set of provider-specific exceptions scattered throughout Hermes.
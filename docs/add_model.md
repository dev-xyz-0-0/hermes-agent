# Adding Model Providers to Hermes

Hermes does not treat a model name as globally valid across every provider.
The effective runtime route is the combination of:

```text
model
+ provider
+ authentication
+ base URL
+ API mode
= actual inference backend
```

For example, these are different runtime configurations even though the model
name looks the same:

```yaml
model:
  provider: openrouter
  default: grok-4.5
```

```yaml
model:
  provider: xai
  default: grok-4.5
```

```yaml
model:
  provider: xai-oauth
  default: grok-4.5
```

Each provider can use a different endpoint, credential type, request protocol,
model normalization rule, and runtime adapter.

## The Provider Pipeline

Provider support should be split into distinct layers:

```text
provider registration
        |
        v
authentication
        |
        v
runtime credential resolution
        |
        v
model discovery / validation
        |
        v
model selection and config persistence
        |
        v
inference transport
```

Keeping those layers separate prevents provider-specific conditionals from
leaking through the CLI, gateway, and agent runtime.

## Files Usually Involved

Most provider additions touch a subset of these files:

```text
hermes_cli/auth.py
    Provider registry, auth status, provider aliases, shared auth-store helpers.

hermes_cli/runtime_provider.py
    Runtime route resolution: provider, base_url, api_key/token, api_mode.

hermes_cli/models.py
    Curated model lists, provider detection, model validation.

hermes_cli/providers.py
    Provider overlays, aliases, display labels, transport defaults, and
    provider metadata used by `/model <name> --provider <provider>`.

hermes_cli/model_normalize.py
    Provider-specific model ID normalization.

hermes_cli/model_switch.py
    Shared `/model` switching pipeline for CLI and gateway, including the
    authenticated-provider list used by Telegram and Discord pickers.

hermes_cli/main.py
    Interactive `hermes model` provider/model picker flows.

hermes_cli/<provider>_models.py
    Optional provider-specific live model discovery.

hermes_cli/auth_<provider>.py
    Optional provider-specific OAuth or complex auth logic.
```

API-key providers may only need registry/runtime/model-list changes. OAuth or
external-process providers usually need their own auth module and tests.

## Provider Is Not Model

A provider describes how Hermes connects. A model describes what Hermes asks
for after that connection is resolved.

```text
Provider        Authentication        Example model
---------------------------------------------------
openrouter      API key               x-ai/grok-4.5
xai             API key               grok-4.5
xai-oauth       OAuth bearer token     grok-4.5
openai-codex    OAuth bearer token     gpt-5.4
anthropic       API key / token        claude-sonnet-4.6
```

If a vendor supports multiple authentication modes, keep them distinct. For
xAI, `xai` means API-key authentication and `xai-oauth` means OAuth
authentication. Do not silently repoint existing aliases such as `grok` unless
you intentionally want to change user behavior.

## 1. Register The Provider

Add the provider to `PROVIDER_REGISTRY` in `hermes_cli/auth.py`.

```python
"xai-oauth": ProviderConfig(
    id="xai-oauth",
    name="xAI Grok OAuth (SuperGrok / Premium+)",
    auth_type="oauth_external",
    inference_base_url="https://api.x.ai/v1",
)
```

Then make aliases explicit in `resolve_provider()` if needed:

```python
"xai-oauth": "xai-oauth",
"grok-oauth": "xai-oauth",
```

For API-key providers, include the default base URL and accepted environment
variables:

```python
"example": ProviderConfig(
    id="example",
    name="Example AI",
    auth_type="api_key",
    inference_base_url="https://api.example.com/v1",
    api_key_env_vars=("EXAMPLE_API_KEY",),
    base_url_env_var="EXAMPLE_BASE_URL",
)
```

Also add a provider overlay in `hermes_cli/providers.py` when the provider is
not fully described by models.dev or needs Hermes-specific behavior. The
overlay controls transport, OAuth-vs-API-key auth type, base URL overrides,
aggregator behavior, and extra environment variables.

```python
"xai-oauth": HermesOverlay(
    transport="codex_responses",
    auth_type="oauth_external",
    base_url_override="https://api.x.ai/v1",
    base_url_env_var="XAI_BASE_URL",
)
```

Add aliases only when they are intentionally unambiguous:

```python
"grok-oauth": "xai-oauth",
```

Keep broad aliases stable. For example, `grok` should continue resolving to
the canonical xAI API-key provider unless you intentionally want to change that
behavior for existing users.

Add a display label when the provider is not in models.dev, or when the
models.dev label is not what users should see:

```python
_LABEL_OVERRIDES = {
    "xai-oauth": "xAI Grok OAuth",
}
```

## 2. Add Authentication

Simple API-key providers can use `resolve_api_key_provider_credentials()` once
registered. More complex providers should isolate their logic in a dedicated
module such as `auth_xai.py` or `auth_codex.py`.

Provider-specific auth modules should own:

```text
OAuth discovery
device-code or browser login
token storage
token refresh
expiration checks
provider-specific auth errors
runtime credential construction
```

Expose two separate concepts:

```python
get_<provider>_auth_status()
resolve_<provider>_runtime_credentials()
```

Status functions are for UI and should avoid returning secrets. Runtime
credential functions are for inference and should return the bearer credential
or API key.

## 3. Resolve Runtime Credentials

Every provider must eventually produce a runtime dictionary:

```python
{
    "provider": "xai-oauth",
    "api_mode": "codex_responses",
    "base_url": "https://api.x.ai/v1",
    "api_key": "<runtime bearer token>",
    "source": "hermes-auth-store",
    "requested_provider": "xai-oauth",
}
```

The `api_key` field is the runtime authorization secret. It may contain an API
key, an OAuth access token, or a short-lived provider token.

Add explicit handling in `hermes_cli/runtime_provider.py` for any provider that
cannot be resolved by the generic API-key path. OAuth providers need a runtime
branch; otherwise they can fall through to the wrong provider.

For `xai-oauth`, runtime resolution must call:

```python
resolve_xai_oauth_runtime_credentials()
```

and return:

```python
{
    "provider": "xai-oauth",
    "api_mode": "codex_responses",
    "base_url": "https://api.x.ai/v1",
    "api_key": "<xAI OAuth access token>",
}
```

## 4. Decide The API Mode

Authentication and transport are separate decisions. A provider can authenticate
with OAuth but still use an OpenAI-compatible endpoint, a Responses-compatible
endpoint, or Anthropic Messages.

Common Hermes API modes:

```text
chat_completions
codex_responses
anthropic_messages
```

Set `api_mode` in `runtime_provider.py`, and only honor a persisted
`model.api_mode` when it belongs to the same configured provider. This prevents
stale transport settings from leaking across provider switches.

## 5. Add Model Discovery

Providers with a live `/models` endpoint should get a small model module:

```text
hermes_cli/xai_models.py
hermes_cli/codex_models.py
```

Expose a simple public function:

```python
get_xai_model_ids(access_token=None, base_url=None)
```

Use live discovery when possible and keep a conservative fallback list for
temporary API failures:

```python
DEFAULT_XAI_MODELS = [
    "grok-4.6",
]
```

The provider-specific model module is also the right place for lifecycle rules,
such as filtering retired models or normalizing provider-prefixed IDs.

## 6. Add Curated Models

Add curated model IDs to `_PROVIDER_MODELS` in `hermes_cli/models.py`.
These lists are intentionally smaller than the full upstream catalog: they are
the models Hermes should show in quick pickers and prefer for agent use.

```python
_PROVIDER_MODELS = {
    "xai-oauth": [
        "grok-4.20",
        "grok-4.20-reasoning",
        "grok-4.20-non-reasoning",
        "grok-4.20-multi-agent",
        "grok-build-0.1",
        "grok-4.5",
        "grok-4",
        "grok-3",
        "grok-3-mini",
    ],
}
```

Use provider-native model IDs. Aggregators such as OpenRouter normally use
vendor-prefixed IDs like `x-ai/grok-4.5`; direct providers such as xAI usually
use bare IDs like `grok-4.5`.

If adding a model to an existing provider, this may be the only required code
change. If adding a new provider slug, the provider must also be registered in
`auth.py` and `providers.py`, otherwise the model list may be unreachable.

## 7. Wire The Model Picker

`hermes model` should orchestrate provider-specific helpers rather than
implementing auth or model parsing inline.

A provider flow in `hermes_cli/main.py` usually does this:

```text
1. Check current config
2. Check auth status
3. Login if needed
4. Resolve runtime credentials
5. Fetch model IDs
6. Prompt for model selection
7. Save model.default
8. Save model.provider and model.base_url
```

For xAI OAuth, the flow is:

```text
_model_flow_xai_oauth()
        |
        +-- get_xai_oauth_auth_status()
        +-- _login_xai_oauth()
        +-- resolve_xai_oauth_runtime_credentials()
        +-- get_xai_model_ids()
        +-- _prompt_model_selection()
        +-- _save_model_choice()
        +-- _update_config_for_provider()
```

The saved config should identify both the model and provider:

```yaml
model:
  provider: xai-oauth
  default: grok-4.5
  base_url: https://api.x.ai/v1
```

## 8. Make Gateway Pickers See It

Telegram and Discord do not build their provider buttons directly from
`_PROVIDER_MODELS`. They call:

```text
gateway/run.py
    _handle_model_command()
        |
        +-- list_authenticated_providers()
        |
        +-- adapter.send_model_picker()
```

`list_authenticated_providers()` in `hermes_cli/model_switch.py` only returns
providers that are both known and authenticated. That means a curated model
list is not enough by itself.

A provider appears in gateway pickers when:

```text
1. The provider slug is discoverable:
   - mapped through PROVIDER_TO_MODELS_DEV in agent/models_dev.py, or
   - present in HERMES_OVERLAYS in hermes_cli/providers.py, or
   - defined by the user under providers: in config.yaml.

2. Credentials are detectable:
   - API-key providers have one of their env vars set, or
   - OAuth/external providers have an auth-store or credential-pool entry.

3. The provider has at least one curated model:
   - _PROVIDER_MODELS[provider_slug] is non-empty, or
   - the provider returns models through the relevant fallback path.
```

Telegram additionally filters out providers whose `total_models <= 0` before
rendering buttons. If a provider is configured as current but does not appear
in Telegram, check these in order:

```text
[ ] Is the provider in HERMES_OVERLAYS or PROVIDER_TO_MODELS_DEV?
[ ] Does list_authenticated_providers() return it?
[ ] Does _PROVIDER_MODELS contain the exact provider slug?
[ ] Is total_models greater than 0?
[ ] Is the running gateway restarted after code/config changes?
```

For example, adding `_PROVIDER_MODELS["xai-oauth"]` is not enough unless
`xai-oauth` is also registered in `HERMES_OVERLAYS` with
`auth_type="oauth_external"`. Otherwise the picker never asks for that model
list.

## 9. Keep Model Switching And Runtime In Sync

`hermes model` saving a provider is not enough. The next chat turn calls
`resolve_runtime_provider()` before the agent is initialized. That resolver must
return the same provider the picker saved.

If those layers disagree, the UI can show one provider while inference uses
another.

The `xai-oauth` regression looked like this:

```text
Configured:
  provider: xai-oauth
  model: grok-4.5

Runtime before the fix:
  provider: openrouter
  model: grok-4.5
  endpoint: https://openrouter.ai/api/v1

Observed error:
  HTTP 401: Missing Authentication header
```

The missing OpenRouter key was only a symptom. The real bug was that
`xai-oauth` had no runtime branch, so it fell through to OpenRouter instead of
loading the xAI OAuth token.

## 10. Test Each Layer

Provider integrations should include focused tests for each layer.

Authentication tests:

```text
login flow
token storage
token refresh
expiration handling
safe status output
runtime credential resolution
```

Model tests:

```text
normalization
live model discovery
authorization headers
API failure fallback
model filtering
retirement handling
```

Runtime provider tests:

```text
explicit provider resolves to itself
base_url is provider-specific
api_key/token comes from the correct auth source
api_mode is correct
OpenRouter fallback is not used accidentally
explicit api_key/base_url overrides work
```

Picker/provider-list tests:

```text
registered provider appears when credentials exist
OAuth auth-store entries are treated as credentials
providers with zero curated models are hidden
current provider is marked current
picker callback passes the selected provider slug to switch_model()
```

For the `xai-oauth` fix, the critical regression assertions are:

```python
resolved = resolve_runtime_provider(requested="xai-oauth")

assert resolved["provider"] == "xai-oauth"
assert resolved["api_mode"] == "codex_responses"
assert resolved["base_url"] == "https://api.x.ai/v1"
assert resolved["api_key"]
```

Run focused tests while developing:

```bash
source venv/bin/activate
python -m pytest tests/hermes_cli/test_runtime_provider_resolution.py -q
python -m pytest tests/hermes_cli/test_xai_oauth.py tests/hermes_cli/test_xai_models.py -q
```

Before pushing, run the full suite:

```bash
source venv/bin/activate
python -m pytest tests/ -q
```

## Provider Integration Checklist

Use this checklist for new providers:

```text
[ ] Provider registered in PROVIDER_REGISTRY
[ ] Aliases are explicit and backwards-compatible
[ ] Auth status and runtime credentials are separate
[ ] OAuth or complex auth lives in a provider-specific module
[ ] Provider overlay exists in hermes_cli/providers.py when needed
[ ] Display label and aliases are explicit
[ ] Curated models are listed under the exact provider slug in models.py
[ ] runtime_provider.py returns provider/base_url/api_key/api_mode
[ ] Model IDs are normalized for the target provider
[ ] Live model discovery has conservative fallback behavior
[ ] `hermes model` saves model.default, model.provider, and base_url
[ ] Telegram/Discord picker provider list can discover the provider
[ ] Tests cover auth, model discovery, runtime resolution, and picker flow
[ ] No provider silently falls through to OpenRouter unless intended
```

The design question is not “where do I add this model?” Break it into five
questions instead:

```text
1. Provider: how does Hermes identify it?
2. Authentication: how does the user authenticate?
3. Runtime credentials: what does inference receive?
4. Models: how does Hermes discover and normalize model IDs?
5. Transport: which API mode sends the request?
```

If those boundaries stay clean, adding providers remains repeatable instead of
turning into a trail of special cases.

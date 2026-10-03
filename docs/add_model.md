# General Guide: How Hermes Maps Providers to Models

Hermes does not treat a model name as globally valid across every provider.

The effective runtime configuration is determined by the combination of:

```text
model
+
provider
+
provider authentication
+
provider API mode
+
provider base URL
        ↓
actual inference backend
```

For example, these are fundamentally different runtime configurations:

```text
provider: openrouter
model: grok-4.5
```

```text
provider: xai
model: grok-4.5
```

```text
provider: xai-oauth
model: grok-4.5
```

Even though the model name may look identical, Hermes can use a different endpoint, authentication mechanism, request protocol, and runtime adapter depending on the provider.

---

# 1. Hermes Provider Resolution Flow

At a high level, Hermes resolves models approximately like this:

```text
hermes model
     │
     │ saves
     ▼
provider + model
     │
     ▼
config / profile
     │
     ▼
runtime_provider.py
     │
     ├── provider-specific authentication
     ├── base_url
     ├── api_mode
     ├── API key / OAuth token
     └── runtime provider name
     │
     ▼
agent runtime
     │
     ├── chat_completions
     ├── codex_responses
     ├── anthropic_messages
     └── other provider modes
     │
     ▼
Provider API
```

This distinction is important:

```text
Model selection
!=
Runtime provider resolution
```

`hermes model` may correctly save and display a provider, but the runtime still needs an explicit implementation for that provider.

If `runtime_provider.py` does not know how to resolve the selected provider, the configuration displayed by Hermes can differ from the provider actually used at inference time.

---

# 2. Provider Selection vs Runtime Resolution

Consider:

```yaml
provider: xai-oauth
model: grok-4.5
```

There are two separate stages.

## Stage 1 — Configuration

Hermes records:

```text
provider = xai-oauth
model    = grok-4.5
```

Commands such as:

```bash
hermes model
```

may therefore correctly show:

```text
xai-oauth
grok-4.5
```

That only proves the configuration layer understands `xai-oauth`.

It does not prove that the runtime resolver supports it.

## Stage 2 — Runtime resolution

Before making an inference request, Hermes needs to turn:

```text
xai-oauth
```

into something approximately equivalent to:

```python
RuntimeProvider(
    provider="xai-oauth",
    model="grok-4.5",
    base_url="https://api.x.ai/v1",
    api_key="<OAuth bearer token>",
    api_mode="codex_responses",
)
```

If that runtime branch does not exist, the provider can fall through to another resolver.

That was the root cause of the `xai-oauth` issue.

---

# 3. Root Cause: `xai-oauth` Fell Through to OpenRouter

The configuration layer correctly understood:

```text
provider: xai-oauth
```

and `hermes model` correctly saved and displayed it.

However, `runtime_provider.py` had no explicit runtime-resolution branch for:

```text
xai-oauth
```

This was especially important because `xai-oauth` is an:

```text
oauth_external
```

provider rather than a normal API-key provider.

As a result, runtime resolution did not obtain the xAI OAuth credentials.

Instead, the provider fell through to the existing OpenRouter resolution path.

The effective runtime configuration therefore became something like:

```text
Requested:

provider: xai-oauth
model: grok-4.5


Actually executed:

provider: openrouter
model: grok-4.5
endpoint: https://openrouter.ai/api/v1
```

This explains the observed log:

```text
Provider: openrouter
Model: grok-4.5
Endpoint: https://openrouter.ai/api/v1

HTTP 401:
Missing Authentication header
```

The missing OpenRouter authentication was therefore a secondary symptom.

The real problem was not:

```text
OPENROUTER_API_KEY is missing
```

The real problem was:

```text
xai-oauth was never resolved at runtime.
```

Because Hermes incorrectly routed the request through OpenRouter, it then looked for OpenRouter credentials that were never supposed to be required.

The failure chain was:

```text
hermes model
     │
     └── xai-oauth / grok-4.5
              │
              ▼
runtime_provider.py
              │
              ├── no xai-oauth branch
              │
              ▼
       provider fallthrough
              │
              ▼
          OpenRouter
              │
              ├── no OPENROUTER_API_KEY
              │
              ▼
        HTTP 401
Missing Authentication header
```

---

# 4. Runtime Fix for `xai-oauth`

The fix was to add dedicated runtime credential resolution for the provider.

A new resolver was added:

```python
resolve_xai_oauth_runtime_credentials()
```

Its responsibility is to retrieve the xAI OAuth credentials from the Hermes authentication store and construct the correct runtime provider configuration.

The expected runtime mapping is now:

```text
provider:
    xai-oauth

base_url:
    https://api.x.ai/v1

authentication:
    OAuth bearer token from Hermes auth store

api_mode:
    codex_responses
```

Conceptually:

```python
if provider == "xai-oauth":
    credentials
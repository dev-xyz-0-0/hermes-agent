"""Text-only adapter for the official Qoder Agent SDK."""

import asyncio
import importlib
import json
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace

DEFAULT_MODEL = "account-default"
REQUEST_TIMEOUT = 120
INTERRUPT_TIMEOUT = 3
CLEANUP_TIMEOUT = 15
_CONTEXT_WINDOWS = {}


class QoderError(RuntimeError):
    def __init__(self, message, *, status_code=None, partial_text=""):
        super().__init__(message)
        self.status_code = status_code
        self.partial_text = partial_text


def preview_enabled():
    return os.environ.get("HERMES_QODER_PREVIEW") == "1"


def credentials():
    if not preview_enabled():
        raise QoderError("Set HERMES_QODER_PREVIEW=1 to enable the Qoder preview.")
    token = os.environ.get("QODER_PERSONAL_ACCESS_TOKEN", "").strip()
    if not token:
        raise QoderError("Set QODER_PERSONAL_ACCESS_TOKEN to a Qoder personal access token.")
    _sdk()
    return token


def _sdk():
    try:
        return importlib.import_module("qoder_agent_sdk")
    except ImportError:
        raise QoderError("Install the Qoder SDK with pip install 'hermes-agent[qoder]'.") from None


def _client(sdk, token, directory, model=None, system_prompt=None):
    allowed_env = {
        "PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    env = {key: None for key in os.environ if key.upper() not in allowed_env}
    env["QODER_CONFIG_DIR"] = directory
    env["HOME"] = directory
    env["USERPROFILE"] = directory
    return sdk.QoderSDKClient(options=sdk.QoderAgentOptions(
        auth=sdk.access_token(token),
        model=None if model == DEFAULT_MODEL else model,
        system_prompt=system_prompt,
        tools=[],
        skills=[],
        setting_sources=[],
        mcp_servers={},
        strict_mcp_config=True,
        include_partial_messages=True,
        cwd=directory,
        env=env,
    ))


async def _close(client, *, interrupt=False):
    try:
        if interrupt:
            try:
                async with asyncio.timeout(INTERRUPT_TIMEOUT):
                    await client.interrupt()
            except Exception:
                pass
    finally:
        async with asyncio.timeout(CLEANUP_TIMEOUT):
            # SDK 1.0.17 can leave a transport before the query exists if
            # cancellation occurs during subprocess startup.
            transport = getattr(client, "_transport", None)
            try:
                if transport is not None and getattr(client, "_query", None) is None:
                    await transport.close()
                    await transport._cleanup_auth_payload()
            finally:
                await client.disconnect()


def discover_models(token):
    async def discover():
        sdk = _sdk()
        with TemporaryDirectory(prefix="hermes-qoder-") as directory:
            client = _client(sdk, token, directory)
            try:
                async with asyncio.timeout(20):
                    await client.connect()
                    models = await client.get_available_models()
                    return {
                        model["value"]: model for model in models
                        if model.get("value") and model.get("isEnabled", True)
                    }
            finally:
                await _close(client)

    try:
        models = _run_sync(discover)
    except Exception as exc:
        raise QoderError(
            f"Qoder model discovery failed ({type(exc).__name__}). Check the token and bundled qodercli."
        ) from None
    for model_id, model in models.items():
        tiers = (model.get("context_config") or {}).values()
        for tier in tiers:
            if tier.get("is_default") and isinstance(tier.get("token_count"), int):
                _CONTEXT_WINDOWS[model_id] = tier["token_count"]
                break
    return models


def context_window(model):
    return _CONTEXT_WINDOWS.get(model)


def _run_sync(operation):
    # Gateway callers can already be running an asyncio event loop.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(operation())).result()


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def has_attachment(content):
    return isinstance(content, list) and any(
        not isinstance(block, dict) or block.get("type") != "text"
        for block in content
    )


def _prompt(messages):
    system = []
    conversation = []
    for message in messages:
        role = message.get("role")
        text = _text(message.get("content"))
        if not text:
            continue
        if role in {"system", "developer"}:
            system.append(text)
        elif role in {"user", "assistant"}:
            conversation.append({"role": role, "content": text})
    if len(conversation) == 1:
        return "\n\n".join(system), conversation[0]["content"]
    return "\n\n".join(system), (
        "The JSON below contains the conversation so far in chronological order. "
        "Respond to the latest user message.\n"
        + json.dumps(conversation, ensure_ascii=False)
    )


def request(messages, model, token, on_delta, is_interrupted):
    sdk = _sdk()
    system, prompt = _prompt(messages)
    deltas = []

    async def complete():
        with TemporaryDirectory(prefix="hermes-qoder-") as directory:
            client = _client(sdk, token, directory, model, system)
            interrupted = False
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    await client.connect(prompt)
                    return await _receive(sdk, client, model, deltas, on_delta)
            except (asyncio.CancelledError, TimeoutError):
                interrupted = True
                raise
            finally:
                await _close(client, interrupt=interrupted)

    async def run():
        task = asyncio.create_task(complete())
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
            if is_interrupted():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                raise InterruptedError("Qoder request interrupted.")
        return task.result()

    try:
        return _run_sync(run)
    except Exception as exc:
        if isinstance(exc, InterruptedError):
            raise
        if isinstance(exc, QoderError) and not deltas:
            raise
        if isinstance(exc, TimeoutError):
            raise QoderError("Qoder request timed out.", status_code=504, partial_text="".join(deltas)) from None
        if isinstance(exc, QoderError):
            message = str(exc)
        else:
            message = f"Qoder SDK request failed ({type(exc).__name__}). Check credentials and the bundled qodercli."
        raise QoderError(
            message, status_code=getattr(exc, "status_code", None),
            partial_text="".join(deltas),
        ) from None


async def _receive(sdk, client, model, deltas, on_delta):
    complete_text = []
    credits = []
    seen_requests = set()
    async for message in client.receive_response():
        if isinstance(message, sdk.StreamEvent):
            event = message.event
            delta = event.get("delta", {})
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                if text := delta.get("text"):
                    deltas.append(text)
                    on_delta(text)
        elif isinstance(message, sdk.AssistantMessage):
            model = message.model or model
            usage = message.usage or {}
            request_id = usage.get("request_id") or message.message_id or message.uuid
            if "credits" in usage and (not request_id or request_id not in seen_requests):
                credits.append(usage["credits"])
                if request_id:
                    seen_requests.add(request_id)
            for block in message.content:
                if isinstance(block, sdk.ToolUseBlock):
                    raise QoderError("Qoder returned a tool call during a text-only turn.")
                if isinstance(block, sdk.TextBlock):
                    complete_text.append(block.text)
        elif isinstance(message, sdk.ResultMessage):
            if message.is_error or message.subtype != "success":
                code = message.error_code
                status = {
                    105: 401, 500: 503, 10408: 504, 10500: 503, 10605: 429,
                }.get(code, 400)
                detail = message.subtype
                if code in {110, 113, 114, 115, 116, 117, 118, 119, 122}:
                    status = 402
                    detail = "usage limit or insufficient credits"
                elif code == 80411:
                    detail = "input content is too long"
                raise QoderError(f"Qoder turn failed ({detail}, code {code}).", status_code=status)
            text = "".join(complete_text) or "".join(deltas) or message.result
            if not text or not text.strip():
                raise QoderError("Qoder returned an empty response.")
            usage = message.usage or {}
            return SimpleNamespace(
                model=model,
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=text, tool_calls=None),
                )],
                usage=SimpleNamespace(
                    prompt_tokens=usage.get("input_tokens", 0),
                    completion_tokens=usage.get("output_tokens", 0),
                    total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                ) if usage else None,
                qoder_credits=sum(credits) if credits else message.total_credits,
            )
    raise QoderError("Qoder closed the stream before returning a result.")

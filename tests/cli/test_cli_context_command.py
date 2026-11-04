from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _agent():
    return SimpleNamespace(
        model="provider/model",
        session_id="cli-session",
        context_compressor=SimpleNamespace(
            context_length=200_000,
            threshold_tokens=100_000,
            threshold_percent=0.5,
            last_prompt_tokens=97_000,
            last_prompt_tokens_source="provider_usage",
            last_prompt_tokens_observed_at=1.0,
            compression_count=0,
        ),
        _cached_system_prompt="You are Hermes.",
        _memory_store=None,
        tools=[],
    )


def _cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli.agent = _agent()
    cli.conversation_history = [{"role": "user", "content": "hello"}]
    cli.console = SimpleNamespace(print=lambda *_args, **_kwargs: None)
    return cli


def test_context_and_ctx_dispatch_identically():
    cli = _cli()
    with patch.object(cli, "_handle_context_command") as handler:
        cli.process_command("/context")
        cli.process_command("/ctx")
    assert [call.args[0] for call in handler.call_args_list] == ["/context", "/ctx"]


def test_context_all_uses_expanded_renderer():
    cli = _cli()
    with (
        patch("cli._cprint") as output,
        patch("agent.context_breakdown.render_context_details", return_value="expanded") as expanded,
    ):
        cli._handle_context_command("/context all")
    expanded.assert_called_once()
    output.assert_called_once_with("expanded")


def test_context_uses_cached_state_without_rebuilding_prompt():
    cli = _cli()
    cli.agent._build_system_prompt = MagicMock(side_effect=AssertionError("must not rebuild"))
    before = cli.agent._cached_system_prompt
    with patch("cli._cprint") as output:
        cli._handle_context_command("/context")
    assert "97.0%" in output.call_args.args[0]
    assert cli.agent._cached_system_prompt == before
    cli.agent._build_system_prompt.assert_not_called()


def test_context_diagnose_uses_read_only_renderer():
    cli = _cli()
    with patch("cli._cprint") as output:
        cli._handle_context_command("/context diagnose")
    assert "Context diagnosis" in output.call_args.args[0]
    assert "Measurement: provider_usage" in output.call_args.args[0]


def test_context_without_agent_is_explicitly_unavailable():
    cli = _cli()
    cli.agent = None
    with patch("cli._cprint") as output:
        cli._handle_context_command("/context")
    assert "no active agent" in output.call_args.args[0]


def test_manual_compress_bypasses_cooldown_once_and_preserves_on_failure():
    cli = _cli()
    cli.conversation_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
        for i in range(6)
    ]
    original = cli.conversation_history
    cli.agent.compression_enabled = True
    cli.agent._compress_context = MagicMock(return_value=(original, "system"))
    with patch("builtins.print") as output:
        cli._manual_compress()
    assert cli.agent._compress_context.call_count == 1
    assert cli.agent._compress_context.call_args.kwargs["manual"] is True
    assert cli.conversation_history is original
    assert "Transcript preserved" in output.call_args_list[-1].args[0]

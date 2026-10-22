"""Validated, profile-scoped gateway quick commands.

This module deliberately keeps custom commands separate from the process-global
``COMMAND_REGISTRY``.  The native registry remains authoritative while the
active gateway exposes a small validated overlay to dispatch and menu code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SUPPORTED_TYPES = {"alias", "skill", "prompt", "exec"}
_BUSY_BYPASS_COMMANDS = {"approve", "deny", "status", "stop", "new"}


@dataclass(frozen=True)
class CustomCommand:
    name: str
    type: str
    description: str
    target: str = ""
    text: str = ""
    command: str = ""
    terminal_name: str = ""
    skill_key: str = ""


class CustomCommandRegistry:
    """Immutable validated view of ``GatewayConfig.quick_commands``."""

    def __init__(self, commands: Mapping[str, CustomCommand] | None = None):
        self._commands = dict(commands or {})

    def get(self, name: str | None) -> CustomCommand | None:
        if not name:
            return None
        return self._commands.get(name.lower().lstrip("/"))

    def values(self) -> tuple[CustomCommand, ...]:
        return tuple(self._commands[name] for name in sorted(self._commands))

    def resolve(self, name: str | None) -> CustomCommand | None:
        """Return the terminal custom command for *name*, following aliases."""
        command = self.get(name)
        if command and command.type == "alias" and command.terminal_name:
            return self.get(command.terminal_name) or command
        return command

    def alias_target(self, name: str | None) -> str | None:
        command = self.get(name)
        if command and command.type == "alias":
            return command.target
        return None

    def should_bypass_active_guard(self, name: str | None) -> bool:
        command = self.get(name)
        if not command or command.type != "alias":
            return False
        from hermes_cli.commands import resolve_command

        target = resolve_command(command.target)
        return bool(target and target.name in _BUSY_BYPASS_COMMANDS)

    def help_lines(self) -> list[str]:
        return [
            f"`/{cmd.name}` -- {cmd.description}"
            for cmd in self.values()
            if cmd.type != "exec"
        ]

    def telegram_entries(self) -> list[tuple[str, str]]:
        from hermes_cli.commands import _sanitize_telegram_name

        entries: list[tuple[str, str]] = []
        for cmd in self.values():
            if cmd.type == "exec":
                continue
            description = cmd.description[:40]
            entries.append((_sanitize_telegram_name(cmd.name), description))
        return entries


def validate_custom_commands(raw: Any) -> tuple[CustomCommandRegistry, list[str]]:
    """Validate quick commands, returning usable entries and diagnostics.

    A bad entry is isolated rather than preventing gateway startup.  Aliases
    depending on a bad entry are rejected as well.
    """
    if raw in (None, {}):
        return CustomCommandRegistry(), []
    if not isinstance(raw, Mapping):
        return CustomCommandRegistry(), ["quick_commands must be a mapping"]

    from agent.skill_commands import get_skill_commands, resolve_skill_command_key
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, _sanitize_telegram_name

    native_names = set(GATEWAY_KNOWN_COMMANDS)
    skills = get_skill_commands()
    skill_names = {key.lstrip("/") for key in skills}
    plugin_names: set[str] = set()
    try:
        from hermes_cli.plugins import get_plugin_manager
        plugin_names = set(getattr(get_plugin_manager(), "_plugin_commands", {}))
    except Exception:
        pass

    diagnostics: list[str] = []
    candidates: dict[str, CustomCommand] = {}
    telegram_owners: dict[str, str] = {
        _sanitize_telegram_name(name): name for name in native_names
    }
    telegram_owners.update(
        {_sanitize_telegram_name(name): name for name in skill_names | plugin_names}
    )

    for raw_name, raw_spec in raw.items():
        name = str(raw_name).lower().lstrip("/")
        prefix = f"quick_commands.{raw_name}"
        if not _NAME_RE.fullmatch(name):
            diagnostics.append(f"{prefix}: invalid command name")
            continue
        if name in native_names or name in skill_names or name in plugin_names:
            diagnostics.append(f"{prefix}: command name collides with an existing command")
            continue
        tg_name = _sanitize_telegram_name(name)
        owner = telegram_owners.get(tg_name)
        if not tg_name or owner:
            diagnostics.append(
                f"{prefix}: Telegram command name collides with /{owner}" if owner
                else f"{prefix}: invalid Telegram command name"
            )
            continue
        if not isinstance(raw_spec, Mapping):
            diagnostics.append(f"{prefix}: definition must be a mapping")
            continue
        if raw_spec.get("enabled", True) is False:
            continue
        if not isinstance(raw_spec.get("enabled", True), bool):
            diagnostics.append(f"{prefix}.enabled must be a boolean")
            continue
        command_type = raw_spec.get("type")
        if command_type not in _SUPPORTED_TYPES:
            diagnostics.append(f"{prefix}.type must be one of: alias, skill, prompt, exec")
            continue
        description = raw_spec.get("description", "")
        if not isinstance(description, str) or not description.strip():
            diagnostics.append(f"{prefix}.description must be a non-empty string")
            continue

        target = raw_spec.get("target", "")
        text = raw_spec.get("text", "")
        exec_command = raw_spec.get("command", "")
        if command_type in {"alias", "skill"} and (
            not isinstance(target, str) or not target.strip()
        ):
            diagnostics.append(f"{prefix}.target must be a non-empty string")
            continue
        if command_type == "prompt" and (not isinstance(text, str) or not text.strip()):
            diagnostics.append(f"{prefix}.text must be a non-empty string")
            continue
        if command_type == "exec" and (
            not isinstance(exec_command, str) or not exec_command.strip()
        ):
            diagnostics.append(f"{prefix}.command must be a non-empty string")
            continue

        skill_key = ""
        if command_type == "skill":
            normalized_target = target.strip().lstrip("/")
            skill_key = resolve_skill_command_key(normalized_target) or ""
            if not skill_key:
                diagnostics.append(f"{prefix}: skill '{normalized_target}' is not installed or enabled")
                continue
            target = normalized_target

        candidates[name] = CustomCommand(
            name=name,
            type=command_type,
            description=description.strip(),
            target=target.strip().lower().lstrip("/") if isinstance(target, str) else "",
            text=text.strip() if isinstance(text, str) else "",
            command=exec_command.strip() if isinstance(exec_command, str) else "",
            skill_key=skill_key,
        )
        telegram_owners[tg_name] = name

    valid = dict(candidates)
    for name, command in list(candidates.items()):
        if command.type != "alias":
            continue
        seen = {name}
        target = command.target
        terminal_custom = ""
        error = ""
        while target in candidates:
            target_command = candidates[target]
            if target_command.type == "exec":
                error = "aliases to deprecated exec commands are not allowed"
                break
            if target in seen:
                error = "alias cycle detected"
                break
            seen.add(target)
            terminal_custom = target
            if target_command.type != "alias":
                break
            target = target_command.target
        else:
            from hermes_cli.commands import resolve_command
            native = resolve_command(target)
            if native is None or native.name not in native_names:
                error = f"unknown alias target '{target}'"
            else:
                target = native.name

        if error:
            diagnostics.append(f"quick_commands.{name}: {error}")
            valid.pop(name, None)
            continue
        valid[name] = CustomCommand(
            **{**command.__dict__, "target": target, "terminal_name": terminal_custom}
        )

    # Remove aliases whose custom target was rejected in the pass above.
    changed = True
    while changed:
        changed = False
        for name, command in list(valid.items()):
            if command.type == "alias" and command.terminal_name and command.terminal_name not in valid:
                diagnostics.append(f"quick_commands.{name}: alias target is invalid")
                valid.pop(name)
                changed = True

    return CustomCommandRegistry(valid), diagnostics


_active_registry = CustomCommandRegistry()


def set_active_custom_commands(registry: CustomCommandRegistry) -> None:
    global _active_registry
    _active_registry = registry


def get_active_custom_commands() -> CustomCommandRegistry:
    return _active_registry

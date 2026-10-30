"""Public menu metadata for plugins that route messages without command filters."""

from dataclasses import replace

from .discovery import DiscoveryResult, PublicEntry, cached_discovery, discover
from .registration import command_roots


def is_message_listener(handler):
    kinds = {
        cls.__name__ for item in handler.event_filters for cls in type(item).__mro__
    }
    if kinds & {"CommandFilter", "CommandGroupFilter"}:
        return False
    event_type = getattr(getattr(handler, "event_type", None), "name", None)
    if event_type is not None:
        return event_type == "AdapterMessageEvent"
    return bool(kinds & {"EventMessageTypeFilter", "RegexFilter"})


def listener_catalog(plugin, handler, options, event=None, *, cached_only=False):
    """Use loaded source routes first; user entries remain an explicit fallback."""
    if not is_message_listener(handler) and not command_roots(handler):
        return DiscoveryResult()
    scanned = (
        cached_discovery(plugin, handler)
        if cached_only
        else discover(plugin, handler, event)
    )
    if not scanned.context_allowed:
        return scanned
    entries = list(scanned.entries) if options.get("auto_discover", True) else []
    name = str(plugin.name or "")
    for row in options.get("extra_commands", []):
        owner, sep, definition = str(row).partition("::")
        if not sep or owner.strip().casefold() != name.casefold():
            continue
        command, sep, description = definition.partition("=")
        command = " ".join(command.strip().lstrip("/").split())
        if not sep or not command or not description.strip() or len(command) > 160:
            continue
        if any(
            command == restricted or command.startswith(restricted + " ")
            for restricted in scanned.restricted_commands
        ):
            continue
        if any(entry.command == command for entry in entries):
            continue
        entries.append(PublicEntry(command, description.strip()))
    return replace(scanned, entries=tuple(entries))


def public_entries(plugin, handler, options, event=None, *, cached_only=False):
    return listener_catalog(
        plugin, handler, options, event, cached_only=cached_only
    ).entries


def context_allowed(plugin, event):
    """Platform declarations apply even when no source route can be recovered."""
    supported = getattr(plugin, "support_platforms", ())
    return not supported or event.get_platform_name() in supported

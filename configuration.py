"""Populate AstrBot's standard list/options configuration dropdowns."""

from .catalog import command_names
from .compatibility import public_entries


def collect_choices(plugins, handlers, options, *, cached_only=False):
    """Build dropdown data in a worker without touching the live WebUI schema."""
    plugins = list(plugins)
    plugin_choices = {}
    for plugin in plugins:
        if not plugin.name or (
            getattr(plugin, "reserved", False)
            and not options.get("include_builtin", False)
        ):
            continue
        label = getattr(plugin, "display_name", None) or plugin.name
        plugin_choices[plugin.name] = f"{label}（{plugin.name}）"
    group_choices = {}
    for handler in handlers:
        candidates = [
            p
            for p in plugins
            if getattr(p, "module_path", None)
            and (
                handler.handler_module_path == p.module_path
                or handler.handler_module_path.startswith(p.module_path + ".")
            )
        ]
        if not candidates:
            continue
        plugin = max(candidates, key=lambda p: len(p.module_path))
        if plugin.name not in plugin_choices:
            continue
        label = getattr(plugin, "display_name", None) or plugin.name
        entries = (
            public_entries(plugin, handler, options, cached_only=True)
            if cached_only
            else public_entries(plugin, handler, options)
        )
        for entry in entries:
            parts = entry.command.split()
            for size in range(1, min(len(parts) + 1, 4)):
                group = " ".join(parts[:size])
                group_choices[f"{plugin.name}::{group}"] = (
                    f"{label} / {group}（含全部子入口）"
                )
        for item in handler.event_filters:
            if not hasattr(item, "group_name"):
                continue
            names = command_names(item)
            if names:
                group_choices[f"{plugin.name}::{names[0]}"] = (
                    f"{label} / {names[0]}（含全部子指令）"
                )
    return {"hidden_plugins": plugin_choices, "hidden_groups": group_choices}


def apply_choices(config, collected):
    """Publish on the event loop, preserving the user's current selections."""
    schema = getattr(config, "schema", None)
    if not isinstance(schema, dict):
        return
    for key, values in collected.items():
        if key not in schema:
            continue
        choices = dict(values)
        for selected in config.get(key, []):
            choices.setdefault(selected, f"{selected}（已保存的排除项）")
        ordered = sorted(choices, key=lambda value: (choices[value].casefold(), value))
        schema[key]["options"] = ordered
        schema[key]["labels"] = [choices[value] for value in ordered]


def refresh_choices(config, plugins, handlers):
    """Synchronous helper for offline tools; lifecycle hooks use a worker."""
    if isinstance(getattr(config, "schema", None), dict):
        apply_choices(config, collect_choices(plugins, handlers, config))

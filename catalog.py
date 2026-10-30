"""Read AstrBot metadata without running commands or arbitrary plugin filters.

Filter recognition uses class ancestry names to avoid importing AstrBot during
offline preview/tests. The adapter only supplies the trusted runtime registry.
"""

import inspect
import math
import re
from dataclasses import dataclass
from functools import partial

from .compatibility import context_allowed, is_message_listener, listener_catalog
from .registration import command_names


@dataclass(frozen=True)
class CommandEntry:
    plugin: str
    plugin_label: str
    command: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    usage: str = ""
    conditional: bool = False
    kind: str = "command"
    use_prefix: bool = True


@dataclass(frozen=True)
class MenuPage:
    entries: tuple[CommandEntry, ...]
    number: int
    total_pages: int
    total_entries: int
    plugin_count: int
    keyword: str = ""
    has_features: bool = False


def _kind(value, name):
    return any(cls.__name__ == name for cls in type(value).__mro__)


def _command(value):
    return _kind(value, "CommandFilter") or _kind(value, "CommandGroupFilter")


def _normal(text):
    return " ".join(str(text).strip().lstrip("/").split()).casefold()


def _hidden_group(plugin, names, rules):
    for rule in rules:
        scope, separator, group = str(rule).partition("::")
        if separator and _normal(scope) != _normal(plugin):
            continue
        group = _normal(group if separator else scope)
        if group and any(
            _normal(name) == group or _normal(name).startswith(group + " ")
            for name in names
        ):
            return True
    return False


def _summary(text, limit=76):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(text or ""))
    for line in text.splitlines():
        line = re.sub(r"[`*_#]", "", line).strip()
        if line and not re.match(
            r"(?i)^(args|parameters|returns|用法|参数)\s*[:：]", line
        ):
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def _usage(handler):
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return ""
    if params and params[0].name in ("self", "cls"):
        params.pop(0)
    if params:  # AstrBot's first handler argument is always the event.
        params.pop(0)
    parts = []
    for param in params:
        if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            parts.append(f"[{param.name}…]")
        elif param.default is param.empty:
            parts.append(f"<{param.name}>")
        else:
            parts.append(f"[{param.name}]")
    return " ".join(parts)


def _handler_doc(handler):
    # AstrBot binds plugin methods with partial; partial's own doc is generic.
    while isinstance(handler, partial):
        handler = handler.func
    return inspect.getdoc(handler)


def _known_filter(item, name):
    cls = type(item)
    return cls.__name__ == name and cls.__module__ in {
        "astrbot.core.star.filter.event_message_type",
        "astrbot.core.star.filter.platform_adapter_type",
        __package__ + ".tests.fakes",
    }


def _filter_state(filters, event, config):
    conditional = False
    for item in filters:
        if _command(item):
            continue
        if _kind(item, "PermissionTypeFilter"):
            # A menu requested by an administrator still uses ordinary access.
            if getattr(getattr(item, "permission_type", None), "name", "") != "MEMBER":
                return False, conditional
        elif any(
            _known_filter(item, name)
            for name in ("EventMessageTypeFilter", "PlatformAdapterTypeFilter")
        ):
            try:
                if not item.filter(event, config):
                    return False, conditional
            except Exception:
                return False, conditional
        else:
            # Custom filters can consume quotas, mutate the event or perform IO.
            conditional = True
    return True, conditional


def collect_commands(handlers, plugins, event, config, options):
    """Collect enabled, non-management commands in the current session.

    Rebuild on every invocation so installs, unloads, renames and permissions
    become visible immediately. Parent group restrictions are inherited.
    """
    handlers = list(handlers)
    plugins = list(plugins)
    owners, parents = {}, {}
    for handler in handlers:
        for item in handler.event_filters:
            if _command(item):
                owners[id(item)] = handler

    def walk(group, ancestors, seen):
        if id(group) in seen:
            return
        chain = [*ancestors, group]
        for child in getattr(group, "sub_command_filters", ()):
            inherited = parents.setdefault(id(child), {})
            inherited.update({id(g): g for g in chain})
            if _kind(child, "CommandGroupFilter"):
                walk(child, chain, seen | {id(group)})

    for handler in handlers:
        for item in handler.event_filters:
            if _kind(item, "CommandGroupFilter"):
                walk(item, [], set())

    hidden_plugins = {_normal(x) for x in options.get("hidden_plugins", [])}
    hidden_commands = {_normal(x) for x in options.get("hidden_commands", [])}
    overrides = {}
    for line in options.get("command_descriptions", []):
        key, sep, value = str(line).partition("=")
        if sep and value.strip():
            overrides[_normal(key)] = _summary(value)
    enabled_plugins = config.get("plugin_set", ["*"])
    result, seen = [], set()
    supplemental = []
    features, explicit_plugins = {}, set()
    blocked_supplements = {}

    def append_entry(
        plugin,
        label,
        names,
        description,
        usage,
        conditional,
        kind="command",
        use_prefix=True,
    ):
        if (
            not names
            or {_normal(n) for n in names} & hidden_commands
            or _hidden_group(plugin, names, options.get("hidden_groups", []))
        ):
            return
        key = (plugin, names[0])
        if key in seen:
            return
        seen.add(key)
        result.append(
            CommandEntry(
                plugin=plugin,
                plugin_label=label,
                command=names[0],
                aliases=tuple(sorted(names[1:])),
                description=overrides.get(_normal(names[0])) or _summary(description),
                usage=usage,
                conditional=conditional,
                kind=kind,
                use_prefix=use_prefix,
            )
        )

    for handler in handlers:
        module = handler.handler_module_path
        candidates = [
            p
            for p in plugins
            if getattr(p, "module_path", None)
            and (module == p.module_path or module.startswith(p.module_path + "."))
        ]
        if not candidates:
            continue
        meta = max(candidates, key=lambda p: len(p.module_path))
        if not getattr(meta, "activated", True):
            continue
        reserved = getattr(meta, "reserved", False)
        if reserved:
            if not options.get("include_builtin", False) or config.get(
                "disable_builtin_commands", False
            ):
                continue
        elif enabled_plugins is not None and "*" not in enabled_plugins:
            if meta.name not in enabled_plugins:
                continue
        name = str(meta.name or module)
        label = str(getattr(meta, "display_name", None) or name)
        if {_normal(name), _normal(label)} & hidden_plugins:
            continue

        standard_routes = False
        for command in handler.event_filters:
            if not _command(command):
                continue
            filters = [
                *handler.event_filters,
                *getattr(command, "custom_filter_list", ()),
            ]
            parent_disabled = not getattr(handler, "enabled", True)
            for group in parents.get(id(command), {}).values():
                filters.extend(getattr(group, "custom_filter_list", ()))
                parent_handler = owners.get(id(group))
                if parent_handler:
                    parent_disabled |= not getattr(parent_handler, "enabled", True)
                    filters.extend(parent_handler.event_filters)
            allowed, conditional = _filter_state(filters, event, config)
            if parent_disabled or not allowed:
                blocked_supplements.setdefault(name, set()).update(
                    command_names(command)
                )
                continue
            if _kind(command, "CommandGroupFilter"):
                continue
            standard_routes = True
            explicit_plugins.add(name)
            names = command_names(command)
            description = (
                _summary(getattr(handler, "desc", ""))
                or _summary(_handler_doc(handler.handler))
                or _summary(getattr(meta, "desc", ""))
                or f"{label}提供的指令"
            )
            append_entry(
                name, label, names, description, _usage(handler.handler), conditional
            )
        if not getattr(handler, "enabled", True):
            continue
        if module == __package__ + ".main":
            # Our menu implementation reads catalog/revision bookkeeping. Its
            # registered command is sufficient and avoids recursive analysis.
            continue
        if (standard_routes or is_message_listener(handler)) and context_allowed(
            meta, event
        ):
            allowed, conditional = _filter_state(handler.event_filters, event, config)
            if allowed:
                discovered = listener_catalog(meta, handler, options, event)
                blocked_supplements.setdefault(name, set()).update(
                    discovered.restricted_commands
                )
                if not discovered.context_allowed:
                    continue
                features.setdefault(name, (label, meta))
                if discovered.entries:
                    explicit_plugins.add(name)
                supplemental.extend(
                    (name, label, entry, conditional or entry.conditional)
                    for entry in discovered.entries
                )
    # Prefer real registered command metadata even if a listener came first.
    for name, label, entry, conditional in supplemental:
        if _hidden_group(
            name, [entry.command, *entry.aliases], blocked_supplements.get(name, ())
        ):
            continue
        append_entry(
            name,
            label,
            [entry.command, *entry.aliases],
            entry.description,
            entry.usage,
            conditional,
            use_prefix=entry.use_prefix,
        )
    for name, (label, meta) in features.items():
        if name not in explicit_plugins:
            append_entry(
                name,
                label,
                ["自定义消息入口"],
                getattr(meta, "desc", "") or "通过自定义消息入口提供功能。",
                "查看插件说明了解触发方式",
                True,
                kind="feature",
            )
    return sorted(
        result,
        key=lambda e: (e.plugin_label.casefold(), e.plugin, e.command.casefold()),
    )


def paginate(entries, query="", page_size=12):
    tokens = str(query).strip().split()
    number = 1
    if tokens and tokens[-1].isascii() and tokens[-1].isdigit():
        number = max(1, int(tokens.pop()[:8]))
    keyword = " ".join(tokens)
    if keyword:
        needle = keyword.casefold()
        entries = [
            e
            for e in entries
            if needle
            in " ".join(
                (e.plugin, e.plugin_label, e.command, e.description, *e.aliases)
            ).casefold()
        ]
    size = max(6, min(24, int(page_size)))
    total = len(entries)
    pages = max(1, math.ceil(total / size))
    number = min(number, pages)
    start = (number - 1) * size
    return MenuPage(
        tuple(entries[start : start + size]),
        number,
        pages,
        total,
        len({e.plugin for e in entries}),
        keyword,
        any(entry.kind == "feature" for entry in entries),
    )


def menu_pages(entries, query="", page_size=12):
    """Return the full menu, or just the explicitly requested page."""
    first = paginate(entries, query, page_size)
    tokens = str(query).split()
    if tokens and tokens[-1].isascii() and tokens[-1].isdigit():
        return [first]
    return [
        first,
        *[
            paginate(entries, f"{query} {number}".strip(), page_size)
            for number in range(2, first.total_pages + 1)
        ],
    ]


def text_menu(page, title="AstrBot的帮助菜单", prefix="/", trigger="菜单"):
    counter = "个入口" if page.has_features else "条指令"
    lines = [
        title,
        f"{page.plugin_count} 个插件 · {page.total_entries} {counter} · {page.number}/{page.total_pages} 页",
    ]
    if page.keyword:
        lines.append(f"搜索：{page.keyword}")
    current = None
    for item in page.entries:
        if item.plugin != current:
            current = item.plugin
            lines.extend(["", f"【{item.plugin_label}】"])
        usage = f" {item.usage}" if item.usage else ""
        entry_prefix = prefix if item.use_prefix else ""
        command = (
            entry_prefix + item.command if item.kind == "command" else item.command
        )
        lines.append(f"{command}{usage} — {item.description}")
        if item.aliases:
            lines.append("别名：" + "、".join(entry_prefix + a for a in item.aliases))
        if item.conditional:
            lines.append("（有额外条件，请以插件实际提示为准）")
    if not page.entries:
        lines.extend(["", "没有找到匹配的公开指令，换个关键词试试吧。"])
    lines.extend(
        [
            "",
            f"{prefix}{trigger} 2 翻页 · {prefix}{trigger} 关键词 搜索 · {prefix}{trigger} 文字 复制指令",
            "<参数> 必填，[参数] 可选；额外条件以各插件实际提示为准。",
        ]
    )
    return "\n".join(lines)

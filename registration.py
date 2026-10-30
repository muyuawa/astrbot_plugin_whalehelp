"""Read command names shared by standard metadata and static route discovery."""


def command_names(command):
    """Use the runtime helper, or the equivalent AstrBot 4.0 name expansion."""
    getter = getattr(command, "get_complete_command_names", None)
    if callable(getter):
        names = getter()
    else:
        group = hasattr(command, "group_name")
        parent_group = getattr(command, "parent_group", None)
        parents = (
            (command_names(parent_group) if parent_group else [""])
            if group
            else command.parent_command_names or [""]
        )
        fragment = command.group_name if group else command.command_name
        names = [
            f"{parent} {name}".strip()
            for name in [fragment, *sorted(command.alias)]
            for parent in parents
        ]
    return list(dict.fromkeys(" ".join(name.split()) for name in names if name.strip()))


def command_roots(handler):
    names = []
    for item in getattr(handler, "event_filters", ()):
        kinds = {cls.__name__ for cls in type(item).__mro__}
        if "CommandFilter" in kinds and "CommandGroupFilter" not in kinds:
            names.extend(command_names(item))
    return tuple(dict.fromkeys(names))
